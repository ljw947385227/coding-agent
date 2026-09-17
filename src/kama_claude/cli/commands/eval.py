from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from pydantic import ValidationError

from kama_claude.core.config import KamaConfig
from kama_claude.core.harness import EvaluationHarness, EvaluationResult, EvaluationSuite


def cmd_eval(
    task_file: str,
    config: KamaConfig,
    *,
    output: str | None,
    repeats: int,
    keep_worktrees: bool,
) -> None:
    path = Path(task_file).expanduser().resolve()
    try:
        suite = EvaluationSuite.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValidationError) as exc:
        print(f"error: cannot load evaluation suite: {exc}", file=sys.stderr)
        sys.exit(2)

    suite = _resolve_repositories(suite, path.parent)
    output_root = (
        Path(output).expanduser().resolve()
        if output is not None
        else Path("~/.kama/evaluations").expanduser().resolve()
    )
    output_root.mkdir(parents=True, exist_ok=True)
    try:
        results = asyncio.run(
            _run_suite(
                suite,
                config,
                output_root,
                repeats=repeats,
                keep_worktrees=keep_worktrees,
            )
        )
    except KeyboardInterrupt:
        sys.exit(130)

    summary = _summary(results)
    summary_path = output_root / "summary.json"
    summary_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"report: {summary_path}")
    sys.exit(0 if summary["successful"] == summary["runs"] else 1)


async def _run_suite(
    suite: EvaluationSuite,
    config: KamaConfig,
    output_root: Path,
    *,
    repeats: int,
    keep_worktrees: bool,
) -> list[EvaluationResult]:
    harness = EvaluationHarness(config, output_root=output_root)
    results: list[EvaluationResult] = []
    for repeat in range(1, repeats + 1):
        for task in suite.tasks:
            print(f"[eval] {task.name} repeat={repeat}/{repeats}")
            result = await harness.run(task, keep_worktree=keep_worktrees)
            results.append(result)
            verification = (
                "passed" if result.verification_passed else "failed"
            )
            print(
                f"[eval] {result.status} run={result.run_id} "
                f"verification={verification} duration={result.metrics.duration_ms}ms"
            )
    return results


def _resolve_repositories(suite: EvaluationSuite, base: Path) -> EvaluationSuite:
    tasks = []
    for task in suite.tasks:
        repository = Path(task.repository).expanduser()
        if not repository.is_absolute():
            repository = base / repository
        tasks.append(task.model_copy(update={"repository": repository.resolve().as_posix()}))
    return suite.model_copy(update={"tasks": tasks})


def _summary(results: list[EvaluationResult]) -> dict[str, object]:
    durations = sorted(result.metrics.duration_ms for result in results)
    successful = sum(result.status == "success" for result in results)
    verification_passed = sum(result.verification_passed is True for result in results)
    return {
        "runs": len(results),
        "successful": successful,
        "success_rate": successful / len(results) if results else 0.0,
        "verification_passed": verification_passed,
        "verification_pass_rate": verification_passed / len(results) if results else 0.0,
        "duration_p50_ms": _percentile(durations, 0.50),
        "duration_p95_ms": _percentile(durations, 0.95),
        "input_tokens": sum(result.metrics.input_tokens for result in results),
        "output_tokens": sum(result.metrics.output_tokens for result in results),
        "tool_calls": sum(result.metrics.tool_calls for result in results),
        "tool_failures": sum(result.metrics.tool_failures for result in results),
        "results": [result.model_dump(mode="json") for result in results],
    }


def _percentile(values: list[int], quantile: float) -> int:
    if not values:
        return 0
    index = round((len(values) - 1) * quantile)
    return values[index]
