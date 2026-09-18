from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import tempfile
import time
import uuid
from pathlib import Path

from kama_claude.core.sandbox.errors import SandboxConfigurationError
from kama_claude.core.sandbox.local import LocalExecutionBackend
from kama_claude.core.sandbox.models import (
    DockerBackendOptions,
    ExecutionRequest,
    ExecutionResult,
)
from kama_claude.core.tools.file_filter import is_ignored_file

_DEFAULT_SENSITIVE_PATTERNS = (
    ".env",
    ".env.*",
    "*.pem",
    "*.key",
    "secrets.json",
    ".git",
    ".venv",
)
_MAX_MASK_TARGETS = 128
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class DockerExecutionBackend:
    # 初始化使用本地 Docker Engine 的受限 Linux 容器执行后端
    def __init__(self, options: DockerBackendOptions) -> None:
        self._options = options
        self._host = LocalExecutionBackend()

    # 创建一次性容器执行请求，并在超时、取消或正常结束后强制清理
    async def run(self, request: ExecutionRequest) -> ExecutionResult:
        started = time.monotonic()
        workspace = request.workspace.resolve()
        if not workspace.is_dir():
            message = f"sandbox workspace is not a directory: {workspace}"
            return _launch_failure(message, started)
        try:
            docker_cli = _resolve_docker_cli(self._options.cli)
            _validate_mount_path(workspace)
        except SandboxConfigurationError as exc:
            return _launch_failure(str(exc), started)

        container_name = f"kama-{uuid.uuid4().hex[:16]}"
        with tempfile.TemporaryDirectory(prefix="kama-sandbox-mask-") as temporary:
            try:
                mask_mounts = _prepare_mask_mounts(
                    workspace,
                    Path(temporary),
                    (*_DEFAULT_SENSITIVE_PATTERNS, *request.sensitive_patterns),
                )
                create_argv = self._build_create_argv(
                    docker_cli,
                    container_name,
                    request,
                    mask_mounts,
                )
            except (OSError, SandboxConfigurationError) as exc:
                return _launch_failure(str(exc), started)

            cleanup_done = False
            try:
                create_result = await self._run_host_command(
                    create_argv,
                    workspace,
                    timeout_seconds=min(60.0, request.timeout_seconds),
                    max_output_bytes=min(request.max_output_bytes, 64 * 1024),
                )
                if create_result.exit_code != 0 or create_result.timed_out:
                    message = create_result.output or "docker create failed"
                    return ExecutionResult(
                        exit_code=create_result.exit_code,
                        output=message,
                        duration_ms=_elapsed_ms(started),
                        backend="docker",
                        timed_out=create_result.timed_out,
                        output_truncated=create_result.output_truncated,
                        launch_error="docker create failed",
                    )
                start_result = await self._run_host_command(
                    (docker_cli, "start", "--attach", container_name),
                    workspace,
                    timeout_seconds=request.timeout_seconds,
                    max_output_bytes=request.max_output_bytes,
                )
                if start_result.timed_out:
                    result = ExecutionResult(
                        exit_code=None,
                        output=start_result.output,
                        duration_ms=_elapsed_ms(started),
                        backend="docker",
                        timed_out=True,
                        output_truncated=start_result.output_truncated,
                    )
                else:
                    state = await self._inspect_state(docker_cli, container_name, workspace)
                    exit_code = _integer_or_none(state.get("ExitCode"))
                    oom_killed = state.get("OOMKilled") is True
                    state_error = state.get("Error")
                    output = start_result.output
                    if isinstance(state_error, str) and state_error and not output:
                        output = state_error
                    result = ExecutionResult(
                        exit_code=exit_code,
                        output=output,
                        duration_ms=_elapsed_ms(started),
                        backend="docker",
                        oom_killed=oom_killed,
                        output_truncated=start_result.output_truncated,
                    )
            except asyncio.CancelledError:
                await asyncio.shield(
                    self._remove_container(docker_cli, container_name, workspace)
                )
                cleanup_done = True
                raise
            finally:
                if not cleanup_done:
                    cleanup_error = await self._remove_container(
                        docker_cli,
                        container_name,
                        workspace,
                    )
            return ExecutionResult(
                exit_code=result.exit_code,
                output=result.output,
                duration_ms=result.duration_ms,
                backend=result.backend,
                timed_out=result.timed_out,
                cancelled=result.cancelled,
                oom_killed=result.oom_killed,
                output_truncated=result.output_truncated,
                launch_error=result.launch_error,
                cleanup_error=cleanup_error,
            )

    # 查询镜像的安全元数据，仅返回 ID、平台、默认用户和 Kama 标签
    async def inspect_image(self, workspace: Path) -> dict[str, object]:
        started = time.monotonic()
        try:
            docker_cli = _resolve_docker_cli(self._options.cli)
        except SandboxConfigurationError as exc:
            return {"available": False, "error": str(exc)}
        result = await self._run_host_command(
            (docker_cli, "image", "inspect", self._options.image),
            workspace,
            timeout_seconds=15.0,
            max_output_bytes=512 * 1024,
        )
        if result.exit_code != 0:
            return {
                "available": False,
                "error": result.output or "docker image inspect failed",
                "duration_ms": _elapsed_ms(started),
            }
        try:
            decoded = json.loads(result.output)
        except json.JSONDecodeError:
            return {"available": False, "error": "docker image inspect returned invalid JSON"}
        if not isinstance(decoded, list) or not decoded or not isinstance(decoded[0], dict):
            return {"available": False, "error": "docker image inspect returned no image"}
        raw = decoded[0]
        raw_config = raw.get("Config")
        image_config = raw_config if isinstance(raw_config, dict) else {}
        raw_labels = image_config.get("Labels")
        labels = raw_labels if isinstance(raw_labels, dict) else {}
        kama_labels = {
            str(key): str(value)
            for key, value in labels.items()
            if str(key).startswith("kama.")
        }
        return {
            "available": True,
            "id": raw.get("Id"),
            "os": raw.get("Os"),
            "architecture": raw.get("Architecture"),
            "user": image_config.get("User"),
            "labels": kama_labels,
            "duration_ms": _elapsed_ms(started),
        }

    # 构造不允许模型控制安全边界参数的 docker create argv
    def _build_create_argv(
        self,
        docker_cli: str,
        container_name: str,
        request: ExecutionRequest,
        mask_mounts: tuple[str, ...],
    ) -> tuple[str, ...]:
        workspace_mount = _mount_spec(request.workspace.resolve(), "/workspace")
        argv = [
            docker_cli,
            "create",
            "--name",
            container_name,
            "--label",
            "kama.managed=true",
            "--label",
            f"kama.container={container_name}",
            "--network",
            self._options.network,
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--pids-limit",
            str(self._options.pids_limit),
            "--memory",
            f"{self._options.memory_mb}m",
            "--cpus",
            str(self._options.cpus),
            "--user",
            _container_user(self._options.user),
            "--init",
            "--stop-timeout",
            "1",
            "--workdir",
            "/workspace",
            "--mount",
            workspace_mount,
            "--tmpfs",
            f"/tmp:rw,nosuid,nodev,size={self._options.tmpfs_mb}m",
        ]
        for mount in mask_mounts:
            argv.extend(("--mount", mount))
        environment = {
            "CI": "true",
            "HOME": "/tmp",
            "XDG_CACHE_HOME": "/tmp/cache",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONUNBUFFERED": "1",
            "PYTHONPATH": "/workspace/src",
            **request.environment,
        }
        for name, value in sorted(environment.items()):
            if _ENV_NAME.fullmatch(name) is None or "\x00" in value:
                raise SandboxConfigurationError(f"invalid sandbox environment variable: {name}")
            argv.extend(("--env", f"{name}={value}"))
        argv.append(self._options.image)
        argv.extend(_container_command(request))
        return tuple(argv)

    # 读取停止容器的实际退出状态和 OOM 标记
    async def _inspect_state(
        self,
        docker_cli: str,
        container_name: str,
        workspace: Path,
    ) -> dict[str, object]:
        result = await self._run_host_command(
            (
                docker_cli,
                "inspect",
                "--format",
                "{{json .State}}",
                container_name,
            ),
            workspace,
            timeout_seconds=10.0,
            max_output_bytes=16 * 1024,
        )
        if result.exit_code != 0:
            return {}
        for line in reversed(result.output.splitlines()):
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                return {str(key): value for key, value in payload.items()}
        return {}

    # 强制删除容器并返回无法清理时的诊断文本
    async def _remove_container(
        self,
        docker_cli: str,
        container_name: str,
        workspace: Path,
    ) -> str | None:
        result = await self._run_host_command(
            (docker_cli, "rm", "--force", container_name),
            workspace,
            timeout_seconds=10.0,
            max_output_bytes=16 * 1024,
        )
        if result.exit_code == 0 or "No such container" in result.output:
            return None
        return result.output or f"failed to remove container {container_name}"

    # 在宿主机通过 argv 方式调用受信任的 Docker CLI
    async def _run_host_command(
        self,
        argv: tuple[str, ...],
        workspace: Path,
        *,
        timeout_seconds: float,
        max_output_bytes: int,
    ) -> ExecutionResult:
        return await self._host.run(
            ExecutionRequest(
                argv=argv,
                workspace=workspace,
                timeout_seconds=timeout_seconds,
                max_output_bytes=max_output_bytes,
            )
        )


