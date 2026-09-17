from __future__ import annotations

import asyncio
import os
import signal
import subprocess
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

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
    def __init__(self, *, workspace_root: str | Path | None = None) -> None:
        self._workspace = WorkspaceBoundary.from_path(workspace_root or Path.cwd())

    # 在子进程中执行 shell 命令，合并 stdout/stderr，超时或非零退出码时返回错误
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        p = BashParams.model_validate(params)
        command = p.command
        timeout = p.timeout

        try:
            if os.name == "nt":
                proc = await asyncio.create_subprocess_shell(
                    command,
                    cwd=self._workspace.root,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                    creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
                )
            else:
                proc = await asyncio.create_subprocess_shell(
                    command,
                    cwd=self._workspace.root,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                    start_new_session=True,
                )
            try:
                stdout_bytes, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            except TimeoutError:
                await _stop_process_tree(proc)
                return ToolResult(
                    content=f"[timeout after {timeout}s]",
                    is_error=True,
                    error_type="timeout",
                )
            except asyncio.CancelledError:
                await _stop_process_tree(proc)
                raise
        except Exception as exc:
            return ToolResult(content=str(exc), is_error=True, error_type="runtime_error")

        output = stdout_bytes.decode("utf-8", errors="replace")
        truncated = len(stdout_bytes) > _MAX_OUTPUT_BYTES
        if truncated:
            output = output[:_MAX_OUTPUT_BYTES] + "\n[truncated]"

        returncode = proc.returncode or 0
        if returncode != 0:
            return ToolResult(
                content=f"[exit {returncode}]\n{output}",
                is_error=True,
                error_type="runtime_error",
            )
        return ToolResult(content=output or "[no output]")


# 取消或超时时终止 shell 及其派生的 Git/LFS/测试进程，避免服务器遗留孤儿任务
async def _stop_process_tree(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    try:
        if os.name == "nt":
            killer = await asyncio.create_subprocess_exec(
                "taskkill",
                "/PID",
                str(process.pid),
                "/T",
                "/F",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(killer.wait(), timeout=3.0)
        else:
            killpg = getattr(os, "killpg")
            killpg(process.pid, signal.SIGTERM)
        await asyncio.wait_for(process.wait(), timeout=1.0)
    except ProcessLookupError:
        return
    except TimeoutError:
        if os.name != "nt":
            try:
                killpg = getattr(os, "killpg")
                killpg(process.pid, getattr(signal, "SIGKILL", 9))
            except ProcessLookupError:
                pass
        elif process.returncode is None:
            process.kill()
        await process.wait()
