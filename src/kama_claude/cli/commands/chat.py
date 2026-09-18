from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

from kama_claude.core.config import KamaConfig
from kama_claude.core.transport.socket_client import IpcError, SocketClient

_DECISION_MAP: dict[str, str] = {
    "y": "allow_once",
    "a": "always_allow",
    "n": "deny_once",
    "d": "always_deny",
}


class ChatPrinter:
    # 初始化 chat 模式的流式输出状态和待审批权限请求
    def __init__(self) -> None:
        self._inline = False
        self.pending_permission_id: str | None = None

    # 若当前 LLM token 尚未换行，则补一个换行
    def _ensure_newline(self) -> None:
        if self._inline:
            print()
            self._inline = False

    # 按事件类型打印 chat 输出、等待提示和权限审批请求
    async def handle(self, event: dict[str, Any]) -> None:
        t = event.get("type", "")
        if t == "llm.token":
            print(event.get("token", ""), end="", flush=True)
            self._inline = True
        elif t == "tool.call_started":
            self._ensure_newline()
            print(f"[tool] {event.get('tool_name', '')}")
        elif t == "permission.requested":
            self._ensure_newline()
            tool_name = str(event.get("tool_name", ""))
            param_preview = str(event.get("param_preview", ""))
            tool_use_id = str(event.get("tool_use_id", ""))
            print(f"[permission] {tool_name}  {param_preview}")
            print("  y=allow once  a=always allow  n=deny once  d=always deny")
            self.pending_permission_id = tool_use_id
        elif t == "session.waiting_for_input":
            self._ensure_newline()
            self.pending_permission_id = None
            print("[waiting for input]")
        elif t == "session.closed":
            self._ensure_newline()
            print("session closed.")


# 在线程池中读取 stdin，避免阻塞 socket event loop
async def _readline(prompt: str) -> str:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, input, prompt)


# 打印恢复出的有效会话消息链
def _print_history(messages: list[dict[str, Any]]) -> None:
    for message in messages:
        role = str(message.get("role", "unknown"))
        content = message.get("content", "")
        if not isinstance(content, str):
            content = json.dumps(content, ensure_ascii=False)
        print(f"[{role}] {content}")


# 让用户通过序号从当前工作区的会话简介列表中选择
async def _select_session(client: SocketClient, workspace_root: str) -> str | None:
    result = await client.send_command(
        "session.list",
        {"workspace_root": workspace_root, "include_closed": True},
    )
    sessions = list(result.get("sessions", []))
    if not sessions:
        print("no resumable sessions in this workspace")
        return None
    print(f"resumable sessions in {workspace_root}:")
    for index, item in enumerate(sessions, start=1):
        print(
            f"  {index}. {item.get('title') or '(empty session)'}  "
            f"[{item.get('status')}]  {item.get('updated_at')}"
        )
    while True:
        selected = (await _readline("select session number (q to cancel)> ")).strip()
        if selected.lower() in {"q", "quit", "exit"}:
            return None
        if selected.isdigit() and 1 <= int(selected) <= len(sessions):
            return str(sessions[int(selected) - 1]["session_id"])
        print(f"please enter a number from 1 to {len(sessions)}, or q to cancel")


# 异步核心：创建或恢复 chat session，循环读取输入并处理权限审批
async def _chat_async(
    config: KamaConfig,
    *,
    resume: bool = False,
    session_id: str | None = None,
) -> int:
    client = SocketClient(config.host, config.port)
    try:
        await client.connect()
    except (ConnectionRefusedError, OSError):
        print(f"error: core not running ({config.host}:{config.port})", file=sys.stderr)
        return 1

    printer = ChatPrinter()
    client.on_event(printer.handle)
    loop_task = asyncio.create_task(client.run_event_loop())

    try:
        await client.send_command(
            "event.subscribe",
            {
                "topics": ["session.*", "run.*", "tool.*", "llm.token", "permission.*"],
                "scope": "global",
            },
        )
        workspace_root = Path.cwd().resolve().as_posix()
        if resume:
            session_id = session_id or await _select_session(client, workspace_root)
            if session_id is None:
                return 1
            resumed = await client.send_command(
                "session.resume",
                {"session_id": session_id, "workspace_root": workspace_root},
            )
            _print_history(list(resumed.get("messages", [])))
        else:
            created = await client.send_command(
                "session.create",
                {"mode": "chat", "workspace_root": workspace_root},
            )
            session_id = str(created["session_id"])
        print(f"[session: {session_id}]")

        while True:
            try:
                line = await _readline("> ")
            except (EOFError, KeyboardInterrupt):
                break
            content = line.strip()
            if not content:
                continue

            # 有待审批的权限请求时，将用户输入解释为决策而非聊天消息
            if printer.pending_permission_id:
                decision = _DECISION_MAP.get(content.lower())
                if decision is None:
                    print("  enter y (allow once), a (always allow), "
                          "n (deny once), d (always deny)")
                    continue
                tool_use_id = printer.pending_permission_id
                printer.pending_permission_id = None
                await client.send_command(
                    "permission.respond",
                    {"tool_use_id": tool_use_id, "decision": decision},
                )
                continue

            await client.send_command(
                "session.send_message",
                {"session_id": session_id, "content": content},
            )
    except IpcError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    finally:
        loop_task.cancel()
        try:
            await loop_task
        except asyncio.CancelledError:
            pass
        await client.close()
    return 0


# 执行 kama chat 命令
def cmd_chat(config: KamaConfig) -> None:
    try:
        exit_code = asyncio.run(_chat_async(config))
    except KeyboardInterrupt:
        sys.exit(130)
    sys.exit(exit_code)


# 执行 kama resume [session_id] 并从当前工作区继续会话
def cmd_resume(config: KamaConfig, session_id: str | None = None) -> None:
    try:
        exit_code = asyncio.run(
            _chat_async(config, resume=True, session_id=session_id)
        )
    except KeyboardInterrupt:
        sys.exit(130)
    sys.exit(exit_code)
