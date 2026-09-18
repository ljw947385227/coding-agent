from __future__ import annotations

import json
import logging
import os
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from kama_claude.core.session.model import Session

logger = logging.getLogger(__name__)

MessageContent = str | list[dict[str, Any]]
SessionStoreFaultHook = Callable[[str, dict[str, Any]], None]
_COMPACT_ACK = "Understood, I'll continue from this summary."


class SessionIntegrityError(RuntimeError):
    pass


# 返回当前 UTC 时间的 ISO 8601 字符串
def _now() -> str:
    return datetime.now(UTC).isoformat()


# 生成带类型前缀的全局唯一 thread 节点 ID
def _new_node_id(kind: str) -> str:
    prefix = "cmp" if kind == "compact" else "msg"
    return f"{prefix}-{uuid.uuid4().hex}"


# 判断消息内容是否是非空且仅含 tool_result 的 block 列表
def _is_tool_result_content(content: object) -> bool:
    return (
        isinstance(content, list)
        and bool(content)
        and all(isinstance(block, dict) and block.get("type") == "tool_result" for block in content)
    )


class SessionStore:
    # 初始化 session 文件存储根目录
    def __init__(
        self,
        root: Path,
        *,
        fault_hook: SessionStoreFaultHook | None = None,
    ) -> None:
        self._root = root.expanduser()
        self._fault_hook = fault_hook
        self._root.mkdir(parents=True, exist_ok=True)

    # 返回指定 session 的目录路径
    def session_dir(self, sid: str) -> Path:
        return self._root / sid

    # 按目录名稳定列出具有 meta.json 的持久化 session
    def list_session_ids(self) -> list[str]:
        return sorted(
            entry.name
            for entry in self._root.iterdir()
            if entry.is_dir() and not entry.is_symlink() and (entry / "meta.json").is_file()
        )

    # 返回指定 session 下的 runs 目录路径
    def runs_dir(self, sid: str) -> Path:
        return self.session_dir(sid) / "runs"

    # 返回指定 session 的追加式 thread 文件路径
    def thread_file(self, sid: str) -> Path:
        return self.session_dir(sid) / "thread.jsonl"

    # 原子写入 session meta，避免 current_id 指向半写入状态
    def write_meta(self, session: Session) -> None:
        self._write_meta(session)

    # 写入 meta，并在节点事务内暴露临时文件和原子替换故障阶段
    def _write_meta(
        self,
        session: Session,
        fault_node: dict[str, Any] | None = None,
    ) -> None:
        directory = self.session_dir(session.id)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "meta.json"
        temporary = directory / "meta.json.tmp"
        temporary.write_text(
            json.dumps(session.to_dict(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        if fault_node is not None:
            self._emit_fault("after_meta_temp_write", fault_node)
            self._emit_fault("before_meta_replace", fault_node)
        temporary.replace(path)
        if fault_node is not None:
            self._emit_fault("after_meta_replace", fault_node)

    # 调用可选故障注入回调，生产环境未配置时保持零行为
    def _emit_fault(self, phase: str, node: dict[str, Any]) -> None:
        if self._fault_hook is not None:
            self._fault_hook(phase, node)

    # 从 meta.json 读取 session meta
    def read_meta(self, sid: str) -> Session:
        data = json.loads((self.session_dir(sid) / "meta.json").read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("session meta must be a JSON object")
        return Session.from_dict(data)

    # 严格校验恢复所需的节点 ID、父链和当前 Head，并迁移旧会话 Head
    def validate_for_recovery(self, session: Session) -> bool:
        nodes = self._read_nodes_strict(session.id)
        by_id: dict[str, dict[str, Any]] = {}
        for node in nodes:
            node_id = str(node["id"])
            if node_id in by_id:
                raise SessionIntegrityError(f"duplicate thread node id: {node_id}")
            by_id[node_id] = node

        for node_id, node in by_id.items():
            parent = node.get("parent_id")
            if parent is not None and str(parent) not in by_id:
                raise SessionIntegrityError(f"thread node {node_id} has missing parent {parent}")
        self._validate_parent_cycles(by_id)

        migrated = False
        if session.current_id is None and nodes:
            session.current_id = str(nodes[-1]["id"])
            migrated = True
        if session.current_id is not None and session.current_id not in by_id:
            raise SessionIntegrityError(f"thread current_id does not exist: {session.current_id}")
        if session.schema_version < 3:
            session.schema_version = 3
            migrated = True
        return migrated

    # 在 active Run 崩溃于 JSONL 刷盘和 meta 推进之间时恢复唯一尾节点
    def recover_interrupted_tail(self, session: Session) -> bool:
        if session.current_id is None or not session.run_ids:
            return False
        nodes = self._read_nodes_strict(session.id)
        last_run_id = session.run_ids[-1]
        candidates = [
            node
            for node in nodes
            if node.get("parent_id") == session.current_id and node.get("run_id") == last_run_id
        ]
        if len(candidates) != 1:
            return False
        session.current_id = str(candidates[0]["id"])
        return True

    # 严格读取 thread JSONL，在启动恢复时拒绝破损或非法节点
    def _read_nodes_strict(self, sid: str) -> list[dict[str, Any]]:
        path = self.thread_file(sid)
        if not path.exists():
            return []
        nodes: list[dict[str, Any]] = []
        previous_id: str | None = None
        text = path.read_text(encoding="utf-8")
        for line_no, line in enumerate(text.splitlines(), start=1):
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise SessionIntegrityError(
                    f"invalid thread JSON at line {line_no}: {exc.msg}"
                ) from exc
            if not isinstance(row, dict):
                raise SessionIntegrityError(f"thread row {line_no} must be a JSON object")
            node = dict(row)
            node_id = str(node.get("id") or f"legacy-{line_no:012d}")
            node["id"] = node_id
            node.setdefault("parent_id", previous_id)
            node.setdefault("kind", "message")
            self._validate_node_shape(node, line_no)
            nodes.append(node)
            previous_id = node_id
        return nodes

    # 校验单个 thread 节点的最小持久化结构
    def _validate_node_shape(self, node: dict[str, Any], line_no: int) -> None:
        kind = node.get("kind")
        if kind == "message":
            if node.get("role") not in ("user", "assistant"):
                raise SessionIntegrityError(f"message node at line {line_no} has invalid role")
            if "content" not in node:
                raise SessionIntegrityError(f"message node at line {line_no} has no content")
            return
        if kind == "compact":
            if not isinstance(node.get("summary"), str):
                raise SessionIntegrityError(f"compact node at line {line_no} has invalid summary")
            return
        raise SessionIntegrityError(f"thread node at line {line_no} has invalid kind")

    # 检查全部节点父链是否存在循环引用
    def _validate_parent_cycles(self, by_id: dict[str, dict[str, Any]]) -> None:
        complete: set[str] = set()
        for start_id in by_id:
            path: set[str] = set()
            node_id: str | None = start_id
            while node_id is not None and node_id not in complete:
                if node_id in path:
                    raise SessionIntegrityError(f"thread parent cycle detected at node {node_id}")
                path.add(node_id)
                parent = by_id[node_id].get("parent_id")
                node_id = str(parent) if parent is not None else None
            complete.update(path)

    # 读取全部 thread 节点，并为旧格式行提供稳定的虚拟链兼容
    def read_all_nodes(self, sid: str) -> list[dict[str, Any]]:
        path = self.thread_file(sid)
        if not path.exists():
            return []

        nodes: list[dict[str, Any]] = []
        previous_id: str | None = None
        for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                logger.warning("skip broken thread row sid=%s line=%s", sid, line_no)
                continue
            if not isinstance(row, dict):
                logger.warning("skip non-object thread row sid=%s line=%s", sid, line_no)
                continue

            node = dict(row)
            node_id = str(node.get("id") or f"legacy-{line_no:012d}")
            node["id"] = node_id
            node.setdefault("parent_id", previous_id)
            node.setdefault("kind", "message")
            nodes.append(node)
            previous_id = node_id
        return nodes

    # 在旧 session 尚无 current_id 时，从最后一个已落盘节点推断当前 Head
    def _ensure_current_id(self, session: Session) -> None:
        if session.current_id is not None:
            return
        nodes = self.read_all_nodes(session.id)
        if nodes:
            session.current_id = str(nodes[-1]["id"])

    # 先追加并刷盘一个节点，再原子推进 session current_id
    def _append_node(self, session: Session, node: dict[str, Any]) -> str:
        self._ensure_current_id(session)
        node_id = _new_node_id(str(node["kind"]))
        ts = _now()
        row = {
            "id": node_id,
            "parent_id": session.current_id,
            "kind": node["kind"],
            "ts": ts,
            **{key: value for key, value in node.items() if key != "kind"},
        }

        directory = self.session_dir(session.id)
        directory.mkdir(parents=True, exist_ok=True)
        self._emit_fault("before_node_write", row)
        with self.thread_file(session.id).open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
            self._emit_fault("after_node_write", row)
            handle.flush()
            self._emit_fault("after_node_flush", row)
            os.fsync(handle.fileno())
            self._emit_fault("after_node_fsync", row)

        session.current_id = node_id
        session.schema_version = 3
        session.updated_at = ts
        self._write_meta(session, row)
        return node_id

    # 追加一条带 ID 和 parent_id 的 Anthropic API 消息节点
    def append_message(
        self,
        session: Session,
        role: str,
        content: MessageContent,
        run_id: str | None = None,
    ) -> str:
        node: dict[str, Any] = {
            "kind": "message",
            "role": role,
            "content": content,
        }
        if run_id is not None:
            node["run_id"] = run_id
        return self._append_node(session, node)

    # 批量按生成顺序追加普通消息和 compact 节点
    def append_entries(
        self,
        session: Session,
        entries: list[dict[str, Any]],
        run_id: str,
    ) -> list[str]:
        return [self.append_entry(session, entry, run_id) for entry in entries]

    # 立即追加一个普通消息或 compact 逻辑节点并返回节点 ID
    def append_entry(
        self,
        session: Session,
        entry: dict[str, Any],
        run_id: str,
    ) -> str:
        kind = entry.get("kind")
        if kind == "message":
            message = entry["message"]
            return self.append_message(
                session,
                role=str(message["role"]),
                content=message["content"],
                run_id=run_id,
            )
        if kind == "compact":
            return self.append_compact(
                session,
                summary=str(entry["summary"]),
                original_tokens=int(entry["original_tokens"]),
                summary_tokens=int(entry["summary_tokens"]),
                run_id=run_id,
            )
        raise ValueError(f"unknown thread entry kind: {kind!r}")

    # 追加一个累积摘要 compact 节点并推进 current_id
    def append_compact(
        self,
        session: Session,
        summary: str,
        original_tokens: int,
        summary_tokens: int,
        run_id: str | None = None,
    ) -> str:
        self._ensure_current_id(session)
        node: dict[str, Any] = {
            "kind": "compact",
            "summary": summary,
            "covers_through": session.current_id,
            "original_tokens": original_tokens,
            "summary_tokens": summary_tokens,
        }
        if run_id is not None:
            node["run_id"] = run_id
        return self._append_node(session, node)

    # 从 current_id 沿 parent_id 回溯到最近 compact 节点或链根
    def read_active_nodes(self, sid: str) -> list[dict[str, Any]]:
        nodes = self.read_all_nodes(sid)
        if not nodes:
            return []

        by_id = {str(node["id"]): node for node in nodes}
        meta_path = self.session_dir(sid) / "meta.json"
        current_id = self.read_meta(sid).current_id if meta_path.exists() else None
        if current_id is None:
            current_id = str(nodes[-1]["id"])
        if current_id not in by_id:
            logger.warning("thread current_id missing sid=%s current_id=%s", sid, current_id)
            return []

        reversed_chain: list[dict[str, Any]] = []
        visited: set[str] = set()
        node_id: str | None = current_id
        while node_id is not None:
            if node_id in visited:
                logger.warning("thread parent cycle sid=%s node_id=%s", sid, node_id)
                return []
            visited.add(node_id)

            node = by_id.get(node_id)
            if node is None:
                logger.warning("thread parent missing sid=%s node_id=%s", sid, node_id)
                return []
            reversed_chain.append(node)
            if node.get("kind") == "compact":
                break
            parent_id = node.get("parent_id")
            node_id = str(parent_id) if parent_id is not None else None

        reversed_chain.reverse()
        return reversed_chain

    # 从指定节点沿 parent_id 回溯到最近 compact 或链根，供历史分支选择使用
    def read_nodes_to(
        self,
        sid: str,
        node_id: str,
        *,
        stop_at_compact: bool = True,
    ) -> list[dict[str, Any]]:
        nodes = self.read_all_nodes(sid)
        by_id = {str(node["id"]): node for node in nodes}
        if node_id not in by_id:
            raise SessionIntegrityError(f"thread node does not exist: {node_id}")
        chain: list[dict[str, Any]] = []
        visited: set[str] = set()
        current: str | None = node_id
        while current is not None:
            if current in visited:
                raise SessionIntegrityError(f"thread parent cycle detected at node {current}")
            visited.add(current)
            node = by_id.get(current)
            if node is None:
                raise SessionIntegrityError(f"thread node has missing parent: {current}")
            chain.append(node)
            if stop_at_compact and node.get("kind") == "compact":
                break
            parent = node.get("parent_id")
            current = str(parent) if parent is not None else None
        chain.reverse()
        return chain

    # 恢复当前 Head 的模型消息视图，compact 节点在内存中投影为摘要消息对
    def read_messages(
        self,
        sid: str,
        *,
        trim_orphans: bool = True,
    ) -> list[dict[str, Any]]:
        return self._read_messages_recursive(sid, trim_orphans=trim_orphans, visited=set())

    # 递归拼接分支来源和本地追加链，并让本地 compact 截断继承历史
    def _read_messages_recursive(
        self,
        sid: str,
        *,
        trim_orphans: bool,
        visited: set[str],
    ) -> list[dict[str, Any]]:
        if sid in visited:
            raise SessionIntegrityError(f"session fork cycle detected at {sid}")
        visited.add(sid)
        session = self.read_meta(sid)
        local_nodes = self.read_active_nodes(sid)
        messages: list[dict[str, Any]] = []
        has_local_compact = any(node.get("kind") == "compact" for node in local_nodes)
        if (
            not has_local_compact
            and session.forked_from_session_id is not None
            and session.forked_from_node_id is not None
        ):
            messages.extend(
                self._read_messages_at(
                    session.forked_from_session_id,
                    session.forked_from_node_id,
                    visited=visited,
                )
            )
        messages.extend(self._nodes_to_messages(sid, local_nodes))
        visited.remove(sid)
        return self._trim_orphan_tool_use(messages) if trim_orphans else messages

    # 将来源 Session 的指定节点链投影为模型消息，支持多级分支继承
    def _read_messages_at(
        self,
        sid: str,
        node_id: str,
        *,
        visited: set[str],
    ) -> list[dict[str, Any]]:
        if sid in visited:
            raise SessionIntegrityError(f"session fork cycle detected at {sid}")
        visited.add(sid)
        session = self.read_meta(sid)
        local_nodes = self.read_nodes_to(sid, node_id)
        messages: list[dict[str, Any]] = []
        has_local_compact = any(node.get("kind") == "compact" for node in local_nodes)
        if (
            not has_local_compact
            and session.forked_from_session_id is not None
            and session.forked_from_node_id is not None
        ):
            messages.extend(
                self._read_messages_at(
                    session.forked_from_session_id,
                    session.forked_from_node_id,
                    visited=visited,
                )
            )
        messages.extend(self._nodes_to_messages(sid, local_nodes))
        visited.remove(sid)
        return messages

    # 把普通消息和 compact 节点转换为 Anthropic messages 视图
    def _nodes_to_messages(
        self,
        sid: str,
        nodes: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = []
        for node in nodes:
            kind = node.get("kind")
            if kind == "compact":
                messages.extend(
                    [
                        {"role": "user", "content": str(node.get("summary", ""))},
                        {"role": "assistant", "content": _COMPACT_ACK},
                    ]
                )
                continue
            role = node.get("role")
            if role not in ("user", "assistant"):
                logger.warning(
                    "skip unknown thread role sid=%s node_id=%s role=%s",
                    sid,
                    node.get("id"),
                    role,
                )
                continue
            message = {"role": role, "content": node.get("content", "")}
            if self._merge_adjacent_tool_results(messages, message):
                continue
            messages.append(message)
        return messages

    # 将逐工具持久化的相邻 tool_result 节点合并为模型要求的一条 user 消息
    def _merge_adjacent_tool_results(
        self,
        messages: list[dict[str, Any]],
        message: dict[str, Any],
    ) -> bool:
        if not messages or message.get("role") != "user":
            return False
        current = message.get("content")
        previous = messages[-1]
        previous_content = previous.get("content")
        if (
            previous.get("role") != "user"
            or not _is_tool_result_content(previous_content)
            or not _is_tool_result_content(current)
        ):
            return False
        assert isinstance(previous_content, list)
        assert isinstance(current, list)
        previous_content.extend(current)
        return True

    # 裁掉尾部未配对 tool_use 以及其后的消息，避免 Anthropic messages.invalid
    def _trim_orphan_tool_use(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        pending: set[str] = set()
        last_balanced = 0
        for idx, msg in enumerate(messages, start=1):
            content = msg.get("content")
            if isinstance(content, list):
                if msg.get("role") == "assistant":
                    for block in content:
                        if block.get("type") == "tool_use":
                            pending.add(str(block.get("id", "")))
                elif msg.get("role") == "user":
                    for block in content:
                        if block.get("type") == "tool_result":
                            pending.discard(str(block.get("tool_use_id", "")))
            if not pending:
                last_balanced = idx
        if pending:
            logger.warning("trim orphan tool_use blocks from thread")
            return messages[:last_balanced]
        return messages

    # 读取 notes.md 全文，文件不存在时返回空字符串
    def read_notes(self, sid: str) -> str:
        path = self.session_dir(sid) / "notes.md"
        if not path.exists():
            return ""
        return path.read_text(encoding="utf-8")

    # 将一条主动笔记追加到 notes.md
    def append_note(self, sid: str, content: str, run_id: str) -> None:
        path = self.session_dir(sid)
        path.mkdir(parents=True, exist_ok=True)
        with (path / "notes.md").open("a", encoding="utf-8") as handle:
            handle.write(f"## Note ({_now()}, {run_id})\n{content}\n\n")
