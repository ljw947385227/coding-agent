from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import asdict
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from kama_claude.core.sandbox import ExecutionBackend, LocalExecutionBackend
from kama_claude.core.tools.base import BaseTool, ToolResult
from kama_claude.core.verification import (
    VerificationKind,
    VerificationManager,
    VerificationReport,
)
from kama_claude.core.workspace import WorkspaceBoundary


class VerifyProjectParams(BaseModel):
    model_config = ConfigDict(extra="ignore")
    path: str = "."
    checks: list[VerificationKind] = Field(default_factory=list)
    run: bool = False
    fail_fast: bool = True
    include_raw_output: bool = False
    incremental: bool = True
    timeout_seconds: float = Field(default=120.0, ge=1.0, le=900.0)
    baseline_tree: str | None = Field(default=None, pattern=r"^[0-9a-f]{40,64}$")
    baseline_repository: str | None = None
    baseline_checkpoint_id: str | None = None


class VerifyProjectTool(BaseTool):
    params_model = VerifyProjectParams
    name = "verify_project"
    description = (
        "Detect Python, Node, Rust, and Go verification commands. Uses the runtime task "
        "baseline to select related Python tests when available. By default only returns a "
        "reviewable plan; set run=true to execute it after permission approval."
    )
    input_schema: dict[str, object] = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Project root directory."},
            "checks": {
                "type": "array",
                "items": {"enum": ["test", "lint", "typecheck", "build"]},
                "description": "Optional check kinds; empty means all detected checks.",
            },
            "run": {
                "type": "boolean",
                "description": "Execute the plan instead of only previewing it.",
            },
            "fail_fast": {
                "type": "boolean",
                "description": "Skip remaining checks after the first non-passing result.",
            },
            "include_raw_output": {
                "type": "boolean",
                "description": (
                    "Include bounded raw logs even when structured diagnostics are available."
                ),
            },
            "incremental": {
                "type": "boolean",
                "description": (
                    "Use runtime checkpoint diff and the persistent test index when available."
                ),
            },
            "timeout_seconds": {
                "type": "number",
                "minimum": 1,
                "maximum": 900,
                "description": "Timeout applied independently to each command.",
            },
        },
    }

    # 创建仅允许验证会话工作区子目录的项目验证工具
    def __init__(
        self,
        *,
        workspace_root: str | Path | None = None,
        execution_backend: ExecutionBackend | None = None,
        ignore_files: Sequence[str] | None = None,
    ) -> None:
        self._workspace = WorkspaceBoundary.from_path(workspace_root or Path.cwd())
        self._execution_backend = execution_backend or LocalExecutionBackend()
        self._ignore_files = tuple(ignore_files or ())

    # 生成验证计划并在明确要求时执行经过审批的项目命令
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        parsed = VerifyProjectParams.model_validate(params)
        try:
            root = self._workspace.resolve(parsed.path)
            if not root.is_dir():
                raise ValueError(f"not a directory: {parsed.path}")
            manager = VerificationManager(
                root,
                timeout_seconds=parsed.timeout_seconds,
                execution_backend=self._execution_backend,
                sensitive_patterns=self._ignore_files,
            )
            if (
                parsed.incremental
                and parsed.baseline_tree is not None
                and parsed.baseline_repository is not None
            ):
                repository = self._workspace.resolve(parsed.baseline_repository)
                if repository != self._workspace.root:
                    raise PermissionError("incremental baseline repository must be workspace root")
                plan = await manager.incremental_plan(
                    parsed.baseline_tree,
                    repository,
                    parsed.checks or None,
                    parsed.baseline_checkpoint_id,
                )
            else:
                plan = manager.plan(parsed.checks or None)
            payload: dict[str, object] = {"ran": False, "plan": asdict(plan)}
            if parsed.run:
                report = await manager.run(plan, fail_fast=parsed.fail_fast)
                payload.update(
                    {
                        "ran": True,
                        "report": _report_payload(report, parsed.include_raw_output),
                    }
                )
            return ToolResult(content=json.dumps(payload, ensure_ascii=False, indent=2))
        except (OSError, ValueError) as exc:
            return ToolResult(
                content=json.dumps(
                    {"error": "verification_failed", "message": str(exc)},
                    ensure_ascii=False,
                ),
                is_error=True,
                error_type="runtime_error",
            )


# 为模型构建低成本验证报告，已解析错误默认省略重复原始日志
def _report_payload(
    report: VerificationReport,
    include_raw_output: bool,
) -> dict[str, object]:
    payload = asdict(report)
    results = payload.get("results")
    if include_raw_output or not isinstance(results, tuple):
        return payload
    for result in results:
        if not isinstance(result, dict):
            continue
        output = str(result.get("output", ""))
        diagnostics = result.get("diagnostics")
        status = result.get("status")
        if diagnostics or status in ("passed", "skipped"):
            projected = ""
        else:
            projected = _bounded_fallback(output, 4 * 1024)
        result["output"] = projected
        result["raw_output_omitted"] = projected != output
    return payload


# 按 UTF-8 字节预算保留未知失败日志的头尾片段
def _bounded_fallback(output: str, limit: int) -> str:
    raw = output.encode("utf-8")
    if len(raw) <= limit:
        return output
    marker = b"\n[... raw verification output omitted ...]\n"
    remaining = max(2, limit - len(marker))
    head = raw[: remaining // 2]
    tail = raw[-(remaining - len(head)) :]
    return (head + marker + tail).decode("utf-8", errors="replace")
