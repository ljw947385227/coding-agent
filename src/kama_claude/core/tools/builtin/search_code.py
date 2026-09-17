from __future__ import annotations

import asyncio
import fnmatch
import json
import os
import re
import shutil
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from kama_claude.core.tools.base import BaseTool, ToolResult
from kama_claude.core.tools.file_filter import is_ignored_file, ripgrep_exclude_globs
from kama_claude.core.workspace import WorkspaceBoundary

_MAX_FILE_BYTES = 1 * 1024 * 1024
_MAX_LINE_CHARS = 500
_SKIP_DIRS = {".git", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".venv", "node_modules"}
_SearchBackend = Literal["auto", "ripgrep", "python"]


class SearchCodeParams(BaseModel):
    model_config = ConfigDict(extra="ignore")
    query: str = Field(min_length=1)
    path: str = "."
    file_glob: str = ""
    regex: bool = False
    case_sensitive: bool = False
    max_results: int = Field(default=100, ge=1, le=500)


class SearchCodeTool(BaseTool):
    params_model = SearchCodeParams
    name = "search_code"
    description = (
        "Search text across source files and return path:line:column matches. "
        "Supports fixed-string or regular-expression queries, optional filename glob filtering, "
        "case sensitivity, and a bounded result count."
    )
    input_schema: dict[str, object] = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Text or regular expression to find."},
            "path": {
                "type": "string",
                "description": "File or directory to search (default '.').",
            },
            "file_glob": {
                "type": "string",
                "description": "Optional filename glob such as '*.py'.",
            },
            "regex": {
                "type": "boolean",
                "description": "Interpret query as a regular expression (default false).",
            },
            "case_sensitive": {
                "type": "boolean",
                "description": "Use case-sensitive matching (default false).",
            },
            "max_results": {
                "type": "integer",
                "description": "Maximum matches to return (default 100, max 500).",
            },
        },
        "required": ["query"],
    }

    # 创建支持自动选择 Ripgrep 或 Python 后端的代码搜索工具
    def __init__(
        self,
        *,
        backend: _SearchBackend = "auto",
        ignore_files: Sequence[str] | None = None,
        workspace_root: str | Path | None = None,
    ) -> None:
        self._backend = backend
        self._ignore_files = tuple(ignore_files or ())
        self._workspace = WorkspaceBoundary.from_path(workspace_root or Path.cwd())

    # 在指定文件或目录中搜索代码文本并返回带位置的有界结果
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        p = SearchCodeParams.model_validate(params)
        root = self._workspace.resolve(p.path)
        match_root = root if root.is_dir() else root.parent
        if root.is_file() and is_ignored_file(
            root, self._ignore_files, root=match_root
        ):
            return ToolResult(content="No matches found.")

        flags = 0 if p.case_sensitive else re.IGNORECASE
        pattern_text = p.query if p.regex else re.escape(p.query)
        try:
            pattern = re.compile(pattern_text, flags)
        except re.error as exc:
            return ToolResult(
                content=f"invalid regular expression: {exc}",
                is_error=True,
                error_type="runtime_error",
            )

        result: tuple[list[str], bool] | None = None
        ripgrep_path = shutil.which("rg")
        if self._backend != "python" and ripgrep_path is not None:
            result = await _search_with_ripgrep(
                ripgrep_path, root, p, self._ignore_files
            )
        if result is None:
            matches, truncated = await asyncio.to_thread(
                _search_with_python,
                root,
                p,
                pattern,
                self._ignore_files,
                self._workspace,
            )
        else:
            matches, truncated = result

        if not matches:
            return ToolResult(content="No matches found.")
        if truncated:
            matches.append(f"[truncated after {p.max_results} matches]")
        return ToolResult(content="\n".join(matches))


# 通过 Ripgrep JSON 流读取匹配结果并在达到全局上限后停止子进程
async def _search_with_ripgrep(
    executable: str,
    root: Path,
    params: SearchCodeParams,
    ignore_files: Sequence[str],
) -> tuple[list[str], bool] | None:
    args = [
        executable,
        "--json",
        "--sort",
        "path",
        "--hidden",
        "--no-messages",
        "--max-filesize",
        str(_MAX_FILE_BYTES),
    ]
    args.append("--case-sensitive" if params.case_sensitive else "--ignore-case")
    if not params.regex:
        args.append("--fixed-strings")
    if params.file_glob:
        args.extend(["--glob", params.file_glob])
    for directory in sorted(_SKIP_DIRS):
        args.extend(["--glob", f"!**/{directory}/**"])
    for exclude_glob in ripgrep_exclude_globs(ignore_files):
        args.extend(["--glob", exclude_glob])
    args.extend(["--", params.query, str(root)])

    try:
        process = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            limit=_MAX_FILE_BYTES * 4,
        )
    except OSError:
        return None

    if process.stdout is None:
        await _stop_process(process)
        return None

    matches: list[str] = []
    truncated = False
    try:
        while raw_line := await process.stdout.readline():
            match = _parse_ripgrep_match(raw_line)
            if match is None:
                continue
            matches.append(match)
            if len(matches) >= params.max_results:
                truncated = True
                await _stop_process(process)
                break
        if process.returncode is None:
            await process.wait()
    except asyncio.CancelledError:
        await _stop_process(process)
        raise

    if not truncated and process.returncode not in {0, 1}:
        return None
    return matches, truncated


