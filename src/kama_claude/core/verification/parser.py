from __future__ import annotations

import re
from collections.abc import Sequence
from pathlib import Path
from typing import Protocol

from kama_claude.core.verification.model import (
    Diagnostic,
    DiagnosticCategory,
    DiagnosticSeverity,
    VerificationStatus,
)

_MYPY_LINE = re.compile(
    r"^(?P<file>.+?):(?P<line>\d+)(?::(?P<column>\d+))?: "
    r"(?P<severity>error|warning|note): (?P<message>.*?)(?:  \[(?P<code>[^]]+)\])?$"
)
_RUFF_COMPACT = re.compile(
    r"^(?P<file>.+?):(?P<line>\d+):(?P<column>\d+): "
    r"(?P<code>[A-Z]+\d+) (?P<message>.+)$"
)
_RUFF_RICH = re.compile(r"^(?P<code>[A-Z]+\d+) (?P<message>.+)$")
_RUFF_LOCATION = re.compile(
    r"^\s*-->\s+(?P<file>.+?):(?P<line>\d+):(?P<column>\d+)\s*$"
)
_GENERIC_LOCATION = re.compile(
    r"^(?P<file>.+?):(?P<line>\d+)(?::(?P<column>\d+))?:\s*"
    r"(?:(?P<severity>error|warning|note):\s*)?(?P<message>.+)$",
    re.IGNORECASE,
)


class DiagnosticParser(Protocol):
    # 将指定工具的有界输出转换为统一诊断列表
    def parse(self, tool: str, output: str) -> tuple[Diagnostic, ...]: ...


class PytestParser:
    # 从 pytest short summary 和 collection error 中提取测试级诊断
    def parse(self, tool: str, output: str) -> tuple[Diagnostic, ...]:
        lines = output.splitlines()
        diagnostics: list[Diagnostic] = []
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("FAILED "):
                test_id, message = _split_pytest_summary(stripped[7:])
                file = test_id.split("::", 1)[0] or None
                diagnostics.append(
                    Diagnostic(
                        tool=tool,
                        category=_pytest_category(message),
                        message=message or "pytest test failed",
                        file=file,
                        line=_find_pytest_line(lines, file),
                        test_id=test_id,
                        evidence=(stripped,),
                    )
                )
            elif stripped.startswith("ERROR collecting "):
                file = stripped[len("ERROR collecting ") :].strip() or None
                diagnostics.append(
                    Diagnostic(
                        tool=tool,
                        category="collection_error",
                        message="pytest failed while collecting tests",
                        file=file,
                        line=_find_pytest_line(lines, file),
                        evidence=(stripped,),
                    )
                )
            elif stripped.startswith("ERROR "):
                test_id, message = _split_pytest_summary(stripped[6:])
                file = test_id.split("::", 1)[0] or None
                collection_error = "::" not in test_id
                diagnostics.append(
                    Diagnostic(
                        tool=tool,
                        category="collection_error" if collection_error else "unknown",
                        message=message
                        or (
                            "pytest failed while collecting tests"
                            if collection_error
                            else "pytest test setup or teardown failed"
                        ),
                        file=file,
                        line=_find_pytest_line(lines, file),
                        test_id=None if collection_error else test_id,
                        evidence=(stripped,),
                    )
                )
        return _deduplicate(diagnostics)


class RuffParser:
    # 解析 Ruff compact 或默认富文本格式中的规则编号和源码位置
    def parse(self, tool: str, output: str) -> tuple[Diagnostic, ...]:
        lines = output.splitlines()
        diagnostics: list[Diagnostic] = []
        for index, line in enumerate(lines):
            compact = _RUFF_COMPACT.match(line.strip())
            if compact is not None:
                diagnostics.append(_ruff_diagnostic(tool, compact.groupdict(), line.strip()))
                continue
            rich = _RUFF_RICH.match(line.strip())
            if rich is None:
                continue
            location = _next_ruff_location(lines, index + 1)
            if location is None:
                continue
            fields = {**rich.groupdict(), **location.groupdict()}
            diagnostics.append(_ruff_diagnostic(tool, fields, line.strip()))
        return _deduplicate(diagnostics)


class MypyParser:
    # 解析 Mypy 文本输出中的位置、级别、错误码和消息
    def parse(self, tool: str, output: str) -> tuple[Diagnostic, ...]:
        diagnostics: list[Diagnostic] = []
        for line in output.splitlines():
            match = _MYPY_LINE.match(line.strip())
            if match is None:
                continue
            fields = match.groupdict()
            diagnostics.append(
                Diagnostic(
                    tool=tool,
                    category="type_error",
                    severity=_severity(fields["severity"]),
                    message=fields["message"],
                    file=fields["file"],
                    line=int(fields["line"]),
                    column=_optional_int(fields["column"]),
                    code=fields["code"],
                    evidence=(line.strip(),),
                )
            )
        return _deduplicate(diagnostics)


class GenericLocationParser:
    # 对未知工具提取通用 path:line:column 位置格式作为安全降级
    def parse(self, tool: str, output: str) -> tuple[Diagnostic, ...]:
        diagnostics: list[Diagnostic] = []
        for line in output.splitlines():
            match = _GENERIC_LOCATION.match(line.strip())
            if match is None:
                continue
            fields = match.groupdict()
            diagnostics.append(
                Diagnostic(
                    tool=tool,
                    category="unknown",
                    severity=_severity(fields["severity"] or "error"),
                    message=fields["message"],
                    file=fields["file"],
                    line=int(fields["line"]),
                    column=_optional_int(fields["column"]),
                    evidence=(line.strip(),),
                )
            )
        return _deduplicate(diagnostics)


