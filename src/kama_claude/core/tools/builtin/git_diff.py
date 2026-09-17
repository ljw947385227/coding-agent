from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from kama_claude.core.tools.base import BaseTool, ToolResult
from kama_claude.core.tools.builtin._git import (
    GitCommandResult,
    discover_repository,
    git_error_message,
    run_git,
)
from kama_claude.core.workspace import WorkspaceBoundary

_DiffScope = Literal["unstaged", "staged", "all"]


class GitDiffParams(BaseModel):
    model_config = ConfigDict(extra="ignore")
    path: str = "."
    scope: _DiffScope = "unstaged"
    file: str = ""
    context_lines: int = Field(default=3, ge=0, le=20)
    max_bytes: int = Field(default=64 * 1024, ge=1024, le=256 * 1024)


class GitDiffTool(BaseTool):
    params_model = GitDiffParams
    name = "git_diff"
    description = (
        "Return a bounded read-only Git patch as structured JSON. Select unstaged, staged, "
        "or both scopes and optionally restrict the diff to one repository-relative file."
    )
    input_schema: dict[str, object] = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Directory inside the repository (default '.').",
            },
            "scope": {
                "type": "string",
                "enum": ["unstaged", "staged", "all"],
                "description": "Diff scope (default 'unstaged').",
            },
            "file": {
                "type": "string",
                "description": "Optional literal repository-relative file path.",
            },
            "context_lines": {
                "type": "integer",
                "description": "Unified diff context lines (default 3, max 20).",
            },
            "max_bytes": {
                "type": "integer",
                "description": "Maximum retained patch bytes (default 65536, max 262144).",
            },
        },
    }

    # 创建仅允许读取会话工作区内仓库差异的工具
    def __init__(self, *, workspace_root: str | Path | None = None) -> None:
        self._workspace = WorkspaceBoundary.from_path(workspace_root or Path.cwd())

    # 读取指定范围的 Git patch 并返回包含截断和二进制标记的 JSON
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        parsed = GitDiffParams.model_validate(params)
        path = self._workspace.resolve(parsed.path)
        if not path.is_dir():
            raise NotADirectoryError(f"not a directory: {parsed.path}")
        _validate_file_path(parsed.file)
        repository, discovery = await discover_repository(path)
        if repository is None:
            return _error_result("not_git_repository", git_error_message(discovery))
        self._workspace.resolve(str(repository))

        scopes = [parsed.scope] if parsed.scope != "all" else ["staged", "unstaged"]
        per_scope_budget = parsed.max_bytes // len(scopes)
        sections: list[dict[str, object]] = []
        truncated = False
        for scope in scopes:
            result = await _read_diff(
                repository,
                scope,
                parsed.file,
                parsed.context_lines,
                per_scope_budget,
            )
            if result.timed_out:
                return ToolResult(
                    content=git_error_message(result),
                    is_error=True,
                    error_type="timeout",
                )
            if result.returncode != 0 and not result.truncated:
                return _error_result("git_diff_failed", git_error_message(result))
            patch = result.stdout.decode("utf-8", errors="replace")
            if result.truncated:
                patch += "\n[truncated]"
            truncated = truncated or result.truncated
            sections.append(
                {
                    "scope": scope,
                    "has_changes": bool(result.stdout),
                    "binary": "Binary files " in patch or "GIT binary patch" in patch,
                    "patch": patch,
                }
            )

        payload = {
            "repository": repository.as_posix(),
            "scope": parsed.scope,
            "file": parsed.file or None,
            "truncated": truncated,
            "sections": sections,
        }
        return ToolResult(content=json.dumps(payload, ensure_ascii=False, indent=2))


# 执行单个 staged 或 unstaged 范围的有界 Git diff
async def _read_diff(
    repository: Path,
    scope: str,
    file_path: str,
    context_lines: int,
    max_bytes: int,
) -> GitCommandResult:
    arguments = [
        "diff",
        "--no-ext-diff",
        "--no-color",
        "--find-renames",
        f"--unified={context_lines}",
    ]
    if scope == "staged":
        arguments.append("--cached")
    arguments.append("--")
    if file_path:
        arguments.append(f":(literal){file_path}")
    return await run_git(
        repository,
        arguments,
        max_stdout_bytes=max_bytes,
    )


# 校验可选文件过滤器只能表示仓库内的字面相对路径
def _validate_file_path(file_path: str) -> None:
    if not file_path:
        return
    path = Path(file_path)
    if path.anchor or ".." in path.parts:
        raise PermissionError(f"file path must stay inside the repository: {file_path}")


# 构造结构化 Git diff 运行错误
def _error_result(code: str, message: str) -> ToolResult:
    content = json.dumps({"error": code, "message": message}, ensure_ascii=False)
    return ToolResult(content=content, is_error=True, error_type="runtime_error")
