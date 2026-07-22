"""Versioned cross-wave contracts for broad-but-trustworthy Alpha research."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Any, ClassVar, Mapping

from ..contracts import CanonicalContract, FailureCode
from ..contracts.base import fail, require_hash, require_identifier, require_text
from ..research_campaign import MethodModel, ResearchDepth, ResearchMode


RESEARCH_OS_SCHEMA_VERSION = "alphaforge.research-os/v1"


class StrEnum(str, Enum):
    """Python 3.10-compatible stable string enum."""


class AnswerLevel(StrEnum):
    """The strongest claim an answer section is allowed to make."""

    CONCEPTUAL = "CONCEPTUAL"
    SOURCED_RESEARCH = "SOURCED_RESEARCH"
    AUXILIARY_RESEARCH = "AUXILIARY_RESEARCH"
    OFFICIAL_EVALUATED = "OFFICIAL_EVALUATED"


class CapabilityKind(StrEnum):
    FROZEN_MARKET_DATA = "FROZEN_MARKET_DATA"
    DOCUMENT_RESEARCH = "DOCUMENT_RESEARCH"
    READ_ONLY_MARKET_INVESTIGATION = "READ_ONLY_MARKET_INVESTIGATION"
    ISOLATED_CODE_EXPERIMENT = "ISOLATED_CODE_EXPERIMENT"
    FACTOR_DAG = "FACTOR_DAG"
    PIT = "PIT"
    OFFICIAL_EVIDENCE = "OFFICIAL_EVIDENCE"
    PROMOTION = "PROMOTION"


class ValidationDisposition(StrEnum):
    NOT_EVALUATED = "NOT_EVALUATED"
    REJECT = "REJECT"
    REVISE = "REVISE"
    VALIDATED_RESEARCH = "VALIDATED_RESEARCH"
    CHALLENGER = "CHALLENGER"


ANSWER_LEVEL_AUTHORITY: Mapping[AnswerLevel, frozenset[str]] = MappingProxyType(
    {
        AnswerLevel.CONCEPTUAL: frozenset({"research_planner", "research_group", "llm"}),
        AnswerLevel.SOURCED_RESEARCH: frozenset({"document_service", "research_group", "llm"}),
        AnswerLevel.AUXILIARY_RESEARCH: frozenset({"isolated_sandbox", "qlib_adapter", "research_group"}),
        AnswerLevel.OFFICIAL_EVALUATED: frozenset({"evidence_service", "promotion_controller"}),
    }
)


def _enum(value: Any, enum_type: type[Enum], name: str) -> None:
    if not isinstance(value, enum_type):
        fail(FailureCode.INVALID_VALUE, f"{name} must be {enum_type.__name__}", field=name)


def _texts(value: tuple[str, ...], name: str, *, required: bool = False) -> None:
    if not isinstance(value, tuple) or (required and not value):
        fail(FailureCode.INVALID_VALUE, f"{name} must be a tuple", field=name)
    for item in value:
        require_text(item, name)
    if len(value) != len(set(value)):
        fail(FailureCode.INVALID_VALUE, f"{name} contains duplicates", field=name)


@dataclass(frozen=True)
class DataNeed(CanonicalContract):
    field_name: str
    required: bool
    pit_required: bool = True
    acceptable_proxies: tuple[str, ...] = ()
    reason: str = ""
    schema_version: ClassVar[str] = RESEARCH_OS_SCHEMA_VERSION

    def __post_init__(self) -> None:
        require_identifier(self.field_name, "field_name")
        _texts(self.acceptable_proxies, "acceptable_proxies")
        if self.reason:
            require_text(self.reason, "reason")
        if self.required and not self.pit_required:
            fail(FailureCode.INVALID_VALUE, "required official data must require PIT", field="pit_required")


@dataclass(frozen=True)
class DataRequirementPlan(CanonicalContract):
    universe: tuple[str, ...]
    factor_lookback_sessions: int
    label_horizon_sessions: int
    train_sessions: int
    validation_sessions: int
    embargo_sessions: int
    buffer_sessions: int
    minimum_total_sessions: int
    minimum_symbol_count: int
    needs: tuple[DataNeed, ...]
    schema_version: ClassVar[str] = RESEARCH_OS_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _texts(self.universe, "universe", required=True)
        for name in (
            "factor_lookback_sessions", "label_horizon_sessions", "train_sessions",
            "validation_sessions", "embargo_sessions", "buffer_sessions",
            "minimum_total_sessions", "minimum_symbol_count",
        ):
            value = getattr(self, name)
            if not isinstance(value, int) or value < (0 if name in {"embargo_sessions", "buffer_sessions"} else 1):
                fail(FailureCode.INVALID_VALUE, f"{name} is invalid", field=name)
        lower_bound = self.factor_lookback_sessions + self.train_sessions + self.validation_sessions
        lower_bound += self.label_horizon_sessions + self.embargo_sessions + self.buffer_sessions
        if self.minimum_total_sessions < lower_bound:
            fail(FailureCode.INVALID_VALUE, "minimum_total_sessions is smaller than the declared protocol", field="minimum_total_sessions")
        if not isinstance(self.needs, tuple) or not self.needs:
            fail(FailureCode.MISSING_REQUIRED_FIELD, "needs are required", field="needs")
        if len({need.field_name for need in self.needs}) != len(self.needs):
            fail(FailureCode.INVALID_VALUE, "duplicate data needs", field="needs")


@dataclass(frozen=True)
class CapabilityRequirement(CanonicalContract):
    capability: CapabilityKind
    required: bool
    reason: str
    schema_version: ClassVar[str] = RESEARCH_OS_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _enum(self.capability, CapabilityKind, "capability")
        require_text(self.reason, "reason")


@dataclass(frozen=True)
class ResearchIntent(CanonicalContract):
    question: str
    mode: ResearchMode
    depth: ResearchDepth
    factor_lookback_sessions: int | None
    label_horizon_sessions: int
    requested_outputs: tuple[str, ...]
    schema_version: ClassVar[str] = RESEARCH_OS_SCHEMA_VERSION

    def __post_init__(self) -> None:
        require_text(self.question, "question")
        _enum(self.mode, ResearchMode, "mode")
        _enum(self.depth, ResearchDepth, "depth")
        if self.factor_lookback_sessions is not None and self.factor_lookback_sessions <= 0:
            fail(FailureCode.INVALID_VALUE, "factor_lookback_sessions must be positive", field="factor_lookback_sessions")
        if self.label_horizon_sessions <= 0:
            fail(FailureCode.INVALID_VALUE, "label_horizon_sessions must be positive", field="label_horizon_sessions")
        _texts(self.requested_outputs, "requested_outputs", required=True)


@dataclass(frozen=True)
class ResearchQuestionAssessment(CanonicalContract):
    intent: ResearchIntent
    answerable_levels: tuple[AnswerLevel, ...]
    unavailable_official_reasons: tuple[str, ...] = ()
    clarification_questions: tuple[str, ...] = ()
    schema_version: ClassVar[str] = RESEARCH_OS_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if not isinstance(self.intent, ResearchIntent):
            fail(FailureCode.INVALID_VALUE, "intent must be ResearchIntent", field="intent")
        if not self.answerable_levels:
            fail(FailureCode.MISSING_REQUIRED_FIELD, "at least one answer level is required", field="answerable_levels")
        for level in self.answerable_levels:
            _enum(level, AnswerLevel, "answerable_levels")
        _texts(self.unavailable_official_reasons, "unavailable_official_reasons")
        _texts(self.clarification_questions, "clarification_questions")
        if AnswerLevel.OFFICIAL_EVALUATED not in self.answerable_levels and not self.unavailable_official_reasons:
            fail(FailureCode.MISSING_REQUIRED_FIELD, "official limitations must be explicit", field="unavailable_official_reasons")


@dataclass(frozen=True)
class ResearchGroupAssignment(CanonicalContract):
    group_id: str
    role: str
    score: float
    reasons: tuple[str, ...]
    tool_whitelist: tuple[str, ...]
    candidate_budget: int
    schema_version: ClassVar[str] = RESEARCH_OS_SCHEMA_VERSION

    def __post_init__(self) -> None:
        require_identifier(self.group_id, "group_id")
        require_text(self.role, "role")
        if not isinstance(self.score, (int, float)) or not 0.0 <= float(self.score) <= 1.0:
            fail(FailureCode.INVALID_VALUE, "score must be in [0, 1]", field="score")
        _texts(self.reasons, "reasons", required=True)
        _texts(self.tool_whitelist, "tool_whitelist", required=True)
        if self.candidate_budget < 1 or self.candidate_budget > 3:
            fail(FailureCode.INVALID_VALUE, "candidate_budget must be 1..3", field="candidate_budget")


@dataclass(frozen=True)
class ResearchRoutePlan(CanonicalContract):
    campaign_hash: str
    assignments: tuple[ResearchGroupAssignment, ...]
    method_models: tuple[MethodModel, ...]
    capability_requirements: tuple[CapabilityRequirement, ...]
    schema_version: ClassVar[str] = RESEARCH_OS_SCHEMA_VERSION

    def __post_init__(self) -> None:
        require_hash(self.campaign_hash, "campaign_hash")
        if not isinstance(self.assignments, tuple) or not 1 <= len(self.assignments) <= 6:
            fail(FailureCode.INVALID_VALUE, "select one to six research groups", field="assignments")
        if len({assignment.group_id for assignment in self.assignments}) != len(self.assignments):
            fail(FailureCode.INVALID_VALUE, "duplicate research group", field="assignments")
        if not self.method_models:
            fail(FailureCode.MISSING_REQUIRED_FIELD, "method_models are required", field="method_models")
        for model in self.method_models:
            _enum(model, MethodModel, "method_models")


@dataclass(frozen=True)
class ResearchGroupSubmission(CanonicalContract):
    campaign_hash: str
    group_id: str
    hypothesis_hash: str
    candidate_draft_hashes: tuple[str, ...]
    answer_level: AnswerLevel
    source_refs: tuple[str, ...] = ()
    schema_version: ClassVar[str] = RESEARCH_OS_SCHEMA_VERSION

    def __post_init__(self) -> None:
        require_hash(self.campaign_hash, "campaign_hash")
        require_identifier(self.group_id, "group_id")
        require_hash(self.hypothesis_hash, "hypothesis_hash")
        if not 1 <= len(self.candidate_draft_hashes) <= 3:
            fail(FailureCode.INVALID_VALUE, "each group must submit one to three candidates", field="candidate_draft_hashes")
        for value in self.candidate_draft_hashes:
            require_hash(value, "candidate_draft_hashes")
        _enum(self.answer_level, AnswerLevel, "answer_level")
        if self.answer_level is AnswerLevel.OFFICIAL_EVALUATED:
            fail(FailureCode.UNAUTHORIZED_CAPABILITY, "research groups cannot emit official evidence", field="answer_level")
        _texts(self.source_refs, "source_refs")


@dataclass(frozen=True)
class CandidateValidationRequest(CanonicalContract):
    campaign_hash: str
    group_id: str
    candidate_draft_hash: str
    data_plan_hash: str
    protocol_hash: str
    family_id: str
    schema_version: ClassVar[str] = RESEARCH_OS_SCHEMA_VERSION

    def __post_init__(self) -> None:
        for name in ("campaign_hash", "candidate_draft_hash", "data_plan_hash", "protocol_hash"):
            require_hash(getattr(self, name), name)
        require_identifier(self.group_id, "group_id")
        require_identifier(self.family_id, "family_id")


@dataclass(frozen=True)
class ValidationResultRef(CanonicalContract):
    request_hash: str
    evidence_pack_hash: str | None
    disposition: ValidationDisposition
    failure_codes: tuple[str, ...] = ()
    schema_version: ClassVar[str] = RESEARCH_OS_SCHEMA_VERSION

    def __post_init__(self) -> None:
        require_hash(self.request_hash, "request_hash")
        _enum(self.disposition, ValidationDisposition, "disposition")
        if self.evidence_pack_hash is not None:
            require_hash(self.evidence_pack_hash, "evidence_pack_hash")
        if self.disposition is not ValidationDisposition.NOT_EVALUATED and self.evidence_pack_hash is None:
            fail(FailureCode.MISSING_REQUIRED_FIELD, "evaluated result requires evidence_pack_hash", field="evidence_pack_hash")
        _texts(self.failure_codes, "failure_codes")


@dataclass(frozen=True)
class AnswerSection(CanonicalContract):
    title: str
    level: AnswerLevel
    authority: str
    content: str
    artifact_hashes: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()
    schema_version: ClassVar[str] = RESEARCH_OS_SCHEMA_VERSION

    def __post_init__(self) -> None:
        require_text(self.title, "title")
        _enum(self.level, AnswerLevel, "level")
        require_identifier(self.authority, "authority")
        require_text(self.content, "content")
        if self.authority not in ANSWER_LEVEL_AUTHORITY[self.level]:
            fail(FailureCode.UNAUTHORIZED_CAPABILITY, "authority cannot emit this answer level", field="authority")
        for value in self.artifact_hashes:
            require_hash(value, "artifact_hashes")
        _texts(self.limitations, "limitations")
        if self.level is not AnswerLevel.OFFICIAL_EVALUATED and not self.limitations:
            fail(FailureCode.MISSING_REQUIRED_FIELD, "non-official answer sections require limitations", field="limitations")