class ParserRegistry:
    # 初始化内置工具解析器和通用位置降级解析器
    def __init__(self) -> None:
        self._parsers: dict[str, DiagnosticParser] = {
            "pytest": PytestParser(),
            "ruff": RuffParser(),
            "mypy": MypyParser(),
        }
        self._generic = GenericLocationParser()

    # 注册或覆盖一个工具专用解析器
    def register(self, tool: str, parser: DiagnosticParser) -> None:
        self._parsers[tool.lower()] = parser

    # 按执行状态和工具类型生成诊断，未知格式安全降级为空或通用位置
    def parse(
        self,
        tool: str,
        status: VerificationStatus,
        output: str,
    ) -> tuple[Diagnostic, ...]:
        normalized_tool = tool.lower() or "unknown"
        process_diagnostic = _process_diagnostic(normalized_tool, status, output)
        if process_diagnostic is not None:
            return (process_diagnostic,)
        if status != "failed":
            return ()
        parser = self._parsers.get(normalized_tool)
        diagnostics = parser.parse(normalized_tool, output) if parser is not None else ()
        if diagnostics:
            return diagnostics
        return self._generic.parse(normalized_tool, output)


# 从 pytest short summary 右侧分离 test ID 和错误摘要
def _split_pytest_summary(summary: str) -> tuple[str, str]:
    if " - " not in summary:
        return summary.strip(), ""
    test_id, message = summary.split(" - ", 1)
    return test_id.strip(), message.strip()


# 根据 pytest 错误摘要映射稳定的通用失败类别
def _pytest_category(message: str) -> DiagnosticCategory:
    lowered = message.lower()
    if "modulenotfounderror" in lowered or "importerror" in lowered:
        return "import_error"
    if "syntaxerror" in lowered:
        return "syntax_error"
    if "timeout" in lowered:
        return "timeout"
    if "dependency" in lowered or "no module named" in lowered:
        return "dependency_error"
    return "assertion_failure"


# 在 pytest traceback 中查找指定测试文件最后出现的源码行号
def _find_pytest_line(lines: Sequence[str], file: str | None) -> int | None:
    if not file:
        return None
    normalized_file = file.replace("\\", "/")
    pattern = re.compile(rf"^{re.escape(normalized_file)}:(?P<line>\d+):")
    found: int | None = None
    for line in lines:
        match = pattern.match(line.strip().replace("\\", "/"))
        if match is not None:
            found = int(match.group("line"))
    return found


# 从 Ruff 富文本错误标题之后查找最近的位置箭头行
def _next_ruff_location(lines: Sequence[str], start: int) -> re.Match[str] | None:
    for line in lines[start : start + 4]:
        match = _RUFF_LOCATION.match(line)
        if match is not None:
            return match
    return None


# 从 Ruff 匹配字段构造统一 lint 诊断
def _ruff_diagnostic(tool: str, fields: dict[str, str | None], evidence: str) -> Diagnostic:
    message = re.sub(r"^\[\*\]\s+", "", fields["message"] or "Ruff check failed")
    return Diagnostic(
        tool=tool,
        category="lint_error",
        message=message,
        file=fields["file"],
        line=int(fields["line"] or 0) or None,
        column=int(fields["column"] or 0) or None,
        code=fields["code"],
        evidence=(evidence,),
    )


# 将可选数字字符串转换为整数
def _optional_int(value: str | None) -> int | None:
    return int(value) if value is not None else None


# 将解析得到的级别限制在统一枚举范围内
def _severity(value: str) -> DiagnosticSeverity:
    normalized = value.lower()
    if normalized == "warning":
        return "warning"
    if normalized == "note":
        return "note"
    return "error"


# 为超时和进程启动错误生成与工具格式无关的诊断
def _process_diagnostic(
    tool: str,
    status: VerificationStatus,
    output: str,
) -> Diagnostic | None:
    if status == "timeout":
        return Diagnostic(
            tool=tool,
            category="timeout",
            message="verification command timed out",
            evidence=tuple(output.splitlines()[-1:]),
        )
    if status == "error":
        message = next((line.strip() for line in output.splitlines() if line.strip()), "")
        return Diagnostic(
            tool=tool,
            category="process_error",
            message=message or "verification command could not be executed",
            evidence=(message,) if message else (),
        )
    return None


# 按关键字段稳定去重解析器产生的重复诊断
def _deduplicate(diagnostics: Sequence[Diagnostic]) -> tuple[Diagnostic, ...]:
    unique: list[Diagnostic] = []
    seen: set[tuple[object, ...]] = set()
    for diagnostic in diagnostics:
        key = (
            diagnostic.category,
            diagnostic.file,
            diagnostic.line,
            diagnostic.column,
            diagnostic.code,
            diagnostic.test_id,
            diagnostic.message,
        )
        if key not in seen:
            seen.add(key)
            unique.append(diagnostic)
    return tuple(unique)


# 从验证命令参数中推断旧计划缺失的常见工具名称
def infer_tool(command: Sequence[str]) -> str:
    known = {"pytest", "ruff", "mypy", "jest", "vitest", "eslint", "tsc", "cargo", "go"}
    for part in command:
        name = Path(part).stem.lower()
        if name in known:
            return name
    return "unknown"
