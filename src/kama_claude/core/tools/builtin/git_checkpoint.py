from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from kama_claude.core.git.checkpoint import CheckpointError, CheckpointManager
from kama_claude.core.tools.base import BaseTool, ToolResult
from kama_claude.core.tools.builtin._git import (
    discover_repository,
    git_error_message,
)
from kama_claude.core.workspace import WorkspaceBoundary


class GitCheckpointParams(BaseModel):
    model_config = ConfigDict(extra="ignore")
    path: str = "."
    label: str = Field(default="", max_length=120)


class GitCheckpointTool(BaseTool):
    params_model = GitCheckpointParams
    name = "git_checkpoint"
    description = (
        "Create a recoverable Git tree checkpoint without modifying the real index or worktree. "
        "Captures tracked, staged, unstaged, and non-ignored untracked file content."
    )
    input_schema: dict[str, object] = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Directory inside the repository."},
            "label": {"type": "string", "description": "Optional checkpoint label."},
        },
    }

    # 创建带可选 Session 关联信息的 Git checkpoint 工具
    def __init__(
        self,
        *,
        session_id: str | None = None,
        node_id: str | None = None,
        run_id: str | None = None,
        workspace_root: str | Path | None = None,
    ) -> None:
        self._session_id = session_id
        self._node_id = node_id
        self._run_id = run_id
        self._workspace = WorkspaceBoundary.from_path(workspace_root or Path.cwd())

    # 创建 checkpoint 并返回树对象及 Session 关联元数据
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        parsed = GitCheckpointParams.model_validate(params)
        path = self._workspace.resolve(parsed.path)
        if not path.is_dir():
            raise NotADirectoryError(f"not a directory: {parsed.path}")
        repository, discovery = await discover_repository(path)
        if repository is None:
            return _error_result(git_error_message(discovery))
        self._workspace.resolve(str(repository))
        try:
            checkpoint = await CheckpointManager(repository).create(
                label=parsed.label,
                session_id=self._session_id,
                node_id=self._node_id,
                run_id=self._run_id,
            )
        except CheckpointError as exc:
            return _error_result(str(exc))
        return ToolResult(
            content=json.dumps(
                {"created": True, "checkpoint": asdict(checkpoint)},
                ensure_ascii=False,
                indent=2,
            )
        )


# 构造 checkpoint 工具的结构化错误
def _error_result(message: str) -> ToolResult:
    return ToolResult(
        content=json.dumps({"error": "checkpoint_failed", "message": message}),
        is_error=True,
        error_type="runtime_error",
    )
