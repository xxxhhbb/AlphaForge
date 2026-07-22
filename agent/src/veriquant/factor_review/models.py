"""Canonical DRAFT-only contracts for independent factor review."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import ClassVar

from ..contracts import CanonicalContract, FailureCode
from ..contracts.base import fail, require_hash, require_identifier, require_text
from ..specialist_research import CandidatePreflightResult, SpecialistCandidateDraft


SCHEMA_VERSION = "alphaforge.factor-review/v1"


class FactorReviewVerdict(str, Enum):
    ACCEPT_FOR_CERTIFICATION = "ACCEPT_FOR_CERTIFICATION"
    REVISE = "REVISE"
    REJECT_DRAFT = "REJECT_DRAFT"


class FactorReviewFindingCode(str, Enum):
    MISSING_APPROVED_FIELD = "MISSING_APPROVED_FIELD"
    UNTRADEABLE_PROXY = "UNTRADEABLE_PROXY"
    EXPRESSION_HYPOTHESIS_MISMATCH = "EXPRESSION_HYPOTHESIS_MISMATCH"
    WINDOW_OUT_OF_POLICY = "WINDOW_OUT_OF_POLICY"
    HORIZON_OUT_OF_POLICY = "HORIZON_OUT_OF_POLICY"
    DUPLICATE_DRAFT = "DUPLICATE_DRAFT"
    HIGH_SIMILARITY = "HIGH_SIMILARITY"
    CERTIFICATION_REVISION_REQUIRED = "CERTIFICATION_REVISION_REQUIRED"
    CERTIFICATION_REJECTED_AFTER_REVISION = "CERTIFICATION_REJECTED_AFTER_REVISION"


@dataclass(frozen=True)
class FactorReviewPolicy(CanonicalContract):
    policy_id: str = "factor-review-visible-v1"
    max_lookback_sessions: int = 1260
    max_horizon_sessions: int = 252
    similarity_revision_threshold: float = 0.82
    forbidden_proxy_fields: tuple[str, ...] = (
        "future_return",
        "label",
        "target_return",
        "sealed_metric",
        "orders",
        "positions",
    )
    SCHEMA_VERSION: ClassVar[str] = SCHEMA_VERSION

    def __post_init__(self) -> None:
        require_identifier(self.policy_id, "policy_id")
        for name in ("max_lookback_sessions", "max_horizon_sessions"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                fail(FailureCode.INVALID_VALUE, f"{name} must be positive")
        if not isinstance(self.similarity_revision_threshold, float) or not 0.5 <= self.similarity_revision_threshold < 1.0:
            fail(FailureCode.INVALID_VALUE, "similarity threshold must be in [0.5, 1.0)")
        if not self.forbidden_proxy_fields or len(set(self.forbidden_proxy_fields)) != len(self.forbidden_proxy_fields):
            fail(FailureCode.INVALID_VALUE, "forbidden proxy fields must be unique and non-empty")
        for value in self.forbidden_proxy_fields:
            require_identifier(value, "forbidden_proxy_fields")


@dataclass(frozen=True)
class RegisteredDraftReference(CanonicalContract):
    candidate_id: str
    candidate_hash: str
    family_id: str
    normalized_expression: str
    SCHEMA_VERSION: ClassVar[str] = SCHEMA_VERSION

    def __post_init__(self) -> None:
        require_identifier(self.candidate_id, "candidate_id")
        require_hash(self.candidate_hash, "candidate_hash")
        require_identifier(self.family_id, "family_id")
        require_text(self.normalized_expression, "normalized_expression")


@dataclass(frozen=True)
class FactorReviewFinding(CanonicalContract):
    code: FactorReviewFindingCode
    severity: str
    message: str
    related_candidate_id: str | None = None
    SCHEMA_VERSION: ClassVar[str] = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if not isinstance(self.code, FactorReviewFindingCode):
            fail(FailureCode.INVALID_VALUE, "review finding code is invalid")
        if self.severity not in {"WARNING", "ERROR"}:
            fail(FailureCode.INVALID_VALUE, "review finding severity is invalid")
        require_text(self.message, "message")
        if self.related_candidate_id is not None:
            require_identifier(self.related_candidate_id, "related_candidate_id")


@dataclass(frozen=True)
class FactorRevisionAdvice(CanonicalContract):
    source_candidate_id: str
    source_candidate_hash: str
    revision_number: int
    reason_codes: tuple[str, ...]
    instructions: tuple[str, ...]
    SCHEMA_VERSION: ClassVar[str] = SCHEMA_VERSION

    def __post_init__(self) -> None:
        require_identifier(self.source_candidate_id, "source_candidate_id")
        require_hash(self.source_candidate_hash, "source_candidate_hash")
        if self.revision_number != 1:
            fail(FailureCode.INVALID_VALUE, "reviewer may recommend exactly one revision round")
        if not self.reason_codes or not self.instructions:
            fail(FailureCode.MISSING_REQUIRED_FIELD, "revision advice requires reasons and instructions")
        for value in self.reason_codes:
            require_identifier(value, "reason_codes")
        for value in self.instructions:
            require_text(value, "instructions")


@dataclass(frozen=True)
class FactorReviewRequest(CanonicalContract):
    proposer_id: str
    reviewer_id: str
    candidate: SpecialistCandidateDraft
    preflight: CandidatePreflightResult
    available_fields: tuple[str, ...]
    policy: FactorReviewPolicy
    registered_drafts: tuple[RegisteredDraftReference, ...] = ()
    previous_review_hash: str | None = None
    SCHEMA_VERSION: ClassVar[str] = SCHEMA_VERSION

    def __post_init__(self) -> None:
        require_identifier(self.proposer_id, "proposer_id")
        require_identifier(self.reviewer_id, "reviewer_id")
        if self.proposer_id == self.reviewer_id:
            fail(FailureCode.SELF_APPROVAL, "factor reviewer must be independent from proposer")
        if not isinstance(self.candidate, SpecialistCandidateDraft):
            fail(FailureCode.INVALID_VALUE, "candidate must be a SpecialistCandidateDraft")
        if not isinstance(self.preflight, CandidatePreflightResult):
            fail(FailureCode.INVALID_VALUE, "preflight must be CandidatePreflightResult")
        if self.preflight.candidate_id != self.candidate.candidate_id or self.preflight.draft_hash != self.candidate.artifact_hash:
            fail(FailureCode.INVALID_HASH, "preflight does not bind the reviewed candidate")
        if not self.available_fields or len(set(self.available_fields)) != len(self.available_fields):
            fail(FailureCode.INVALID_VALUE, "available_fields must be unique and non-empty")
        for value in self.available_fields:
            require_identifier(value, "available_fields")
        if not isinstance(self.policy, FactorReviewPolicy):
            fail(FailureCode.INVALID_VALUE, "policy must be FactorReviewPolicy")
        if self.candidate.revision_number == 0 and self.previous_review_hash is not None:
            fail(FailureCode.INVALID_STATE, "original draft cannot carry previous review identity")
        if self.candidate.revision_number > 0:
            if self.candidate.revision_number != 1 or self.previous_review_hash is None:
                fail(FailureCode.INVALID_STATE, "review path permits one bound revision with previous review hash")
            require_hash(self.previous_review_hash, "previous_review_hash")


@dataclass(frozen=True)
class FactorReviewDecision(CanonicalContract):
    candidate_id: str
    candidate_hash: str
    proposer_id: str
    reviewer_id: str
    verdict: FactorReviewVerdict
    policy_hash: str
    preflight_hash: str
    findings: tuple[FactorReviewFinding, ...]
    revision_advice: FactorRevisionAdvice | None
    parent_candidate_id: str | None
    revision_number: int
    previous_review_hash: str | None
    authority: str = "DRAFT_REVIEW_ONLY"
    SCHEMA_VERSION: ClassVar[str] = SCHEMA_VERSION

    def __post_init__(self) -> None:
        require_identifier(self.candidate_id, "candidate_id")
        require_hash(self.candidate_hash, "candidate_hash")
        require_identifier(self.proposer_id, "proposer_id")
        require_identifier(self.reviewer_id, "reviewer_id")
        if self.proposer_id == self.reviewer_id:
            fail(FailureCode.SELF_APPROVAL, "review decision cannot be self-issued")
        if not isinstance(self.verdict, FactorReviewVerdict):
            fail(FailureCode.INVALID_VALUE, "review verdict is invalid")
        require_hash(self.policy_hash, "policy_hash")
        require_hash(self.preflight_hash, "preflight_hash")
        if self.verdict is FactorReviewVerdict.ACCEPT_FOR_CERTIFICATION and (self.findings or self.revision_advice is not None):
            fail(FailureCode.INVALID_STATE, "accepted draft cannot carry unresolved findings")
        if self.verdict is FactorReviewVerdict.REVISE and self.revision_advice is None:
            fail(FailureCode.MISSING_REQUIRED_FIELD, "REVISE requires bounded revision advice")
        if self.verdict is FactorReviewVerdict.REJECT_DRAFT and self.revision_advice is not None:
            fail(FailureCode.INVALID_STATE, "rejected draft cannot request another revision")
        if self.parent_candidate_id is not None:
            require_identifier(self.parent_candidate_id, "parent_candidate_id")
        if self.revision_number not in {0, 1}:
            fail(FailureCode.INVALID_VALUE, "review lineage supports at most one revision")
        if self.previous_review_hash is not None:
            require_hash(self.previous_review_hash, "previous_review_hash")
        if self.authority != "DRAFT_REVIEW_ONLY":
            fail(FailureCode.UNAUTHORIZED_CAPABILITY, "reviewer has no Evidence or Promotion authority")

