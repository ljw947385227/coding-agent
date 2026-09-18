from __future__ import annotations

from typing import Protocol

from kama_claude.core.sandbox.models import ExecutionRequest, ExecutionResult


class ExecutionBackend(Protocol):
    # 在受控执行环境中运行请求并返回有界结构化结果
    async def run(self, request: ExecutionRequest) -> ExecutionResult: ...
