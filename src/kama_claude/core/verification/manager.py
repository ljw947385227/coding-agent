from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path

from kama_claude.core.verification.detector import discover_verification_plan
from kama_claude.core.verification.model import (
    VerificationCheck,
    VerificationKind,
    VerificationPlan,
    VerificationReport,
)
from kama_claude.core.verification.runner import VerificationRunner
from kama_claude.core.verification.test_index import ProjectTestIndexManager


class VerificationManager:
    # 初始化项目根目录及单项检查的资源边界
    def __init__(
        self,
        root: Path,
        *,
        timeout_seconds: float = 120.0,
        max_output_bytes: int = 32 * 1024,
    ) -> None:
        self._root = root.resolve()
        self._runner = VerificationRunner(
            timeout_seconds=timeout_seconds,
            max_output_bytes=max_output_bytes,
        )

    # 探测项目生态并生成可审查的验证计划
    def plan(
        self, selected: Sequence[VerificationKind] | None = None
    ) -> VerificationPlan:
        return discover_verification_plan(self._root, selected)

    # 生成验证计划并在任务基线可用时用持久化依赖索引选择相关 pytest 文件
    async def incremental_plan(
        self,
        baseline_tree: str,
        repository: Path,
        selected: Sequence[VerificationKind] | None = None,
        baseline_checkpoint_id: str | None = None,
    ) -> VerificationPlan:
        plan = self.plan(selected)
        if not any(check.ecosystem == "python" and check.tool == "pytest" for check in plan.checks):
            return plan
        selection = await ProjectTestIndexManager(self._root, repository).select(baseline_tree)
        selection = replace(
            selection,
            baseline_checkpoint_id=baseline_checkpoint_id,
        )
        checks = tuple(
            _apply_test_selection(check, selection.selected_tests) for check in plan.checks
        )
        strategy = (
            f"Selected {len(selection.selected_tests)} related pytest file(s)."
            if selection.strategy == "related"
            else f"Incremental tests fell back to full: {selection.fallback_reason}."
        )
        return replace(
            plan,
            checks=checks,
            warnings=(*plan.warnings, strategy),
            test_selection=selection,
        )

    # 执行给定计划并返回统一结构化报告
    async def run(
        self, plan: VerificationPlan, *, fail_fast: bool = True
    ) -> VerificationReport:
        return await self._runner.run(plan, fail_fast=fail_fast)


# 将检测器生成的全量 pytest 目标替换为增量选择出的测试文件
def _apply_test_selection(
    check: VerificationCheck,
    selected_tests: tuple[str, ...],
) -> VerificationCheck:
    if not selected_tests or check.ecosystem != "python" or check.tool != "pytest":
        return check
    try:
        pytest_position = check.command.index("pytest")
    except ValueError:
        return check
    return replace(
        check,
        command=(*check.command[: pytest_position + 1], *selected_tests, "-q"),
        source="incremental-test-index",
    )
