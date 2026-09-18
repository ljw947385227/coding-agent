from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from kama_claude.core.verification.model import VerificationKind


class EvaluationCheckSpec(BaseModel):
    """An oracle command executed without a shell after the agent finishes."""

    model_config = ConfigDict(extra="forbid")

    kind: VerificationKind = "test"
    ecosystem: str = "custom"
    tool: str = "unknown"
    command: list[str] = Field(min_length=1)

    @field_validator("command")
    @classmethod
    def _non_empty_arguments(cls, value: list[str]) -> list[str]:
        if any(not argument for argument in value):
            raise ValueError("command arguments must not be empty")
        return value


class EvaluationSandboxSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    backend: Literal["local", "docker"] = "local"
    image: str | None = None
    network: Literal["none", "bridge"] = "none"
    memory_mb: int | None = Field(default=None, gt=0)
    cpus: float | None = Field(default=None, gt=0)
    pids_limit: int | None = Field(default=None, gt=0)
    tmpfs_mb: int | None = Field(default=None, gt=0)


class EvaluationExpectations(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sandbox_info: Literal["required", "forbidden", "optional"] = "optional"
    max_sandbox_info_calls: int = Field(default=1, ge=0)
    allow_source_changes: bool = True
    required_tools: list[str] = Field(default_factory=list)
    forbidden_tools: list[str] = Field(default_factory=list)
    answer_patterns: list[str] = Field(default_factory=list)


class EvaluationTask(BaseModel):
    """A reproducible coding-agent task pinned to a Git revision."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    repository: str = Field(min_length=1)
    goal: str = Field(min_length=1)
    base_ref: str = "HEAD"
    checks: list[EvaluationCheckSpec] = Field(min_length=1)
    timeout_seconds: float = Field(default=900.0, gt=0)
    verification_timeout_seconds: float = Field(default=300.0, gt=0)
    max_steps: int | None = Field(default=None, gt=0)
    model: str | None = None
    system_prompt: str | None = None
    tool_whitelist: list[str] | None = None
    sandbox: EvaluationSandboxSpec | None = None
    oracle_backend: Literal["local", "sandbox"] = "local"
    expectations: EvaluationExpectations | None = None


class EvaluationSuite(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tasks: list[EvaluationTask] = Field(min_length=1)


class EvaluationMetrics(BaseModel):
    duration_ms: int = 0
    steps: int = 0
    tool_calls: int = 0
    tool_failures: int = 0
    permission_requests: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    sandbox_info_calls: int = 0
    tool_calls_by_name: dict[str, int] = Field(default_factory=dict)


class EvaluationScore(BaseModel):
    passed: bool
    failures: list[str] = Field(default_factory=list)
    sandbox_info_calls: int = 0
    matched_answer_patterns: list[str] = Field(default_factory=list)


class EvaluationResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_name: str
    run_id: str
    status: Literal["success", "failed", "timeout", "infrastructure_error"]
    reason: str | None = None
    agent_status: str | None = None
    agent_reason: str | None = None
    answer: str = ""
    repository: str
    base_ref: str
    base_commit: str | None = None
    worktree: str | None = None
    worktree_kept: bool = False
    cleanup_error: str | None = None
    verification_passed: bool | None = None
    changed_files: list[str] = Field(default_factory=list)
    patch_file: str | None = None
    patch_truncated: bool = False
    artifact_dir: str
    metrics: EvaluationMetrics = Field(default_factory=EvaluationMetrics)
    score: EvaluationScore | None = None
