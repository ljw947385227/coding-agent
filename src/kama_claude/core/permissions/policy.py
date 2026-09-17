from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any


class PermissionDecision(StrEnum):
    ALLOW = "allow"
    DENY = "deny"
    ASK = "ask"


# 检测 bash 命令是否操作 cwd 之外路径的正则规则列表（强制触发 ASK，不可被 allow_patterns 绕过）
OUTSIDE_CWD_HEURISTICS: list[str] = [
    r"(^|\s)/[^\s]",              # absolute path
    r"(^|\s)~",                   # tilde home
    r"(^|\s)\.\.(/|$|\s)",        # parent traversal
    r"\$\{?HOME\b",               # $HOME variable
    r"\$\{?PWD\b",                # $PWD variable
    r"(^|\s|;|&&|\|\|)cd(\s|$)",  # explicit cd
]

_OUTSIDE_CWD_RE: list[re.Pattern[str]] = [re.compile(p) for p in OUTSIDE_CWD_HEURISTICS]
_CD_RE = re.compile(
    r"(?:^|[;&|])\s*cd(?:\s+/d)?(?:\s+(?P<path>\"[^\"]*\"|'[^']*'|\S+))?",
    re.IGNORECASE,
)


def _path_is_within(path: Path, root: Path) -> bool:
    """Return whether *path* is root or one of root's descendants."""
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _cd_is_outside(command: str, workspace_root: Path | None) -> bool:
    """Check explicit ``cd`` targets without rejecting safe workspace changes.

    The shell tool already starts in ``workspace_root``.  Agents nevertheless
    commonly prefix commands with ``cd /d <workspace>`` on Windows.  That is
    safe, while an unresolved/absolute target outside the workspace must still
    require approval.  With no root supplied we retain the old conservative
    behavior for callers that do not have workspace context.
    """
    matches = list(_CD_RE.finditer(command))
    if not matches:
        return False
    if workspace_root is None:
        return True

    root = workspace_root.expanduser().resolve(strict=False)
    for match in matches:
        raw_target = match.group("path")
        if not raw_target:
            return True  # ``cd`` with no argument means the user's home directory.
        target = raw_target.strip().strip('"\'')
        if not target or "$" in target or "%" in target:
            return True  # Variables cannot be safely resolved by the policy layer.

        candidate = Path(target).expanduser()
        if not candidate.is_absolute():
            candidate = root / candidate
        candidate = candidate.resolve(strict=False)
        if not _path_is_within(candidate, root):
            return True
    return False


# 判断 bash 命令是否命中 outside-cwd 启发式规则
def matches_outside_cwd(command: str, workspace_root: Path | None = None) -> bool:
    # The generic ``cd`` expression is handled separately so a cd into the
    # authorized workspace can be distinguished from a cd outside it.
    # Strip Windows' ``/d`` option first; otherwise the generic absolute-path
    # heuristic mistakes the option itself for an outside POSIX path.
    heuristic_command = re.sub(r"(?i)(\bcd)\s+/d\b", r"\1", command)
    for pat, source in zip(_OUTSIDE_CWD_RE, OUTSIDE_CWD_HEURISTICS):
        if "cd(" in source:
            continue
        if pat.search(heuristic_command):
            return True
    return _cd_is_outside(command, workspace_root)


@dataclass
class ToolPolicy:
    default: PermissionDecision
    allow_patterns: list[str] = field(default_factory=list)
    deny_patterns: list[str] = field(default_factory=list)


DEFAULT_POLICIES: dict[str, ToolPolicy] = {
    "bash":       ToolPolicy(default=PermissionDecision.ASK),
    "edit_file":  ToolPolicy(default=PermissionDecision.ASK),
    "write_file": ToolPolicy(default=PermissionDecision.ASK),
    "read_file":  ToolPolicy(default=PermissionDecision.ALLOW),
    "search_code": ToolPolicy(default=PermissionDecision.ALLOW),
    "git_status": ToolPolicy(default=PermissionDecision.ALLOW),
    "git_diff":   ToolPolicy(default=PermissionDecision.ALLOW),
    "git_checkpoint": ToolPolicy(default=PermissionDecision.ALLOW),
    "git_rollback": ToolPolicy(default=PermissionDecision.ASK),
    "verify_project": ToolPolicy(default=PermissionDecision.ASK),
    "list_dir":   ToolPolicy(default=PermissionDecision.ALLOW),
    "note_save":  ToolPolicy(default=PermissionDecision.ALLOW),
}

# 未在 DEFAULT_POLICIES 中登记的工具的兜底策略
_UNKNOWN_TOOL_DEFAULT = PermissionDecision.ASK

# bash 参数中展示用的关键字段映射
_PREVIEW_KEY: dict[str, str] = {
    "bash":       "command",
    "edit_file":  "path",
    "read_file":  "path",
    "search_code": "query",
    "git_status": "path",
    "git_diff":   "file",
    "git_checkpoint": "label",
    "git_rollback": "checkpoint_id",
    "verify_project": "path",
    "write_file": "path",
    "list_dir":   "path",
    "note_save":  "content",
}
_PREVIEW_MAX = 60


# 为权限审批事件生成人类可读的参数摘要
def param_preview(tool_name: str, params: dict[str, Any]) -> str:
    key = _PREVIEW_KEY.get(tool_name)
    if key and key in params:
        val = str(params[key])
        if len(val) > _PREVIEW_MAX:
            val = val[:_PREVIEW_MAX] + "…"
        return f"{key}={val!r}"
    snippet = str(params)
    return snippet[:_PREVIEW_MAX] if len(snippet) > _PREVIEW_MAX else snippet


# 对工具 + 参数执行 4 层静态策略评估，返回 ALLOW/DENY/ASK
def evaluate(
    tool_name: str,
    params: dict[str, Any],
    policy: ToolPolicy | None = None,
    *,
    workspace_root: Path | None = None,
) -> PermissionDecision:
    if policy is None:
        policy = DEFAULT_POLICIES.get(tool_name)

    if policy is None:
        return _UNKNOWN_TOOL_DEFAULT

    if tool_name == "git_rollback" and not bool(params.get("apply", False)):
        return PermissionDecision.ALLOW

    if tool_name == "verify_project" and not bool(params.get("run", False)):
        return PermissionDecision.ALLOW

    command = str(params.get("command", "")) if tool_name == "bash" else ""

    # Tier 1: deny_patterns (bash only)
    if command:
        for pat in policy.deny_patterns:
            if re.search(pat, command):
                return PermissionDecision.DENY

    # Tier 2: OUTSIDE_CWD_HEURISTICS — forced ASK, not bypassable
    if command and matches_outside_cwd(command, workspace_root):
        return PermissionDecision.ASK

    # Tier 3: allow_patterns (bash only)
    if command:
        for pat in policy.allow_patterns:
            if re.search(pat, command):
                return PermissionDecision.ALLOW

    # Tier 4: tool default
    return policy.default
