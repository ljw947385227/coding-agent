from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from kama_claude.core.config import ExecutionConfig
from kama_claude.core.sandbox import ExecutionBackend
from kama_claude.core.sandbox.info import SandboxInspector
from kama_claude.core.tools.base import BaseTool, ToolResult


class SandboxInfoParams(BaseModel):
    model_config = ConfigDict(extra="ignore")
    probe: bool = True


class SandboxInfoTool(BaseTool):
    params_model = SandboxInfoParams
    name = "sandbox_info"
    description = (
        "Inspect the configured command execution environment without exposing secrets. "
        "Use it when failures suggest missing tools or dependencies, version drift, permissions, "
        "network policy, architecture, resource limits, or a stale image. Do not use it for "
        "ordinary assertion, syntax, lint, or type errors without environment evidence."
    )
    input_schema: dict[str, object] = {
        "type": "object",
        "properties": {
            "probe": {
                "type": "boolean",
                "description": (
                    "Run a read-only probe in the actual execution backend. Default true."
                ),
            }
        },
    }

    # 创建绑定当前工作区和执行后端的只读沙箱信息工具
    def __init__(
        self,
        config: ExecutionConfig,
        execution_backend: ExecutionBackend,
        workspace_root: Path,
    ) -> None:
        self._inspector = SandboxInspector(config, execution_backend, workspace_root)

    # 返回脱敏后的配置、实际运行时、镜像和环境漂移信息
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        parsed = SandboxInfoParams.model_validate(params)
        try:
            payload = await self._inspector.inspect(probe=parsed.probe)
        except (OSError, ValueError) as exc:
            return ToolResult(content=str(exc), is_error=True, error_type="runtime_error")
        return ToolResult(content=json.dumps(payload, ensure_ascii=False, indent=2))
