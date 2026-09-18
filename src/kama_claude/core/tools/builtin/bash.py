from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from kama_claude.core.sandbox import ExecutionBackend, ExecutionRequest, LocalExecutionBackend
from kama_claude.core.sandbox.diagnostics import sandbox_failure_hint
from kama_claude.core.tools.base import BaseTool, ToolResult
from kama_claude.core.workspace import WorkspaceBoundary

_MAX_OUTPUT_BYTES = 64 * 1024  # 64 KB
_DEFAULT_TIMEOUT = 60


class BashParams(BaseModel):
    model_config = ConfigDict(extra="ignore")
    command: str
    timeout: int = Field(default=_DEFAULT_TIMEOUT, ge=1, le=120)


class BashTool(BaseTool):
    params_model = BashParams
    name = "bash"
    description = (
        "Execute a shell command and return its output (stdout + stderr combined). "
        "Non-interactive only — commands requiring user input will hang and time out. "
        "Prefer short, focused commands. Output is truncated at 64 KB."
    )
    input_schema: dict[str, object] = {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "Shell command to execute.",
            },
            "timeout": {
                "type": "integer",
                "description": f"Maximum seconds to wait (default {_DEFAULT_TIMEOUT}, max 120).",
            },
        },
        "required": ["command"],
    }

    # 创建固定在会话工作区执行命令的终端工具
    def __init__(
        self,
        *,
        workspace_root: str | Path | None = None,
        execution_backend: ExecutionBackend | None = None,
        ignore_files: Sequence[str] | None = None,
    ) -> None:
        self._workspace = WorkspaceBoundary.from_path(workspace_root or Path.cwd())
        self._execution_backend = execution_backend or LocalExecutionBackend()
        self._ignore_files = tuple(ignore_files or ())

    # 在子进程中执行 shell 命令，合并 stdout/stderr，超时或非零退出码时返回错误
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        p = BashParams.model_validate(params)
        try:
            result = await self._execution_backend.run(
                ExecutionRequest(
                    shell_command=p.command,
                    workspace=self._workspace.root,
                    timeout_seconds=float(p.timeout),
                    max_output_bytes=_MAX_OUTPUT_BYTES,
                    environment={"PYTHONDONTWRITEBYTECODE": "1"},
                    sensitive_patterns=self._ignore_files,
                )
            )
        except Exception as exc:
            return ToolResult(content=str(exc), is_error=True, error_type="runtime_error")
        hint = sandbox_failure_hint(result)
        output = result.output
        if hint is not None:
            output = f"{output}\n\n[environment hint] {hint}".strip()
        if result.timed_out:
            return ToolResult(
                content=f"[timeout after {p.timeout}s]\n{output}".rstrip(),
                is_error=True,
                error_type="timeout",
            )
        if result.launch_error is not None:
            return ToolResult(
                content=output or result.launch_error,
                is_error=True,
                error_type="runtime_error",
            )
        if result.oom_killed:
            return ToolResult(
                content=f"[sandbox out of memory]\n{output}".rstrip(),
                is_error=True,
                error_type="runtime_error",
            )
        if result.exit_code != 0:
            return ToolResult(
                content=f"[exit {result.exit_code}]\n{output}".rstrip(),
                is_error=True,
                error_type="runtime_error",
            )
        if result.cleanup_error is not None:
            return ToolResult(
                content=f"{output}\n[sandbox cleanup error] {result.cleanup_error}".strip(),
                is_error=True,
                error_type="runtime_error",
            )
        return ToolResult(content=output or "[no output]")
