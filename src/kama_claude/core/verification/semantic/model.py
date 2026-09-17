from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True, slots=True)
class ChangedRanges:
    # 统一保存语言无关的旧、新文件修改行集合
    path: str
    old_lines: tuple[int, ...] = ()
    new_lines: tuple[int, ...] = ()


@dataclass(frozen=True, slots=True)
class UnifiedSymbol:
    # 统一描述不同语言解析器输出的符号及其源码范围
    language: str
    kind: str
    name: str
    qualified_name: str
    start_line: int
    end_line: int
    body_start_line: int
    signature: str


@dataclass(frozen=True, slots=True)
class SemanticChangeSummary:
    # 保存语言解析器和语言无关分类器共同生成的变更摘要
    path: str
    old_lines: tuple[int, ...] = ()
    new_lines: tuple[int, ...] = ()
    old_symbols: tuple[str, ...] = ()
    new_symbols: tuple[str, ...] = ()
    change_type: str = "unknown"
    risk: Literal["low", "high"] = "high"
    safe_to_narrow: bool = False
    detail: str = ""
