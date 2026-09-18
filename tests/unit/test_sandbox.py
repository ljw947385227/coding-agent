from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from kama_claude.core.config import ExecutionConfig, get_config
from kama_claude.core.sandbox import (
    DockerBackendOptions,
    DockerExecutionBackend,
    ExecutionRequest,
    ExecutionResult,
    LocalExecutionBackend,
)
from kama_claude.core.sandbox.diagnostics import sandbox_failure_hint
from kama_claude.core.sandbox.docker import _container_command, _prepare_mask_mounts
from kama_claude.core.sandbox.info import environment_fingerprint, sandbox_system_prompt
from kama_claude.core.tools.builtin.bash import BashTool
from kama_claude.core.tools.builtin.sandbox_info import SandboxInfoTool
from kama_claude.core.verification.model import VerificationCheck, VerificationPlan
from kama_claude.core.verification.runner import VerificationRunner


class StubExecutionBackend:
    # 初始化固定结果并记录最后一次执行请求
    def __init__(self, result: ExecutionResult) -> None:
        self.result = result
        self.request: ExecutionRequest | None = None

    # 保存执行请求并返回预设结果
    async def run(self, request: ExecutionRequest) -> ExecutionResult:
        self.request = request
        return self.result


# 功能：验证本地后端以 argv 形式在指定工作区执行并返回合并输出
# 设计：用当前 Python 解释器避免依赖 PATH，同时打印 cwd 和 stderr 覆盖工作区及流合并语义
@pytest.mark.asyncio
async def test_local_backend_executes_in_workspace(tmp_path: Path) -> None:
    backend = LocalExecutionBackend()

    result = await backend.run(
        ExecutionRequest(
            argv=(
                sys.executable,
                "-c",
                "import pathlib,sys; print(pathlib.Path.cwd()); print('err', file=sys.stderr)",
            ),
            workspace=tmp_path,
            timeout_seconds=5,
            max_output_bytes=4096,
        )
    )

    assert result.exit_code == 0
    assert tmp_path.resolve().as_posix().lower() in result.output.replace("\\", "/").lower()
    assert "err" in result.output


# 功能：验证本地后端超时后终止进程并返回结构化超时状态
# 设计：短预算运行长休眠命令，不依赖异常文本，直接断言 timed_out 与非成功退出
@pytest.mark.asyncio
async def test_local_backend_times_out(tmp_path: Path) -> None:
    result = await LocalExecutionBackend().run(
        ExecutionRequest(
            argv=(sys.executable, "-c", "import time; time.sleep(5)"),
            workspace=tmp_path,
            timeout_seconds=0.05,
            max_output_bytes=1024,
        )
    )

    assert result.timed_out
    assert result.exit_code != 0


# 功能：验证超量输出会被有界截断且同时保留头尾诊断信息
# 设计：生成带明确首尾标记的大输出，用很小字节预算锁定内存边界与诊断可用性
@pytest.mark.asyncio
async def test_local_backend_bounds_output(tmp_path: Path) -> None:
    result = await LocalExecutionBackend().run(
        ExecutionRequest(
            argv=(sys.executable, "-c", "print('HEAD' + 'x' * 4000 + 'TAIL')"),
            workspace=tmp_path,
            timeout_seconds=5,
            max_output_bytes=128,
        )
    )

    assert result.output_truncated
    assert "HEAD" in result.output
    assert "TAIL" in result.output
    assert "output truncated" in result.output


# 功能：验证 Docker 创建参数固定包含关键隔离边界且模型命令不能覆盖它们
# 设计：直接检查 argv 而不启动 Docker，稳定覆盖无网络、只读根、降权、资源限制和工作区挂载
def test_docker_create_argv_contains_security_boundaries(tmp_path: Path) -> None:
    backend = DockerExecutionBackend(DockerBackendOptions(image="sandbox:test"))
    request = ExecutionRequest(
        shell_command="echo hello",
        workspace=tmp_path,
        timeout_seconds=5,
        max_output_bytes=1024,
    )

    argv = backend._build_create_argv("docker", "kama-test", request, ())

    assert ("--network", "none") == _option_pair(argv, "--network")
    assert "--read-only" in argv
    assert ("--cap-drop", "ALL") == _option_pair(argv, "--cap-drop")
    assert ("--security-opt", "no-new-privileges") == _option_pair(
        argv, "--security-opt"
    )
    assert ("--pids-limit", "64") == _option_pair(argv, "--pids-limit")
    assert argv[-4:] == ("sandbox:test", "/bin/sh", "-lc", "echo hello")


