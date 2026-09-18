"""Public verification API with cycle-safe lazy exports."""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from kama_claude.core.verification.controller import (
        VerificationController as VerificationController,
    )
    from kama_claude.core.verification.controller import VerificationMode as VerificationMode
    from kama_claude.core.verification.manager import (
        VerificationManager as VerificationManager,
    )
    from kama_claude.core.verification.model import Diagnostic as Diagnostic
    from kama_claude.core.verification.model import (
        DiagnosticCategory as DiagnosticCategory,
    )
    from kama_claude.core.verification.model import (
        DiagnosticSeverity as DiagnosticSeverity,
    )
    from kama_claude.core.verification.model import (
        SemanticChangeSummary as SemanticChangeSummary,
    )
    from kama_claude.core.verification.model import TestSelection as TestSelection
    from kama_claude.core.verification.model import (
        TestSelectionReason as TestSelectionReason,
    )
    from kama_claude.core.verification.model import VerificationCheck as VerificationCheck
    from kama_claude.core.verification.model import VerificationKind as VerificationKind
    from kama_claude.core.verification.model import VerificationPlan as VerificationPlan
    from kama_claude.core.verification.model import VerificationReport as VerificationReport
    from kama_claude.core.verification.model import VerificationResult as VerificationResult
    from kama_claude.core.verification.parser import ParserRegistry as ParserRegistry

_EXPORT_MODULES = {
    "Diagnostic": "model",
    "DiagnosticCategory": "model",
    "DiagnosticSeverity": "model",
    "ParserRegistry": "parser",
    "SemanticChangeSummary": "model",
    "TestSelection": "model",
    "TestSelectionReason": "model",
    "VerificationCheck": "model",
    "VerificationController": "controller",
    "VerificationKind": "model",
    "VerificationManager": "manager",
    "VerificationMode": "controller",
    "VerificationPlan": "model",
    "VerificationReport": "model",
    "VerificationResult": "model",
}

__all__ = list(_EXPORT_MODULES)


def __getattr__(name: str) -> Any:
    module_name = _EXPORT_MODULES.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(f"{__name__}.{module_name}"), name)
    globals()[name] = value
    return value
