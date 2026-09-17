from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import time
from pathlib import Path

from kama_claude.core.verification.model import (
    VerificationCheck,
    VerificationPlan,
    VerificationReport,
    VerificationResult,
    VerificationStatus,
)
from kama_claude.core.verification.parser import ParserRegistry, infer_tool

_DEFAULT_OUTPUT_BYTES = 32 * 1024


class VerificationRunner:
    # 初始化每项检查的超时和输出预算
    def __init__(
        self,
        *,
        timeout_seconds: float = 120.0,
        max_output_bytes: int = _DEFAULT_OUTPUT_BYTES,
        parser_registry: ParserRegistry | None = None,
    ) -> None:
        self._timeout_seconds = timeout_seconds
        self._max_output_bytes = max_output_bytes
        self._parsers = parser_registry or ParserRegistry()

    # 串行执行验证计划并按 fail_fast 策略标记剩余检查
    async def run(
        self, plan: VerificationPlan, *, fail_fast: bool = True
    ) -> VerificationReport:
        started = time.monotonic()
        results: list[VerificationResult] = []
        halted = False
        root = Path(plan.root)
        for check in plan.checks:
            if halted:
                results.append(_skipped_result(check))
                continue
            result = await self._run_check(root, check)
            results.append(result)
            halted = fail_fast and result.status != "passed"
        passed = bool(results) and all(result.status == "passed" for result in results)
        return VerificationReport(
            root=plan.root,
            passed=passed,
            elapsed_ms=int((time.monotonic() - started) * 1000),
            results=tuple(results),
        )

    # 以无 shell 子进程执行单项检查并捕获超时、退出码和有界输出
    async def _run_check(
        self, root: Path, check: VerificationCheck
    ) -> VerificationResult:
        started = time.monotonic()
        try:
            if os.name == "nt":
                process = await asyncio.create_subprocess_exec(
                    *check.command,
                    cwd=root,
                    env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                    creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
                )
            else:
                process = await asyncio.create_subprocess_exec(
                    *check.command,
                    cwd=root,
                    env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                    start_new_session=True,
                )
        except OSError as exc:
            return _result(
                check,
                "error",
                started,
                self._parsers,
                exit_code=None,
                output=str(exc),
            )
        if process.stdout is None:
            await _stop_process(process)
            return _result(
                check,
                "error",
                started,
                self._parsers,
                exit_code=process.returncode,
                output="failed to open verification output pipe",
            )

        output_task = asyncio.create_task(
            _read_head_tail(process.stdout, self._max_output_bytes)
        )
        timed_out = False
        try:
            await asyncio.wait_for(process.wait(), timeout=self._timeout_seconds)
        except TimeoutError:
            timed_out = True
            await _stop_process(process)
        except asyncio.CancelledError:
            await _stop_process(process)
            output_task.cancel()
            await asyncio.gather(output_task, return_exceptions=True)
            raise
        output, truncated = await output_task
        if timed_out:
            status: VerificationStatus = "timeout"
        else:
            status = "passed" if process.returncode == 0 else "failed"
        return _result(
            check,
            status,
            started,
            self._parsers,
            exit_code=process.returncode,
            output=output,
            truncated=truncated,
        )


# 构造包含耗时和输出信息的单项验证结果
def _result(
    check: VerificationCheck,
    status: VerificationStatus,
    started: float,
    parsers: ParserRegistry,
    *,
    exit_code: int | None,
    output: str,
    truncated: bool = False,
) -> VerificationResult:
    tool = check.tool if check.tool != "unknown" else infer_tool(check.command)
    return VerificationResult(
        kind=check.kind,
        ecosystem=check.ecosystem,
        tool=tool,
        command=check.command,
        status=status,
        exit_code=exit_code,
        elapsed_ms=int((time.monotonic() - started) * 1000),
        output=output,
        output_truncated=truncated,
        diagnostics=parsers.parse(tool, status, output),
    )


# 为 fail_fast 未执行的检查构造结构化 skipped 结果
def _skipped_result(check: VerificationCheck) -> VerificationResult:
    return VerificationResult(
        kind=check.kind,
        ecosystem=check.ecosystem,
        tool=check.tool if check.tool != "unknown" else infer_tool(check.command),
        command=check.command,
        status="skipped",
        exit_code=None,
        elapsed_ms=0,
        output="Skipped because an earlier verification check failed.",
    )


# 有界保留子进程输出头尾并持续排空管道避免死锁
async def _read_head_tail(
    reader: asyncio.StreamReader, limit: int
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
        raw = bytes(head) + b"\n[... verification output truncated ...]\n" + bytes(tail)
    else:
        raw = bytes(head) + bytes(tail)
    return raw.decode("utf-8", errors="replace").strip(), truncated


# 终止验证子进程并在必要时升级为强制结束
async def _stop_process(process: asyncio.subprocess.Process) -> None:
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
