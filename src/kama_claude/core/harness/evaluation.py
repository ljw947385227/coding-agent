from __future__ import annotations

import asyncio
import json
import re
import tempfile
import time
from copy import deepcopy
from dataclasses import asdict
from pathlib import Path

from kama_claude.core.config import KamaConfig
from kama_claude.core.events.bus import EventBus
from kama_claude.core.git.checkpoint import CheckpointManager
from kama_claude.core.git.process import git_error_message, run_git
from kama_claude.core.harness.metrics import MetricsCollector
from kama_claude.core.harness.models import (
    EvaluationExpectations,
    EvaluationResult,
    EvaluationSandboxSpec,
    EvaluationScore,
    EvaluationTask,
)
from kama_claude.core.llm.base import LLMProvider
from kama_claude.core.runner import AgentHarness, RunOutcome
from kama_claude.core.runs import new_run_id
from kama_claude.core.sandbox import LocalExecutionBackend, create_execution_backend
from kama_claude.core.verification.model import (
    VerificationCheck,
    VerificationPlan,
    VerificationReport,
)
from kama_claude.core.verification.runner import VerificationRunner

_PATCH_LIMIT = 8 * 1024 * 1024
_SAFE_NAME = re.compile(r"[^0-9A-Za-z._-]+")


class EvaluationRunner:
    """Run one coding task in an isolated Git worktree and preserve evidence."""

    def __init__(
        self,
        config: KamaConfig,
        *,
        output_root: Path | None = None,
        provider: LLMProvider | None = None,
    ) -> None:
        self._config = config
        self._output_root = (
            output_root or Path("~/.kama/evaluations").expanduser()
        ).resolve()
        self._provider = provider

    async def run(
        self,
        task: EvaluationTask,
        *,
        keep_worktree: bool = False,
    ) -> EvaluationResult:
        started = time.monotonic()
        run_id = new_run_id()
        task_name = _safe_name(task.name)
        artifact_dir = self._output_root / f"{task_name}-{run_id}"
        artifact_dir.mkdir(parents=True, exist_ok=False)
        worktree = (
            Path(tempfile.gettempdir()) / "kama-harness-worktrees" / run_id
        ).resolve()
        repository = Path(task.repository).expanduser().resolve()
        collector = MetricsCollector()
        result = EvaluationResult(
            task_name=task.name,
            run_id=run_id,
            status="infrastructure_error",
            repository=repository.as_posix(),
            base_ref=task.base_ref,
            worktree=worktree.as_posix(),
            artifact_dir=artifact_dir.as_posix(),
        )
        _write_json(artifact_dir / "request.json", task.model_dump(mode="json"))

        worktree_added = False
        try:
            base_commit = await _resolve_commit(repository, task.base_ref)
            result.base_commit = base_commit
            await _add_worktree(repository, worktree, base_commit)
            worktree_added = True

            config = deepcopy(self._config)
            if task.max_steps is not None:
                config.agent.max_steps = task.max_steps
            if task.model is not None:
                config.llm.default_model = task.model
            if task.sandbox is not None:
                _apply_sandbox_config(config, task.sandbox)

            bus = EventBus()
            bus.subscribe(collector.handle)
            runner = AgentHarness(
                config,
                bus=bus,
                provider=self._provider,
                runs_dir=artifact_dir / "runs",
            )

            outcome: RunOutcome
            try:
                outcome = await asyncio.wait_for(
                    runner.run_and_capture(
                        task.goal,
                        run_id=run_id,
                        system_prompt_override=task.system_prompt,
                        tool_whitelist=task.tool_whitelist,
                        include_global_context=False,
                        workspace_root=worktree,
                    ),
                    timeout=task.timeout_seconds,
                )
                result.agent_status = outcome.status
                result.agent_reason = outcome.reason
                result.answer = outcome.result
            except TimeoutError:
                result.status = "timeout"
                result.reason = f"agent exceeded {task.timeout_seconds:g}s timeout"
                result.agent_status = "failed"
                result.agent_reason = "harness_timeout"

            changed_files, patch_file, truncated = await _capture_patch(
                worktree,
                artifact_dir,
            )
            result.changed_files = changed_files
            result.patch_file = patch_file
            result.patch_truncated = truncated

            if task.expectations is not None:
                result.score = _score_expectations(
                    task.expectations,
                    result,
                    collector.tool_calls_by_name,
                )
                _write_json(
                    artifact_dir / "score.json",
                    result.score.model_dump(mode="json"),
                )
            _write_json(artifact_dir / "tool_trace.json", collector.tool_trace)

            report = await _run_oracle(task, worktree, config)
            result.verification_passed = report.passed
            _write_json(artifact_dir / "verification.json", asdict(report))

            if result.status != "timeout":
                behavior_passed = result.score is None or result.score.passed
                if result.agent_status == "success" and report.passed and behavior_passed:
                    result.status = "success"
                    result.reason = None
                else:
                    result.status = "failed"
                    if result.agent_reason is not None:
                        result.reason = result.agent_reason
                    elif not report.passed:
                        result.reason = "verification_failed"
                    elif not behavior_passed:
                        result.reason = "behavior_expectation_failed"
                    else:
                        result.reason = "agent_failed"
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            result.status = "infrastructure_error"
            result.reason = str(exc)
        finally:
            if worktree_added and not keep_worktree:
                cleanup_error = await _remove_worktree(repository, worktree)
                result.cleanup_error = cleanup_error
                if cleanup_error is not None:
                    result.status = "infrastructure_error"
                    result.reason = "worktree_cleanup_failed"
            elif worktree_added:
                result.worktree_kept = True

            duration_ms = int((time.monotonic() - started) * 1000)
            result.metrics = collector.snapshot(duration_ms)
            _write_json(
                artifact_dir / "metrics.json",
                result.metrics.model_dump(mode="json"),
            )
            _write_json(artifact_dir / "result.json", result.model_dump(mode="json"))
        return result


