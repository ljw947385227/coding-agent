from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import time
from pathlib import Path

from kama_claude.core.sandbox.models import ExecutionRequest, ExecutionResult


class LocalExecutionBackend:
    # 使用宿主机子进程执行请求，保留迁移前的本地运行行为
    async def run(self, request: ExecutionRequest) -> ExecutionResult:
        started = time.monotonic()
        workspace = request.workspace.resolve()
        environment = {**os.environ, **request.environment}
        try:
            if request.shell_command is not None:
                process = await _create_shell_process(
                    request.shell_command,
                    workspace,
                    environment,
                )
            else:
                process = await _create_exec_process(
                    request.argv,
                    workspace,
                    environment,
                )
        except OSError as exc:
            return ExecutionResult(
                exit_code=None,
                output=str(exc),
                duration_ms=_elapsed_ms(started),
                backend="local",
                launch_error=str(exc),
            )

        if process.stdout is None:
            await _stop_process_tree(process)
            message = "failed to open process output pipe"
            return ExecutionResult(
                exit_code=process.returncode,
                output=message,
                duration_ms=_elapsed_ms(started),
                backend="local",
                launch_error=message,
            )

        output_task = asyncio.create_task(
            _read_head_tail(process.stdout, request.max_output_bytes)
        )
        timed_out = False
        try:
            await asyncio.wait_for(process.wait(), timeout=request.timeout_seconds)
        except TimeoutError:
            timed_out = True
            await _stop_process_tree(process)
        except asyncio.CancelledError:
            await _stop_process_tree(process)
            output_task.cancel()
            await asyncio.gather(output_task, return_exceptions=True)
            raise
        output, truncated = await output_task
        return ExecutionResult(
            exit_code=process.returncode,
            output=output,
            duration_ms=_elapsed_ms(started),
            backend="local",
            timed_out=timed_out,
            output_truncated=truncated,
        )


# 创建合并标准输出与错误输出的本地 shell 子进程
async def _create_shell_process(
    command: str,
    workspace: Path,
    environment: dict[str, str],
) -> asyncio.subprocess.Process:
    if os.name == "nt":
        return await asyncio.create_subprocess_shell(
            command,
            cwd=workspace,
            env=environment,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
        )
    return await asyncio.create_subprocess_shell(
        command,
        cwd=workspace,
        env=environment,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        start_new_session=True,
    )


# 创建不经过 shell 解释的本地 argv 子进程
async def _create_exec_process(
    argv: tuple[str, ...],
    workspace: Path,
    environment: dict[str, str],
) -> asyncio.subprocess.Process:
    if os.name == "nt":
        return await asyncio.create_subprocess_exec(
            *argv,
            cwd=workspace,
            env=environment,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
        )
    return await asyncio.create_subprocess_exec(
        *argv,
        cwd=workspace,
        env=environment,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        start_new_session=True,
    )


# 有界保留进程输出头尾并持续排空管道避免死锁
async def _read_head_tail(
    reader: asyncio.StreamReader,
    limit: int,
) -> tuple[str, bool]:
    head_limit = max(1, limit // 2)
    tail_limit = max(1, limit - head_limit)
    head = bytearray()
    tail = bytearray()
    total = 0
    while chunk := await reader.read(64 * 1024):
        total += len(chunk)
        if len(head) < head_limit:
            take = min(head_limit - len(head), len(chunk))
            head.extend(chunk[:take])
            chunk = chunk[take:]
        if chunk:
            tail.extend(chunk)
            if len(tail) > tail_limit:
                del tail[:-tail_limit]
    truncated = total > limit
    if truncated:
        raw = bytes(head) + b"\n[... output truncated ...]\n" + bytes(tail)
    else:
        raw = bytes(head) + bytes(tail)
    return raw.decode("utf-8", errors="replace").strip(), truncated


# 取消或超时时终止子进程及其派生进程树
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


# 返回从单调时钟起点到当前的整数毫秒数
def _elapsed_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)
