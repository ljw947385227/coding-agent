from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from kama_claude.core.bus.events import StepFinishedEvent, StepStartedEvent
from kama_claude.core.context import ExecutionContext
from kama_claude.core.events.bus import EventBus
from kama_claude.core.llm.base import LLMProvider
from kama_claude.core.tools.invocation import invoke_tool
from kama_claude.core.tools.registry import ToolRegistry
from kama_claude.core.verification.controller import VerificationController

if TYPE_CHECKING:
    from kama_claude.core.compact.compactor import Compactor
    from kama_claude.core.permissions.manager import PermissionManager


log = logging.getLogger(__name__)


# 返回当前 UTC 时间的 ISO 8601 字符串
def _now() -> str:
    return datetime.now(UTC).isoformat()


class AgentLoop:
    # 初始化循环依赖以及可选的权限、压缩和验证控制器
    def __init__(
        self,
        provider: LLMProvider,
        registry: ToolRegistry,
        bus: EventBus,
        *,
        permission_manager: PermissionManager | None = None,
        compactor: Compactor | None = None,
        compact_threshold: float = 0.80,
        session_id: str = "",
        verification_controller: VerificationController | None = None,
        workspace_root: Path | None = None,
    ) -> None:
        self._provider = provider
        self._registry = registry
        self._bus = bus
        self._permission_manager = permission_manager
        self._compactor = compactor
        self._compact_threshold = compact_threshold
        self._session_id = session_id
        self._verification = verification_controller
        self._workspace_root = workspace_root

    # 驱动 plan→act→observe 循环直到上下文终止；CancelledError 向上传播
    async def run(self, context: ExecutionContext) -> None:
        while not context.is_done():
            context.step += 1
            await self._bus.publish(
                StepStartedEvent(run_id=context.run_id, step=context.step, ts=_now())
            )

            # [plan] call LLM — API errors terminate the run
            try:
                verification_prompt = (
                    self._verification.system_prompt() if self._verification is not None else ""
                )
                response = await self._provider.chat(
                    messages=context.messages,
                    tool_schemas=self._registry.tool_schemas(),
                    bus=self._bus,
                    run_id=context.run_id,
                    step=context.step,
                    system=context.system_prompt(
                        "You are a helpful AI assistant. "
                        "Use the available tools to complete the user's goal. "
                        "When the goal is fully achieved, respond with a final answer "
                        "and do not call any more tools."
                    )
                    + verification_prompt,
                )
            except asyncio.CancelledError:
                context.mark_failed("cancelled")
                raise
            except Exception:
                logging.getLogger(__name__).exception(
                    "LLM call failed run_id=%s step=%d", context.run_id, context.step
                )
                context.mark_failed("llm_error")
                break

            if self._verification is not None and response.tool_calls:
                response.tool_calls = [
                    await self._verification.prepare_tool(tool_call)
                    for tool_call in response.tool_calls
                ]

            # [observe] append assistant content blocks to context
            # thinking blocks must come first and be preserved verbatim for extended thinking mode
            blocks: list[dict[str, object]] = list(response.thinking_blocks)
            if response.text:
                blocks.append({"type": "text", "text": response.text})
            for tc in response.tool_calls:
                blocks.append({"type": "tool_use", "id": tc.id, "name": tc.name, "input": tc.input})

            finish_action = "allow"
            finish_reason: str | None = None
            auto_verification = None
            if response.stop_reason == "end_turn" and self._verification is not None:
                finish_action, finish_reason = self._verification.finish_action()
                if finish_action == "verify":
                    if self._registry.get("verify_project") is None:
                        finish_action = "fail"
                        finish_reason = "verification_tool_unavailable"
                    else:
                        auto_verification = self._verification.auto_tool_call(
                            context.run_id,
                            context.step,
                        )
                        blocks.append(
                            {
                                "type": "tool_use",
                                "id": auto_verification.id,
                                "name": auto_verification.name,
                                "input": auto_verification.input,
                            }
                        )
            context.add_assistant_message(blocks)

            # [act] execute each requested tool; errors become tool results so loop continues
            if response.stop_reason == "tool_use":
                for tc in response.tool_calls:
                    result = await invoke_tool(
                        self._registry,
                        tc,
                        self._bus,
                        context.run_id,
                        permission_manager=self._permission_manager,
                        session_id=self._session_id,
                        workspace_root=self._workspace_root,
                    )
                    node_id = context.add_tool_result(
                        tc.id, result.content, is_error=result.is_error
                    )
                    if self._verification is not None:
                        self._verification.observe_tool(tc.name, tc.input, result)
                        await self._verification.checkpoint_after_tool(
                            tc.name,
                            tc.input,
                            result,
                            node_id,
                        )
            elif response.stop_reason == "max_tokens" and response.tool_calls:
                # Output token limit hit mid-tool-call; input is incomplete.
                # Add synthetic error results so the conversation stays balanced.
                for tc in response.tool_calls:
                    context.add_tool_result(
                        tc.id,
                        "Error: output token limit reached before this tool call "
                        "could be completed. "
                        "Please break the task into smaller steps and try again.",
                        is_error=True,
                    )

            if auto_verification is not None and self._verification is not None:
                result = await invoke_tool(
                    self._registry,
                    auto_verification,
                    self._bus,
                    context.run_id,
                    permission_manager=self._permission_manager,
                    session_id=self._session_id,
                    workspace_root=self._workspace_root,
                )
                node_id = context.add_tool_result(
                    auto_verification.id,
                    result.content,
                    is_error=result.is_error,
                )
                self._verification.observe_tool(
                    auto_verification.name,
                    auto_verification.input,
                    result,
                )
                await self._verification.checkpoint_after_tool(
                    auto_verification.name,
                    auto_verification.input,
                    result,
                    node_id,
                )

            # Termination check — end_turn wins over max_steps if both hit on same step
            if response.stop_reason == "end_turn":
                if finish_action == "fail":
                    context.mark_failed(finish_reason or "verification_failed")
                elif auto_verification is None or (
                    self._verification is not None and not self._verification.dirty
                ):
                    context.result = response.text or ""
                    context.mark_success()
                elif self._verification is not None:
                    terminal_reason = self._verification.terminal_reason()
                    if terminal_reason is not None:
                        context.mark_failed(terminal_reason)
                    elif context.step >= context.max_steps:
                        context.mark_failed("exceeded_max_steps")
            elif context.step >= context.max_steps:
                context.mark_failed("exceeded_max_steps")

            # 工具结果追加完毕（messages 末尾为 user）后检查压缩，仅在 run 继续时触发
            # 此时压缩结果 [user_summary, assistant_ack] 对下一次 LLM 调用是合法输入
            if (
                not context.is_done()
                and (response.stop_reason == "tool_use" or auto_verification is not None)
                and self._compactor is not None
                and self._compact_threshold > 0
                and response.usage is not None
                and response.usage.context_pct >= self._compact_threshold
            ):
                await self._compactor.compact(context, self._provider)

            await self._bus.publish(
                StepFinishedEvent(run_id=context.run_id, step=context.step, ts=_now())
            )
