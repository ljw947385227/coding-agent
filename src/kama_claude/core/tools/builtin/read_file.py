from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from kama_claude.core.tools.base import BaseTool, ToolResult
from kama_claude.core.tools.file_filter import is_ignored_file
from kama_claude.core.workspace import WorkspaceBoundary

_MAX_BYTES = 512 * 1024  # 512 KB


class ReadFileParams(BaseModel):
    model_config = ConfigDict(extra="ignore")
    path: str


class ReadFileTool(BaseTool):
    params_model = ReadFileParams
    name = "read_file"
    description = (
        "Read the text content of a file. "
        "Path must be relative to the current working directory. "
        "Configured ignored files and files larger than 512 KB are not read."
    )
    input_schema: dict[str, object] = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Relative path to the file (relative to current working directory).",
            }
        },
        "required": ["path"],
    }

    # 创建带用户追加忽略文件规则的文本读取工具
    def __init__(
        self,
        *,
        ignore_files: Sequence[str] | None = None,
        workspace_root: str | Path | None = None,
    ) -> None:
        self._ignore_files = tuple(ignore_files or ())
        self._workspace = WorkspaceBoundary.from_path(workspace_root or Path.cwd())

    # 在读取内容前检查忽略规则和文件大小并返回文本
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        path_str = ReadFileParams.model_validate(params).path

        path = self._workspace.resolve(path_str)
        if is_ignored_file(path, self._ignore_files, root=self._workspace.root):
            raise PermissionError(f"file is ignored by configuration: {path_str}")
        size = path.stat().st_size
        if size > _MAX_BYTES:
            return ToolResult(
                content=f"file is too large to read: {size} bytes (limit {_MAX_BYTES})",
                is_error=True,
                error_type="runtime_error",
            )
        raw = _read_file_bytes(path)
        if len(raw) > _MAX_BYTES:
            return ToolResult(
                content=f"file grew beyond the read limit of {_MAX_BYTES} bytes",
                is_error=True,
                error_type="runtime_error",
            )

        return ToolResult(content=raw.decode("utf-8", errors="replace"))


# 最多读取阈值后一字节以防文件在 stat 后并发增长
def _read_file_bytes(path: Path) -> bytes:
    with path.open("rb") as stream:
        return stream.read(_MAX_BYTES + 1)
