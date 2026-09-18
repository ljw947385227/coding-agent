from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from kama_claude.core.git.checkpoint import CheckpointError, CheckpointManager
from kama_claude.core.git.process import discover_repository
from kama_claude.core.llm.types import ToolCallBlock
from kama_claude.core.tools.base import ToolResult

VerificationMode = Literal["off", "suggest", "required_on_write"]
FinishAction = Literal["allow", "verify", "fail"]

_MUTATING_TOOLS = {"edit_file", "write_file"}
_STATE_READ_ONLY_TOOLS = {
    "read_file",
    "search_code",
    "list_dir",
    "git_status",
    "git_diff",
    "verify_project",
    "note_save",
    "git_checkpoint",
    "task_create",
    "task_update",
    "task_list",
    "task_get",
    "spawn_agent",
    "agent_result",
}


@dataclass(slots=True)
class VerificationController:
    mode: VerificationMode = "required_on_write"
    max_attempts: int = 3
    max_total_seconds: float = 600.0
    dirty: bool = False
    attempts: int = 0
    last_status: str = "idle"
    last_failure: str = ""
    workspace_root: Path | None = None
    session_id: str | None = None
    node_id: str | None = None
    run_id: str | None = None
    baseline_checkpoint_id: str | None = None
    baseline_tree: str | None = None
    baseline_repository: str | None = None
    baseline_error: str = ""
    _started_at: float | None = None
    _baseline_attempted: bool = False

    # 在首次写操作前建立任务 checkpoint，并为验证调用注入内部增量基线
    async def prepare_tool(self, tool_call: ToolCallBlock) -> ToolCallBlock:
        if self.mode != "off" and _is_mutation(tool_call.name, tool_call.input):
            await self._ensure_baseline(_checkpoint_paths(tool_call.name, tool_call.input))
        if tool_call.name != "verify_project" or self.baseline_tree is None:
            return tool_call
        prepared = dict(tool_call.input)
        prepared.update(self._baseline_params())
        return ToolCallBlock(id=tool_call.id, name=tool_call.name, input=prepared)

    # 把成功写操作和验证结果归并到当前有界验证周期
    def observe_tool(
        self,
        tool_name: str,
        params: dict[str, object],
        result: ToolResult,
    ) -> None:
        if _is_mutation(tool_name, params) and not result.is_error:
            self._mark_dirty()
            return
        if tool_name == "verify_project" and params.get("run") is True:
            self._observe_verification(result)

    # 在成功的潜在写操作结果节点后保存精确代码状态，普通消息节点沿父链继承它
    async def checkpoint_after_tool(
        self,
        tool_name: str,
        params: dict[str, object],
        result: ToolResult,
        node_id: str | None,
    ) -> str | None:
        if (
            result.is_error
            or node_id is None
            or self.workspace_root is None
            or not may_change_code_state(tool_name, params)
        ):
            return None
        repository, _result = await discover_repository(self.workspace_root.resolve())
        if repository is None:
            return None
        try:
            checkpoint = await CheckpointManager(repository).create(
                label=f"state after {tool_name}",
                kind="mutation",
                session_id=self.session_id,
                node_id=node_id,
                run_id=self.run_id,
                paths=_checkpoint_paths(tool_name, params),
            )
        except (CheckpointError, OSError):
            return None
        return checkpoint.id

    # 返回 Agent 结束前应允许、自动验证或失败的硬门禁决策
    def finish_action(self) -> tuple[FinishAction, str | None]:
        if self.mode in ("off", "suggest") or not self.dirty:
            return "allow", None
        terminal_reason = self.terminal_reason()
        if terminal_reason is not None:
            return "fail", terminal_reason
        return "verify", None

    # 构造由 Loop 注入且仍经过统一权限系统的验证工具调用
    def auto_tool_call(self, run_id: str, step: int) -> ToolCallBlock:
        params: dict[str, object] = {
            "path": ".",
            "checks": [],
            "run": True,
            "fail_fast": True,
            "include_raw_output": False,
            "incremental": True,
        }
        params.update(self._baseline_params())
        return ToolCallBlock(
            id=f"auto_verify_{run_id}_{step}_{self.attempts + 1}",
            name="verify_project",
            input=params,
        )

    # 返回当前验证周期已经无法继续的稳定失败原因
    def terminal_reason(self) -> str | None:
        if not self.dirty:
            return None
        if self.last_status == "permission_denied":
            return "verification_permission_denied"
        if self.attempts >= self.max_attempts:
            return "verification_attempts_exhausted"
        if self._elapsed_seconds() >= self.max_total_seconds:
            return "verification_time_budget_exhausted"
        return None

    # 生成随状态变化的系统约束，帮助模型在硬门禁前主动验证和修复
    def system_prompt(self) -> str:
        if self.mode == "off":
            return ""
        policy = (
            "After successful edit_file/write_file or an applied git_rollback, use "
            "verify_project with run=true. Read its structured diagnostics, repair failures, "
            "and verify again before claiming completion."
        )
        if self.mode == "suggest":
            return "\n\n## Verification Policy\n" + policy
        remaining = max(0, self.max_attempts - self.attempts)
        state = "workspace is unverified" if self.dirty else "workspace is verified or unchanged"
        return (
            "\n\n## Required Verification Policy\n"
            + policy
            + " The runtime enforces this requirement before a successful end turn. "
            + f"Current state: {state}; remaining verification attempts: {remaining}."
        )

    # 开始或延续一次从代码修改到验证通过的周期
    def _mark_dirty(self) -> None:
        if not self.dirty:
            self.attempts = 0
            self._started_at = time.monotonic()
        self.dirty = True
        self.last_status = "unverified"

    # 解析 verify_project 的 JSON 结果并更新验证周期状态
    def _observe_verification(self, result: ToolResult) -> None:
        if not self.dirty:
            return
        self.attempts += 1
        if result.is_error:
            self.last_status = result.error_type or "tool_error"
            self.last_failure = result.content
            return
        try:
            payload = json.loads(result.content)
        except json.JSONDecodeError:
            self.last_status = "invalid_report"
            self.last_failure = "verify_project returned invalid JSON"
            return
        if not isinstance(payload, dict) or payload.get("ran") is not True:
            self.last_status = "not_run"
            self.last_failure = "verify_project did not execute a verification plan"
            return
        plan = payload.get("plan")
        if isinstance(plan, dict) and plan.get("checks") == []:
            self._mark_verified("not_applicable")
            return
        report = payload.get("report")
        if isinstance(report, dict) and report.get("passed") is True:
            self._mark_verified("passed")
            return
        self.last_status = "failed"
        self.last_failure = _report_failure_summary(report)

    # 清除 dirty 门禁并保存最近一次成功或无需验证的状态
    def _mark_verified(self, status: str) -> None:
        self.dirty = False
        self.last_status = status
        self.last_failure = ""
        self._started_at = None

    # 返回当前验证周期占用的单调时钟秒数
    def _elapsed_seconds(self) -> float:
        if self._started_at is None:
            return 0.0
        return time.monotonic() - self._started_at

    # 首次潜在写入前创建不会改变真实 index/worktree 的验证基线 checkpoint
    async def _ensure_baseline(self, paths: list[str] | None = None) -> None:
        if self._baseline_attempted or self.workspace_root is None:
            return
        self._baseline_attempted = True
        workspace = self.workspace_root.resolve()
        repository, result = await discover_repository(workspace)
        if repository is None:
            self.baseline_error = (
                result.stderr.decode("utf-8", errors="replace").strip()
                or "workspace is not a Git repository"
            )
            return
        repository = repository.resolve()
        if repository != workspace:
            self.baseline_error = "workspace root must equal repository root for incremental tests"
            return
        try:
            checkpoint = await CheckpointManager(repository).create(
                label="automatic verification baseline",
                kind="verification",
                session_id=self.session_id,
                node_id=self.node_id,
                run_id=self.run_id,
                paths=paths,
            )
        except (CheckpointError, OSError) as exc:
            self.baseline_error = str(exc)
            return
        self.baseline_checkpoint_id = checkpoint.id
        self.baseline_tree = checkpoint.worktree_tree
        self.baseline_repository = checkpoint.repository

    # 构造只由 Runtime 注入且不会暴露为模型必填参数的基线字段
    def _baseline_params(self) -> dict[str, object]:
        if self.baseline_tree is None or self.baseline_repository is None:
            return {}
        return {
            "baseline_tree": self.baseline_tree,
            "baseline_repository": self.baseline_repository,
            "baseline_checkpoint_id": self.baseline_checkpoint_id,
        }


