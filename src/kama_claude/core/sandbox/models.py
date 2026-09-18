from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

NetworkMode = Literal["none", "bridge"]


@dataclass(frozen=True, slots=True)
class ExecutionRequest:
    workspace: Path
    timeout_seconds: float
    max_output_bytes: int
    argv: tuple[str, ...] = ()
    shell_command: str | None = None
    environment: dict[str, str] = field(default_factory=dict)
    sensitive_patterns: tuple[str, ...] = ()

    # 校验请求只选择 argv 或 shell_command 中的一种执行形式
    def __post_init__(self) -> None:
        if bool(self.argv) == bool(self.shell_command):
            raise ValueError("exactly one of argv or shell_command must be provided")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if self.max_output_bytes <= 0:
            raise ValueError("max_output_bytes must be positive")


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    exit_code: int | None
    output: str
    duration_ms: int
    backend: str
    timed_out: bool = False
    cancelled: bool = False
    oom_killed: bool = False
    output_truncated: bool = False
    launch_error: str | None = None
    cleanup_error: str | None = None


@dataclass(frozen=True, slots=True)
class DockerBackendOptions:
    cli: str = "docker"
    image: str = "kama-sandbox-python:3.12-v1"
    network: NetworkMode = "none"
    memory_mb: int = 512
    cpus: float = 1.0
    pids_limit: int = 64
    tmpfs_mb: int = 256
    user: str = "10001:10001"
