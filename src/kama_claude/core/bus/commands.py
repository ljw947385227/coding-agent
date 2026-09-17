from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import AfterValidator, BaseModel, Discriminator

from kama_claude.core.session.model import SessionMode, SessionStatus


# 校验客户端工作区是非空绝对路径，避免退化为 daemon 当前目录
def _validate_absolute_workspace(value: str) -> str:
    path = Path(value).expanduser()
    if not value.strip() or not path.is_absolute():
        raise ValueError("workspace_root must be an absolute path")
    return value


AbsoluteWorkspace = Annotated[str, AfterValidator(_validate_absolute_workspace)]


class PingCommand(BaseModel):
    type: Literal["core.ping"] = "core.ping"
    client: str


class PongResult(BaseModel):
    server_version: str
    uptime_ms: int
    received_at: str  # ISO 8601


class AgentRunCommand(BaseModel):
    type: Literal["agent.run"] = "agent.run"
    goal: str
    workspace_root: AbsoluteWorkspace


class AgentRunResult(BaseModel):
    run_id: str


class AgentListRunsCommand(BaseModel):
    type: Literal["agent.list_runs"] = "agent.list_runs"
    workspace_root: AbsoluteWorkspace
    session_id: str | None = None


class AgentListRunsResult(BaseModel):
    runs: list[dict[str, Any]]


class AgentCancelCommand(BaseModel):
    type: Literal["agent.cancel"] = "agent.cancel"
    run_id: str
    workspace_root: AbsoluteWorkspace
    cascade: bool = True


class AgentCancelResult(BaseModel):
    run_id: str
    status: Literal["accepted", "already_finished", "not_found"]


class AgentSnoozeCommand(BaseModel):
    type: Literal["agent.snooze"] = "agent.snooze"
    run_id: str
    workspace_root: AbsoluteWorkspace
    seconds: float | None = None


class AgentSnoozeResult(BaseModel):
    run_id: str
    ok: bool


class EventSubscribeCommand(BaseModel):
    type: Literal["event.subscribe"] = "event.subscribe"
    topics: list[str]  # fnmatch 模式，如 ["step.*", "tool.*"]
    scope: str = "global"  # "global" | "run:<run_id>"
    replay_from_run: str | None = None  # 设置则先从 events.jsonl 回放历史再接实时流


class EventSubscribeResult(BaseModel):
    subscription_id: str
    replayed_count: int = 0


class SessionCreateCommand(BaseModel):
    type: Literal["session.create"] = "session.create"
    mode: SessionMode = "chat"
    title: str = ""
    workspace_root: AbsoluteWorkspace


class SessionCreateResult(BaseModel):
    session_id: str
    status: SessionStatus
    test_total: int | None = None


class SessionListCommand(BaseModel):
    type: Literal["session.list"] = "session.list"
    workspace_root: AbsoluteWorkspace
    include_closed: bool = False


class SessionSummary(BaseModel):
    session_id: str
    title: str
    status: SessionStatus
    updated_at: str
    last_run_id: str | None = None
    workspace_root: AbsoluteWorkspace
    is_branch: bool = False


class SessionListResult(BaseModel):
    sessions: list[SessionSummary]


class SessionResumeCommand(BaseModel):
    type: Literal["session.resume"] = "session.resume"
    session_id: str
    workspace_root: AbsoluteWorkspace


class SessionResumeResult(BaseModel):
    session_id: str
    status: SessionStatus
    title: str
    workspace_root: AbsoluteWorkspace
    messages: list[dict[str, Any]]
    test_total: int | None = None


class SessionBranchCommand(BaseModel):
    type: Literal["session.branch"] = "session.branch"
    session_id: str
    workspace_root: AbsoluteWorkspace
    node_id: str | None = None
    title: str = ""


class SessionBranchResult(BaseModel):
    session_id: str
    status: SessionStatus
    title: str
    workspace_root: AbsoluteWorkspace
    source_session_id: str
    source_node_id: str
    checkpoint_id: str
    messages: list[dict[str, Any]]
    test_total: int | None = None


class SessionSendMessageCommand(BaseModel):
    type: Literal["session.send_message"] = "session.send_message"
    session_id: str
    content: str


class SessionSendMessageResult(BaseModel):
    run_id: str


class SessionGetHistoryCommand(BaseModel):
    type: Literal["session.get_history"] = "session.get_history"
    session_id: str


class SessionGetHistoryResult(BaseModel):
    messages: list[dict[str, Any]]


class SessionCloseCommand(BaseModel):
    type: Literal["session.close"] = "session.close"
    session_id: str


class SessionCloseResult(BaseModel):
    status: SessionStatus


class PermissionRespondCommand(BaseModel):
    type: Literal["permission.respond"] = "permission.respond"
    tool_use_id: str
    # "allow_once" | "always_allow" | "deny_once" | "always_deny"
    decision: str


class PermissionRespondResult(BaseModel):
    ok: bool = True


class SessionCompactCommand(BaseModel):
    type: Literal["session.compact"] = "session.compact"
    session_id: str
    focus: str = ""


class SessionCompactResult(BaseModel):
    summary_tokens: int
    saved_tokens: int


# 根据 type 字段决定命令类型的判别联合
Command = Annotated[
    PingCommand
    | AgentRunCommand
    | AgentListRunsCommand
    | AgentCancelCommand
    | AgentSnoozeCommand
    | EventSubscribeCommand
    | SessionCreateCommand
    | SessionListCommand
    | SessionResumeCommand
    | SessionBranchCommand
    | SessionSendMessageCommand
    | SessionGetHistoryCommand
    | SessionCloseCommand
    | PermissionRespondCommand
    | SessionCompactCommand,
    Discriminator("type"),
]
