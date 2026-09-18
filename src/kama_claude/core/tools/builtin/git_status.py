from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from kama_claude.core.tools.base import BaseTool, ToolResult
from kama_claude.core.tools.builtin._git import (
    discover_repository,
    git_error_message,
    run_git,
)
from kama_claude.core.workspace import WorkspaceBoundary

_MAX_STATUS_BYTES = 1024 * 1024
_STATUS_NAMES = {
    ".": "unchanged",
    "M": "modified",
    "T": "type_changed",
    "A": "added",
    "D": "deleted",
    "R": "renamed",
    "C": "copied",
    "U": "unmerged",
    "?": "untracked",
}


class GitStatusParams(BaseModel):
    model_config = ConfigDict(extra="ignore")
    path: str = "."
    include_untracked: bool = True
    max_entries: int = Field(default=100, ge=1, le=2000)


class GitStatusTool(BaseTool):
    params_model = GitStatusParams
    name = "git_status"
    description = (
        "Return structured read-only Git worktree status as JSON, including branch, staged, "
        "unstaged, untracked, renamed, deleted, copied, and conflicted entries."
    )
    input_schema: dict[str, object] = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Directory inside the repository (default '.').",
            },
            "include_untracked": {
                "type": "boolean",
                "description": "Include untracked files recursively (default true).",
            },
            "max_entries": {
                "type": "integer",
                "description": "Maximum status entries to return (default 100, max 2000).",
            },
        },
    }

    # 创建仅允许检查会话工作区内仓库的状态工具
    def __init__(self, *, workspace_root: str | Path | None = None) -> None:
        self._workspace = WorkspaceBoundary.from_path(workspace_root or Path.cwd())

    # 读取 Git porcelain v2 状态并输出稳定的 JSON 数据结构
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        parsed = GitStatusParams.model_validate(params)
        path = self._workspace.resolve(parsed.path)
        if not path.is_dir():
            raise NotADirectoryError(f"not a directory: {parsed.path}")
        repository, discovery = await discover_repository(path)
        if repository is None:
            return _error_result("not_git_repository", git_error_message(discovery))
        self._workspace.resolve(str(repository))

        untracked = "all" if parsed.include_untracked else "no"
        result = await run_git(
            repository,
            [
                "status",
                "--porcelain=v2",
                "--branch",
                "-z",
                f"--untracked-files={untracked}",
            ],
            max_stdout_bytes=_MAX_STATUS_BYTES,
        )
        if result.timed_out:
            return ToolResult(
                content=git_error_message(result),
                is_error=True,
                error_type="timeout",
            )
        if result.returncode != 0 and not result.truncated:
            return _error_result("git_status_failed", git_error_message(result))

        branch, all_entries = _parse_porcelain_v2(result.stdout)
        entry_truncated = len(all_entries) > parsed.max_entries
        entries = all_entries[: parsed.max_entries]
        payload = {
            "repository": repository.as_posix(),
            "branch": branch,
            "clean": not all_entries,
            "summary": _summarize(all_entries),
            "entries": entries,
            "truncated": result.truncated or entry_truncated,
        }
        return ToolResult(content=json.dumps(payload, ensure_ascii=False, indent=2))


# 解析以 NUL 分隔的 porcelain v2 输出并保留含空格或换行的路径
def _parse_porcelain_v2(
    raw: bytes,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    branch: dict[str, Any] = {
        "head": None,
        "oid": None,
        "upstream": None,
        "ahead": 0,
        "behind": 0,
    }
    entries: list[dict[str, Any]] = []
    records = raw.decode("utf-8", errors="replace").split("\0")
    index = 0
    while index < len(records):
        record = records[index]
        while record.startswith("# "):
            newline = record.find("\n")
            if newline < 0:
                _parse_branch_header(record, branch)
                record = ""
                break
            _parse_branch_header(record[:newline], branch)
            record = record[newline + 1 :]
        if not record:
            index += 1
            continue
        entry, consumes_original = _parse_status_record(record)
        if entry is not None and consumes_original and index + 1 < len(records):
            entry["original_path"] = records[index + 1]
            index += 1
        if entry is not None:
            entries.append(entry)
        index += 1
    return branch, entries


# 把一条分支元数据记录写入统一分支对象
def _parse_branch_header(record: str, branch: dict[str, Any]) -> None:
    if record.startswith("# branch.oid "):
        branch["oid"] = record.removeprefix("# branch.oid ")
    elif record.startswith("# branch.head "):
        branch["head"] = record.removeprefix("# branch.head ")
    elif record.startswith("# branch.upstream "):
        branch["upstream"] = record.removeprefix("# branch.upstream ")
    elif record.startswith("# branch.ab "):
        parts = record.removeprefix("# branch.ab ").split()
        if len(parts) == 2:
            branch["ahead"] = int(parts[0].removeprefix("+"))
            branch["behind"] = int(parts[1].removeprefix("-"))


# 解析普通、重命名、冲突和未跟踪四类状态记录
def _parse_status_record(record: str) -> tuple[dict[str, Any] | None, bool]:
    if record.startswith("1 "):
        fields = record.split(" ", 8)
        if len(fields) != 9:
            return None, False
        return _make_entry("ordinary", fields[1], fields[8], fields[2]), False
    if record.startswith("2 "):
        fields = record.split(" ", 9)
        if len(fields) != 10:
            return None, False
        return _make_entry("renamed_or_copied", fields[1], fields[9], fields[2]), True
    if record.startswith("u "):
        fields = record.split(" ", 10)
        if len(fields) != 11:
            return None, False
        return _make_entry("unmerged", fields[1], fields[10], fields[2]), False
    if record.startswith("? "):
        return _make_entry("untracked", "??", record[2:], None), False
    return None, False


# 将 XY 状态码转换成可读的 staged 和 worktree 状态
def _make_entry(kind: str, xy: str, path: str, submodule: str | None) -> dict[str, Any]:
    index_code = xy[0] if xy else "."
    worktree_code = xy[1] if len(xy) > 1 else "."
    entry: dict[str, Any] = {
        "path": path,
        "kind": kind,
        "index": _STATUS_NAMES.get(index_code, index_code),
        "worktree": _STATUS_NAMES.get(worktree_code, worktree_code),
    }
    if submodule is not None and submodule != "N...":
        entry["submodule"] = submodule
    return entry


# 汇总 staged、unstaged、untracked、conflicted 和 renamed 条目数
def _summarize(entries: list[dict[str, Any]]) -> dict[str, int]:
    unchanged = {"unchanged", "untracked"}
    return {
        "total": len(entries),
        "staged": sum(str(entry["index"]) not in unchanged for entry in entries),
        "unstaged": sum(str(entry["worktree"]) not in unchanged for entry in entries),
        "untracked": sum(entry["kind"] == "untracked" for entry in entries),
        "conflicted": sum(entry["kind"] == "unmerged" for entry in entries),
        "renamed": sum("renamed" in {entry["index"], entry["worktree"]} for entry in entries),
    }


# 构造结构化 Git 工具运行错误
def _error_result(code: str, message: str) -> ToolResult:
    content = json.dumps({"error": code, "message": message}, ensure_ascii=False)
    return ToolResult(content=content, is_error=True, error_type="runtime_error")
