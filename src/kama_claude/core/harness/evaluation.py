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
    EvaluationResult,
    EvaluationTask,
)
from kama_claude.core.llm.base import LLMProvider
from kama_claude.core.runner import AgentRunner, RunOutcome
from kama_claude.core.runs import new_run_id
from kama_claude.core.verification.model import (
    VerificationCheck,
    VerificationPlan,
    VerificationReport,
)
from kama_claude.core.verification.runner import VerificationRunner

_PATCH_LIMIT = 8 * 1024 * 1024
_SAFE_NAME = re.compile(r"[^0-9A-Za-z._-]+")


class EvaluationHarness:
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

            bus = EventBus()
            bus.subscribe(collector.handle)
            runner = AgentRunner(
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

            report = await _run_oracle(task, worktree)
            result.verification_passed = report.passed
            _write_json(artifact_dir / "verification.json", asdict(report))

            if result.status != "timeout":
                if result.agent_status == "success" and report.passed:
                    result.status = "success"
                    result.reason = None
                else:
                    result.status = "failed"
                    result.reason = result.agent_reason or (
                        "verification_failed" if not report.passed else "agent_failed"
                    )
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


async def _run_oracle(task: EvaluationTask, worktree: Path) -> VerificationReport:
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
    return await VerificationRunner(
        timeout_seconds=task.verification_timeout_seconds,
    ).run(plan)


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
