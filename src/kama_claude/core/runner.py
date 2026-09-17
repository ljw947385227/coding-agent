from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from kama_claude.core.bus.events import RunCancelledEvent, RunFinishedEvent, RunStartedEvent
from kama_claude.core.compact.compactor import Compactor
from kama_claude.core.config import KamaConfig
from kama_claude.core.context import ExecutionContext
from kama_claude.core.events.bus import EventBus, EventHandler
from kama_claude.core.events.writer import EventWriter
from kama_claude.core.llm.base import LLMProvider
from kama_claude.core.llm.provider import AnthropicProvider
from kama_claude.core.loop import AgentLoop
from kama_claude.core.mcp.server import McpServerManager
from kama_claude.core.memory.loader import load_context_file
from kama_claude.core.permissions.manager import PermissionManager
from kama_claude.core.run_manager import RunManager
from kama_claude.core.runs import RUNS_DIR, new_run_id
from kama_claude.core.session.model import Session
from kama_claude.core.session.store import SessionStore
from kama_claude.core.subagent.registry import BackgroundTaskRegistry
from kama_claude.core.subagent.tool import AgentResultTool, SpawnAgentTool
from kama_claude.core.task.manager import TaskManager
from kama_claude.core.tools.builtin import (
    BashTool,
    EditFileTool,
    GitCheckpointTool,
    GitDiffTool,
    GitRollbackTool,
    GitStatusTool,
    ListDirTool,
    NoteSaveTool,
    ReadFileTool,
    SearchCodeTool,
    TaskCreateTool,
    TaskGetTool,
    TaskListTool,
    TaskUpdateTool,
    VerifyProjectTool,
    WriteFileTool,
)
from kama_claude.core.tools.registry import ToolRegistry
from kama_claude.core.trace.provider import TracingProvider
from kama_claude.core.trace.writer import TraceWriter
from kama_claude.core.verification import VerificationController


# 返回当前 UTC 时间的 ISO 8601 字符串
def _now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass
class RunOutcome:
    status: str
    result: str
    reason: str | None