# 功能：验证宿主 uv 与 Python 命令会映射为沙箱镜像内可用的等价命令
# 设计：分别覆盖 uv run、uv build 和 Windows Python 路径，避免把宿主绝对路径泄漏进容器
def test_container_command_maps_host_tooling(tmp_path: Path) -> None:
    common = {"workspace": tmp_path, "timeout_seconds": 5, "max_output_bytes": 1024}

    assert _container_command(ExecutionRequest(argv=("uv", "run", "pytest"), **common)) == (
        "pytest",
    )
    assert _container_command(ExecutionRequest(argv=("uv", "build"), **common)) == (
        "python",
        "-m",
        "build",
    )
    assert _container_command(
        ExecutionRequest(argv=(r"C:\Python312\python.exe", "-m", "pytest"), **common)
    ) == ("python", "-m", "pytest")


# 功能：验证敏感文件和目录会转换为只读空覆盖挂载
# 设计：创建默认规则与用户规则两类目标，检查容器目标路径而不依赖临时源路径名称
def test_sensitive_paths_are_masked(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    masks = tmp_path / "masks"
    workspace.mkdir()
    masks.mkdir()
    (workspace / ".env").write_text("TOKEN=secret", encoding="utf-8")
    (workspace / ".git").mkdir()
    (workspace / "private.json").write_text("{}", encoding="utf-8")

    mounts = _prepare_mask_mounts(
        workspace,
        masks,
        (".env", ".git", "private.json"),
    )

    assert any("dst=/workspace/.env" in mount and "readonly" in mount for mount in mounts)
    assert any("dst=/workspace/.git" in mount and "readonly" in mount for mount in mounts)
    assert any("dst=/workspace/private.json" in mount for mount in mounts)


# 功能：验证 BashTool 将命令、超时和敏感规则交给注入的执行后端
# 设计：用记录请求的 stub 隔离真实进程，直接检查 Agent 工具到沙箱抽象的接线
@pytest.mark.asyncio
async def test_bash_tool_uses_injected_backend(tmp_path: Path) -> None:
    backend = StubExecutionBackend(
        ExecutionResult(exit_code=0, output="ok", duration_ms=1, backend="stub")
    )
    tool = BashTool(
        workspace_root=tmp_path,
        execution_backend=backend,
        ignore_files=("secret.json",),
    )

    result = await tool.invoke({"command": "echo ok", "timeout": 7})

    assert result.content == "ok"
    assert backend.request is not None
    assert backend.request.shell_command == "echo ok"
    assert backend.request.timeout_seconds == 7
    assert backend.request.sensitive_patterns == ("secret.json",)


# 功能：验证 VerificationRunner 使用同一后端并将沙箱失败映射为验证失败
# 设计：返回固定非零退出结果，既检查 argv 请求传播也检查统一 VerificationResult 状态
@pytest.mark.asyncio
async def test_verification_runner_uses_injected_backend(tmp_path: Path) -> None:
    backend = StubExecutionBackend(
        ExecutionResult(exit_code=2, output="failed", duration_ms=1, backend="stub")
    )
    plan = VerificationPlan(
        root=str(tmp_path),
        ecosystems=("python",),
        checks=(
            VerificationCheck(
                kind="test",
                ecosystem="python",
                command=("pytest", "-q"),
                source="test",
                tool="pytest",
            ),
        ),
    )

    report = await VerificationRunner(execution_backend=backend).run(plan)

    assert not report.passed
    assert report.results[0].status == "failed"
    assert backend.request is not None
    assert backend.request.argv == ("pytest", "-q")


# 功能：验证 Docker 沙箱的 TOML 配置可以完整加载并保留类型
# 设计：一次设置后端、镜像、网络和资源字段，覆盖嵌套表白名单及数值转换
def test_docker_execution_toml_config_loaded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        '[execution]\nbackend = "docker"\n'
        '[execution.docker]\nimage = "sandbox:test"\nnetwork = "bridge"\n'
        "memory_mb = 768\ncpus = 1.5\npids_limit = 80\ntmpfs_mb = 128\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("KAMA_CONFIG", str(config_file))
    monkeypatch.delenv("KAMA_EXECUTION_BACKEND", raising=False)

    config = get_config()

    assert config.execution.backend == "docker"
    assert config.execution.docker.image == "sandbox:test"
    assert config.execution.docker.network == "bridge"
    assert config.execution.docker.memory_mb == 768
    assert config.execution.docker.cpus == 1.5
    assert config.execution.docker.pids_limit == 80
    assert config.execution.docker.tmpfs_mb == 128


# 功能：验证 Docker 沙箱关键字段可以被最高优先级环境变量覆盖
# 设计：从空目录设置后端、镜像与资源变量，锁定部署时无需修改配置文件的入口
def test_docker_execution_environment_overrides(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("KAMA_CONFIG", raising=False)
    monkeypatch.setenv("KAMA_EXECUTION_BACKEND", "docker")
    monkeypatch.setenv("KAMA_DOCKER_IMAGE", "sandbox:env")
    monkeypatch.setenv("KAMA_DOCKER_MEMORY_MB", "1024")
    monkeypatch.setenv("KAMA_DOCKER_CPUS", "2")

    config = get_config()

    assert config.execution.backend == "docker"
    assert config.execution.docker.image == "sandbox:env"
    assert config.execution.docker.memory_mb == 1024
    assert config.execution.docker.cpus == 2.0


# 功能：验证非法 Docker 网络模式在启动阶段被清晰拒绝
# 设计：通过环境变量注入任意字符串，确保危险配置不会被原样传递给 Docker CLI
def test_invalid_docker_network_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("KAMA_CONFIG", raising=False)
    monkeypatch.setenv("KAMA_DOCKER_NETWORK", "host")

    with pytest.raises(SystemExit, match="KAMA_DOCKER_NETWORK"):
        get_config()


# 功能：验证 sandbox_info 使用实际后端探针并只返回脱敏环境信息
# 设计：用固定 JSON 的 stub 后端避免依赖 Docker，检查配置、宿主、探针和环境指纹四个区块
@pytest.mark.asyncio
async def test_sandbox_info_returns_safe_runtime_description(tmp_path: Path) -> None:
    backend = StubExecutionBackend(
        ExecutionResult(
            exit_code=0,
            output=json.dumps(
                {
                    "os": "linux",
                    "architecture": "amd64",
                    "python": "3.12.0",
                    "python_executable": "/usr/bin/python",
                    "cwd": "/workspace",
                    "uid": 10001,
                    "tools": {"python": True, "pytest": False},
                }
            ),
            duration_ms=1,
            backend="stub",
        )
    )
    tool = SandboxInfoTool(ExecutionConfig(), backend, tmp_path)

    result = await tool.invoke({"probe": True})
    payload = json.loads(result.content)

    assert not result.is_error
    assert payload["backend"] == "local"
    assert payload["observed"]["uid"] == 10001
    assert "environment" in payload
    assert "environment_variables" not in result.content


# 功能：验证依赖输入变化会改变环境指纹并进入 Docker 启动摘要
# 设计：连续修改同一个 lockfile，比较两次哈希并检查摘要包含镜像与新指纹
def test_sandbox_fingerprint_tracks_dependency_inputs(tmp_path: Path) -> None:
    lockfile = tmp_path / "uv.lock"
    lockfile.write_text("version=1", encoding="utf-8")
    first, inputs = environment_fingerprint(tmp_path)
    lockfile.write_text("version=2", encoding="utf-8")
    second, _ = environment_fingerprint(tmp_path)
    config = ExecutionConfig(backend="docker")

    prompt = sandbox_system_prompt(config, tmp_path)

    assert inputs == ["uv.lock"]
    assert first != second
    assert config.docker.image in prompt
    assert second in prompt


# 功能：验证环境提示只覆盖缺依赖等环境证据而不干扰普通断言失败
# 设计：对 ModuleNotFoundError 和 AssertionError 构造同退出码结果，隔离验证文本分类边界
def test_sandbox_failure_hint_distinguishes_environment_errors() -> None:
    missing = ExecutionResult(
        exit_code=1,
        output="ModuleNotFoundError: No module named 'pytest'",
        duration_ms=1,
        backend="docker",
    )
    assertion = ExecutionResult(
        exit_code=1,
        output="AssertionError: expected 2 but got 1",
        duration_ms=1,
        backend="docker",
    )

    assert sandbox_failure_hint(missing) is not None
    assert sandbox_failure_hint(assertion) is None


# 返回 argv 中给定选项及其紧随值，便于安全参数断言
def _option_pair(argv: tuple[str, ...], option: str) -> tuple[str, str]:
    index = argv.index(option)
    return argv[index], argv[index + 1]