async def _resolve_commit(repository: Path, base_ref: str) -> str:
    if not repository.is_dir():
        raise FileNotFoundError(f"repository does not exist: {repository}")
    resolved = await run_git(
        repository,
        ["rev-parse", "--verify", f"{base_ref}^{{commit}}"],
        max_stdout_bytes=4096,
        timeout=30.0,
    )
    if resolved.returncode != 0 or resolved.timed_out or resolved.truncated:
        raise RuntimeError(f"cannot resolve base_ref {base_ref!r}: {git_error_message(resolved)}")
    commit = resolved.stdout.decode("utf-8", errors="replace").strip()
    if not commit:
        raise RuntimeError(f"Git returned an empty commit for {base_ref!r}")
    return commit


async def _add_worktree(repository: Path, target: Path, commit: str) -> None:
    if target.exists():
        raise FileExistsError(f"evaluation worktree already exists: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        added = await run_git(
            repository,
            ["worktree", "add", "--detach", str(target), commit],
            max_stdout_bytes=1024 * 1024,
            timeout=60.0,
        )
    except asyncio.CancelledError:
        await asyncio.shield(_remove_worktree(repository, target))
        raise
    if added.returncode != 0 or added.timed_out or added.truncated:
        await _remove_worktree(repository, target)
        raise RuntimeError(f"cannot create evaluation worktree: {git_error_message(added)}")


async def _remove_worktree(repository: Path, target: Path) -> str | None:
    removed = await run_git(
        repository,
        ["worktree", "remove", "--force", str(target)],
        max_stdout_bytes=1024 * 1024,
        timeout=60.0,
    )
    if removed.returncode != 0 or removed.timed_out or removed.truncated:
        return git_error_message(removed)
    return None


async def _capture_patch(
    worktree: Path,
    artifact_dir: Path,
) -> tuple[list[str], str | None, bool]:
    manager = CheckpointManager(worktree)
    current = await manager.capture_state()
    base_tree_result = await run_git(
        worktree,
        ["rev-parse", "HEAD^{tree}"],
        max_stdout_bytes=4096,
        timeout=30.0,
    )
    if (
        base_tree_result.returncode != 0
        or base_tree_result.timed_out
        or base_tree_result.truncated
    ):
        raise RuntimeError(f"cannot resolve base tree: {git_error_message(base_tree_result)}")
    base_tree = base_tree_result.stdout.decode("utf-8", errors="replace").strip()
    changed_files = _changed_paths(
        await manager.diff_trees(base_tree, current["worktree_tree"])
    )
    if not changed_files:
        return [], None, False

    patch = await run_git(
        worktree,
        [
            "diff",
            "--binary",
            "--no-color",
            "--no-ext-diff",
            base_tree,
            current["worktree_tree"],
        ],
        max_stdout_bytes=_PATCH_LIMIT,
        timeout=60.0,
    )
    if patch.returncode != 0 or patch.timed_out:
        raise RuntimeError(f"cannot capture evaluation patch: {git_error_message(patch)}")
    if patch.truncated:
        marker = artifact_dir / "agent.patch.truncated"
        marker.write_bytes(patch.stdout)
        return changed_files, marker.as_posix(), True
    path = artifact_dir / "agent.patch"
    path.write_bytes(patch.stdout)
    return changed_files, path.as_posix(), False


