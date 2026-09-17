from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, TypedDict

from kama_claude.core.git.process import (
    GitCommandResult,
    git_error_message,
    run_git,
)

_OUTPUT_LIMIT = 4 * 1024 * 1024
_CHECKPOINT_ID = re.compile(r"^cp-[0-9a-f]{32}$")
_MAX_UNTRACKED_FILES = 1_000
_MAX_UNTRACKED_BYTES = 100 * 1024 * 1024
_MAX_UNTRACKED_FILE_BYTES = 20 * 1024 * 1024
_PATH_BATCH_SIZE = 100


class CheckpointError(RuntimeError):
    pass


class RepositoryState(TypedDict):
    head: str | None
    index_tree: str
    worktree_tree: str


@dataclass(slots=True)
class Checkpoint:
    id: str
    created_at: str
    repository: str
    head: str | None
    index_tree: str
    worktree_tree: str
    label: str
    kind: Literal["manual", "undo", "verification", "mutation"]
    session_id: str | None = None
    node_id: str | None = None
    run_id: str | None = None
    source_checkpoint_id: str | None = None


class CheckpointManager:
    # 初始化绑定单个 Git 工作树的 checkpoint 管理器
    def __init__(self, repository: Path) -> None:
        self._repository = repository.resolve()

    # 创建不修改真实 index 和工作树的 Git tree checkpoint
    async def create(
        self,
        *,
        label: str = "",
        kind: Literal["manual", "undo", "verification", "mutation"] = "manual",
        session_id: str | None = None,
        node_id: str | None = None,
        run_id: str | None = None,
        source_checkpoint_id: str | None = None,
        paths: Sequence[str] | None = None,
    ) -> Checkpoint:
        state = await self._capture_state(paths=paths)
        checkpoint = Checkpoint(
            id=f"cp-{uuid.uuid4().hex}",
            created_at=datetime.now(UTC).isoformat(),
            repository=self._repository.as_posix(),
            head=state["head"],
            index_tree=state["index_tree"],
            worktree_tree=state["worktree_tree"],
            label=label,
            kind=kind,
            session_id=session_id,
            node_id=node_id,
            run_id=run_id,
            source_checkpoint_id=source_checkpoint_id,
        )
        await self._retain_checkpoint_trees(checkpoint)
        await self._write_checkpoint(checkpoint)
        return checkpoint

    # 为增量索引公开捕获当前仓库状态的只读快照能力
    async def capture_state(self) -> RepositoryState:
        return await self._capture_state()

    # 为增量索引公开两个 worktree tree 之间的文件级变化
    async def diff_trees(self, base_tree: str, current_tree: str) -> list[str]:
        return await self._diff_trees(base_tree, current_tree)

    # 返回单个仓库路径在两个 tree 之间的零上下文补丁供语义行分析
    async def diff_tree_patch(
        self, base_tree: str, current_tree: str, path: str
    ) -> str:
        relative = self._validate_tree_path(path)
        result = await self._run_checked_result(
            [
                "diff",
                "--unified=0",
                "--no-color",
                "--no-ext-diff",
                base_tree,
                current_tree,
                "--",
                relative,
            ]
        )
        return result.stdout.decode("utf-8", errors="replace")

    # 在读取内容前检查 Git blob 大小并返回指定 tree 中的 UTF-8 文本
    async def read_tree_text(
        self, tree: str, path: str, *, max_bytes: int = 1024 * 1024
    ) -> str | None:
        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        relative = self._validate_tree_path(path)
        object_name = f"{tree}:{relative}"
        size_text = await self._run_text(["cat-file", "-s", object_name])
        try:
            size = int(size_text)
        except ValueError as exc:
            raise CheckpointError(f"invalid Git blob size for {relative}") from exc
        if size > max_bytes:
            return None
        result = await run_git(
            self._repository,
            ["show", object_name],
            max_stdout_bytes=max_bytes + 1,
            timeout=60.0,
        )
        if result.returncode != 0 or result.timed_out or result.truncated:
            raise CheckpointError(git_error_message(result))
        return result.stdout.decode("utf-8", errors="strict")

    # 列出指定 tree 中的全部路径以构建与快照一致的项目索引
    async def list_tree_paths(self, tree: str) -> tuple[str, ...]:
        result = await self._run_checked_result(["ls-tree", "-r", "-z", "--name-only", tree])
        return tuple(
            path for path in result.stdout.decode("utf-8", errors="replace").split("\0") if path
        )

    # 返回当前工作树对应的 Git 私有目录供内部索引保存元数据
    async def git_directory(self) -> Path:
        return await self._git_directory()

    # 用内部 ref 保留最新索引 tree，避免离线期间被 Git GC 清理
    async def retain_index_tree(self, tree: str) -> None:
        await self._run_checked(["update-ref", "refs/kama/test-index/current", tree])

    # 用内部 Git refs 长期保留 checkpoint 的 index/worktree tree，避免被 GC 回收
    async def _retain_checkpoint_trees(self, checkpoint: Checkpoint) -> None:
        namespace = checkpoint.id.removeprefix("cp-")
        await self._run_checked(
            ["update-ref", f"refs/kama/checkpoints/{namespace}/index", checkpoint.index_tree]
        )
        await self._run_checked(
            [
                "update-ref",
                f"refs/kama/checkpoints/{namespace}/worktree",
                checkpoint.worktree_tree,
            ]
        )

    # 返回目标 checkpoint 与当前状态之间的恢复预览和确认令牌
    async def preview(self, checkpoint_id: str) -> dict[str, Any]:
        checkpoint = await self.load(checkpoint_id)
        current = await self._capture_state()
        changes = await self._diff_trees(checkpoint.worktree_tree, current["worktree_tree"])
        return {
            "checkpoint": asdict(checkpoint),
            "current": current,
            "expected_state": _state_token(current),
            "head_matches": current["head"] == checkpoint.head,
            "changes_to_discard": changes,
            "requires_apply": True,
        }

    # 校验确认令牌后恢复 checkpoint 并自动保存可撤销快照
    async def rollback(self, checkpoint_id: str, expected_state: str) -> dict[str, Any]:
        checkpoint = await self.load(checkpoint_id)
        current = await self._capture_state()
        if current["head"] != checkpoint.head:
            raise CheckpointError("HEAD changed since checkpoint; rollback refused")
        if not expected_state or expected_state != _state_token(current):
            raise CheckpointError("repository state changed after preview; run preview again")

        undo = await self.create(
            label=f"undo before rollback to {checkpoint.id}",
            kind="undo",
            source_checkpoint_id=checkpoint.id,
        )
        await self._remove_untracked_files()
        await self._run_checked(["read-tree", "--reset", "-u", checkpoint.worktree_tree])
        await self._run_checked(["read-tree", checkpoint.index_tree])
        restored = await self._capture_state()
        if (
            restored["index_tree"] != checkpoint.index_tree
            or restored["worktree_tree"] != checkpoint.worktree_tree
        ):
            raise CheckpointError(f"rollback verification failed; undo checkpoint is {undo.id}")
        return {
            "applied": True,
            "checkpoint_id": checkpoint.id,
            "undo_checkpoint_id": undo.id,
            "restored_state": restored,
        }

    # 从 Git 私有目录读取并校验 checkpoint 元数据
    async def load(self, checkpoint_id: str) -> Checkpoint:
        if _CHECKPOINT_ID.fullmatch(checkpoint_id) is None:
            raise CheckpointError("invalid checkpoint id")
        directory = await self._checkpoint_directory()
        path = directory / f"{checkpoint_id}.json"
        if not path.exists():
            raise CheckpointError(f"checkpoint not found: {checkpoint_id}")
        data = json.loads(path.read_text(encoding="utf-8"))
        checkpoint = Checkpoint(**data)
        if Path(checkpoint.repository).resolve() != self._repository:
            raise CheckpointError("checkpoint belongs to a different repository")
        return checkpoint

    # 按创建时间列出属于指定 Session 的全部可恢复 checkpoint
    async def list_for_session(self, session_id: str) -> tuple[Checkpoint, ...]:
        directory = await self._checkpoint_directory()
        checkpoints: list[Checkpoint] = []
        for path in sorted(directory.glob("cp-*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                checkpoint = Checkpoint(**data)
            except (OSError, TypeError, ValueError, json.JSONDecodeError):
                continue
            if (
                checkpoint.session_id == session_id
                and Path(checkpoint.repository).resolve() == self._repository
            ):
                checkpoints.append(checkpoint)
        checkpoints.sort(key=lambda item: item.created_at)
        return tuple(checkpoints)

    # 将已加载 checkpoint 的树状态写入当前工作树，用于初始化独立分支 worktree
    async def restore_loaded(self, checkpoint: Checkpoint) -> RepositoryState:
        current = await self._capture_state()
        if current["head"] != checkpoint.head:
            raise CheckpointError("worktree HEAD does not match checkpoint")
        await self._remove_untracked_files()
        await self._run_checked(["read-tree", "--reset", "-u", checkpoint.worktree_tree])
        await self._run_checked(["read-tree", checkpoint.index_tree])
        restored = await self._capture_state()
        if (
            restored["index_tree"] != checkpoint.index_tree
            or restored["worktree_tree"] != checkpoint.worktree_tree
        ):
            raise CheckpointError("worktree restore verification failed")
        return restored

    # 捕获 HEAD、真实 index tree 和包含未跟踪文件的 worktree tree
    async def _capture_state(self, *, paths: Sequence[str] | None = None) -> RepositoryState:
        head_result = await run_git(
            self._repository,
            ["rev-parse", "--verify", "HEAD"],
            max_stdout_bytes=1024,
        )
        head = _result_text(head_result) if head_result.returncode == 0 else None
        index_tree = await self._run_text(["write-tree"])
        worktree_tree = await self._write_worktree_tree(index_tree, paths=paths)
        return {"head": head, "index_tree": index_tree, "worktree_tree": worktree_tree}

    # 使用临时 index 将当前工作树写成树对象而不改变用户暂存区
    async def _write_worktree_tree(
        self,
        index_tree: str,
        *,
        paths: Sequence[str] | None = None,
    ) -> str:
        git_directory = await self._git_directory()
        temporary_index = git_directory / f"kama-index-{uuid.uuid4().hex}.tmp"
        environment = {"GIT_INDEX_FILE": str(temporary_index)}
        try:
            await self._bounded_tracked_paths()
            untracked_paths = (
                await self._bounded_untracked_paths()
                if paths is None
                else self._bounded_explicit_paths(paths)
            )
            # 从真实 index tree 起步，兼容尚无首个 commit 但已经部分暂存的仓库。
            await self._run_checked(["read-tree", index_tree], env=environment)
            # 已跟踪修改只更新现有 index 条目；未跟踪文件必须先通过数量和容量预算。
            await self._run_checked(["add", "-u", "--", "."], env=environment)
            for offset in range(0, len(untracked_paths), _PATH_BATCH_SIZE):
                batch = untracked_paths[offset : offset + _PATH_BATCH_SIZE]
                await self._run_checked(["add", "--", *batch], env=environment)
            return await self._run_text(["write-tree"], env=environment)
        finally:
            temporary_index.unlink(missing_ok=True)
            temporary_index.with_suffix(temporary_index.suffix + ".lock").unlink(missing_ok=True)

    # 只枚举未忽略文件的路径和元数据；在 Git/LFS 读取内容前执行硬预算检查
    async def _bounded_untracked_paths(self) -> list[str]:
        result = await run_git(
            self._repository,
            ["ls-files", "--others", "--exclude-standard", "-z"],
            max_stdout_bytes=_OUTPUT_LIMIT,
            timeout=15.0,
        )
        if result.returncode != 0 or result.timed_out or result.truncated:
            raise CheckpointError(
                "cannot safely enumerate untracked files before checkpoint: "
                + git_error_message(result)
            )
        paths = [
            value for value in result.stdout.decode("utf-8", errors="replace").split("\0") if value
        ]
        return self._validate_snapshot_budget(paths)

    # 校验工具声明的精确变更路径，目录和工作区外路径不会扩展为隐式全量扫描
    def _bounded_explicit_paths(self, paths: Sequence[str]) -> list[str]:
        normalized: list[str] = []
        for raw in paths:
            relative = Path(raw)
            if relative.is_absolute() or ".." in relative.parts:
                raise CheckpointError(f"unsafe checkpoint path: {raw}")
            candidate = (self._repository / relative).resolve()
            if not candidate.is_relative_to(self._repository):
                raise CheckpointError(f"checkpoint path escapes repository: {raw}")
            if not candidate.exists() and not candidate.is_symlink():
                continue
            if candidate.is_dir():
                raise CheckpointError(f"checkpoint path must be a file: {raw}")
            normalized.append(relative.as_posix())
        return self._validate_snapshot_budget(normalized)

    # 已跟踪文件同样先基于 diff 路径做预算，避免 add -u 隐式读取大型 LFS 内容
    async def _bounded_tracked_paths(self) -> list[str]:
        result = await run_git(
            self._repository,
            ["diff", "--name-only", "-z"],
            max_stdout_bytes=_OUTPUT_LIMIT,
            timeout=15.0,
        )
        if result.returncode != 0 or result.timed_out or result.truncated:
            raise CheckpointError(
                "cannot safely enumerate tracked changes before checkpoint: "
                + git_error_message(result)
            )
        paths = [
            value for value in result.stdout.decode("utf-8", errors="replace").split("\0") if value
        ]
        return self._validate_snapshot_budget(paths, category="changed")

    # 对候选路径执行文件数、单文件和总字节限制，不读取任何文件内容
    def _validate_snapshot_budget(
        self, paths: list[str], *, category: str = "untracked"
    ) -> list[str]:
        if len(paths) > _MAX_UNTRACKED_FILES:
            raise CheckpointError(
                "checkpoint skipped: "
                f"{len(paths)} {category} files exceed limit {_MAX_UNTRACKED_FILES}; "
                "configure .gitignore before retrying"
            )
        total_bytes = 0
        for relative in paths:
            candidate = self._repository / relative
            try:
                size = candidate.lstat().st_size
            except FileNotFoundError:
                # ``git diff --name-only`` includes unstaged deletions.  They
                # contain no worktree bytes to budget and ``git add -u`` below
                # will record the deletion in the temporary index.  Missing
                # untracked/explicit paths still indicate a concurrent change.
                if category == "changed":
                    continue
                raise CheckpointError(
                    f"cannot inspect checkpoint path {relative}: file disappeared"
                ) from None
            except OSError as exc:
                raise CheckpointError(f"cannot inspect checkpoint path {relative}: {exc}") from exc
            if size > _MAX_UNTRACKED_FILE_BYTES:
                raise CheckpointError(
                    "checkpoint skipped: "
                    f"{relative} is {size} bytes, exceeding per-file limit "
                    f"{_MAX_UNTRACKED_FILE_BYTES}"
                )
            total_bytes += size
            if total_bytes > _MAX_UNTRACKED_BYTES:
                raise CheckpointError(
                    f"checkpoint skipped: {category} files exceed total byte limit "
                    f"{_MAX_UNTRACKED_BYTES}"
                )
        return paths

    # 查询 Git 实际私有目录并规范化为绝对路径
    async def _git_directory(self) -> Path:
        value = await self._run_text(["rev-parse", "--git-dir"])
        path = Path(value)
        return path.resolve() if path.is_absolute() else (self._repository / path).resolve()

    # 返回不会进入工作树快照的 checkpoint 元数据目录
    async def _checkpoint_directory(self) -> Path:
        git_directory = await self._git_directory()
        directory = git_directory / "kama-checkpoints"
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    # 原子写入 checkpoint 元数据并刷盘
    async def _write_checkpoint(self, checkpoint: Checkpoint) -> None:
        directory = await self._checkpoint_directory()
        path = directory / f"{checkpoint.id}.json"
        temporary = directory / f"{checkpoint.id}.json.tmp"
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(asdict(checkpoint), handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)

    # 列出从目标状态恢复时将被丢弃的文件级变化
    async def _diff_trees(self, target_tree: str, current_tree: str) -> list[str]:
        result = await self._run_checked_result(
            [
                "diff-tree",
                "--no-commit-id",
                "--name-status",
                "-r",
                "--find-renames",
                target_tree,
                current_tree,
            ]
        )
        return [line for line in _result_text(result).splitlines() if line]

    # 校验 tree 内路径为仓库相对普通路径并返回 POSIX 形式
    def _validate_tree_path(self, path: str) -> str:
        relative = Path(path)
        if relative.is_absolute() or not relative.parts or ".." in relative.parts:
            raise CheckpointError(f"unsafe Git tree path: {path}")
        return relative.as_posix()

    # 删除当前非忽略未跟踪文件以便目标树能够精确重建
    async def _remove_untracked_files(self) -> None:
        result = await self._run_checked_result(
            ["ls-files", "--others", "--exclude-standard", "-z"]
        )
        for relative in result.stdout.decode("utf-8", errors="replace").split("\0"):
            if not relative:
                continue
            candidate = self._repository / relative
            if ".." in Path(relative).parts:
                raise CheckpointError(f"unsafe untracked path: {relative}")
            if not candidate.is_symlink() and not candidate.resolve().is_relative_to(
                self._repository
            ):
                raise CheckpointError(f"untracked path escapes repository: {relative}")
            if candidate.is_symlink() or candidate.is_file():
                candidate.unlink()

    # 执行 Git 命令并在失败、超时或截断时抛出 checkpoint 错误
    async def _run_checked_result(
        self, arguments: list[str], *, env: dict[str, str] | None = None
    ) -> GitCommandResult:
        result = await run_git(
            self._repository,
            arguments,
            max_stdout_bytes=_OUTPUT_LIMIT,
            timeout=60.0,
            env=env,
        )
        if result.returncode != 0 or result.timed_out or result.truncated:
            raise CheckpointError(git_error_message(result))
        return result

    # 执行无需输出的 Git 命令并校验结果
    async def _run_checked(
        self, arguments: list[str], *, env: dict[str, str] | None = None
    ) -> None:
        await self._run_checked_result(arguments, env=env)

    # 执行 Git 命令并返回去除首尾空白的文本
    async def _run_text(self, arguments: list[str], *, env: dict[str, str] | None = None) -> str:
        return _result_text(await self._run_checked_result(arguments, env=env))


# 将已校验 Git 命令结果解码为 UTF-8 文本
def _result_text(result: GitCommandResult) -> str:
    return result.stdout.decode("utf-8", errors="replace").strip()


# 根据仓库状态的三个稳定字段生成乐观并发确认令牌
def _state_token(state: RepositoryState) -> str:
    raw = "\0".join(str(state.get(key) or "") for key in ("head", "index_tree", "worktree_tree"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
