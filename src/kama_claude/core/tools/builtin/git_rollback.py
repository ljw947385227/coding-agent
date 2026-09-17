from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from kama_claude.core.git.checkpoint import CheckpointError, CheckpointManager
from kama_claude.core.tools.base import BaseTool, ToolResult
from kama_claude.core.tools.builtin._git import (
    discover_repository,
    git_error_message,
)
from kama_claude.core.workspace import WorkspaceBoundary


class GitRollbackParams(BaseModel):
    model_config = ConfigDict(extra="ignore")
    path: str = "."
    checkpoint_id: str
    apply: bool = False
    expected_state: str = ""


class GitRollbackTool(BaseTool):
    params_model = GitRollbackParams
    name = "git_rollback"
    description = (
        "Preview or apply rollback to a Git checkpoint. Preview first to obtain expected_state; "
        "apply refuses if HEAD, index, or worktree changed and creates an undo checkpoint."
    )
    input_schema: dict[str, object] = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Directory inside the repository."},
            "checkpoint_id": {"type": "string", "description": "Checkpoint ID to restore."},
            "apply": {"type": "boolean", "description": "Apply instead of preview."},
            "expected_state": {
                "type": "string",
                "description": "State token returned by the immediately preceding preview.",
            },
        },
        "required": ["checkpoint_id"],
    }

    # 创建仅允许恢复会话工作区内仓库的回滚工具
    def __init__(self, *, workspace_root: str | Path | None = None) -> None:
        self._workspace = WorkspaceBoundary.from_path(workspace_root or Path.cwd())

    # 预览或执行带乐观并发保护和自动 undo 的 checkpoint 恢复
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        parsed = GitRollbackParams.model_validate(params)
        path = self._workspace.resolve(parsed.path)
        if not path.is_dir():
            raise NotADirectoryError(f"not a directory: {parsed.path}")
        repository, discovery = await discover_repository(path)
        if repository is None:
            return _error_result(git_error_message(discovery))
        self._workspace.resolve(str(repository))
        manager = CheckpointManager(repository)
        try:
            payload = (
                await manager.rollback(parsed.checkpoint_id, parsed.expected_state)
                if parsed.apply
                else await manager.preview(parsed.checkpoint_id)
            )
        except (CheckpointError, OSError, json.JSONDecodeError) as exc:
            return _error_result(str(exc))
        return ToolResult(content=json.dumps(payload, ensure_ascii=False, indent=2))


# 构造 rollback 工具的结构化错误
def _error_result(message: str) -> ToolResult:
    return ToolResult(
        content=json.dumps({"error": "rollback_failed", "message": message}),
        is_error=True,
        error_type="runtime_error",
    )
