from __future__ import annotations

from kama_claude.core.config import ExecutionConfig
from kama_claude.core.sandbox.base import ExecutionBackend
from kama_claude.core.sandbox.docker import DockerExecutionBackend
from kama_claude.core.sandbox.local import LocalExecutionBackend
from kama_claude.core.sandbox.models import DockerBackendOptions


# 根据配置创建本地或 Docker 命令执行后端
def create_execution_backend(config: ExecutionConfig) -> ExecutionBackend:
    if config.backend == "local":
        return LocalExecutionBackend()
    docker = config.docker
    return DockerExecutionBackend(
        DockerBackendOptions(
            cli=docker.cli,
            image=docker.image,
            network=docker.network,
            memory_mb=docker.memory_mb,
            cpus=docker.cpus,
            pids_limit=docker.pids_limit,
            tmpfs_mb=docker.tmpfs_mb,
            user=docker.user,
        )
    )