async def _run_oracle(
    task: EvaluationTask,
    worktree: Path,
    config: KamaConfig,
) -> VerificationReport:
    checks = tuple(
        VerificationCheck(
            kind=spec.kind,
            ecosystem=spec.ecosystem,
            command=tuple(spec.command),
            source="evaluation_harness",
            tool=spec.tool,
        )
        for spec in task.checks
    )
    plan = VerificationPlan(
        root=worktree.as_posix(),
        ecosystems=tuple(dict.fromkeys(spec.ecosystem for spec in task.checks)),
        checks=checks,
    )
    execution_backend = (
        create_execution_backend(config.execution)
        if task.oracle_backend == "sandbox"
        else LocalExecutionBackend()
    )
    return await VerificationRunner(
        timeout_seconds=task.verification_timeout_seconds,
        execution_backend=execution_backend,
    ).run(plan)


# 将任务级沙箱场景覆盖应用到隔离后的 Harness 配置副本
def _apply_sandbox_config(config: KamaConfig, sandbox: EvaluationSandboxSpec) -> None:
    config.execution.backend = sandbox.backend
    config.execution.docker.network = sandbox.network
    if sandbox.image is not None:
        config.execution.docker.image = sandbox.image
    for field_name in ("memory_mb", "cpus", "pids_limit", "tmpfs_mb"):
        value = getattr(sandbox, field_name)
        if value is not None:
            setattr(config.execution.docker, field_name, value)


# 对工具选择、源码修改和最终诊断文本执行可复现的行为评分
def _score_expectations(
    expected: EvaluationExpectations,
    result: EvaluationResult,
    calls: dict[str, int],
) -> EvaluationScore:
    failures: list[str] = []
    sandbox_calls = calls.get("sandbox_info", 0)
    if expected.sandbox_info == "required" and sandbox_calls == 0:
        failures.append("sandbox_info was required but not called")
    if expected.sandbox_info == "forbidden" and sandbox_calls > 0:
        failures.append("sandbox_info was forbidden but called")
    if sandbox_calls > expected.max_sandbox_info_calls:
        failures.append(
            f"sandbox_info called {sandbox_calls} times; maximum is "
            f"{expected.max_sandbox_info_calls}"
        )
    if not expected.allow_source_changes and result.changed_files:
        failures.append("source changes were forbidden: " + ", ".join(result.changed_files))
    for tool_name in expected.required_tools:
        if calls.get(tool_name, 0) == 0:
            failures.append(f"required tool was not called: {tool_name}")
    for tool_name in expected.forbidden_tools:
        if calls.get(tool_name, 0) > 0:
            failures.append(f"forbidden tool was called: {tool_name}")
    matched: list[str] = []
    for pattern in expected.answer_patterns:
        if re.search(pattern, result.answer, re.IGNORECASE | re.MULTILINE) is None:
            failures.append(f"answer did not match pattern: {pattern}")
        else:
            matched.append(pattern)
    return EvaluationScore(
        passed=not failures,
        failures=failures,
        sandbox_info_calls=sandbox_calls,
        matched_answer_patterns=matched,
    )


def _safe_name(value: str) -> str:
    normalized = _SAFE_NAME.sub("-", value.strip()).strip("-._")
    return normalized[:60] or "task"


def _changed_paths(name_status: tuple[str, ...] | list[str]) -> list[str]:
    paths: list[str] = []
    for row in name_status:
        columns = row.split("\t")
        for path in columns[1:]:
            if path and path not in paths:
                paths.append(path)
    return paths


def _write_json(path: Path, payload: object) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


# 兼容早期错误命名；新代码应使用 EvaluationRunner，避免与生产 AgentHarness 混淆
EvaluationHarness = EvaluationRunner
