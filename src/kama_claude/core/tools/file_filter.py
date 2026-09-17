from __future__ import annotations

import fnmatch
from collections.abc import Sequence
from pathlib import Path


# 判断文件是否命中用户追加的文件名、后缀或 glob 忽略规则
def is_ignored_file(path: Path, patterns: Sequence[str], *, root: Path) -> bool:
    try:
        relative = path.relative_to(root).as_posix()
    except ValueError:
        relative = path.as_posix()
    name = path.name
    for raw_pattern in patterns:
        pattern = raw_pattern.strip().replace("\\", "/")
        if not pattern:
            continue
        if "/" in pattern or any(token in pattern for token in "*?["):
            if fnmatch.fnmatch(relative, pattern) or fnmatch.fnmatch(name, pattern):
                return True
        elif pattern.startswith(".") and name.endswith(pattern):
            return True
        elif name == pattern:
            return True
    return False


# 把用户文件规则转换为 Ripgrep 的排除 glob 参数
def ripgrep_exclude_globs(patterns: Sequence[str]) -> list[str]:
    globs: list[str] = []
    for raw_pattern in patterns:
        pattern = raw_pattern.strip().replace("\\", "/")
        if not pattern:
            continue
        if pattern.startswith(".") and not any(token in pattern for token in "*?["):
            pattern = f"*{pattern}"
        globs.append(f"!{pattern}")
    return globs
