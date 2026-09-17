from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from kama_claude.core.bus.envelope import HandlerError
from kama_claude.core.bus.events import (
    SessionClosedEvent,
    SessionCreatedEvent,
    SessionMessageReceivedEvent,
    SessionResumedEvent,
    SessionWaitingForInputEvent,
    SkillInvokedEvent,
)
from kama_claude.core.compact.budget import TOOL_RESULT_KEEP, TOOL_RESULT_LIMIT
from kama_claude.core.events.bus import EventBus
from kama_claude.core.git.checkpoint import Checkpoint, CheckpointError, CheckpointManager
from kama_claude.core.git.process import discover_repository
from kama_claude.core.git.worktree import create_checkpoint_worktree
from kama_claude.core.runs import new_run_id
from kama_claude.core.session.model import Session, SessionMode
from kama_claude.core.session.store import SessionIntegrityError, SessionStore
from kama_claude.core.skills.loader import SkillLoader
from kama_claude.core.verification.controller import may_change_code_state
from kama_claude.core.verification.test_index import ProjectTestIndexManager
from kama_claude.core.workspace import WorkspaceBoundary

if TYPE_CHECKING:
    from kama_claude.core.llm.base import LLMProvider
    from kama_claude.core.runner import AgentRunner

SESSION_NOT_FOUND = -32010
SESSION_CLOSED = -32011
SESSION_BUSY = -32012
SESSION_WORKSPACE_MISMATCH = -32013
SESSION_BRANCH_UNAVAILABLE = -32014
_DEFAULT_TITLE_CHARS = 30

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SessionRecoveryReport:
    loaded: tuple[str, ...] = ()
    interrupted: tuple[str, ...] = ()
    errors: dict[str, str] = field(default_factory=dict)


# 返回当前 UTC 时间的 ISO 8601 字符串
def _now() -> str:
    return datetime.now(UTC).isoformat()


# 将首条用户消息规范化为适合列表展示的简短 Session 介绍
def _make_session_title(content: str) -> str:
    normalized = " ".join(content.split())
    if len(normalized) <= _DEFAULT_TITLE_CHARS:
        return normalized
    return normalized[:_DEFAULT_TITLE_CHARS] + "…"