# 判断工具调用是否成功后可能改变工作区内容
def _is_mutation(tool_name: str, params: dict[str, object]) -> bool:
    if tool_name in _MUTATING_TOOLS:
        return True
    return tool_name == "git_rollback" and params.get("apply") is True


# 保守判断工具是否可能改变代码状态，未知扩展工具按可写处理以保证分支恢复正确性
def may_change_code_state(tool_name: str, params: dict[str, object]) -> bool:
    if tool_name == "git_rollback":
        return params.get("apply") is True
    if tool_name == "bash":
        return not _is_read_only_shell_command(str(params.get("command", "")))
    return tool_name not in _STATE_READ_ONLY_TOOLS


# 为路径明确的内建写工具返回精确快照范围；None 表示未知工具需走有界全量预检
def _checkpoint_paths(tool_name: str, params: dict[str, object]) -> list[str] | None:
    if tool_name not in _MUTATING_TOOLS:
        return None
    path = params.get("path")
    return [str(path)] if isinstance(path, str) and path else None


# 只接受不含 shell 组合/重定向符的明确查询命令，避免把可写命令误判为只读
def _is_read_only_shell_command(command: str) -> bool:
    normalized = " ".join(command.strip().lower().split())
    if not normalized or any(token in normalized for token in (";", "&&", "||", "|", ">", "<")):
        return False
    read_only = (
        r"git (?:-c \S+ )*(?:status|diff|log|show|rev-parse|ls-files|branch)(?:\s|$)",
        r"(?:pwd|ls|dir|tree)(?:\s|$)",
        r"(?:get-childitem|get-location)(?:\s|$)",
    )
    return any(re.match(pattern, normalized) is not None for pattern in read_only)


# 从结构化报告提取用于状态观测的简短失败摘要
def _report_failure_summary(report: object) -> str:
    if not isinstance(report, dict):
        return "verification report is missing"
    results = report.get("results")
    if not isinstance(results, list):
        return "verification report has no results"
    failures: list[str] = []
    for result in results:
        if not isinstance(result, dict) or result.get("status") in ("passed", "skipped"):
            continue
        tool = str(result.get("tool", "unknown"))
        diagnostics = result.get("diagnostics")
        count = len(diagnostics) if isinstance(diagnostics, list) else 0
        failures.append(f"{tool}: {count} diagnostic(s)")
    return "; ".join(failures) or "verification failed"
