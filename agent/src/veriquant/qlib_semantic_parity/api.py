"""Stable public surface for Qlib semantic-parity evidence."""

from .matrix import ParityObservation, build_operator_evidence
from .models import (
    OperatorParityEvidence,
    OperatorSemanticPolicy,
    PARITY_OPERATORS,
    PARITY_POLICY_VERSION,
    PARITY_SCHEMA_VERSION,
    QlibSemanticParityReport,
    SemanticParityStatus,
)
from .policy import DIFFERENCE_CODES, EXPECTED_DISPOSITIONS, OPERATOR_POLICIES, SEMANTIC_POLICY_HASH
from .reference import (
    conditional,
    rolling_corr,
    rolling_cov,
    rolling_extreme,
    rolling_extreme_index,
    rolling_quantile,
    rolling_rank,
    rolling_rsquare,
    rolling_wma,
)

__all__ = [
    "DIFFERENCE_CODES",
    "EXPECTED_DISPOSITIONS",
    "OPERATOR_POLICIES",
    "OperatorParityEvidence",
    "OperatorSemanticPolicy",
    "PARITY_OPERATORS",
    "PARITY_POLICY_VERSION",
    "PARITY_SCHEMA_VERSION",
    "ParityObservation",
    "QlibSemanticParityReport",
    "SEMANTIC_POLICY_HASH",
    "SemanticParityStatus",
    "build_operator_evidence",
    "conditional",
    "rolling_corr",
    "rolling_cov",
    "rolling_extreme",
    "rolling_extreme_index",
    "rolling_quantile",
    "rolling_rank",
    "rolling_rsquare",
    "rolling_wma",
]
