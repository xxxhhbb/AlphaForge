"""Approved-proxy real-data adapter to deterministic visible Official Evidence."""

from .models import (
    FactorAttemptStatus,
    RealOfficialEvidenceFactorResult,
    RealOfficialEvidenceRun,
)
from .service import (
    APPROVED_QUALIFICATION_HASH,
    PINNED_AUXILIARY_RESULT_HASH,
    PinnedRealOfficialInputPaths,
    RealOfficialEvidenceAdapter,
    default_factor_candidates,
)

__all__ = [
    "APPROVED_QUALIFICATION_HASH",
    "PINNED_AUXILIARY_RESULT_HASH",
    "FactorAttemptStatus",
    "PinnedRealOfficialInputPaths",
    "RealOfficialEvidenceAdapter",
    "RealOfficialEvidenceFactorResult",
    "RealOfficialEvidenceRun",
    "default_factor_candidates",
]