# 从一条 Ripgrep JSON 事件中提取与 Python 后端一致的位置和预览格式
def _parse_ripgrep_match(raw_line: bytes) -> str | None:
    try:
        payload: object = json.loads(raw_line)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(payload, dict) or payload.get("type") != "match":
        return None
    data = payload.get("data")
    if not isinstance(data, dict):
        return None
    path_data = data.get("path")
    lines_data = data.get("lines")
    submatches = data.get("submatches")
    line_no = data.get("line_number")
    if not isinstance(path_data, dict) or not isinstance(lines_data, dict):
        return None
    path_text = path_data.get("text")
    line = lines_data.get("text")
    if (
        not isinstance(path_text, str)
        or not isinstance(line, str)
        or not isinstance(line_no, int)
        or not isinstance(submatches, list)
        or not submatches
        or not isinstance(submatches[0], dict)
    ):
        return None
    start = submatches[0].get("start")
    if not isinstance(start, int):
        return None
    line = line.rstrip("\r\n")
    column = len(line.encode("utf-8")[:start].decode("utf-8", errors="ignore")) + 1
    return f"{Path(path_text).as_posix()}:{line_no}:{column}:{line[:_MAX_LINE_CHARS]}"


# 温和终止仍在运行的搜索进程并在必要时强制回收
async def _stop_process(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    try:
        process.terminate()
        await asyncio.wait_for(process.wait(), timeout=1.0)
    except ProcessLookupError:
        return
    except TimeoutError:
        process.kill()
        await process.wait()


# 使用标准库遍历文件作为没有 Ripgrep 或 Ripgrep 执行失败时的兼容后端
def _search_with_python(
    root: Path,
    params: SearchCodeParams,
    pattern: re.Pattern[str],
    ignore_files: Sequence[str],
    workspace: WorkspaceBoundary,
) -> tuple[list[str], bool]:
    matches: list[str] = []
    match_root = root if root.is_dir() else root.parent
    for candidate, size in _iter_files(root, workspace):
        if params.file_glob and not fnmatch.fnmatch(candidate.name, params.file_glob):
            continue
        if is_ignored_file(candidate, ignore_files, root=match_root):
            continue
        if size > _MAX_FILE_BYTES:
            continue
        try:
            raw = _read_search_file(candidate)
        except OSError:
            continue
        if len(raw) > _MAX_FILE_BYTES or b"\x00" in raw[:8192]:
            continue

        text = raw.decode("utf-8", errors="replace")
        for line_no, line in enumerate(text.splitlines(), start=1):
            match = pattern.search(line)
            if match is None:
                continue
            preview = line[:_MAX_LINE_CHARS]
            matches.append(f"{candidate.as_posix()}:{line_no}:{match.start() + 1}:{preview}")
            if len(matches) >= params.max_results:
                return matches, True
    return matches, False


# 有界读取搜索候选文件以防 stat 后文件并发增长
def _read_search_file(path: Path) -> bytes:
    with path.open("rb") as stream:
        return stream.read(_MAX_FILE_BYTES + 1)


# 按稳定顺序遍历文件并复用目录项中的大小元数据
def _iter_files(
    root: Path, workspace: WorkspaceBoundary
) -> Iterator[tuple[Path, int]]:
    if root.is_file():
        try:
            yield root, root.stat().st_size
        except OSError:
            return
        return

    yield from _iter_directory(root, workspace)


# 递归扫描单个目录并在读取内容前返回每个文件的大小
def _iter_directory(
    directory: Path, workspace: WorkspaceBoundary
) -> Iterator[tuple[Path, int]]:
    try:
        entries = sorted(os.scandir(directory), key=lambda entry: entry.name)
    except OSError:
        return
    directories: list[Path] = []
    for entry in entries:
        try:
            if entry.is_dir(follow_symlinks=False):
                if entry.name not in _SKIP_DIRS:
                    directories.append(Path(entry.path))
            elif entry.is_file(follow_symlinks=True):
                candidate = Path(entry.path)
                try:
                    workspace.resolve(str(candidate))
                except (FileNotFoundError, PermissionError):
                    continue
                yield candidate, entry.stat(follow_symlinks=True).st_size
        except OSError:
            continue
    for child in directories:
        yield from _iter_directory(child, workspace)
