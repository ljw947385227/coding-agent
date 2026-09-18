from __future__ import annotations

import os
from pathlib import Path

import pytest

from kama_claude.core.config import ExecutionConfig
from kama_claude.core.sandbox import (
    DockerBackendOptions,
    DockerExecutionBackend,
    ExecutionRequest,
)
from kama_claude.core.sandbox.docker import _resolve_docker_cli
from kama_claude.core.sandbox.info import SandboxInspector

pytestmark = pytest.mark.skipif(
    os.environ.get("KAMA_DOCKER_INTEGRATION") != "1",
    reason="set KAMA_DOCKER_INTEGRATION=1 with a running Docker Engine",
)


# 功能：验证真实 Docker 后端可写挂载工作区，同时遮蔽敏感文件并以非 root 用户执行
# 设计：在临时工作区放置诱饵 secret，容器读取、写文件和打印 uid，联合覆盖挂载语义与身份边界
@pytest.mark.asyncio
async def test_docker_workspace_mask_and_non_root(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("TOKEN=must-not-leak", encoding="utf-8")
    backend = DockerExecutionBackend(DockerBackendOptions())

    result = await backend.run(
        ExecutionRequest(
            argv=(
                "python",
                "-c",
                "import os; from pathlib import Path; "
                "print('secret=' + Path('.env').read_text()); "
                "Path('created.txt').write_text('ok'); print('uid=' + str(os.getuid()))",
            ),
            workspace=tmp_path,
            timeout_seconds=20,
            max_output_bytes=4096,
        )
    )

    assert result.exit_code == 0, result.output
    assert "must-not-leak" not in result.output
    assert "secret=" in result.output
    assert "uid=10001" in result.output
    assert (tmp_path / "created.txt").read_text(encoding="utf-8") == "ok"


# 功能：验证真实 Docker 后端默认禁网且只读根文件系统拒绝越界写入
# 设计：在容器中分别尝试外网 TCP 与根目录写文件，捕获异常并用稳定标记断言强制隔离
@pytest.mark.asyncio
async def test_docker_blocks_network_and_root_write(tmp_path: Path) -> None:
    backend = DockerExecutionBackend(DockerBackendOptions())
    script = (
        "import pathlib,socket; "
        "s=socket.socket(); s.settimeout(1); "
        "\ntry: s.connect(('1.1.1.1', 53)); print('network=open')"
        "\nexcept OSError: print('network=blocked')"
        "\ntry: pathlib.Path('/owned').write_text('x'); print('root=writable')"
        "\nexcept OSError: print('root=readonly')"
    )

    result = await backend.run(
        ExecutionRequest(
            argv=("python", "-c", script),
            workspace=tmp_path,
            timeout_seconds=20,
            max_output_bytes=4096,
        )
    )

    assert result.exit_code == 0, result.output
    assert "network=blocked" in result.output
    assert "root=readonly" in result.output


# 功能：验证真实 Docker 命令超时后会强制删除一次性容器
# 设计：让容器长时间休眠并设置极短预算，再用同一 managed 标签查询确认无残留资源
@pytest.mark.asyncio
async def test_docker_timeout_removes_container(tmp_path: Path) -> None:
    backend = DockerExecutionBackend(DockerBackendOptions())

    result = await backend.run(
        ExecutionRequest(
            argv=("python", "-c", "import time; time.sleep(30)"),
            workspace=tmp_path,
            timeout_seconds=0.5,
            max_output_bytes=4096,
        )
    )
    containers = await backend._host.run(
        ExecutionRequest(
            argv=(
                _resolve_docker_cli("docker"),
                "ps",
                "--all",
                "--filter",
                "label=kama.managed=true",
                "--format",
                "{{.Names}}",
            ),
            workspace=tmp_path,
            timeout_seconds=10,
            max_output_bytes=4096,
        )
    )

    assert result.timed_out
    assert containers.exit_code == 0
    assert not containers.output


# 功能：验证 sandbox_info 能同时报告安全宿主摘要、镜像元数据和真实容器运行时
# 设计：通过同一个 Docker 后端执行探针，断言 Windows 宿主与 Linux 沙箱被明确区分且不读取环境变量
@pytest.mark.asyncio
async def test_docker_sandbox_info_distinguishes_host_and_container(tmp_path: Path) -> None:
    backend = DockerExecutionBackend(DockerBackendOptions())
    config = ExecutionConfig(backend="docker")

    payload = await SandboxInspector(config, backend, tmp_path).inspect(probe=True)

    host = payload["host"]
    observed = payload["observed"]
    image = payload["image"]
    environment = payload["environment"]
    assert isinstance(host, dict) and host["os"] == "windows"
    assert isinstance(observed, dict) and observed["os"] == "linux"
    assert observed["uid"] == 10001
    tools = observed["tools"]
    assert isinstance(tools, dict) and tools["python"] is True
    assert tools["pytest"] is False
    assert isinstance(image, dict) and image["available"] is True
    assert str(image["id"]).startswith("sha256:")
    assert isinstance(environment, dict) and environment["stale"] is None
