from __future__ import annotations

from pydantic import BaseModel

from kama_claude.core.harness.models import EvaluationMetrics


class MetricsCollector:
    """Aggregate stable run metrics from the existing EventBus stream."""

    def __init__(self) -> None:
        self.steps = 0
        self.tool_calls = 0
        self.tool_failures = 0
        self.permission_requests = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.cache_read_input_tokens = 0
        self.cache_creation_input_tokens = 0
        self.tool_calls_by_name: dict[str, int] = {}
        self.tool_trace: list[dict[str, object]] = []

    async def handle(self, event: BaseModel) -> None:
        event_type = getattr(event, "type", "")
        if event_type == "step.started":
            self.steps = max(self.steps, int(getattr(event, "step", 0)))
        elif event_type == "tool.call_started":
            self.tool_calls += 1
            tool_name = str(getattr(event, "tool_name", ""))
            self.tool_calls_by_name[tool_name] = self.tool_calls_by_name.get(tool_name, 0) + 1
            self.tool_trace.append(
                {
                    "type": event_type,
                    "tool_use_id": str(getattr(event, "tool_use_id", "")),
                    "tool_name": tool_name,
                    "params": getattr(event, "params", {}),
                }
            )
        elif event_type == "tool.call_failed":
            self.tool_failures += 1
            self.tool_trace.append(
                {
                    "type": event_type,
                    "tool_use_id": str(getattr(event, "tool_use_id", "")),
                    "tool_name": str(getattr(event, "tool_name", "")),
                    "error_class": str(getattr(event, "error_class", "")),
                }
            )
        elif event_type == "tool.call_finished":
            self.tool_trace.append(
                {
                    "type": event_type,
                    "tool_use_id": str(getattr(event, "tool_use_id", "")),
                    "tool_name": str(getattr(event, "tool_name", "")),
                }
            )
        elif event_type == "permission.requested":
            self.permission_requests += 1
        elif event_type == "llm.usage":
            self.input_tokens += int(getattr(event, "input_tokens", 0))
            self.output_tokens += int(getattr(event, "output_tokens", 0))
            self.cache_read_input_tokens += int(
                getattr(event, "cache_read_input_tokens", 0)
            )
            self.cache_creation_input_tokens += int(
                getattr(event, "cache_creation_input_tokens", 0)
            )

    def snapshot(self, duration_ms: int) -> EvaluationMetrics:
        return EvaluationMetrics(
            duration_ms=duration_ms,
            steps=self.steps,
            tool_calls=self.tool_calls,
            tool_failures=self.tool_failures,
            permission_requests=self.permission_requests,
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
            cache_read_input_tokens=self.cache_read_input_tokens,
            cache_creation_input_tokens=self.cache_creation_input_tokens,
            sandbox_info_calls=self.tool_calls_by_name.get("sandbox_info", 0),
            tool_calls_by_name=dict(sorted(self.tool_calls_by_name.items())),
        )
