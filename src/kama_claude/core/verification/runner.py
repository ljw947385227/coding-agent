from __future__ import annotations

import time
from pathlib import Path

from kama_claude.core.sandbox import ExecutionBackend, ExecutionRequest, LocalExecutionBackend
from kama_claude.core.sandbox.diagnostics import sandbox_failure_hint
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
        execution_backend: ExecutionBackend | None = None,
        sensitive_patterns: tuple[str, ...] = (),
    ) -> None:
        self._timeout_seconds = timeout_seconds
        self._max_output_bytes = max_output_bytes
        self._parsers = parser_registry or ParserRegistry()
        self._execution_backend = execution_backend or LocalExecutionBackend()
        self._sensitive_patterns = sensitive_patterns

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

    # 通过可替换执行后端运行单项检查并映射超时、退出码和有界输出
    async def _run_check(
        self, root: Path, check: VerificationCheck
    ) -> VerificationResult:
        started = time.monotonic()
        execution = await self._execution_backend.run(
            ExecutionRequest(
                argv=check.command,
                workspace=root,
                timeout_seconds=self._timeout_seconds,
                max_output_bytes=self._max_output_bytes,
                environment={"PYTHONDONTWRITEBYTECODE": "1"},
                sensitive_patterns=self._sensitive_patterns,
            )
        )
        if execution.timed_out:
            status: VerificationStatus = "timeout"
        elif execution.launch_error is not None or execution.cleanup_error is not None:
            status = "error"
        else:
            status = "passed" if execution.exit_code == 0 else "failed"
        output = execution.output
        environment_hint = sandbox_failure_hint(execution)
        if execution.cleanup_error is not None:
            output = f"{output}\n[sandbox cleanup error] {execution.cleanup_error}".strip()
        return _result(
            check,
            status,
            started,
            self._parsers,
            exit_code=execution.exit_code,
            output=output,
            truncated=execution.output_truncated,
            environment_hint=environment_hint,
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
    environment_hint: str | None = None,
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
        environment_hint=environment_hint,
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
