"""Bridge accepted research candidates into official deterministic evidence."""

from .bridge import (
    BRIDGE_SCHEMA_VERSION,
    CandidateEvidenceValidationRequest,
    CandidateEvidenceValidationResult,
    FrozenEvidenceInputIdentity,
    ResearchEvidenceBridge,
    VisibleResearchDisposition,
    ValidationBridgeFailureCode,
    ValidationBridgeStatus,
    ValidationResultRef,
    VisibleComparisonMetrics,
)

__all__ = [
    "BRIDGE_SCHEMA_VERSION",
    "CandidateEvidenceValidationRequest",
    "CandidateEvidenceValidationResult",
    "FrozenEvidenceInputIdentity",
    "ResearchEvidenceBridge",
    "VisibleResearchDisposition",
    "ValidationBridgeFailureCode",
    "ValidationBridgeStatus",
    "ValidationResultRef",
    "VisibleComparisonMetrics",
]