class SessionManager:
    # 初始化会话管理器，接入存储、runner、事件总线、压缩 provider 和 tool 结果预算
    def __init__(
        self,
        store: SessionStore,
        runner_factory: Callable[[], AgentRunner],
        bus: EventBus,
        provider: LLMProvider | None = None,
        *,
        tool_result_limit: int = TOOL_RESULT_LIMIT,
        tool_result_keep: int = TOOL_RESULT_KEEP,
    ) -> None:
        self._store = store
        self._runner_factory = runner_factory
        self._bus = bus
        self._provider = provider
        self._tool_result_limit = tool_result_limit
        self._tool_result_keep = tool_result_keep
        self._sessions: dict[str, Session] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._skill_loader = SkillLoader()

    # 返回已加载 Session，供 Core 将其工作区与新 Run 绑定
    def get_session(self, sid: str) -> Session:
        return self._get_session(sid)

    # 从磁盘恢复合法 session、重建锁并隔离损坏会话
    async def load_existing_sessions(self) -> SessionRecoveryReport:
        loaded: list[str] = []
        interrupted: list[str] = []
        errors: dict[str, str] = {}
        for sid in self._store.list_session_ids():
            try:
                session = self._store.read_meta(sid)
                if session.id != sid:
                    raise ValueError(f"session id mismatch: directory={sid!r}, meta={session.id!r}")
                migrated = self._store.validate_for_recovery(session)
                if session.status == "active":
                    tail_recovered = self._store.recover_interrupted_tail(session)
                    if tail_recovered:
                        self._store.write_meta(session)
                        migrated = True
                    self._repair_interrupted_tool_calls(session)
                if not session.title:
                    inferred_title = self._infer_title(session.id)
                    if inferred_title:
                        session.title = inferred_title
                        migrated = True
                if session.status == "active":
                    session.status = "interrupted"
                    interrupted.append(sid)
                    migrated = True
                if migrated:
                    self._store.write_meta(session)
                self._sessions[sid] = session
                self._locks[sid] = asyncio.Lock()
                loaded.append(sid)
            except (
                OSError,
                UnicodeError,
                KeyError,
                TypeError,
                ValueError,
                json.JSONDecodeError,
                SessionIntegrityError,
            ) as exc:
                errors[sid] = str(exc)
                logger.error("session recovery skipped sid=%s error=%s", sid, exc)
        report = SessionRecoveryReport(
            loaded=tuple(loaded),
            interrupted=tuple(interrupted),
            errors=errors,
        )
        logger.info(
            "session recovery loaded=%d interrupted=%d skipped=%d",
            len(report.loaded),
            len(report.interrupted),
            len(report.errors),
        )
        return report

    # 创建新 session 并写入 meta.json
    async def create(
        self,
        mode: SessionMode,
        title: str = "",
        workspace_root: str | Path | None = None,
    ) -> Session:
        sid = f"sess-{uuid.uuid4().hex[:12]}"
        ts = _now()
        workspace = WorkspaceBoundary.from_path(workspace_root or Path.cwd())
        session = Session(
            id=sid,
            mode=mode,
            status="active",
            title=title,
            created_at=ts,
            updated_at=ts,
            workspace_root=workspace.as_posix(),
            discovery_root=workspace.as_posix(),
            run_ids=[],
        )
        await self._prepare_test_index(session)
        self._sessions[sid] = session
        self._locks[sid] = asyncio.Lock()
        self._store.write_meta(session)
        await self._bus.publish(SessionCreatedEvent(session_id=sid, mode=mode, ts=ts))
        return session

    # Session 创建时尽力预热测试结构表；索引失败不能阻塞普通对话
    async def _prepare_test_index(self, session: Session) -> None:
        if session.workspace_root is None:
            return
        workspace = Path(session.workspace_root).resolve()
        repository, _result = await discover_repository(workspace)
        if repository is None or repository.resolve() != workspace:
            return
        try:
            summary = await ProjectTestIndexManager(workspace, repository).initialize()
        except (CheckpointError, OSError, ValueError, RuntimeError) as exc:
            logger.debug("test index warmup skipped workspace=%s error=%s", workspace, exc)
            return
        session.test_total = summary.total_tests
        session.test_index_tree = summary.tree_id

    # 列出属于规范化绝对工作区的可恢复 Session 摘要
    async def list_sessions(
        self, workspace_root: str | Path, *, include_closed: bool = False
    ) -> list[dict[str, Any]]:
        workspace = WorkspaceBoundary.from_path(workspace_root).as_posix()
        sessions = [
            session
            for session in self._sessions.values()
            if session.mode == "chat"
            and (session.discovery_root or session.workspace_root) == workspace
            and (include_closed or session.status != "closed")
        ]
        sessions.sort(key=lambda session: session.updated_at, reverse=True)
        return [
            {
                "session_id": session.id,
                "title": session.title,
                "status": session.status,
                "updated_at": session.updated_at,
                "last_run_id": session.run_ids[-1] if session.run_ids else None,
                "workspace_root": session.workspace_root,
                "is_branch": session.forked_from_session_id is not None,
            }
            for session in sessions
        ]

    # 按 ID 恢复同一工作区 Session，并为旧会话首次绑定工作区
    async def resume(
        self, sid: str, workspace_root: str | Path
    ) -> tuple[Session, list[dict[str, Any]]]:
        session = self._get_session(sid)
        workspace = WorkspaceBoundary.from_path(workspace_root).as_posix()
        if session.mode == "one_shot":
            raise HandlerError(SESSION_CLOSED, "one-shot sessions cannot be resumed")
        if session.workspace_root is None:
            session.workspace_root = workspace
            self._store.write_meta(session)
        elif workspace not in {
            session.workspace_root,
            session.discovery_root or session.workspace_root,
        }:
            raise HandlerError(
                SESSION_WORKSPACE_MISMATCH,
                "session belongs to a different workspace",
            )
        if session.status in ("interrupted", "closed"):
            session.status = "waiting_for_input"
            self._store.write_meta(session)
        # 恢复时也刷新一次索引，覆盖用户离线修改后的测试结构变化
        await self._prepare_test_index(session)
        self._store.write_meta(session)
        await self._bus.publish(SessionResumedEvent(session_id=sid, ts=_now()))
        return session, self._store.read_messages(sid)

    # 从任意历史节点创建独立 Session 和 Git worktree，并保留来源链而不复制消息
    async def branch(
        self,
        sid: str,
        workspace_root: str | Path,
        *,
        node_id: str | None = None,
        title: str = "",
    ) -> tuple[Session, str]:
        source = self._get_session(sid)
        if self._locks[sid].locked():
            raise HandlerError(SESSION_BUSY, "session busy")
        requested = WorkspaceBoundary.from_path(workspace_root).as_posix()
        if requested not in {
            source.workspace_root,
            source.discovery_root or source.workspace_root,
        }:
            raise HandlerError(
                SESSION_WORKSPACE_MISMATCH,
                "session belongs to a different workspace",
            )
        target_node_id = node_id or source.current_id
        if target_node_id is None or source.workspace_root is None:
            raise HandlerError(
                SESSION_BRANCH_UNAVAILABLE,
                "session has no branchable message node",
            )
        try:
            chain = self._store.read_nodes_to(
                source.id,
                target_node_id,
                stop_at_compact=False,
            )
        except SessionIntegrityError as exc:
            raise HandlerError(SESSION_BRANCH_UNAVAILABLE, str(exc)) from exc
        repository, result = await discover_repository(Path(source.workspace_root))
        if repository is None:
            message = result.stderr.decode("utf-8", errors="replace").strip()
            raise HandlerError(
                SESSION_BRANCH_UNAVAILABLE,
                message or "session workspace is not a Git repository",
            )
        manager = CheckpointManager(repository)
        try:
            checkpoint = await self._resolve_branch_checkpoint(
                source,
                target_node_id,
                chain,
                manager,
            )
            child_id = f"sess-{uuid.uuid4().hex[:12]}"
            worktree = self._store.session_dir(child_id) / "workspace"
            await create_checkpoint_worktree(repository, worktree, checkpoint)
        except (CheckpointError, OSError) as exc:
            raise HandlerError(SESSION_BRANCH_UNAVAILABLE, str(exc)) from exc

        ts = _now()
        child = Session(
            id=child_id,
            mode="chat",
            status="waiting_for_input",
            title=title or f"{source.title or 'session'} (branch)",
            created_at=ts,
            updated_at=ts,
            workspace_root=worktree.resolve().as_posix(),
            discovery_root=source.discovery_root or source.workspace_root,
            forked_from_session_id=source.id,
            forked_from_node_id=target_node_id,
            fork_checkpoint_id=checkpoint.id,
        )
        await self._prepare_test_index(child)
        self._sessions[child.id] = child
        self._locks[child.id] = asyncio.Lock()
        self._store.write_meta(child)
        await self._bus.publish(SessionCreatedEvent(session_id=child.id, mode="chat", ts=ts))
        return child, checkpoint.id

    # 优先使用目标节点精确快照；当前 Head 可即时捕获，历史节点必须通过写入间隙校验
    async def _resolve_branch_checkpoint(
        self,
        session: Session,
        target_node_id: str,
        chain: list[dict[str, Any]],
        manager: CheckpointManager,
    ) -> Checkpoint:
        if target_node_id == session.current_id:
            return await manager.create(
                label="session branch source",
                kind="mutation",
                session_id=session.id,
                node_id=target_node_id,
            )
        checkpoints = await manager.list_for_session(session.id)
        by_node = {item.node_id: item for item in checkpoints if item.node_id is not None}
        checkpoint: Checkpoint | None = None
        checkpoint_index = -1
        for index in range(len(chain) - 1, -1, -1):
            candidate = by_node.get(str(chain[index]["id"]))
            if candidate is not None:
                checkpoint = candidate
                checkpoint_index = index
                break
        if checkpoint is None:
            raise CheckpointError("no checkpoint reaches this historical node; choose a newer node")
        if _contains_successful_state_change(chain[checkpoint_index + 1 :]):
            raise CheckpointError(
                "the nearest checkpoint predates a successful write; exact historical "
                "code state is unavailable"
            )
        return checkpoint

    # 处理用户消息，追加 thread 并启动一次 agent run
    async def send_message(self, sid: str, content: str, *, run_id: str | None = None) -> str:
        session = self._get_session(sid)
        lock = self._locks[sid]
        if lock.locked():
            raise HandlerError(SESSION_BUSY, "session busy")

        async with lock:
            if session.status == "closed":
                raise HandlerError(SESSION_CLOSED, "session already closed")

            if session.status in ("waiting_for_input", "interrupted"):
                await self._bus.publish(SessionResumedEvent(session_id=sid, ts=_now()))

            run_id = run_id or new_run_id()
            session.run_ids.append(run_id)
            session.status = "active"
            session.updated_at = _now()
            self._store.write_meta(session)
            self._store.append_message(session, "user", content, run_id=run_id)
            await self._bus.publish(
                SessionMessageReceivedEvent(session_id=sid, content=content, ts=_now())
            )

            if not session.title:
                session.title = _make_session_title(content)

            session.updated_at = _now()
            self._store.write_meta(session)

            # Skill 解析：检测 "/" 前缀，展开为系统提示覆盖和工具白名单
            goal = content
            system_prompt_override: str | None = None
            tool_whitelist: list[str] | None = None
            if content.startswith("/"):
                parts = content[1:].split(None, 1)
                skill_name = parts[0]
                arguments = parts[1] if len(parts) > 1 else ""
                skill = self._skill_loader.resolve(skill_name)
                if skill is not None:
                    goal = self._skill_loader.render_prompt(skill, arguments)
                    system_prompt_override = skill.system_prompt_template
                    tool_whitelist = skill.allowed_tools or None
                    await self._bus.publish(
                        SkillInvokedEvent(
                            skill_name=skill_name,
                            arguments=arguments,
                            run_id=run_id,
                            ts=_now(),
                        )
                    )

            runner = self._runner_factory()
            try:
                await runner.run_and_capture(
                    goal,
                    run_id=run_id,
                    session=session,
                    store=self._store,
                    system_prompt_override=system_prompt_override,
                    tool_whitelist=tool_whitelist,
                )
            except asyncio.CancelledError:
                session.status = "interrupted"
                session.updated_at = _now()
                self._store.write_meta(session)
                raise

            session.updated_at = _now()
            if session.mode == "one_shot":
                session.status = "closed"
                await self._bus.publish(SessionClosedEvent(session_id=sid, ts=session.updated_at))
            else:
                session.status = "waiting_for_input"
                await self._bus.publish(
                    SessionWaitingForInputEvent(
                        session_id=sid,
                        last_run_id=run_id,
                        ts=session.updated_at,
                    )
                )
            self._store.write_meta(session)
            return run_id

    # 关闭指定 session 并更新 meta.json
    async def close(self, sid: str) -> None:
        session = self._get_session(sid)
        lock = self._locks[sid]
        if lock.locked():
            raise HandlerError(SESSION_BUSY, "session busy")
        async with lock:
            session.status = "closed"
            session.updated_at = _now()
            self._store.write_meta(session)
            await self._bus.publish(SessionClosedEvent(session_id=sid, ts=session.updated_at))

    # 手动压缩当前消息链，并向 thread.jsonl 追加一个 compact 节点
    async def compact(self, sid: str, focus: str = "") -> Any:
        session = self._get_session(sid)
        lock = self._locks[sid]
        if lock.locked():
            raise HandlerError(SESSION_BUSY, "session busy")
        if self._provider is None:
            raise HandlerError(-32020, "provider not available for compaction")
        async with lock:
            from kama_claude.core.bus.commands import SessionCompactResult
            from kama_claude.core.compact.compactor import Compactor

            messages = self._store.read_messages(sid)
            compactor = Compactor(
                self._bus,
                sid,
                tool_result_limit=self._tool_result_limit,
                tool_result_keep=self._tool_result_keep,
            )
            result = await compactor.compact_messages(messages, self._provider, focus=focus)
            if result is None:
                raise HandlerError(-32021, "compaction failed or not beneficial")
            self._store.append_compact(
                session,
                summary=result.summary_text,
                original_tokens=result.original_token_estimate,
                summary_tokens=result.summary_tokens,
            )
            return SessionCompactResult(
                summary_tokens=result.summary_tokens,
                saved_tokens=max(0, result.original_token_estimate - result.summary_tokens),
            )

    # 从 current_id 回溯并读取指定 session 的当前有效消息链
    async def get_history(self, sid: str) -> list[dict[str, Any]]:
        self._get_session(sid)
        return self._store.read_messages(sid)

    # 从物理消息日志的首条用户文本回填旧 Session 的默认介绍
    def _infer_title(self, sid: str) -> str:
        for node in self._store.read_all_nodes(sid):
            if node.get("kind") != "message" or node.get("role") != "user":
                continue
            content = node.get("content")
            if isinstance(content, str) and content.strip():
                return _make_session_title(content)
            if isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict) or block.get("type") != "text":
                        continue
                    text = block.get("text")
                    if isinstance(text, str) and text.strip():
                        return _make_session_title(text)
        return ""

    # 为中断 Run 中已经持久化但尚无结果的 tool_use 追加合成错误结果
    def _repair_interrupted_tool_calls(self, session: Session) -> int:
        messages = self._store.read_messages(session.id, trim_orphans=False)
        pending: list[str] = []
        for message in messages:
            content = message.get("content")
            if not isinstance(content, list):
                continue
            if message.get("role") == "assistant":
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_use":
                        tool_use_id = block.get("id")
                        if isinstance(tool_use_id, str) and tool_use_id:
                            pending.append(tool_use_id)
            elif message.get("role") == "user":
                completed = {
                    str(block.get("tool_use_id"))
                    for block in content
                    if isinstance(block, dict) and block.get("type") == "tool_result"
                }
                pending = [tool_use_id for tool_use_id in pending if tool_use_id not in completed]
        if not pending:
            return 0
        blocks = [
            {
                "type": "tool_result",
                "tool_use_id": tool_use_id,
                "content": "Error: the previous run was interrupted before this tool completed.",
                "is_error": True,
            }
            for tool_use_id in pending
        ]
        run_id = session.run_ids[-1] if session.run_ids else None
        self._store.append_message(session, "user", blocks, run_id=run_id)
        return len(blocks)

    # 从内存索引取 session，不存在时抛 JSON-RPC 结构化错误
    def _get_session(self, sid: str) -> Session:
        session = self._sessions.get(sid)
        if session is None:
            raise HandlerError(SESSION_NOT_FOUND, "session not found")
        return session


# 检查节点片段中是否包含没有后续 checkpoint 覆盖的成功潜在写操作
def _contains_successful_state_change(nodes: list[dict[str, Any]]) -> bool:
    tool_names: dict[str, tuple[str, dict[str, object]]] = {}
    for node in nodes:
        content = node.get("content")
        if not isinstance(content, list):
            continue
        if node.get("role") == "assistant":
            for block in content:
                if not isinstance(block, dict) or block.get("type") != "tool_use":
                    continue
                tool_id = str(block.get("id", ""))
                name = str(block.get("name", ""))
                raw_input = block.get("input")
                params = dict(raw_input) if isinstance(raw_input, dict) else {}
                tool_names[tool_id] = (name, params)
            continue
        if node.get("role") != "user":
            continue
        for block in content:
            if (
                not isinstance(block, dict)
                or block.get("type") != "tool_result"
                or block.get("is_error") is True
            ):
                continue
            tool = tool_names.get(str(block.get("tool_use_id", "")))
            if tool is not None and may_change_code_state(tool[0], tool[1]):
                return True
    return False
