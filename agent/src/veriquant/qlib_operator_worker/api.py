"""Narrow public surface for the pinned Qlib operator worker."""

from .executor import execute_certified_qlib_operators
from .models import (
    QlibFactorScoreArtifact,
    QlibFactorScoreRow,
    QlibInMemoryFeatureRow,
    QlibOperatorExecutionRequest,
    QlibOperatorWorkerError,
    QlibOperatorWorkerFailure,
    QlibOperatorWorkerFailureCode,
    QlibRuntimeIdentity,
    WORKER_OPERATOR_NAMES,
    WORKER_POLICY_VERSION,
    WORKER_SCHEMA_VERSION,
    inspect_certified_program,
)

__all__ = [
    "QlibFactorScoreArtifact",
    "QlibFactorScoreRow",
    "QlibInMemoryFeatureRow",
    "QlibOperatorExecutionRequest",
    "QlibOperatorWorkerError",
    "QlibOperatorWorkerFailure",
    "QlibOperatorWorkerFailureCode",
    "QlibRuntimeIdentity",
    "WORKER_OPERATOR_NAMES",
    "WORKER_POLICY_VERSION",
    "WORKER_SCHEMA_VERSION",
    "execute_certified_qlib_operators",
    "inspect_certified_program",
]
