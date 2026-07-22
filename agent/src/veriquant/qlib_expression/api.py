"""Narrow integration surface for the isolated Qlib expression package."""

from .canonicalizer import canonicalize_expression
from .models import (
    CertifiedQlibExpression,
    QlibExpressionDiagnostic,
    QlibExpressionDraft,
    QlibExpressionError,
    QlibExpressionFailureCode,
    QlibExpressionPolicy,
    QlibFieldRole,
    QlibOperatorTrust,
    QlibRuntimeAvailability,
    QlibRuntimeStatus,
)
from .operator_manifest import (
    OPERATOR_MANIFEST_SCHEMA_VERSION,
    PINNED_OPERATOR_NAMES,
    PINNED_OPS_SOURCE_HASH,
    PINNED_PIT_SOURCE_HASH,
    PINNED_PYQLIB_VERSION,
    STATIC_QLIB_OPERATOR_MANIFEST,
    QlibOperatorManifest,
    QlibOperatorSpec,
    probe_qlib_runtime,
)
from .parser import parse_qlib_expression
from .validator import certify_qlib_expression

__all__ = [
    "CertifiedQlibExpression",
    "OPERATOR_MANIFEST_SCHEMA_VERSION",
    "PINNED_OPERATOR_NAMES",
    "PINNED_OPS_SOURCE_HASH",
    "PINNED_PIT_SOURCE_HASH",
    "PINNED_PYQLIB_VERSION",
    "QlibExpressionDiagnostic",
    "QlibExpressionDraft",
    "QlibExpressionError",
    "QlibExpressionFailureCode",
    "QlibExpressionPolicy",
    "QlibFieldRole",
    "QlibOperatorManifest",
    "QlibOperatorSpec",
    "QlibOperatorTrust",
    "QlibRuntimeAvailability",
    "QlibRuntimeStatus",
    "STATIC_QLIB_OPERATOR_MANIFEST",
    "canonicalize_expression",
    "certify_qlib_expression",
    "parse_qlib_expression",
    "probe_qlib_runtime",
]
