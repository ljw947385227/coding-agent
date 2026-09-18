from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Coroutine
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel

from kama_claude.core.bus.events import (
    LlmTokenEvent,
    PermissionDeniedEvent,
    PermissionGrantedEvent,
    PermissionRequestedEvent,
    RunAttentionRequiredEvent,
    RunCancelledEvent,
    RunFinishedEvent,
    StepStartedEvent,
    ToolCallFailedEvent,
    ToolCallFinishedEvent,
    ToolCallStartedEvent,
)
from kama_claude.core.config import RunConfig
from kama_claude.core.events.bus import EventBus

CancelStatus = Literal["accepted", "already_finished", "not_found"]
_SHUTDOWN_TIMEOUT_SECONDS = 5.0

logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(slots=True)
class RunHandle:
    run_id: str
    session_id: str
    workspace_root: str
    goal: str
    task: asyncio.Task[Any]
    parent_run_id: str | None
    started_at: float
    phase_started_at: float
    last_progress_at: float
    status: str = "running"
    current_phase: str = "starting"
    current_tool: str | None = None
    attention_reason: str | None = None
    warned_phase: str | None = None
    total_warned: bool = False
    snoozed_until: float = 0.0
    cancel_reason: str | None = None
    cancel_event_published: bool = False

    def summary(self, now: float) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "session_id": self.session_id,
            "workspace_root": self.workspace_root,
            "goal": self.goal,
            "parent_run_id": self.parent_run_id,
            "status": self.status,
            "current_phase": self.current_phase,
            "current_tool": self.current_tool,
            "elapsed_seconds": max(0.0, now - self.started_at),
            "idle_seconds": max(0.0, now - self.last_progress_at),
            "attention_reason": self.attention_reason,
        }


