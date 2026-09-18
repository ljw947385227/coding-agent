from __future__ import annotations

import re

from kama_claude.core.sandbox.models import ExecutionResult

_ENVIRONMENT_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(
            r"command not found|is not recognized as an internal|/bin/sh: .*: not found",
            re.I,
        ),
        "The executable may be missing from the configured execution environment.",
    ),
    (
        re.compile(
            r"ModuleNotFoundError|No module named|cannot find module|module not found",
            re.I,
        ),
        "A project dependency may be missing from the sandbox image.",
    ),
    (
        re.compile(r"permission denied|read-only file system|operation not permitted", re.I),
        "The command may have crossed a sandbox filesystem or privilege boundary.",
    ),
    (
        re.compile(r"network is unreachable|connection refused|temporary failure in name", re.I),
        "The command may require network access that the sandbox policy disables.",
    ),
    (
        re.compile(r"exec format error|wrong architecture|bad cpu type", re.I),
        "The executable architecture may not match the sandbox architecture.",
    ),
)


# 根据结构化状态和输出证据生成按需查询 sandbox_info 的提示
def sandbox_failure_hint(result: ExecutionResult) -> str | None:
    reason: str | None
    if result.oom_killed or result.exit_code in (137, -9):
        reason = "The command may have exceeded the sandbox memory limit."
    elif result.launch_error is not None:
        reason = "The execution backend could not launch the command or container."
    else:
        reason = next(
            (
                message
                for pattern, message in _ENVIRONMENT_PATTERNS
                if pattern.search(result.output)
            ),
            None,
        )
    if reason is None:
        return None
    return (
        f"{reason} Call sandbox_info before changing application code if the failure could be "
        "caused by tool availability, versions, permissions, network policy, architecture, "
        "resource limits, or a stale image."
    )
