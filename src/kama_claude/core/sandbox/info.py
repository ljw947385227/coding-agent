from __future__ import annotations

import hashlib
import json
import platform
import sys
from pathlib import Path

from kama_claude.core.config import ExecutionConfig
from kama_claude.core.sandbox.base import ExecutionBackend
from kama_claude.core.sandbox.docker import DockerExecutionBackend
from kama_claude.core.sandbox.models import ExecutionRequest

_ENVIRONMENT_INPUTS = (
    "Dockerfile",
    "pyproject.toml",
    "uv.lock",
    "requirements.txt",
    "requirements-dev.txt",
    "package.json",
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "Cargo.toml",
    "Cargo.lock",
    "go.mod",
    "go.sum",
    "docker/sandbox/python.Dockerfile",
    "docker/sandbox/python-project.Dockerfile",
)
_PROBE_TOOLS = ("python", "pytest", "ruff", "mypy", "git", "rg", "node", "npm", "cargo", "go")
_MAX_FINGERPRINT_FILE_BYTES = 8 * 1024 * 1024


class SandboxInspector:
    # 保存沙箱配置和执行后端，用于生成安全的环境描述
    def __init__(
        self,
        config: ExecutionConfig,
        backend: ExecutionBackend,
        workspace: Path,
    ) -> None:
        self._config = config
        self._backend = backend
        self._workspace = workspace.resolve()

    # 返回配置、宿主摘要、镜像元数据、运行时探测和环境漂移结果
    async def inspect(self, *, probe: bool = True) -> dict[str, object]:
        fingerprint, inputs = environment_fingerprint(self._workspace)
        payload: dict[str, object] = {
            "backend": self._config.backend,
            "configured": _configured_payload(self._config),
            "host": _host_payload(self._workspace),
            "environment": {
                "fingerprint": fingerprint,
                "inputs": inputs,
            },
            "warnings": [],
        }
        image: dict[str, object] = {}
        if isinstance(self._backend, DockerExecutionBackend):
            image = await self._backend.inspect_image(self._workspace)
            payload["image"] = image
        image_fingerprint = _image_fingerprint(image)
        environment = payload["environment"]
        assert isinstance(environment, dict)
        environment["image_fingerprint"] = image_fingerprint
        environment["stale"] = (
            None if image_fingerprint is None else image_fingerprint != fingerprint
        )
        warnings = payload["warnings"]
        assert isinstance(warnings, list)
        if self._config.backend == "docker":
            warnings.append(
                "Host virtual environments and host environment variables are not inherited."
            )
            if image_fingerprint is None:
                warnings.append(
                    "The image has no kama.environment-fingerprint label; drift is unknown."
                )
            elif image_fingerprint != fingerprint:
                warnings.append(
                    "The project dependency inputs differ from the image fingerprint; "
                    "consider rebuilding the sandbox image."
                )
        if probe:
            payload["observed"] = await self._probe_runtime()
        return payload

    # 在当前执行后端中运行只读 Python 探针，不输出环境变量或密钥
    async def _probe_runtime(self) -> dict[str, object]:
        script = (
            "import json, os, platform, shutil, sys; "
            f"tools={json.dumps(_PROBE_TOOLS)}; "
            "print(json.dumps({'os': platform.system().lower(), "
            "'architecture': platform.machine().lower(), "
            "'python': platform.python_version(), "
            "'python_executable': sys.executable, "
            "'cwd': os.getcwd(), "
            "'uid': os.getuid() if hasattr(os, 'getuid') else None, "
            "'tools': {name: shutil.which(name) is not None for name in tools}}))"
        )
        result = await self._backend.run(
            ExecutionRequest(
                argv=(sys.executable, "-c", script),
                workspace=self._workspace,
                timeout_seconds=20.0,
                max_output_bytes=32 * 1024,
                sensitive_patterns=(),
            )
        )
        if result.exit_code != 0 or result.launch_error is not None or result.timed_out:
            return {
                "available": False,
                "error": result.output or result.launch_error or "sandbox probe failed",
                "timed_out": result.timed_out,
            }
        for line in reversed(result.output.splitlines()):
            try:
                decoded = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(decoded, dict):
                return {"available": True, **decoded}
        return {"available": False, "error": "sandbox probe returned invalid JSON"}


