from __future__ import annotations

import asyncio
import datetime
import fnmatch
import json
import logging
import os
import signal
import time
from datetime import UTC
from pathlib import Path
from typing import Any

from pydantic import BaseModel

import kama_claude
from kama_claude.core.bus.commands import (
    AgentCancelCommand,
    AgentCancelResult,
    AgentListRunsCommand,
    AgentListRunsResult,
    AgentRunCommand,
    AgentRunResult,
    AgentSnoozeCommand,
    AgentSnoozeResult,
    EventSubscribeCommand,
    EventSubscribeResult,
    PermissionRespondCommand,
    PermissionRespondResult,
    PongResult,
    SessionBranchCommand,
    SessionBranchResult,
    SessionCloseCommand,
    SessionCloseResult,
    SessionCompactCommand,
    SessionCompactResult,
    SessionCreateCommand,
    SessionCreateResult,
    SessionGetHistoryCommand,
    SessionGetHistoryResult,
    SessionListCommand,
    SessionListResult,
    SessionResumeCommand,
    SessionResumeResult,
    SessionSendMessageCommand,
    SessionSendMessageResult,
)
from kama_claude.core.bus.envelope import EventPushEnvelope, HandlerError
from kama_claude.core.config import KamaConfig, get_config
from kama_claude.core.events.bus import EventBus
from kama_claude.core.harness import AgentHarness
from kama_claude.core.llm.base import LLMProvider
from kama_claude.core.llm.provider import AnthropicProvider
from kama_claude.core.logging_setup import setup_logging
from kama_claude.core.mcp.server import McpServerManager
from kama_claude.core.permissions.manager import PermissionManager
from kama_claude.core.permissions.storage import load_policy_file
from kama_claude.core.run_manager import RunManager
from kama_claude.core.runs import events_file, new_run_id
from kama_claude.core.session import (
    SessionManager,
    SessionStore,
    SessionStoreFaultHook,
)
from kama_claude.core.session.manager import SESSION_BUSY
from kama_claude.core.trace.record import TraceRecord
from kama_claude.core.trace.writer import TraceWriter
from kama_claude.core.transport.ipc_broadcaster import IpcEventBroadcaster
from kama_claude.core.transport.socket_server import SocketServer, get_connection_writer

logger = logging.getLogger(__name__)


# 安装跨平台退出信号处理器，优先使用 asyncio，必要时回退到标准 signal
def _install_shutdown_handlers(
    loop: asyncio.AbstractEventLoop,
    shutdown: asyncio.Event,
) -> None:
    def _request_shutdown() -> None:
        if shutdown.is_set():
            logger.warning("second shutdown signal received; forcing process exit")
            os._exit(130)
        shutdown.set()

    has_asyncio_signal_handler = False
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _request_shutdown)
            has_asyncio_signal_handler = True
        except (NotImplementedError, RuntimeError):
            # Windows ProactorEventLoop 不支持 add_signal_handler，回退到标准 signal。
            continue

    if not has_asyncio_signal_handler:
        signal.signal(
            signal.SIGINT,
            lambda _sig, _frame: loop.call_soon_threadsafe(_request_shutdown),
        )
        signal.signal(
            signal.SIGTERM,
            lambda _sig, _frame: loop.call_soon_threadsafe(_request_shutdown),
        )
        logger.info("signal handlers: using stdlib fallback")


def _now() -> str:
    return datetime.datetime.now(UTC).isoformat()