class AgentRunner:
    # 组装所有运行时依赖，准备执行一次完整的 agent run
    def __init__(
        self,
        config: KamaConfig,
        *,
        bus: EventBus | None = None,
        provider: LLMProvider | None = None,
        extra_handlers: list[EventHandler] | None = None,
        runs_dir: Path | None = None,
        trace: TraceWriter | None = None,
        permission_manager: PermissionManager | None = None,
        mcp_manager: McpServerManager | None = None,
        run_manager: RunManager | None = None,
    ) -> None:
        self._config = config
        self._bus = bus
        self._provider = provider
        self._extra_handlers: list[EventHandler] = extra_handlers or []
        self._runs_dir = runs_dir or RUNS_DIR
        self._trace = trace
        self._permission_manager = permission_manager
        self._mcp_manager = mcp_manager
        self._run_manager = run_manager
        # 跨 run 共享的后台 subagent 任务注册表
        self._task_registry = BackgroundTaskRegistry()

    # 构建工具注册表，注入 TaskManager（任务工具共享同一实例）；可选注入 SpawnAgentTool
    def _build_registry(
        self,
        task_manager: TaskManager,
        *,
        session: Session | None = None,
        store: SessionStore | None = None,
        run_id: str | None = None,
        provider: LLMProvider | None = None,
        bus: EventBus | None = None,
        child_runs_dir: Path | None = None,
        session_id: str = "",
        tool_whitelist: list[str] | None = None,
        workspace_root: Path | None = None,
    ) -> ToolRegistry:
        allowed: set[str] | None = set(tool_whitelist) if tool_whitelist else None
        effective_workspace_root = (
            workspace_root.resolve()
            if workspace_root is not None
            else (
                Path(session.workspace_root)
                if session is not None and session.workspace_root is not None
                else Path.cwd().resolve()
            )
        )

        def _ok(name: str) -> bool:
            return allowed is None or name in allowed

        registry = ToolRegistry()
        for t in [
            ReadFileTool(
                ignore_files=self._config.files.ignore_files,
                workspace_root=effective_workspace_root,
            ),
            SearchCodeTool(
                ignore_files=self._config.files.ignore_files,
                workspace_root=effective_workspace_root,
            ),
            GitStatusTool(workspace_root=effective_workspace_root),
            GitDiffTool(workspace_root=effective_workspace_root),
            GitCheckpointTool(
                session_id=session.id if session is not None else None,
                node_id=session.current_id if session is not None else None,
                run_id=run_id,
                workspace_root=effective_workspace_root,
            ),
            GitRollbackTool(workspace_root=effective_workspace_root),
            VerifyProjectTool(workspace_root=effective_workspace_root),
            EditFileTool(workspace_root=effective_workspace_root),
            BashTool(workspace_root=effective_workspace_root),
            WriteFileTool(workspace_root=effective_workspace_root),
            ListDirTool(workspace_root=effective_workspace_root),
        ]:
            if _ok(t.name):
                registry.register(t)
        for t in [
            TaskCreateTool(task_manager),
            TaskUpdateTool(task_manager),
            TaskListTool(task_manager),
            TaskGetTool(task_manager),
        ]:
            if _ok(t.name):
                registry.register(t)
        if session is not None and store is not None and run_id is not None:
            note_tool = NoteSaveTool(store, session.id, run_id)
            if _ok(note_tool.name):
                registry.register(note_tool)
        if provider is not None and bus is not None and run_id is not None:
            runs_dir = child_runs_dir or self._runs_dir
            if _ok("spawn_agent"):
                registry.register(
                    SpawnAgentTool(
                        provider=provider,
                        parent_bus=bus,
                        parent_run_id=run_id,
                        permission_manager=self._permission_manager,
                        max_steps=self._config.agent.max_steps,
                        task_registry=self._task_registry,
                        runs_dir=runs_dir,
                        session_id=session_id,
                        ignore_files=self._config.files.ignore_files,
                        workspace_root=effective_workspace_root,
                        depth=0,
                        verification_mode=self._config.verification.mode,
                        verification_max_attempts=self._config.verification.max_attempts,
                        verification_max_total_seconds=(
                            self._config.verification.max_total_seconds
                        ),
                        run_manager=self._run_manager,
                    )
                )
            if _ok("agent_result"):
                registry.register(AgentResultTool(self._task_registry))
        if self._mcp_manager is not None:
            for mcp_tool in self._mcp_manager.get_tools():
                if _ok(mcp_tool.name):
                    registry.register(mcp_tool)
        return registry

    # 执行一次完整的 agent run（委托给 run_and_capture，忽略返回值）
    async def run(self, goal: str, *, run_id: str | None = None) -> None:
        await self.run_and_capture(goal, run_id=run_id)

    # 执行 agent run 并返回 RunOutcome（含最终文字结果）
    async def run_and_capture(
        self,
        goal: str,
        *,
        run_id: str | None = None,
        session: Session | None = None,
        store: SessionStore | None = None,
        system_prompt_override: str | None = None,
        tool_whitelist: list[str] | None = None,
        include_global_context: bool = True,
        workspace_root: Path | None = None,
    ) -> RunOutcome:
        run_id = run_id or new_run_id()
        if session is not None and store is not None:
            run_path = store.runs_dir(session.id) / run_id
            history = store.read_messages(session.id)
            notes = store.read_notes(session.id)
        else:
            run_path = self._runs_dir / run_id
            history = [{"role": "user", "content": goal}]
            notes = ""
        run_path.mkdir(parents=True, exist_ok=True)

        global_ctx = (
            load_context_file(Path("~/.kama/context.md").expanduser())
            if include_global_context
            else ""
        )
        effective_workspace_root = (
            Path(session.workspace_root)
            if session is not None and session.workspace_root is not None
            else (workspace_root or Path.cwd()).resolve()
        )
        project_ctx = load_context_file(effective_workspace_root / ".kama/context.md")

        task_manager = TaskManager(run_path / ".tasks")

        bus = self._bus if self._bus is not None else EventBus()
        for h in self._extra_handlers:
            bus.subscribe(h)

        context = ExecutionContext(
            run_id=run_id,
            goal=goal,
            max_steps=self._config.agent.max_steps,
            prefill_messages=history,
            session_notes=notes,
            global_context=global_ctx,
            project_context=project_ctx,
            system_prompt_override=system_prompt_override,
        )
        if session is not None and store is not None:
            active_session = session
            session_store = store

            # 将 ExecutionContext 新节点同步追加到当前 Session，并把节点 ID 回传给状态快照逻辑
            def _persist_entry(entry: dict[str, Any]) -> str:
                return session_store.append_entry(active_session, entry, run_id)

            context.entry_sink = _persist_entry
        async with EventWriter(run_path / "events.jsonl") as writer:
            writer.subscribe(bus)
            await bus.publish(RunStartedEvent(run_id=run_id, goal=goal, ts=_now()))

            cancelled = False
            cancellation_reason = "cancelled"
            try:
                provider: LLMProvider = self._provider or AnthropicProvider(
                    self._config.llm.default_model
                )
                if self._trace is not None:
                    provider = TracingProvider(
                        provider,
                        self._trace,
                        include_payload=self._config.trace.include_llm_payload,
                    )
                session_id_str = session.id if session is not None else ""
                child_runs_dir = (
                    store.runs_dir(session.id)
                    if session is not None and store is not None
                    else self._runs_dir
                )
                registry = self._build_registry(
                    task_manager,
                    session=session,
                    store=store,
                    run_id=run_id,
                    provider=provider,
                    bus=bus,
                    child_runs_dir=child_runs_dir,
                    session_id=session_id_str,
                    tool_whitelist=tool_whitelist,
                    workspace_root=effective_workspace_root,
                )
                compactor = Compactor(
                    bus,
                    session_id_str,
                    tool_result_limit=self._config.compaction.tool_result_limit,
                    tool_result_keep=self._config.compaction.tool_result_keep,
                )
                verification_controller = None
                verification_available = registry.get("verify_project") is not None
                if session is not None or (
                    self._config.verification.mode != "off" and verification_available
                ):
                    verification_controller = VerificationController(
                        mode=(self._config.verification.mode if verification_available else "off"),
                        max_attempts=self._config.verification.max_attempts,
                        max_total_seconds=self._config.verification.max_total_seconds,
                        workspace_root=effective_workspace_root,
                        session_id=session.id if session is not None else None,
                        node_id=session.current_id if session is not None else None,
                        run_id=run_id,
                    )
                loop = AgentLoop(
                    provider,
                    registry,
                    bus,
                    permission_manager=self._permission_manager,
                    compactor=compactor,
                    compact_threshold=self._config.compaction.auto_threshold,
                    session_id=session_id_str,
                    verification_controller=verification_controller,
                    workspace_root=effective_workspace_root,
                )
                await loop.run(context)
            except asyncio.CancelledError as exc:
                cancelled = True
                if exc.args:
                    cancellation_reason = str(exc.args[0])
                if not context.is_done():
                    context.mark_failed("cancelled")
            except Exception:
                logging.getLogger(__name__).exception(
                    "agent run failed run_id=%s step=%d", run_id, context.step
                )
                if not context.is_done():
                    context.mark_failed("llm_error")

            await bus.publish(
                RunFinishedEvent(
                    run_id=run_id,
                    status=context.status,
                    reason=context.reason,
                    steps=context.step,
                    ts=_now(),
                )
            )
            if cancelled:
                await bus.publish(
                    RunCancelledEvent(
                        run_id=run_id,
                        session_id=session.id if session is not None else "",
                        reason=cancellation_reason,
                        ts=_now(),
                    )
                )

        if session is not None and store is not None:
            pending_entries = context.new_entries[context.persisted_entry_count :]
            store.append_entries(session, pending_entries, run_id=run_id)

        if cancelled:
            raise asyncio.CancelledError()

        return RunOutcome(
            status=context.status,
            result=context.result,
            reason=context.reason,
        )
