from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, cast

SessionStatus = Literal["active", "waiting_for_input", "interrupted", "closed"]
SessionMode = Literal["one_shot", "chat"]


@dataclass
class Session:
    id: str
    mode: SessionMode
    status: SessionStatus
    title: str
    created_at: str
    updated_at: str
    workspace_root: str | None = None
    discovery_root: str | None = None
    run_ids: list[str] = field(default_factory=list)
    current_id: str | None = None
    forked_from_session_id: str | None = None
    forked_from_node_id: str | None = None
    fork_checkpoint_id: str | None = None
    # Session 创建时尽力建立的测试索引摘要；非 Git/非 Python 项目可为空
    test_total: int | None = None
    test_index_tree: str | None = None
    schema_version: int = 3

    # 将 Session 转为可写入 meta.json 的普通 dict
    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "mode": self.mode,
            "status": self.status,
            "title": self.title,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "workspace_root": self.workspace_root,
            "discovery_root": self.discovery_root,
            "run_ids": list(self.run_ids),
            "current_id": self.current_id,
            "forked_from_session_id": self.forked_from_session_id,
            "forked_from_node_id": self.forked_from_node_id,
            "fork_checkpoint_id": self.fork_checkpoint_id,
            "test_total": self.test_total,
            "test_index_tree": self.test_index_tree,
            "schema_version": self.schema_version,
        }

    # 从 meta.json 的 dict 还原 Session 对象
    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Session:
        mode = str(data["mode"])
        status = str(data["status"])
        run_ids = data.get("run_ids", [])
        if mode not in ("one_shot", "chat"):
            raise ValueError(f"invalid session mode: {mode!r}")
        if status not in ("active", "waiting_for_input", "interrupted", "closed"):
            raise ValueError(f"invalid session status: {status!r}")
        if not isinstance(run_ids, list):
            raise ValueError("session run_ids must be a list")
        return cls(
            id=str(data["id"]),
            mode=cast(SessionMode, mode),
            status=cast(SessionStatus, status),
            title=str(data.get("title", "")),
            created_at=str(data["created_at"]),
            updated_at=str(data["updated_at"]),
            workspace_root=(
                str(data["workspace_root"]) if data.get("workspace_root") is not None else None
            ),
            discovery_root=(
                str(data["discovery_root"]) if data.get("discovery_root") is not None else None
            ),
            run_ids=[str(x) for x in run_ids],
            current_id=(str(data["current_id"]) if data.get("current_id") is not None else None),
            forked_from_session_id=(
                str(data["forked_from_session_id"])
                if data.get("forked_from_session_id") is not None
                else None
            ),
            forked_from_node_id=(
                str(data["forked_from_node_id"])
                if data.get("forked_from_node_id") is not None
                else None
            ),
            fork_checkpoint_id=(
                str(data["fork_checkpoint_id"])
                if data.get("fork_checkpoint_id") is not None
                else None
            ),
            test_total=(int(data["test_total"]) if data.get("test_total") is not None else None),
            test_index_tree=(
                str(data["test_index_tree"])
                if data.get("test_index_tree") is not None
                else None
            ),
            schema_version=int(data.get("schema_version", 1)),
        )
