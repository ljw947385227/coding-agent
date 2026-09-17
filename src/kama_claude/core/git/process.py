from __future__ import annotations

import asyncio
import os
import shutil
import signal
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

_STDERR_LIMIT = 64 * 1024
_READ_CHUNK = 64 * 1024


@dataclass(slots=True)
class GitCommandResult:
    stdout: bytes
    stderr: bytes
    returncode: int
    truncated: bool = False
    timed_out: bool = False


# 校验 Git 工具接收的目录路径并拒绝父目录跳转
def validate_directory(path_text: str) -> Path:
    path = Path(path_text)
    if ".." in path.parts:
        raise PermissionError(f"path traversal not allowed: {path_text}")
    if not path.exists():
        raise FileNotFoundError(f"no such path: {path_text}")
    if not path.is_dir():
        raise NotADirectoryError(f"not a directory: {path_text}")
    return path


# 运行 Git 子命令并对输出、超时和取消进行有界处理
async def run_git(
    cwd: Path,
    arguments: list[str],
    *,
    max_stdout_bytes: int,
    timeout: float = 10.0,
    env: Mapping[str, str] | None = None,
) -> GitCommandResult:
    executable = shutil.which("git")
    if executable is None:
        return GitCommandResult(b"", b"git executable not found", 127)
    process_env = os.environ.copy()
    if env is not None:
        process_env.update(env)
    try:
        if os.name == "nt":
            process = await asyncio.create_subprocess_exec(
                executable,
                "-C",
                str(cwd),
                *arguments,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=process_env,
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
            )
        else:
            process = await asyncio.create_subprocess_exec(
                executable,
                "-C",
                str(cwd),
                *arguments,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=process_env,
                start_new_session=True,
            )
    except OSError as exc:
        return GitCommandResult(b"", str(exc).encode("utf-8", errors="replace"), 127)

    if process.stdout is None or process.stderr is None:
        await _stop_process(process)
        return GitCommandResult(b"", b"failed to open git output pipes", 127)

    overflow = asyncio.Event()
    stdout_task = asyncio.create_task(_read_limited(process.stdout, max_stdout_bytes, overflow))
    stderr_task = asyncio.create_task(_read_limited(process.stderr, _STDERR_LIMIT, overflow))
    process_task = asyncio.create_task(process.wait())
    overflow_task = asyncio.create_task(overflow.wait())
    timed_out = False
    try:
        done, _pending = await asyncio.wait(
            {process_task, overflow_task},
            timeout=timeout,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if not done:
            timed_out = True
            await _stop_process(process)
        elif overflow_task in done and overflow.is_set():
            await _stop_process(process)
        else:
            await process_task
        stdout, stdout_truncated = await stdout_task
        stderr, _stderr_truncated = await stderr_task
    except asyncio.CancelledError:
        await _stop_process(process)
        stdout_task.cancel()
        stderr_task.cancel()
        await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
        raise
    finally:
        overflow_task.cancel()
        await asyncio.gather(process_task, overflow_task, return_exceptions=True)

    return GitCommandResult(
        stdout=stdout,
        stderr=stderr,
        returncode=process.returncode if process.returncode is not None else 1,
        truncated=stdout_truncated,
        timed_out=timed_out,
    )


# 持续排空子进程输出但只保留给定字节预算内的前缀
async def _read_limited(
    reader: asyncio.StreamReader, limit: int, overflow: asyncio.Event
) -> tuple[bytes, bool]:
    chunks: list[bytes] = []
    retained = 0
    truncated = False
    while chunk := await reader.read(_READ_CHUNK):
        remaining = max(0, limit - retained)
        if remaining:
            kept = chunk[:remaining]
            chunks.append(kept)
            retained += len(kept)
        if len(chunk) > remaining:
            truncated = True
            overflow.set()
    return b"".join(chunks), truncated


# 终止 Git 子进程并确保操作系统资源得到回收
async def _stop_process(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    try:
        if os.name == "nt":
            killer = await asyncio.create_subprocess_exec(
                "taskkill",
                "/PID",
                str(process.pid),
                "/T",
                "/F",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(killer.wait(), timeout=3.0)
        else:
            killpg = getattr(os, "killpg")
            killpg(process.pid, signal.SIGTERM)
        await asyncio.wait_for(process.wait(), timeout=1.0)
    except ProcessLookupError:
        return
    except TimeoutError:
        if os.name != "nt":
            try:
                killpg = getattr(os, "killpg")
                killpg(process.pid, getattr(signal, "SIGKILL", 9))
            except ProcessLookupError:
                pass
        elif process.returncode is None:
            process.kill()
        await process.wait()


# 查找包含给定目录的 Git 工作树根目录并返回探测结果
async def discover_repository(path: Path) -> tuple[Path | None, GitCommandResult]:
    result = await run_git(
        path,
        ["rev-parse", "--show-toplevel"],
        max_stdout_bytes=16 * 1024,
    )
    if result.returncode != 0 or result.timed_out or result.truncated:
        return None, result
    root_text = result.stdout.decode("utf-8", errors="replace").strip()
    if not root_text:
        return None, GitCommandResult(b"", b"git returned an empty repository path", 1)
    return Path(root_text), result


# 将 Git 命令错误转换成适合工具输出的简短文本
def git_error_message(result: GitCommandResult) -> str:
    if result.timed_out:
        return "git command timed out"
    raw = result.stderr or result.stdout
    message = raw.decode("utf-8", errors="replace").strip()
    return message or f"git command failed with exit code {result.returncode}"
