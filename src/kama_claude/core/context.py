from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ExecutionContext:
    run_id: str
    goal: str
    max_steps: int
    prefill_messages: list[dict[str, Any]] = field(default_factory=list)
    session_notes: str = ""
    global_context: str = ""
    project_context: str = ""
    messages: list[dict[str, Any]] = field(default_factory=list)
    step: int = 0
    status: str = "running"  # "running" | "success" | "failed"
    reason: str | None = None
    result: str = ""
    # 记录本轮新产生的追加式 thread 节点，独立于压缩后的模型消息视图
    new_entries: list[dict[str, Any]] = field(default_factory=list)
    # 可选的同步持久化回调；Session Run 每产生一个逻辑节点就立即调用
    entry_sink: Callable[[dict[str, Any]], str | None] | None = field(
        default=None,
        repr=False,
    )
    persisted_entry_count: int = 0
    last_persisted_node_id: str | None = None
    # skill 或 subagent 角色可覆盖默认 system prompt
    system_prompt_override: str | None = None

    # 初始化消息历史，优先使用 session 完整回放内容
    def __post_init__(self) -> None:
        if self.prefill_messages:
            self.messages = [dict(m) for m in self.prefill_messages]
        elif not self.messages:
            self.messages.append({"role": "user", "content": self.goal})

    # 返回当前 run 的 system prompt；有 override 时跳过 base，直接注入记忆层
    def system_prompt(self, base: str) -> str:
        parts = [self.system_prompt_override if self.system_prompt_override else base]
        if self.global_context.strip():
            parts.append("\n\n## Global Context\n" + self.global_context.strip())
        if self.project_context.strip():
            parts.append("\n\n## Project Context\n" + self.project_context.strip())
        if self.session_notes.strip():
            parts.append(
                "\n\n## Session Notes\n"
                + self.session_notes.strip()
                + "\n\nRemember important durable facts by calling note_save."
            )
        return "".join(parts)

    # 将 LLM 响应的 content blocks 追加为 assistant 消息
    def add_assistant_message(self, content: list[Any]) -> str | None:
        message = {"role": "assistant", "content": content}
        self.messages.append(message)
        return self._record_entry({"kind": "message", "message": message})

    # 将工具调用结果追加为 user 消息；同一步的多个结果共享同一条消息
    def add_tool_result(self, tool_use_id: str, content: str, is_error: bool = False) -> str | None:
        block: dict[str, Any] = {
            "type": "tool_result",
            "tool_use_id": tool_use_id,
            "content": content,
        }
        if is_error:
            block["is_error"] = True

        last = self.messages[-1] if self.messages else None
        if (
            last is not None
            and last["role"] == "user"
            and isinstance(last["content"], list)
            and last["content"]
            and all(b.get("type") == "tool_result" for b in last["content"])
        ):
            last["content"].append(block)
        else:
            message = {"role": "user", "content": [block]}
            self.messages.append(message)
        persisted_message = {"role": "user", "content": [dict(block)]}
        return self._record_entry({"kind": "message", "message": persisted_message})

    # 用摘要替换模型消息视图，并在追加日志中记录单个 compact 节点
    def apply_compaction(
        self,
        summary: str,
        original_tokens: int,
        summary_tokens: int,
    ) -> None:
        self.messages = [
            {"role": "user", "content": summary},
            {"role": "assistant", "content": "Understood, I'll continue from this summary."},
        ]
        self._record_entry(
            {
                "kind": "compact",
                "summary": summary,
                "original_tokens": original_tokens,
                "summary_tokens": summary_tokens,
            }
        )

    # 记录新节点并在配置 Session sink 时同步追加到持久化日志
    def _record_entry(self, entry: dict[str, Any]) -> str | None:
        self.new_entries.append(entry)
        if self.entry_sink is not None:
            node_id = self.entry_sink(entry)
            self.persisted_entry_count += 1
            self.last_persisted_node_id = node_id
            return node_id
        return None

    # 返回 True 表示 loop 应停止（状态不再是 running）
    def is_done(self) -> bool:
        return self.status != "running"

    # 将 run 标记为成功
    def mark_success(self) -> None:
        self.status = "success"

    # 将 run 标记为失败并记录原因
    def mark_failed(self, reason: str) -> None:
        self.status = "failed"
        self.reason = reason
