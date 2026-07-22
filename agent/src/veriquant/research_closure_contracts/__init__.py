"""Stable contracts for AlphaForge formal research closure.

This package is an integration boundary.  It does not calculate factors,
official evidence, promotion, risk, orders, execution, or ledger truth.
"""

from .models import (
    APPROVED_PROXY_VENDOR_LABEL,
    USER_FACING_VOCABULARY,
    CandidateAttemptReceipt,
    CandidateAttemptStatus,
    CandidateResearchOutcome,
    CampaignResearchOutcome,
    MetricDirection,
    OfficialMetricRecord,
    OpaquePromotionReceiptRef,
    ResearchClosureFailureCode,
    ResearchOutcomeStatus,
    ResearchRunStatus,
    ResearchabilityStatus,
    ResearchabilityVerdict,
    UserFacingVocabulary,
    VerifiedDatasetReceipt,
)

__all__ = [
    "APPROVED_PROXY_VENDOR_LABEL",
    "USER_FACING_VOCABULARY",
    "CandidateAttemptReceipt",
    "CandidateAttemptStatus",
    "CandidateResearchOutcome",
    "CampaignResearchOutcome",
    "MetricDirection",
    "OfficialMetricRecord",
    "OpaquePromotionReceiptRef",
    "ResearchClosureFailureCode",
    "ResearchOutcomeStatus",
    "ResearchRunStatus",
    "ResearchabilityStatus",
    "ResearchabilityVerdict",
    "UserFacingVocabulary",
    "VerifiedDatasetReceipt",
]
