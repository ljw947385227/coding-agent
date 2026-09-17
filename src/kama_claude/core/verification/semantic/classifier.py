from __future__ import annotations

from dataclasses import dataclass

from kama_claude.core.verification.semantic.model import (
    ChangedRanges,
    SemanticChangeSummary,
    UnifiedSymbol,
)


@dataclass(frozen=True, slots=True)
class ChangeClassification:
    # 保存语言无关分类器的变化类型、风险和安全缩小结论
    change_type: str
    risk: str
    safe_to_narrow: bool
    detail: str


class ChangeClassifier:
    # 根据统一符号范围和签名比较结果分类源码变化
    def classify(
        self,
        ranges: ChangedRanges,
        old_symbols: tuple[UnifiedSymbol, ...],
        new_symbols: tuple[UnifiedSymbol, ...],
    ) -> ChangeClassification:
        old_by_name = {symbol.qualified_name: symbol for symbol in old_symbols}
        new_by_name = {symbol.qualified_name: symbol for symbol in new_symbols}
        common = tuple(sorted(old_by_name.keys() & new_by_name.keys()))
        safe = bool(common) and set(old_by_name) == set(new_by_name)
        if safe:
            for name in common:
                old_symbol = old_by_name[name]
                new_symbol = new_by_name[name]
                if (
                    old_symbol.kind not in {"function", "method"}
                    or new_symbol.kind != old_symbol.kind
                    or old_symbol.signature != new_symbol.signature
                    or not _lines_within_body(ranges.old_lines, old_symbol)
                    or not _lines_within_body(ranges.new_lines, new_symbol)
                ):
                    safe = False
                    break
        if safe:
            return ChangeClassification(
                change_type="function_body",
                risk="low",
                safe_to_narrow=True,
                detail="same_symbol_and_signature",
            )
        if not old_symbols or not new_symbols:
            return ChangeClassification(
                change_type="module_or_symbol_boundary",
                risk="high",
                safe_to_narrow=False,
                detail="changed_lines_not_in_matching_symbol",
            )
        return ChangeClassification(
            change_type="structure",
            risk="high",
            safe_to_narrow=False,
            detail="symbol_or_signature_changed",
        )


# 将分类结果和新旧统一符号集合组合成对外摘要
def make_summary(
    ranges: ChangedRanges,
    old_symbols: tuple[UnifiedSymbol, ...],
    new_symbols: tuple[UnifiedSymbol, ...],
    classification: ChangeClassification,
) -> SemanticChangeSummary:
    return SemanticChangeSummary(
        path=ranges.path,
        old_lines=ranges.old_lines,
        new_lines=ranges.new_lines,
        old_symbols=tuple(symbol.qualified_name for symbol in old_symbols),
        new_symbols=tuple(symbol.qualified_name for symbol in new_symbols),
        change_type=classification.change_type,
        risk="low" if classification.risk == "low" else "high",
        safe_to_narrow=classification.safe_to_narrow,
        detail=classification.detail,
    )


# 检查所有变化行是否位于同一函数或方法体范围内
def _lines_within_body(lines: tuple[int, ...], symbol: UnifiedSymbol) -> bool:
    return bool(lines) and all(symbol.body_start_line <= line <= symbol.end_line for line in lines)