class CoreApp:
    # 初始化 CoreApp，并允许可靠性测试注入隔离存储、确定性模型和故障阶段
    def __init__(
        self,
        *,
        sessions_root: Path | None = None,
        runner_provider: LLMProvider | None = None,
        store_fault_hook: SessionStoreFaultHook | None = None,
    ) -> None:
        self._start_time = time.monotonic()
        self._bus = EventBus()
        self._broadcaster: IpcEventBroadcaster | None = None
        self._trace: TraceWriter | None = None
        self._config: KamaConfig | None = None
        self._run_manager: RunManager | None = None
        self._sessions: SessionManager | None = None
        self._permission_manager: PermissionManager | None = None
        self._mcp_manager: McpServerManager | None = None
        self._sessions_root = sessions_root
        self._runner_provider = runner_provider
        self._store_fault_hook = store_fault_hook

    # 处理 core.ping 请求，返回服务版本、运行时长和接收时间
    async def _ping_handler(self, params: dict[str, Any]) -> PongResult:
        client = params.get("client", "unknown")
        logger.debug("ping from %s", client)
        return PongResult(
            server_version=kama_claude.__version__,
            uptime_ms=int((time.monotonic() - self._start_time) * 1000),
            received_at=datetime.datetime.now(datetime.UTC).isoformat(),
        )

    # 将 EventBus 事件写入 trace（作为 EventBus 订阅者）
    async def _trace_event_handler(self, event: BaseModel) -> None:
        assert self._trace is not None
        event_dict = event.model_dump()
        self._trace.emit(
            TraceRecord(
                ts=_now(),
                direction="CORE",
                layer="event",
                kind="event",
                run_id=event_dict.get("run_id"),
                data=event_dict,
            )
        )

    # 启动一次 agent run：异步交给生产 AgentHarness 并立即返回 run_id
    async def _agent_run_handler(self, params: dict[str, Any]) -> AgentRunResult:
        assert self._sessions is not None
        cmd = AgentRunCommand.model_validate(params)
        session = await self._sessions.create(
            mode="one_shot",
            title=cmd.goal[:40],
            workspace_root=cmd.workspace_root,
        )
        run_id = new_run_id()
        assert self._run_manager is not None
        self._run_manager.create_run(
            run_id,
            self._sessions.send_message(session.id, cmd.goal, run_id=run_id),
            session_id=session.id,
            workspace_root=cmd.workspace_root,
            goal=cmd.goal,
        )
        return AgentRunResult(run_id=run_id)

    # 返回当前工作区（可选当前 Session）的所有活动 Run
    async def _agent_list_runs_handler(self, params: dict[str, Any]) -> AgentListRunsResult:
        assert self._run_manager is not None
        cmd = AgentListRunsCommand.model_validate(params)
        return AgentListRunsResult(
            runs=self._run_manager.list_runs(
                workspace_root=cmd.workspace_root,
                session_id=cmd.session_id,
            )
        )

    # 按 run_id 请求协作式取消；默认级联取消其全部子 Agent
    async def _agent_cancel_handler(self, params: dict[str, Any]) -> AgentCancelResult:
        assert self._run_manager is not None
        cmd = AgentCancelCommand.model_validate(params)
        status = self._run_manager.cancel(
            cmd.run_id,
            workspace_root=cmd.workspace_root,
            cascade=cmd.cascade,
        )
        return AgentCancelResult(run_id=cmd.run_id, status=status)

    # 延后指定慢 Run 的下一次主动提醒
    async def _agent_snooze_handler(self, params: dict[str, Any]) -> AgentSnoozeResult:
        assert self._run_manager is not None
        cmd = AgentSnoozeCommand.model_validate(params)
        return AgentSnoozeResult(
            run_id=cmd.run_id,
            ok=self._run_manager.snooze(
                cmd.run_id,
                cmd.seconds,
                workspace_root=cmd.workspace_root,
            ),
        )

    # 创建 chat 或 one_shot session，并返回 session_id
    async def _session_create_handler(self, params: dict[str, Any]) -> SessionCreateResult:
        assert self._sessions is not None
        cmd = SessionCreateCommand.model_validate(params)
        session = await self._sessions.create(
            mode=cmd.mode,
            title=cmd.title,
            workspace_root=cmd.workspace_root,
        )
        return SessionCreateResult(
            session_id=session.id,
            status=session.status,
            test_total=session.test_total,
        )

    # 列出当前绝对工作区中可恢复的会话摘要
    async def _session_list_handler(self, params: dict[str, Any]) -> SessionListResult:
        assert self._sessions is not None
        cmd = SessionListCommand.model_validate(params)
        sessions = await self._sessions.list_sessions(
            cmd.workspace_root,
            include_closed=cmd.include_closed,
        )
        return SessionListResult.model_validate({"sessions": sessions})

    # 按会话 ID 恢复当前工作区会话及其有效消息链
    async def _session_resume_handler(self, params: dict[str, Any]) -> SessionResumeResult:
        assert self._sessions is not None
        cmd = SessionResumeCommand.model_validate(params)
        session, messages = await self._sessions.resume(
            cmd.session_id,
            cmd.workspace_root,
        )
        assert session.workspace_root is not None
        return SessionResumeResult(
            session_id=session.id,
            status=session.status,
            title=session.title,
            workspace_root=session.workspace_root,
            messages=messages,
            test_total=session.test_total,
        )

    # 从指定或当前历史节点创建具有独立 Git worktree 的新分支 Session
    async def _session_branch_handler(self, params: dict[str, Any]) -> SessionBranchResult:
        assert self._sessions is not None
        cmd = SessionBranchCommand.model_validate(params)
        session, checkpoint_id = await self._sessions.branch(
            cmd.session_id,
            cmd.workspace_root,
            node_id=cmd.node_id,
            title=cmd.title,
        )
        assert session.workspace_root is not None
        assert session.forked_from_session_id is not None
        assert session.forked_from_node_id is not None
        return SessionBranchResult(
            session_id=session.id,
            status=session.status,
            title=session.title,
            workspace_root=session.workspace_root,
            source_session_id=session.forked_from_session_id,
            source_node_id=session.forked_from_node_id,
            checkpoint_id=checkpoint_id,
            messages=await self._sessions.get_history(session.id),
            test_total=session.test_total,
        )

    # 向 session 发送一条用户消息，注册为可查询、可取消的后台 Run 并立即返回 ID
    async def _session_send_handler(self, params: dict[str, Any]) -> SessionSendMessageResult:
        assert self._sessions is not None
        assert self._run_manager is not None
        cmd = SessionSendMessageCommand.model_validate(params)
        session = self._sessions.get_session(cmd.session_id)
        if self._run_manager.list_runs(session_id=cmd.session_id):
            raise HandlerError(SESSION_BUSY, "session busy")
        run_id = new_run_id()
        self._run_manager.create_run(
            run_id,
            self._sessions.send_message(cmd.session_id, cmd.content, run_id=run_id),
            session_id=cmd.session_id,
            workspace_root=session.workspace_root or Path.cwd(),
            goal=cmd.content,
        )
        return SessionSendMessageResult(run_id=run_id)

    # 返回 session 当前 Head 对应的 Anthropic messages 视图
    async def _session_history_handler(self, params: dict[str, Any]) -> SessionGetHistoryResult:
        assert self._sessions is not None
        cmd = SessionGetHistoryCommand.model_validate(params)
        messages = await self._sessions.get_history(cmd.session_id)
        return SessionGetHistoryResult(messages=messages)

    # 接收客户端权限审批响应，resolve 对应挂起的 Future
    async def _permission_respond_handler(self, params: dict[str, Any]) -> PermissionRespondResult:
        cmd = PermissionRespondCommand.model_validate(params)
        logger.info(
            "permission.respond received tool_use_id=%s decision=%s",
            cmd.tool_use_id,
            cmd.decision,
        )
        if self._permission_manager is None:
            logger.error("permission.respond: PermissionManager not initialized")
            return PermissionRespondResult()
        self._permission_manager.respond(cmd.tool_use_id, cmd.decision)
        return PermissionRespondResult()

    # 手动压缩 session thread，将摘要持久化写入 thread.jsonl
    async def _session_compact_handler(self, params: dict[str, Any]) -> SessionCompactResult:
        assert self._sessions is not None
        cmd = SessionCompactCommand.model_validate(params)
        result = await self._sessions.compact(cmd.session_id, cmd.focus)
        return result  # type: ignore[no-any-return]

    # 关闭 session 并返回 closed 状态
    async def _session_close_handler(self, params: dict[str, Any]) -> SessionCloseResult:
        assert self._sessions is not None
        cmd = SessionCloseCommand.model_validate(params)
        await self._sessions.close(cmd.session_id)
        return SessionCloseResult(status="closed")

    # 注册客户端事件订阅，可选先回放 events.jsonl 历史再接收实时流
    async def _subscribe_handler(self, params: dict[str, Any]) -> EventSubscribeResult:
        cmd = EventSubscribeCommand.model_validate(params)
        writer = get_connection_writer()

        replayed_count = 0
        if cmd.replay_from_run is not None:
            replayed_count = await self._replay_events(cmd.replay_from_run, writer, cmd.topics)

        assert self._broadcaster is not None
        sub_id = self._broadcaster.subscribe(writer, cmd.topics, cmd.scope)
        return EventSubscribeResult(subscription_id=sub_id, replayed_count=replayed_count)

    # 从 events.jsonl 向 writer 回放匹配 topic 的历史事件，返回已回放条数
    async def _replay_events(
        self,
        run_id: str,
        writer: asyncio.StreamWriter,
        topics: list[str],
    ) -> int:
        path = events_file(run_id)
        if not path.exists():
            sessions_root = self._sessions_root or Path("~/.kama/sessions").expanduser()
            for candidate in sessions_root.glob(f"*/runs/{run_id}/events.jsonl"):
                path = candidate
                break
        if not path.exists():
            return 0

        count = 0
        for line in path.read_text().splitlines():
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            event_type: str = event.get("type", "")
            if not any(fnmatch.fnmatch(event_type, p) for p in topics):
                continue
            envelope = EventPushEnvelope(event=event)
            writer.write(envelope.model_dump_json().encode() + b"\n")
            count += 1

        if count:
            await writer.drain()
        return count

    # 启动守护进程：加载配置、初始化日志、启动 trace、启动 TCP 服务器，并等待退出信号
    async def run(self) -> None:
        self._start_time = time.monotonic()
        self._config = get_config()
        setup_logging(self._config)

        if self._config.trace.enabled:
            trace_path = Path(self._config.trace.file).expanduser()
            self._trace = TraceWriter(trace_path)
            await self._trace.start()
            self._bus.subscribe(self._trace_event_handler)

        policy_file = Path("~/.kama/policy.toml").expanduser()
        self._permission_manager = PermissionManager(
            policy_file=policy_file,
            timeout_s=self._config.permission.timeout_s,
        )
        logger.info(
            "permission manager: timeout_s=%.1f  persistent=%d entries",
            self._config.permission.timeout_s,
            len(load_policy_file(policy_file)),
        )

        self._broadcaster = IpcEventBroadcaster(trace=self._trace)
        self._run_manager = RunManager(self._bus, self._config.runs)
        self._bus.subscribe(self._run_manager.observe_event)
        self._run_manager.start()
        self._bus.subscribe(self._broadcaster.handle)
        sessions_root = (
            self._sessions_root or Path("~/.kama/sessions").expanduser()
        ).resolve()
        # 后续事件回放必须复用本次启动实际配置的根目录，不能回退读取用户目录。
        self._sessions_root = sessions_root
        store = SessionStore(sessions_root, fault_hook=self._store_fault_hook)
        assert self._config is not None
        compact_provider = self._runner_provider or AnthropicProvider(
            self._config.llm.default_model
        )

        self._mcp_manager = McpServerManager()
        if self._config.mcp.servers:
            logger.info("mcp: starting %d server(s)", len(self._config.mcp.servers))
            await self._mcp_manager.start_all(self._config.mcp.servers)

        self._sessions = SessionManager(
            store,
            runner_factory=lambda: AgentHarness(
                self._config,  # type: ignore[arg-type]
                bus=self._bus,
                provider=self._runner_provider,
                trace=self._trace,
                permission_manager=self._permission_manager,
                mcp_manager=self._mcp_manager,
                run_manager=self._run_manager,
            ),
            bus=self._bus,
            provider=compact_provider,
            tool_result_limit=self._config.compaction.tool_result_limit,
            tool_result_keep=self._config.compaction.tool_result_keep,
        )
        await self._sessions.load_existing_sessions()

        server = SocketServer(
            self._config.host,
            self._config.port,
            self._broadcaster,
            trace=self._trace,
        )
        server.register("core.ping", self._ping_handler)
        server.register("agent.run", self._agent_run_handler)
        server.register("agent.list_runs", self._agent_list_runs_handler)
        server.register("agent.cancel", self._agent_cancel_handler)
        server.register("agent.snooze", self._agent_snooze_handler)
        server.register("event.subscribe", self._subscribe_handler)
        server.register("session.create", self._session_create_handler)
        server.register("session.list", self._session_list_handler)
        server.register("session.resume", self._session_resume_handler)
        server.register("session.branch", self._session_branch_handler)
        server.register("session.send_message", self._session_send_handler)
        server.register("session.get_history", self._session_history_handler)
        server.register("session.close", self._session_close_handler)
        server.register("permission.respond", self._permission_respond_handler)
        server.register("session.compact", self._session_compact_handler)

        addr = await server.start()
        logger.info("kama-core %s listening addr=%s", kama_claude.__version__, addr)
        logger.info("config: %s", self._config)

        loop = asyncio.get_running_loop()
        shutdown = asyncio.Event()
        _install_shutdown_handlers(loop, shutdown)

        await shutdown.wait()

        logger.info("shutting down")
        # Stop accepting new requests before cancelling runs; otherwise a
        # connected client can create more work while shutdown is draining.
        await server.stop()
        if self._run_manager is not None:
            await self._run_manager.stop()
        if self._mcp_manager is not None:
            try:
                await asyncio.wait_for(self._mcp_manager.stop_all(), timeout=7.0)
            except TimeoutError:
                logger.warning("shutdown: MCP cleanup timed out")
        if self._trace is not None:
            await self._trace.stop()
        logger.info("shutdown complete")


# 同步入口：启动 CoreApp 事件循环
def run() -> None:
    raw_sessions_root = os.environ.get("KAMA_SESSIONS_ROOT")
    sessions_root = Path(raw_sessions_root).expanduser() if raw_sessions_root else None
    asyncio.run(CoreApp(sessions_root=sessions_root).run())
