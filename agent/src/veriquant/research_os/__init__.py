"""Shared contracts for the AlphaForge research operating system.

This package is an orchestration boundary only.  It deliberately owns no
FactorDAG execution, PIT validation, official metrics, promotion, portfolio,
risk, execution, or ledger truth.
"""

from .models import (
    ANSWER_LEVEL_AUTHORITY,
    RESEARCH_OS_SCHEMA_VERSION,
    AnswerLevel,
    AnswerSection,
    CapabilityKind,
    CapabilityRequirement,
    CandidateValidationRequest,
    DataNeed,
    DataRequirementPlan,
    ResearchGroupAssignment,
    ResearchGroupSubmission,
    ResearchIntent,
    ResearchQuestionAssessment,
    ResearchRoutePlan,
    ValidationDisposition,
    ValidationResultRef,
)

__all__ = [
    "ANSWER_LEVEL_AUTHORITY",
    "RESEARCH_OS_SCHEMA_VERSION",
    "AnswerLevel",
    "AnswerSection",
    "CapabilityKind",
    "CapabilityRequirement",
    "CandidateValidationRequest",
    "DataNeed",
    "DataRequirementPlan",
    "ResearchGroupAssignment",
    "ResearchGroupSubmission",
    "ResearchIntent",
    "ResearchQuestionAssessment",
    "ResearchRoutePlan",
    "ValidationDisposition",
    "ValidationResultRef",
]
