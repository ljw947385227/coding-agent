from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from kama_claude.core.verification.semantic.model import (
    SemanticChangeSummary as SemanticChangeSummary,
)

VerificationKind = Literal["test", "lint", "typecheck", "build"]
VerificationStatus = Literal["passed", "failed", "timeout", "error", "skipped"]
TestSelectionStrategy = Literal["related", "full"]
DiagnosticSeverity = Literal["error", "warning", "note"]
DiagnosticCategory = Literal[
    "assertion_failure",
    "collection_error",
    "import_error",
    "syntax_error",
    "type_error",
    "lint_error",
    "timeout",
    "process_error",
    "dependency_error",
    "environment_error",
    "unknown",
]


@dataclass(frozen=True, slots=True)
class Diagnostic:
    tool: str
    category: DiagnosticCategory
    message: str
    severity: DiagnosticSeverity = "error"
    file: str | None = None
    line: int | None = None
    column: int | None = None
    code: str | None = None
    test_id: str | None = None
    evidence: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class VerificationCheck:
    kind: VerificationKind
    ecosystem: str
    command: tuple[str, ...]
    source: str
    tool: str = "unknown"


@dataclass(frozen=True, slots=True)
class TestSelectionReason:
    test: str
    reason: str
    source: str


@dataclass(frozen=True, slots=True)
class TestSelection:
    strategy: TestSelectionStrategy
    baseline_tree: str
    indexed_tree: str
    baseline_checkpoint_id: str | None = None
    changed_files: tuple[str, ...] = ()
    selected_tests: tuple[str, ...] = ()
    reasons: tuple[TestSelectionReason, ...] = ()
    semantic_changes: tuple[SemanticChangeSummary, ...] = ()
    index_updated_files: tuple[str, ...] = ()
    fallback_reason: str | None = None
    # 当前索引识别出的 pytest node 总数及本次实际选择数，供 TUI/报告展示
    total_tests: int = 0
    selected_test_count: int = 0


@dataclass(frozen=True, slots=True)
class VerificationPlan:
    root: str
    ecosystems: tuple[str, ...]
    checks: tuple[VerificationCheck, ...]
    warnings: tuple[str, ...] = ()
    test_selection: TestSelection | None = None


@dataclass(frozen=True, slots=True)
class VerificationResult:
    kind: VerificationKind
    ecosystem: str
    tool: str
    command: tuple[str, ...]
    status: VerificationStatus
    exit_code: int | None
    elapsed_ms: int
    output: str = ""
    output_truncated: bool = False
    diagnostics: tuple[Diagnostic, ...] = ()
    environment_hint: str | None = None


@dataclass(frozen=True, slots=True)
class VerificationReport:
    root: str
    passed: bool
    elapsed_ms: int
    results: tuple[VerificationResult, ...] = field(default_factory=tuple)
