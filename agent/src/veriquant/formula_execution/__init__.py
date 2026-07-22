"""Backend-neutral deterministic formula execution boundary."""

from .legacy import LEGACY_POLICY_HASH, LEGACY_RUNTIME_HASH, LegacyFactorDAGBackend
from .evidence_rows import evaluation_rows_from_scores, score_key
from .models import (
    FORMULA_EXECUTION_SCHEMA_VERSION,
    FactorScoreArtifact,
    FactorScorePoint,
    FormulaExecutionRequest,
    FormulaProgramIdentity,
    FormulaProgramKind,
)
from .service import FormulaExecutionBackend, FormulaExecutionService
from .qlib import (
    KNOWN_DIFFERENCE_OPERATOR_NAMES,
    CertifiedQlibExpressionBackend,
    CertifiedQlibProgramBundle,
    IsolatedCertifiedQlibExpressionBackend,
    configured_isolated_qlib_backend,
)

__all__ = [
    "FORMULA_EXECUTION_SCHEMA_VERSION",
    "FactorScoreArtifact",
    "FactorScorePoint",
    "FormulaExecutionBackend",
    "FormulaExecutionRequest",
    "FormulaExecutionService",
    "FormulaProgramIdentity",
    "FormulaProgramKind",
    "CertifiedQlibExpressionBackend",
    "CertifiedQlibProgramBundle",
    "IsolatedCertifiedQlibExpressionBackend",
    "KNOWN_DIFFERENCE_OPERATOR_NAMES",
    "configured_isolated_qlib_backend",
    "LEGACY_POLICY_HASH",
    "LEGACY_RUNTIME_HASH",
    "LegacyFactorDAGBackend",
    "evaluation_rows_from_scores",
    "score_key",
]
