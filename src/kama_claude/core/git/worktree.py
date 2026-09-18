from __future__ import annotations

from pathlib import Path

from kama_claude.core.git.checkpoint import Checkpoint, CheckpointError, CheckpointManager
from kama_claude.core.git.process import git_error_message, run_git


# 从 checkpoint 创建 detached Git worktree，并精确恢复暂存区、工作树和未跟踪文件
async def create_checkpoint_worktree(
    repository: Path,
    target: Path,
    checkpoint: Checkpoint,
) -> Path:
    repository = repository.resolve()
    target = target.resolve()
    if target.exists():
        raise CheckpointError(f"branch worktree already exists: {target}")
    if checkpoint.head is None:
        raise CheckpointError("cannot create an independent worktree from an unborn HEAD")
    target.parent.mkdir(parents=True, exist_ok=True)
    result = await run_git(
        repository,
        ["worktree", "add", "--detach", str(target), checkpoint.head],
        max_stdout_bytes=1024 * 1024,
        timeout=60.0,
    )
    if result.returncode != 0 or result.timed_out or result.truncated:
        raise CheckpointError(git_error_message(result))
    try:
        await CheckpointManager(target).restore_loaded(checkpoint)
    except Exception:
        await _remove_failed_worktree(repository, target)
        raise
    return target


# 在初始化失败时让 Git 清理刚创建的 worktree 注册和目录
async def _remove_failed_worktree(repository: Path, target: Path) -> None:
    await run_git(
        repository,
        ["worktree", "remove", "--force", str(target)],
        max_stdout_bytes=1024 * 1024,
        timeout=60.0,
    )