class RunManager:
    """Tracks, monitors, and cancels top-level and child agent runs by run_id."""

    def __init__(self, bus: EventBus, config: RunConfig) -> None:
        self._bus = bus
        self._config = config
        self._runs: dict[str, RunHandle] = {}
        self._monitor_task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._monitor_task is None or self._monitor_task.done():
            self._monitor_task = asyncio.create_task(self._monitor())

    async def stop(self) -> None:
        for handle in list(self._runs.values()):
            handle.cancel_reason = "daemon_shutdown"
            handle.task.cancel("daemon_shutdown")
        tasks = [handle.task for handle in self._runs.values()]
        if tasks:
            _done, pending = await asyncio.wait(
                tasks,
                timeout=_SHUTDOWN_TIMEOUT_SECONDS,
            )
            if pending:
                logger.warning(
                    "shutdown: %d run task(s) did not stop within %.1fs",
                    len(pending),
                    _SHUTDOWN_TIMEOUT_SECONDS,
                )
        if self._monitor_task is not None:
            self._monitor_task.cancel()
            await asyncio.gather(self._monitor_task, return_exceptions=True)
            self._monitor_task = None

    def create_run(
        self,
        run_id: str,
        coroutine: Coroutine[Any, Any, Any],
        *,
        session_id: str,
        workspace_root: str | Path,
        goal: str,
        parent_run_id: str | None = None,
    ) -> asyncio.Task[Any]:
        if run_id in self._runs:
            coroutine.close()
            raise ValueError(f"run already registered: {run_id}")
        task = asyncio.create_task(coroutine, name=f"kama-run:{run_id}")
        self.register_task(
            run_id,
            task,
            session_id=session_id,
            workspace_root=workspace_root,
            goal=goal,
            parent_run_id=parent_run_id,
        )
        return task

    def register_task(
        self,
        run_id: str,
        task: asyncio.Task[Any],
        *,
        session_id: str,
        workspace_root: str | Path,
        goal: str,
        parent_run_id: str | None = None,
    ) -> None:
        now = time.monotonic()
        self._runs[run_id] = RunHandle(
            run_id=run_id,
            session_id=session_id,
            workspace_root=Path(workspace_root).resolve().as_posix(),
            goal=goal,
            task=task,
            parent_run_id=parent_run_id,
            started_at=now,
            phase_started_at=now,
            last_progress_at=now,
        )

        def _completed(completed: asyncio.Task[Any]) -> None:
            self._on_done(run_id, completed)

        task.add_done_callback(_completed)

    def list_runs(
        self,
        *,
        workspace_root: str | Path | None = None,
        session_id: str | None = None,
    ) -> list[dict[str, Any]]:
        workspace = (
            Path(workspace_root).resolve().as_posix() if workspace_root is not None else None
        )
        now = time.monotonic()
        return [
            handle.summary(now)
            for handle in self._runs.values()
            if (workspace is None or handle.workspace_root == workspace)
            and (session_id is None or handle.session_id == session_id)
            and not handle.task.done()
        ]

    def cancel(
        self,
        run_id: str,
        *,
        workspace_root: str | Path | None = None,
        cascade: bool = True,
    ) -> CancelStatus:
        handle = self._runs.get(run_id)
        if handle is None:
            return "not_found"
        if (
            workspace_root is not None
            and handle.workspace_root != Path(workspace_root).resolve().as_posix()
        ):
            return "not_found"
        if handle.task.done() or handle.status == "cancelled":
            return "already_finished"
        targets = [handle]
        if cascade:
            targets.extend(self._descendants(run_id))
        for target in reversed(targets):
            if not target.task.done():
                target.status = "cancelling"
                target.current_phase = "cancelling"
                target.cancel_reason = "cancelled_by_user"
                target.task.cancel("cancelled_by_user")
        return "accepted"

    def snooze(
        self,
        run_id: str,
        seconds: float | None = None,
        *,
        workspace_root: str | Path | None = None,
    ) -> bool:
        handle = self._runs.get(run_id)
        if handle is None or handle.task.done():
            return False
        if (
            workspace_root is not None
            and handle.workspace_root != Path(workspace_root).resolve().as_posix()
        ):
            return False
        delay = self._config.warning_snooze_seconds if seconds is None else max(1.0, seconds)
        handle.snoozed_until = time.monotonic() + delay
        handle.attention_reason = None
        handle.warned_phase = None
        return True

    async def observe_event(self, event: BaseModel) -> None:
        run_id = getattr(event, "run_id", None)
        if not isinstance(run_id, str):
            return
        handle = self._runs.get(run_id)
        if handle is None:
            return
        now = time.monotonic()
        if isinstance(event, ToolCallStartedEvent):
            handle.current_phase = "tool"
            handle.current_tool = event.tool_name
            handle.phase_started_at = now
            handle.last_progress_at = now
            handle.warned_phase = None
            handle.attention_reason = None
        elif isinstance(event, PermissionRequestedEvent):
            handle.status = "waiting_user"
            handle.current_phase = "waiting_user"
            handle.last_progress_at = now
        elif isinstance(event, (PermissionGrantedEvent, PermissionDeniedEvent)):
            handle.status = "running"
            handle.current_phase = "tool"
            handle.last_progress_at = now
        elif isinstance(event, (ToolCallFinishedEvent, ToolCallFailedEvent)):
            handle.status = "running"
            handle.current_phase = "agent"
            handle.current_tool = None
            handle.phase_started_at = now
            handle.last_progress_at = now
            handle.warned_phase = None
            handle.attention_reason = None
        elif isinstance(event, StepStartedEvent):
            handle.current_phase = "llm"
            handle.current_tool = None
            handle.phase_started_at = now
            handle.last_progress_at = now
            handle.warned_phase = None
        elif isinstance(event, LlmTokenEvent):
            handle.current_phase = "llm"
            handle.last_progress_at = now
        elif isinstance(event, RunFinishedEvent):
            handle.status = event.status
            handle.current_phase = "finished"
            handle.last_progress_at = now
        elif isinstance(event, RunCancelledEvent):
            handle.cancel_event_published = True

    def _descendants(self, run_id: str) -> list[RunHandle]:
        result: list[RunHandle] = []
        pending = [run_id]
        while pending:
            parent = pending.pop()
            children = [item for item in self._runs.values() if item.parent_run_id == parent]
            result.extend(children)
            pending.extend(child.run_id for child in children)
        return result

    def _on_done(self, run_id: str, task: asyncio.Task[Any]) -> None:
        handle = self._runs.get(run_id)
        if handle is None:
            return
        was_cancelled = task.cancelled()
        if not was_cancelled:
            try:
                task.exception()
            except (asyncio.CancelledError, Exception):
                pass
        if was_cancelled and not handle.cancel_event_published:
            asyncio.create_task(
                self._bus.publish(
                    RunCancelledEvent(
                        run_id=run_id,
                        session_id=handle.session_id,
                        reason=handle.cancel_reason or "cancelled",
                        ts=_now(),
                    )
                )
            )
        self._runs.pop(run_id, None)

    async def _monitor(self) -> None:
        try:
            while True:
                await asyncio.sleep(self._config.monitor_interval_seconds)
                now = time.monotonic()
                for handle in list(self._runs.values()):
                    if handle.task.done() or handle.status in {"waiting_user", "cancelling"}:
                        continue
                    if now < handle.snoozed_until:
                        continue
                    if handle.attention_reason is not None:
                        continue
                    elapsed = now - handle.started_at
                    idle = now - handle.last_progress_at
                    phase_elapsed = now - handle.phase_started_at
                    reason: Literal["tool_slow", "no_progress", "run_slow"] | None = None
                    if (
                        handle.current_tool is not None
                        and phase_elapsed >= self._config.tool_warning_seconds
                        and handle.warned_phase != handle.current_tool
                    ):
                        reason = "tool_slow"
                        handle.warned_phase = handle.current_tool
                    elif (
                        idle >= self._config.stalled_seconds
                        and handle.warned_phase != handle.current_phase
                    ):
                        reason = "no_progress"
                        handle.warned_phase = handle.current_phase
                    elif elapsed >= self._config.slow_run_seconds and not handle.total_warned:
                        reason = "run_slow"
                        handle.total_warned = True
                    if reason is None:
                        continue
                    handle.attention_reason = reason
                    await self._bus.publish(
                        RunAttentionRequiredEvent(
                            run_id=handle.run_id,
                            session_id=handle.session_id,
                            reason=reason,
                            elapsed_seconds=elapsed,
                            idle_seconds=idle,
                            current_phase=handle.current_phase,
                            current_tool=handle.current_tool,
                            ts=_now(),
                        )
                    )
        except asyncio.CancelledError:
            raise
