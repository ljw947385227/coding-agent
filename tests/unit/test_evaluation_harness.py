from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from kama_claude.core.bus.events import LlmUsageEvent
from kama_claude.core.config import KamaConfig
from kama_claude.core.events.bus import EventBus
from kama_claude.core.harness import (
    AgentHarness,
    EvaluationCheckSpec,
    EvaluationExpectations,
    EvaluationRunner,
    EvaluationTask,
)
from kama_claude.core.llm.types import LlmResponse, ToolCallBlock
from kama_claude.core.runner import AgentRunner


def _git(repository: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _repository(root: Path) -> Path:
    root.mkdir()
    (root / "README.md").write_text("fixture\n", encoding="utf-8")
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "harness@example.invalid")
    _git(root, "config", "user.name", "Harness Test")
    _git(root, "add", "README.md")
    _git(root, "commit", "-q", "-m", "fixture")
    return root


class _WriteProvider:
    def __init__(self) -> None:
        self.calls = 0

    async def chat(
        self,
        messages: list[dict[str, object]],
        tool_schemas: list[dict[str, object]],
        bus: EventBus,
        run_id: str,
        *,
        step: int = 0,
        system: str | None = None,
    ) -> LlmResponse:
        self.calls += 1
        await bus.publish(
            LlmUsageEvent(
                run_id=run_id,
                input_tokens=10,
                output_tokens=4,
                cache_read_input_tokens=2,
                cache_creation_input_tokens=1,
                ts=datetime.now(UTC).isoformat(),
            )
        )
        if self.calls == 1:
            return LlmResponse(
                stop_reason="tool_use",
                tool_calls=[
                    ToolCallBlock(
                        id="write-1",
                        name="write_file",
                        input={"path": "result.txt", "content": "ok\n"},
                    )
                ],
            )
        return LlmResponse(stop_reason="end_turn", text="done")


class _BlockingProvider:
    async def chat(
        self,
        messages: list[dict[str, object]],
        tool_schemas: list[dict[str, object]],
        bus: EventBus,
        run_id: str,
        *,
        step: int = 0,
        system: str | None = None,
    ) -> LlmResponse:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


class _SandboxInfoProvider:
    # 初始化两步固定响应序列
    def __init__(self) -> None:
        self.calls = 0

    # 先查询 sandbox_info，再输出可被行为评分验证的环境诊断
    async def chat(
        self,
        messages: list[dict[str, object]],
        tool_schemas: list[dict[str, object]],
        bus: EventBus,
        run_id: str,
        *,
        step: int = 0,
        system: str | None = None,
    ) -> LlmResponse:
        self.calls += 1
        if self.calls == 1:
            assert "Execution Environment" in (system or "")
            assert any(tool["name"] == "sandbox_info" for tool in tool_schemas)
            return LlmResponse(
                stop_reason="tool_use",
                tool_calls=[
                    ToolCallBlock(
                        id="sandbox-1",
                        name="sandbox_info",
                        input={"probe": False},
                    )
                ],
            )
        return LlmResponse(
            stop_reason="end_turn",
            text="The sandbox environment is missing pytest; rebuild the image.",
        )


def _task(repository: Path, *, timeout: float = 5.0) -> EvaluationTask:
    return EvaluationTask(
        name="write fixture",
        repository=repository.as_posix(),
        goal="Create result.txt containing ok",
        timeout_seconds=timeout,
        checks=[
            EvaluationCheckSpec(
                command=[
                    sys.executable,
                    "-c",
                    (
                        "from pathlib import Path; "
                        "assert Path('result.txt').read_text(encoding='utf-8') == 'ok\\n'"
                    ),
                ]
            )
        ],
        tool_whitelist=["write_file"],
    )