# 查找 Docker CLI，兼容 Windows Docker Desktop 未刷新 PATH 的情况
def _resolve_docker_cli(configured: str) -> str:
    expanded = Path(configured).expanduser()
    if expanded.is_absolute():
        if expanded.is_file():
            return str(expanded)
        raise SandboxConfigurationError(f"Docker CLI does not exist: {expanded}")
    discovered = shutil.which(configured)
    if discovered is not None:
        return discovered
    if os.name == "nt" and configured == "docker":
        installed = Path(r"C:\Program Files\Docker\Docker\resources\bin\docker.exe")
        if installed.is_file():
            return str(installed)
    raise SandboxConfigurationError(
        "Docker CLI was not found; install Docker Desktop or configure execution.docker.cli"
    )


# 将宿主机验证命令转换为容器内可用的等价 argv
def _container_command(request: ExecutionRequest) -> tuple[str, ...]:
    if request.shell_command is not None:
        return ("/bin/sh", "-lc", request.shell_command)
    argv = request.argv
    if len(argv) >= 2 and argv[:2] == ("uv", "run"):
        return argv[2:]
    if len(argv) >= 2 and argv[:2] == ("uv", "build"):
        return ("python", "-m", "build", *argv[2:])
    if argv and Path(argv[0]).name.lower() in ("python", "python.exe"):
        return ("python", *argv[1:])
    return argv