# 生成注入系统提示词的低成本沙箱摘要和调用边界
def sandbox_system_prompt(config: ExecutionConfig, workspace: Path) -> str:
    if config.backend == "local":
        return (
            "\n\n## Execution Environment\n"
            "Commands run directly on the host in the authorized workspace. "
            "Use sandbox_info only when an error suggests a missing executable, dependency, "
            "version mismatch, permission boundary, network restriction, architecture issue, "
            "or resource limit. Do not blame normal assertion, syntax, lint, or type errors on "
            "the environment without evidence."
        )
    docker = config.docker
    fingerprint, _ = environment_fingerprint(workspace)
    return (
        "\n\n## Execution Sandbox\n"
        f"Commands run in an ephemeral Docker container using image {docker.image!r}; "
        f"network={docker.network}, memory={docker.memory_mb}MB, cpus={docker.cpus:g}, "
        f"pids={docker.pids_limit}, workspace=/workspace, root filesystem=read-only, "
        f"project_environment_fingerprint={fingerprint}. Host virtual environments and secrets "
        "are not inherited. Call sandbox_info when evidence suggests a missing executable, "
        "dependency, version mismatch, permission boundary, network restriction, architecture "
        "issue, resource limit, or stale image. When sandbox_info is available, use it before "
        "changing source code for a suspected environment problem. Do not call it for ordinary "
        "assertion, syntax, "
        "lint, or type errors without environment evidence. Never claim the image was rebuilt "
        "unless an approved build operation actually completed."
    )


# 计算有限组依赖与镜像输入文件的稳定 SHA-256 指纹
def environment_fingerprint(workspace: Path) -> tuple[str, list[str]]:
    digest = hashlib.sha256()
    inputs: list[str] = []
    for relative in _ENVIRONMENT_INPUTS:
        candidate = workspace / relative
        if candidate.is_symlink() or not candidate.is_file():
            continue
        inputs.append(relative)
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        try:
            size = candidate.stat().st_size
            if size > _MAX_FINGERPRINT_FILE_BYTES:
                digest.update(f"oversized:{size}".encode())
            else:
                digest.update(candidate.read_bytes())
        except OSError as exc:
            digest.update(f"unreadable:{type(exc).__name__}".encode())
        digest.update(b"\0")
    return f"sha256:{digest.hexdigest()}", inputs


# 返回不会泄漏环境变量和用户数据的宿主基本信息
def _host_payload(workspace: Path) -> dict[str, object]:
    return {
        "os": platform.system().lower(),
        "architecture": platform.machine().lower(),
        "python": platform.python_version(),
        "workspace": workspace.as_posix(),
    }


# 将运行配置投影为可安全展示给 Agent 的字段
def _configured_payload(config: ExecutionConfig) -> dict[str, object]:
    if config.backend == "local":
        return {"workspace": "host", "ephemeral": False}
    docker = config.docker
    return {
        "image": docker.image,
        "network": docker.network,
        "memory_mb": docker.memory_mb,
        "cpus": docker.cpus,
        "pids_limit": docker.pids_limit,
        "tmpfs_mb": docker.tmpfs_mb,
        "user": docker.user,
        "workspace": "/workspace",
        "root_filesystem": "read-only",
        "ephemeral": True,
    }


# 从经过筛选的镜像标签中提取构建时环境指纹
def _image_fingerprint(image: dict[str, object]) -> str | None:
    labels = image.get("labels")
    if not isinstance(labels, dict):
        return None
    value = labels.get("kama.environment-fingerprint")
    return value if isinstance(value, str) and value.startswith("sha256:") else None
