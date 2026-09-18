"""Backward-compatible facade for the language-neutral semantic analyzer."""

from __future__ import annotations

from kama_claude.core.verification.semantic.diff_analyzer import (
    DiffAnalyzer,
    changed_lines_from_patch,
)
from kama_claude.core.verification.semantic.model import SemanticChangeSummary, UnifiedSymbol


# 使用默认解析器分析 Python 语义变化，保持旧调用方兼容
def analyze_semantic_change(
    path: str,
    module: str,
    patch: str,
    old_source: str,
    new_source: str,
)-> SemanticChangeSummary:
    return DiffAnalyzer().analyze(path, module, patch, old_source, new_source)


__all__ = [
    "DiffAnalyzer",
    "UnifiedSymbol",
    "analyze_semantic_change",
    "changed_lines_from_patch",
]