# 枚举敏感路径并为其创建空文件或空目录覆盖挂载
def _prepare_mask_mounts(
    workspace: Path,
    temporary: Path,
    patterns: tuple[str, ...],
) -> tuple[str, ...]:
    empty_file = temporary / "empty-file"
    empty_dir = temporary / "empty-dir"
    empty_file.touch()
    empty_dir.mkdir()
    targets: list[tuple[Path, bool]] = []
    for current, dirnames, filenames in os.walk(workspace, followlinks=False):
        current_path = Path(current)
        for dirname in list(dirnames):
            candidate = current_path / dirname
            if is_ignored_file(candidate, patterns, root=workspace):
                targets.append((candidate, True))
                dirnames.remove(dirname)
        for filename in filenames:
            candidate = current_path / filename
            if is_ignored_file(candidate, patterns, root=workspace):
                targets.append((candidate, False))
        if len(targets) > _MAX_MASK_TARGETS:
            raise SandboxConfigurationError(
                f"sandbox sensitive path mask exceeds {_MAX_MASK_TARGETS} targets"
            )
    mounts: list[str] = []
    for target, is_directory in sorted(targets, key=lambda item: item[0].as_posix()):
        relative = target.relative_to(workspace).as_posix()
        if "," in relative:
            raise SandboxConfigurationError(
                f"sandbox cannot safely mask a path containing a comma: {relative}"
            )
        source = empty_dir if is_directory else empty_file
        mounts.append(_mount_spec(source, f"/workspace/{relative}", read_only=True))
    return tuple(mounts)


# 构造 Docker --mount 参数并拒绝会破坏逗号分隔语法的路径
def _mount_spec(source: Path, target: str, *, read_only: bool = False) -> str:
    _validate_mount_path(source)
    if "," in target:
        raise SandboxConfigurationError(f"Docker mount target contains a comma: {target}")
    fields = ["type=bind", f"src={source}", f"dst={target}"]
    if read_only:
        fields.append("readonly")
    return ",".join(fields)


# 校验宿主机挂载源可安全编码为 Docker --mount 参数
def _validate_mount_path(path: Path) -> None:
    if "," in str(path):
        raise SandboxConfigurationError(f"Docker mount source contains a comma: {path}")


# 在 Linux 主机匹配当前用户，在 Windows Docker Desktop 使用镜像内非 root 用户
def _container_user(configured: str) -> str:
    if os.name != "nt" and hasattr(os, "getuid") and hasattr(os, "getgid"):
        getuid = getattr(os, "getuid")
        getgid = getattr(os, "getgid")
        return f"{getuid()}:{getgid()}"
    return configured


# 将未知对象安全转换为 Docker 状态中的整数退出码
def _integer_or_none(value: object) -> int | None:
    return value if isinstance(value, int) else None


# 构造 Docker 启动前失败的稳定结果
def _launch_failure(message: str, started: float) -> ExecutionResult:
    return ExecutionResult(
        exit_code=None,
        output=message,
        duration_ms=_elapsed_ms(started),
        backend="docker",
        launch_error=message,
    )


# 返回 Docker 请求从起点到当前的整数毫秒数
def _elapsed_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)
