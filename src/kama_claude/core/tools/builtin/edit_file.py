from __future__ import annotations

import os
import stat
import uuid
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from kama_claude.core.tools.base import BaseTool, ToolResult
from kama_claude.core.workspace import WorkspaceBoundary

_MAX_BYTES = 1 * 1024 * 1024


class EditFileParams(BaseModel):
    model_config = ConfigDict(extra="ignore")
    path: str
    old_text: str = Field(min_length=1)
    new_text: str
    expected_replacements: int = Field(default=1, ge=1, le=1_000)


class EditFileTool(BaseTool):
    params_model = EditFileParams
    name = "edit_file"
    description = (
        "Atomically replace exact text in an existing UTF-8 file. "
        "The edit is rejected without changing the file unless the current occurrence count "
        "equals expected_replacements, providing conflict detection for precise local edits."
    )
    input_schema: dict[str, object] = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Existing file to edit."},
            "old_text": {
                "type": "string",
                "description": "Exact current text to replace; must be non-empty.",
            },
            "new_text": {"type": "string", "description": "Replacement text."},
            "expected_replacements": {
                "type": "integer",
                "description": "Required occurrence count (default 1).",
            },
        },
        "required": ["path", "old_text", "new_text"],
    }

    # 创建受统一工作区边界保护的原子编辑工具
    def __init__(self, *, workspace_root: str | Path | None = None) -> None:
        self._workspace = WorkspaceBoundary.from_path(workspace_root or Path.cwd())

    # 精确校验旧文本出现次数后原子替换文件内容
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        p = EditFileParams.model_validate(params)
        path = self._workspace.resolve(p.path)
        if not path.exists():
            raise FileNotFoundError(f"no such file: {p.path}")
        if not path.is_file() or path.is_symlink():
            return ToolResult(
                content=f"not an editable regular file: {p.path}",
                is_error=True,
                error_type="runtime_error",
            )

        raw = path.read_bytes()
        if len(raw) > _MAX_BYTES:
            return ToolResult(
                content=f"file too large: {len(raw)} bytes (limit 1 MB)",
                is_error=True,
                error_type="runtime_error",
            )
        text = raw.decode("utf-8")
        occurrences = text.count(p.old_text)
        if occurrences != p.expected_replacements:
            return ToolResult(
                content=(
                    f"edit conflict: expected {p.expected_replacements} occurrence(s) "
                    f"but found {occurrences}; file was not changed"
                ),
                is_error=True,
                error_type="runtime_error",
            )

        updated = text.replace(p.old_text, p.new_text)
        encoded = updated.encode("utf-8")
        if len(encoded) > _MAX_BYTES:
            return ToolResult(
                content=f"updated content too large: {len(encoded)} bytes (limit 1 MB)",
                is_error=True,
                error_type="runtime_error",
            )

        temporary = path.with_name(f".{path.name}.kama-{uuid.uuid4().hex}.tmp")
        try:
            with temporary.open("xb") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, stat.S_IMODE(path.stat().st_mode))
            os.replace(temporary, path)
        finally:
            if temporary.exists():
                temporary.unlink()

        return ToolResult(
            content=(
                f"edited {p.path}: replaced {occurrences} occurrence(s), "
                f"{len(raw)} -> {len(encoded)} bytes"
            )
        )