# 功能：评测任务在独立 worktree 修改代码，执行 oracle，并保留可复现报告和完整 patch
async def test_evaluation_harness_isolates_repository_and_writes_artifacts(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path / "repository")
    output = tmp_path / "evaluations"
    config = KamaConfig()
    config.verification.mode = "off"
    evaluator = EvaluationRunner(
        config,
        output_root=output,
        provider=_WriteProvider(),  # type: ignore[arg-type]
    )

    result = await evaluator.run(_task(repository))

    assert result.status == "success"
    assert result.verification_passed is True
    assert result.changed_files == ["result.txt"]
    assert result.worktree is not None
    assert not Path(result.worktree).exists()
    assert not (repository / "result.txt").exists()
    assert result.patch_file is not None
    assert "result.txt" in Path(result.patch_file).read_text(encoding="utf-8")
    assert result.metrics.steps == 2
    assert result.metrics.tool_calls == 1
    assert result.metrics.tool_failures == 0
    assert result.metrics.input_tokens == 20
    assert result.metrics.output_tokens == 8
    artifact_dir = Path(result.artifact_dir)
    assert (artifact_dir / "request.json").is_file()
    assert (artifact_dir / "metrics.json").is_file()
    assert (artifact_dir / "verification.json").is_file()
    persisted = json.loads((artifact_dir / "result.json").read_text(encoding="utf-8"))
    assert persisted["status"] == "success"


# 功能：任务总超时会取消 Agent、仍执行 oracle，并清理隔离 worktree
async def test_evaluation_harness_times_out_and_cleans_worktree(tmp_path: Path) -> None:
    repository = _repository(tmp_path / "repository")
    config = KamaConfig()
    config.verification.mode = "off"
    evaluator = EvaluationRunner(
        config,
        output_root=tmp_path / "evaluations",
        provider=_BlockingProvider(),  # type: ignore[arg-type]
    )

    result = await evaluator.run(_task(repository, timeout=0.05))

    assert result.status == "timeout"
    assert result.agent_reason == "harness_timeout"
    assert result.worktree is not None
    assert not Path(result.worktree).exists()
    assert result.cleanup_error is None


# 功能：评测 oracle 必须使用非空 argv，拒绝无法复现的空命令
def test_evaluation_check_rejects_empty_command() -> None:
    with pytest.raises(ValidationError):
        EvaluationCheckSpec(command=[])


# 功能：Harness 对 sandbox_info 决策、源码零修改和最终诊断文本执行行为评分
# 设计：用确定性两步 provider 走完整 AgentHarness、事件、worktree、oracle 和 artifact 链路
async def test_evaluation_harness_scores_sandbox_diagnosis(tmp_path: Path) -> None:
    repository = _repository(tmp_path / "repository")
    task = _task(repository)
    task = task.model_copy(
        update={
            "goal": "Diagnose why pytest is unavailable without modifying source files.",
            "tool_whitelist": ["sandbox_info"],
            "checks": [
                EvaluationCheckSpec(
                    command=[sys.executable, "-c", "print('oracle passed')"]
                )
            ],
            "expectations": EvaluationExpectations(
                sandbox_info="required",
                max_sandbox_info_calls=1,
                allow_source_changes=False,
                answer_patterns=["sandbox", "pytest", "rebuild"],
            ),
        }
    )
    config = KamaConfig()
    config.verification.mode = "off"
    evaluator = EvaluationRunner(
        config,
        output_root=tmp_path / "evaluations",
        provider=_SandboxInfoProvider(),  # type: ignore[arg-type]
    )

    result = await evaluator.run(task)

    assert result.status == "success"
    assert result.score is not None and result.score.passed
    assert result.metrics.sandbox_info_calls == 1
    assert result.metrics.tool_calls_by_name == {"sandbox_info": 1}
    artifact_dir = Path(result.artifact_dir)
    assert (artifact_dir / "score.json").is_file()
    trace = json.loads((artifact_dir / "tool_trace.json").read_text(encoding="utf-8"))
    assert trace[0]["tool_name"] == "sandbox_info"


# 功能：生产 Harness 使用新名称，同时保持 AgentRunner 旧导入兼容
# 设计：断言两个公开名称指向同一实现，防止兼容层分叉出第二套运行逻辑
def test_agent_harness_is_canonical_runtime_name() -> None:
    assert AgentRunner is AgentHarness
