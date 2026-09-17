from __future__ import annotations

import re

from kama_claude.core.verification.semantic.classifier import ChangeClassifier, make_summary
from kama_claude.core.verification.semantic.model import (
    ChangedRanges,
    SemanticChangeSummary,
    UnifiedSymbol,
)
from kama_claude.core.verification.semantic.parser import ParserRegistry
from kama_claude.core.verification.semantic.python_parser import PythonParser

_HUNK_HEADER = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


class DiffAnalyzer:
    # 初始化默认 Python 解析器和语言无关变化分类器
    def __init__(
        self,
        registry: ParserRegistry | None = None,
        classifier: ChangeClassifier | None = None,
    ) -> None:
        self._registry = registry or ParserRegistry((PythonParser(),))
        self._classifier = classifier or ChangeClassifier()

    # 按文件后缀解析新旧源码并生成统一语义变更摘要
    def analyze(
        self,
        path: str,
        module: str,
        patch: str,
        old_source: str,
        new_source: str,
    ) -> SemanticChangeSummary:
        ranges = ChangedRanges(path, *changed_lines_from_patch(patch))
        parser = self._registry.for_path(path)
        if parser is None:
            return SemanticChangeSummary(
                path=path,
                old_lines=ranges.old_lines,
                new_lines=ranges.new_lines,
                change_type="unsupported_language",
                risk="high",
                safe_to_narrow=False,
                detail="no_language_parser_registered",
            )
        try:
            old_symbols = _symbols_for_lines(
                parser.parse_symbols(old_source, path=path, module=module),
                ranges.old_lines,
            )
            new_symbols = _symbols_for_lines(
                parser.parse_symbols(new_source, path=path, module=module),
                ranges.new_lines,
            )
        except SyntaxError as exc:
            return SemanticChangeSummary(
                path=path,
                old_lines=ranges.old_lines,
                new_lines=ranges.new_lines,
                change_type="unparseable",
                risk="high",
                safe_to_narrow=False,
                detail=f"{exc.msg}:{exc.lineno or 0}",
            )
        classification = self._classifier.classify(ranges, old_symbols, new_symbols)
        return make_summary(ranges, old_symbols, new_symbols, classification)


# 将零上下文 unified diff hunk 转换成旧、新文件行集合
def changed_lines_from_patch(patch: str) -> tuple[tuple[int, ...], tuple[int, ...]]:
    old_lines: set[int] = set()
    new_lines: set[int] = set()
    for raw_line in patch.splitlines():
        match = _HUNK_HEADER.match(raw_line)
        if match is None:
            continue
        old_start = int(match.group(1))
        old_count = int(match.group(2) or "1")
        new_start = int(match.group(3))
        new_count = int(match.group(4) or "1")
        old_lines.update(range(old_start, old_start + old_count))
        new_lines.update(range(new_start, new_start + new_count))
    return tuple(sorted(old_lines)), tuple(sorted(new_lines))


# 将每个修改行映射到最内层统一符号并稳定去重
def _symbols_for_lines(
    symbols: tuple[UnifiedSymbol, ...], lines: tuple[int, ...]
) -> tuple[UnifiedSymbol, ...]:
    selected: dict[str, UnifiedSymbol] = {}
    for line in lines:
        candidates = [
            symbol for symbol in symbols if symbol.start_line <= line <= symbol.end_line
        ]
        if not candidates:
            continue
        symbol = min(candidates, key=lambda item: item.end_line - item.start_line)
        selected[symbol.qualified_name] = symbol
    return tuple(selected[name] for name in sorted(selected))
