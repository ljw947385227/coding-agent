from __future__ import annotations

from collections.abc import Iterable
from typing import Protocol

from kama_claude.core.verification.semantic.model import UnifiedSymbol


class LanguageParser(Protocol):
    # 暴露解析器支持的语言名称和文件后缀集合
    @property
    def language(self) -> str: ...

    # 暴露解析器支持的源码后缀，便于后续接入 JS/TS、Go 等语言
    @property
    def extensions(self) -> tuple[str, ...]: ...

    # 从源码中提取统一符号模型
    def parse_symbols(
        self, source: str, *, path: str, module: str
    ) -> tuple[UnifiedSymbol, ...]: ...


class ParserRegistry:
    # 按后缀注册语言解析器并保持确定性查找顺序
    def __init__(self, parsers: Iterable[LanguageParser] = ()) -> None:
        self._by_suffix: dict[str, LanguageParser] = {}
        for parser in parsers:
            self.register(parser)

    # 注册一个解析器支持的文件后缀
    def register(self, parser: LanguageParser) -> None:
        for suffix in parser.extensions:
            normalized = suffix if suffix.startswith(".") else f".{suffix}"
            self._by_suffix[normalized.lower()] = parser

    # 根据项目相对路径返回语言解析器，没有支持时返回 None
    def for_path(self, path: str) -> LanguageParser | None:
        suffix = path.rsplit(".", 1)[-1].lower() if "." in path else ""
        return self._by_suffix.get(f".{suffix}")
