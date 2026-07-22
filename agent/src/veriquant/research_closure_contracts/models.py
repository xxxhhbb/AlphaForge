"""Formal research-closure contracts for AlphaForge.

The contracts in this module separate an operational run result from the
research conclusion.  A run can complete cleanly while every candidate is
rejected or sent back for revision.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from types import MappingProxyType
from typing import Any, ClassVar, Mapping

from ..contracts import CanonicalContract, Component, EvidenceClass, FailureCode
from ..contracts.base import (
    fail,
    freeze_mapping,
    require_aware_datetime,
    require_finite_number,
    require_hash,
    require_identifier,
    require_nonempty_tuple,
    require_text,
)


RESEARCH_CLOSURE_SCHEMA_VERSION = "alphaforge.research-closure/v1"
APPROVED_PROXY_VENDOR_LABEL = "APPROVED_PROXY_DATA_VENDOR"


class StrEnum(str, Enum):
    """Python 3.10-compatible string enum with stable JSON values."""


class ResearchRunStatus(StrEnum):
    COMPLETED = "COMPLETED"
    PARTIAL = "PARTIAL"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"


class ResearchOutcomeStatus(StrEnum):
    REJECT = "REJECT"
    REVISE = "REVISE"
    VALIDATED_RESEARCH = "VALIDATED_RESEARCH"
    CHALLENGER = "CHALLENGER"
    NO_OFFICIAL_EVIDENCE = "NO_OFFICIAL_EVIDENCE"


class ResearchabilityStatus(StrEnum):
    RESEARCHABLE = "RESEARCHABLE"
    NEEDS_MORE_INPUT = "NEEDS_MORE_INPUT"
    BLOCKED = "BLOCKED"
    UNAVAILABLE = "UNAVAILABLE"


class CandidateAttemptStatus(StrEnum):
    RESERVED = "RESERVED"
    DRAFT_REJECTED = "DRAFT_REJECTED"
    PARSE_REJECTED = "PARSE_REJECTED"
    PIT_REJECTED = "PIT_REJECTED"
    EVALUATION_REJECTED = "EVALUATION_REJECTED"
    OFFICIAL_EVIDENCE_READY = "OFFICIAL_EVIDENCE_READY"
    CONSUMED_FAILED = "CONSUMED_FAILED"


class MetricDirection(StrEnum):
    HIGHER_IS_BETTER = "HIGHER_IS_BETTER"
    LOWER_IS_BETTER = "LOWER_IS_BETTER"
    TARGET_ZERO = "TARGET_ZERO"
    INFORMATIONAL = "INFORMATIONAL"


class ResearchClosureFailureCode(StrEnum):
    MISSING_DATA_SCOPE = "MISSING_DATA_SCOPE"
    DATASET_NOT_VERIFIED = "DATASET_NOT_VERIFIED"
    OFFICIAL_TUSHARE_MISLABEL = "OFFICIAL_TUSHARE_MISLABEL"
    AUXILIARY_METRIC_MASQUERADE = "AUXILIARY_METRIC_MASQUERADE"
    NO_OFFICIAL_EVIDENCE = "NO_OFFICIAL_EVIDENCE"
    MISSING_PROMOTION_RECEIPT = "MISSING_PROMOTION_RECEIPT"
    SELF_APPROVAL = "SELF_APPROVAL"
    LLM_AUTHORITY_VIOLATION = "LLM_AUTHORITY_VIOLATION"
    USER_LABEL_FORBIDDEN_TERM = "USER_LABEL_FORBIDDEN_TERM"


_PROPOSER_COMPONENTS = frozenset(
    {
        Component.LLM_RESEARCHER,
        Component.RESEARCH_DESK,
        Component.QUANTAALPHA,
        Component.QLIB,
    }
)
_FORBIDDEN_USER_TERMS = ("Desk", "Router", "router", "Swarm", "swarm", "pipeline", "Pipeline")


def _coerce_enum(value: Any, enum_type: type[Enum], field_name: str) -> Enum:
    if isinstance(value, enum_type):
        return value
    try:
        return enum_type(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} has invalid enum value") from exc


def _text_tuple(value: tuple[str, ...], field_name: str, *, nonempty: bool = True) -> tuple[str, ...]:
    if not isinstance(value, tuple) or (nonempty and not value):
        fail(FailureCode.INVALID_VALUE, f"{field_name} must be a tuple")
    for item in value:
        require_text(item, field_name)
    if len(set(value)) != len(value):
        fail(FailureCode.INVALID_VALUE, f"{field_name} contains duplicates")
    return value


def _hash_tuple(value: tuple[str, ...], field_name: str, *, nonempty: bool = False) -> tuple[str, ...]:
    _text_tuple(value, field_name, nonempty=nonempty)
    for item in value:
        require_hash(item, field_name)
    return value


def _component(value: Component | str, field_name: str) -> Component:
    if isinstance(value, Component):
        return value
    try:
        return Component(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} has invalid component") from exc


def _reject_official_tushare(label: str, *, official_flag: bool) -> None:
    require_identifier(label, "provider_source_label")
    if official_flag or "OFFICIAL_TUSHARE" in label:
        fail(
            FailureCode.UNAUTHORIZED_CAPABILITY,
            "approved proxy data must not be represented as official Tushare",
            research_closure_failure=ResearchClosureFailureCode.OFFICIAL_TUSHARE_MISLABEL.value,
        )


def _reject_user_terms(value: str, field_name: str) -> None:
    for token in _FORBIDDEN_USER_TERMS:
        if token in value:
            fail(
                FailureCode.UNAUTHORIZED_CAPABILITY,
                "user-facing vocabulary contains engineering terms",
                field=field_name,
                research_closure_failure=ResearchClosureFailureCode.USER_LABEL_FORBIDDEN_TERM.value,
            )


@dataclass(frozen=True, kw_only=True)
class ResearchabilityVerdict(CanonicalContract):
    SCHEMA_VERSION: ClassVar[str] = RESEARCH_CLOSURE_SCHEMA_VERSION

    verdict_id: str
    status: ResearchabilityStatus
    question_hash: str
    dataset_ready: bool
    calendar_id: str | None
    universe: tuple[str, ...] = ()
    horizon: str | None = None
    decision_time: datetime | None = None
    available_fields: tuple[str, ...] = ()
    failure: ResearchClosureFailureCode | None = None
    message: str = ""

    def __post_init__(self) -> None:
        require_identifier(self.verdict_id, "verdict_id")
        object.__setattr__(self, "status", _coerce_enum(self.status, ResearchabilityStatus, "status"))
        require_hash(self.question_hash, "question_hash")
        if not isinstance(self.dataset_ready, bool):
            fail(FailureCode.INVALID_VALUE, "dataset_ready must be boolean")
        _text_tuple(self.universe, "universe", nonempty=False)
        _text_tuple(self.available_fields, "available_fields", nonempty=False)
        if self.status is ResearchabilityStatus.RESEARCHABLE:
            if not self.dataset_ready or not self.calendar_id or not self.universe or not self.horizon or self.decision_time is None:
                fail(
                    FailureCode.MISSING_REQUIRED_FIELD,
                    "researchable verdict requires dataset, calendar, universe, horizon, and decision_time",
                    research_closure_failure=ResearchClosureFailureCode.MISSING_DATA_SCOPE.value,
                )
            require_identifier(self.calendar_id, "calendar_id")
            require_text(self.horizon, "horizon")
            require_aware_datetime(self.decision_time, "decision_time")
            if self.failure is not None:
                fail(FailureCode.INVALID_STATE, "researchable verdict cannot carry failure")
        else:
            if self.failure is None:
                fail(FailureCode.INVALID_STATE, "non-researchable verdict requires typed failure")
        if self.failure is not None:
            object.__setattr__(
                self,
                "failure",
                _coerce_enum(self.failure, ResearchClosureFailureCode, "failure"),
            )
        if self.message:
            require_text(self.message, "message")
            _reject_user_terms(self.message, "message")


@dataclass(frozen=True, kw_only=True)
class VerifiedDatasetReceipt(CanonicalContract):
    SCHEMA_VERSION: ClassVar[str] = RESEARCH_CLOSURE_SCHEMA_VERSION
    ALLOWED_PRODUCERS: ClassVar[frozenset[Component] | None] = frozenset({Component.DATA_SERVICE})

    receipt_id: str
    provider_source_label: str
    official_tushare_source: bool
    snapshot_hash: str
    manifest_hash: str
    calendar_hash: str
    universe_hash: str
    content_hash: str
    calendar_id: str
    universe: tuple[str, ...]
    fields: tuple[str, ...]
    row_count: int
    session_count: int
    symbol_count: int
    decision_time: datetime
    start_time: datetime
    end_time: datetime
    coverage_by_field: Mapping[str, float] = field(default_factory=dict)
    research_visible_summary_hash: str | None = None

    def __post_init__(self) -> None:
        require_identifier(self.receipt_id, "receipt_id")
        _reject_official_tushare(self.provider_source_label, official_flag=self.official_tushare_source)
        if self.provider_source_label != APPROVED_PROXY_VENDOR_LABEL:
            fail(FailureCode.INVALID_VALUE, "real A-share V1 data must use approved proxy vendor label")
        for name in ("snapshot_hash", "manifest_hash", "calendar_hash", "universe_hash", "content_hash"):
            require_hash(getattr(self, name), name)
        require_identifier(self.calendar_id, "calendar_id")
        _text_tuple(self.universe, "universe")
        _text_tuple(self.fields, "fields")
        for name in ("row_count", "session_count", "symbol_count"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                fail(FailureCode.INVALID_VALUE, f"{name} must be a positive integer")
        require_aware_datetime(self.decision_time, "decision_time")
        require_aware_datetime(self.start_time, "start_time")
        require_aware_datetime(self.end_time, "end_time")
        if self.start_time > self.end_time or self.end_time > self.decision_time:
            fail(FailureCode.INVALID_TIME_ORDER, "dataset times must be start <= end <= decision_time")
        coverage = freeze_mapping(self.coverage_by_field, "coverage_by_field", numeric_values=True)
        for key, value in coverage.items():
            require_identifier(key, "coverage_by_field.key")
            if value < 0.0 or value > 1.0:
                fail(FailureCode.INVALID_VALUE, "coverage must be between 0 and 1")
        object.__setattr__(self, "coverage_by_field", MappingProxyType(dict(sorted(coverage.items()))))
        if self.research_visible_summary_hash is not None:
            require_hash(self.research_visible_summary_hash, "research_visible_summary_hash")


@dataclass(frozen=True, kw_only=True)
class CandidateAttemptReceipt(CanonicalContract):
    SCHEMA_VERSION: ClassVar[str] = RESEARCH_CLOSURE_SCHEMA_VERSION

    attempt_id: str
    candidate_id: str
    family_id: str
    status: CandidateAttemptStatus
    draft_hash: str
    trial_identity_hash: str
    budget_consumed: int
    terminal_event_hash: str | None = None
    failure: ResearchClosureFailureCode | None = None

    def __post_init__(self) -> None:
        for name in ("attempt_id", "candidate_id", "family_id"):
            require_identifier(getattr(self, name), name)
        object.__setattr__(self, "status", _coerce_enum(self.status, CandidateAttemptStatus, "status"))
        require_hash(self.draft_hash, "draft_hash")
        require_hash(self.trial_identity_hash, "trial_identity_hash")
        if isinstance(self.budget_consumed, bool) or not isinstance(self.budget_consumed, int) or self.budget_consumed < 0:
            fail(FailureCode.INVALID_VALUE, "budget_consumed must be a non-negative integer")
        if self.status is not CandidateAttemptStatus.RESERVED and self.budget_consumed <= 0:
            fail(FailureCode.INVALID_STATE, "terminal attempts must consume family budget")
        if self.terminal_event_hash is not None:
            require_hash(self.terminal_event_hash, "terminal_event_hash")
        if self.failure is not None:
            object.__setattr__(
                self,
                "failure",
                _coerce_enum(self.failure, ResearchClosureFailureCode, "failure"),
            )


@dataclass(frozen=True, kw_only=True)
class OfficialMetricRecord(CanonicalContract):
    SCHEMA_VERSION: ClassVar[str] = RESEARCH_CLOSURE_SCHEMA_VERSION
    ALLOWED_PRODUCERS: ClassVar[frozenset[Component] | None] = frozenset({Component.EVIDENCE_SERVICE})

    metric_id: str
    candidate_id: str
    metric_name: str
    value: float
    direction: MetricDirection
    evidence_hash: str
    split: str
    policy_version: str
    evidence_class: EvidenceClass = EvidenceClass.OFFICIAL_EVIDENCE
    available_to_researcher: bool = True

    def __post_init__(self) -> None:
        for name in ("metric_id", "candidate_id", "metric_name", "split", "policy_version"):
            require_identifier(getattr(self, name), name)
        require_finite_number(self.value, "value")
        object.__setattr__(self, "direction", _coerce_enum(self.direction, MetricDirection, "direction"))
        require_hash(self.evidence_hash, "evidence_hash")
        object.__setattr__(self, "evidence_class", _coerce_enum(self.evidence_class, EvidenceClass, "evidence_class"))
        if self.evidence_class is not EvidenceClass.OFFICIAL_EVIDENCE:
            fail(
                FailureCode.AUXILIARY_MASQUERADE,
                "auxiliary or smoke metrics cannot be represented as official metrics",
                research_closure_failure=ResearchClosureFailureCode.AUXILIARY_METRIC_MASQUERADE.value,
            )
        if not isinstance(self.available_to_researcher, bool):
            fail(FailureCode.INVALID_VALUE, "available_to_researcher must be boolean")


@dataclass(frozen=True, kw_only=True)
class OpaquePromotionReceiptRef(CanonicalContract):
    SCHEMA_VERSION: ClassVar[str] = RESEARCH_CLOSURE_SCHEMA_VERSION
    ALLOWED_PRODUCERS: ClassVar[frozenset[Component] | None] = frozenset({Component.PROMOTION_SERVICE})

    receipt_id: str
    receipt_hash: str
    policy_version: str
    approver_component: Component = Component.PROMOTION_SERVICE
    sealed_detail_disclosed: bool = False

    def __post_init__(self) -> None:
        require_identifier(self.receipt_id, "receipt_id")
        require_hash(self.receipt_hash, "receipt_hash")
        require_identifier(self.policy_version, "policy_version")
        object.__setattr__(self, "approver_component", _component(self.approver_component, "approver_component"))
        if self.approver_component is not Component.PROMOTION_SERVICE:
            fail(FailureCode.UNAUTHORIZED_CAPABILITY, "promotion receipt must be produced by Promotion Service")
        if self.sealed_detail_disclosed is not False:
            fail(FailureCode.UNAUTHORIZED_CAPABILITY, "opaque promotion receipt cannot disclose sealed details")


@dataclass(frozen=True, kw_only=True)
class CandidateResearchOutcome(CanonicalContract):
    SCHEMA_VERSION: ClassVar[str] = RESEARCH_CLOSURE_SCHEMA_VERSION

    candidate_id: str
    proposer_component: Component
    attempt: CandidateAttemptReceipt
    outcome: ResearchOutcomeStatus
    official_metrics: tuple[OfficialMetricRecord, ...] = ()
    promotion_receipt: OpaquePromotionReceiptRef | None = None
    reason: str = ""
    next_actions: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        require_identifier(self.candidate_id, "candidate_id")
        object.__setattr__(self, "proposer_component", _component(self.proposer_component, "proposer_component"))
        if self.proposer_component not in _PROPOSER_COMPONENTS:
            fail(FailureCode.INVALID_VALUE, "candidate proposer must be a proposal component")
        if not isinstance(self.attempt, CandidateAttemptReceipt):
            fail(FailureCode.INVALID_VALUE, "attempt must be CandidateAttemptReceipt")
        object.__setattr__(self, "outcome", _coerce_enum(self.outcome, ResearchOutcomeStatus, "outcome"))
        if not isinstance(self.official_metrics, tuple):
            fail(FailureCode.INVALID_VALUE, "official_metrics must be a tuple")
        for metric in self.official_metrics:
            if not isinstance(metric, OfficialMetricRecord):
                fail(FailureCode.INVALID_VALUE, "official_metrics must contain OfficialMetricRecord")
            if metric.candidate_id != self.candidate_id:
                fail(FailureCode.INVALID_VALUE, "metric candidate_id mismatch")
        if self.outcome in {ResearchOutcomeStatus.VALIDATED_RESEARCH, ResearchOutcomeStatus.CHALLENGER} and not self.official_metrics:
            fail(
                FailureCode.INVALID_STATE,
                "validated research or challenger requires official evidence",
                research_closure_failure=ResearchClosureFailureCode.NO_OFFICIAL_EVIDENCE.value,
            )
        if self.outcome is ResearchOutcomeStatus.CHALLENGER:
            if self.promotion_receipt is None:
                fail(
                    FailureCode.MISSING_REQUIRED_FIELD,
                    "challenger requires an opaque independent promotion receipt",
                    research_closure_failure=ResearchClosureFailureCode.MISSING_PROMOTION_RECEIPT.value,
                )
            if self.proposer_component is self.promotion_receipt.approver_component:
                fail(
                    FailureCode.SELF_APPROVAL,
                    "proposer cannot approve its own candidate",
                    research_closure_failure=ResearchClosureFailureCode.SELF_APPROVAL.value,
                )
        elif self.promotion_receipt is not None:
            fail(FailureCode.INVALID_STATE, "promotion receipt is only valid for challenger outcome")
        if self.reason:
            require_text(self.reason, "reason")
            _reject_user_terms(self.reason, "reason")
        _text_tuple(self.next_actions, "next_actions", nonempty=False)
        for index, action in enumerate(self.next_actions):
            _reject_user_terms(action, f"next_actions[{index}]")


@dataclass(frozen=True, kw_only=True)
class CampaignResearchOutcome(CanonicalContract):
    SCHEMA_VERSION: ClassVar[str] = RESEARCH_CLOSURE_SCHEMA_VERSION

    campaign_id: str
    run_id: str
    run_status: ResearchRunStatus
    research_outcome: ResearchOutcomeStatus
    dataset_receipt_hash: str | None
    selected_research_groups: tuple[str, ...]
    candidate_outcomes: tuple[CandidateResearchOutcome, ...]
    report_hash: str | None = None
    artifact_hashes: tuple[str, ...] = ()
    summary: str = ""

    def __post_init__(self) -> None:
        require_identifier(self.campaign_id, "campaign_id")
        require_identifier(self.run_id, "run_id")
        object.__setattr__(self, "run_status", _coerce_enum(self.run_status, ResearchRunStatus, "run_status"))
        object.__setattr__(
            self,
            "research_outcome",
            _coerce_enum(self.research_outcome, ResearchOutcomeStatus, "research_outcome"),
        )
        if self.dataset_receipt_hash is not None:
            require_hash(self.dataset_receipt_hash, "dataset_receipt_hash")
        _text_tuple(self.selected_research_groups, "selected_research_groups", nonempty=False)
        for group in self.selected_research_groups:
            _reject_user_terms(group, "selected_research_groups")
        if not isinstance(self.candidate_outcomes, tuple):
            fail(FailureCode.INVALID_VALUE, "candidate_outcomes must be a tuple")
        for item in self.candidate_outcomes:
            if not isinstance(item, CandidateResearchOutcome):
                fail(FailureCode.INVALID_VALUE, "candidate_outcomes must contain CandidateResearchOutcome")
        if self.research_outcome in {ResearchOutcomeStatus.VALIDATED_RESEARCH, ResearchOutcomeStatus.CHALLENGER}:
            if not any(item.outcome is self.research_outcome for item in self.candidate_outcomes):
                fail(FailureCode.INVALID_STATE, "campaign research outcome requires matching candidate outcome")
            if self.dataset_receipt_hash is None:
                fail(FailureCode.INVALID_STATE, "validated campaign outcome requires verified dataset receipt")
        if self.report_hash is not None:
            require_hash(self.report_hash, "report_hash")
        _hash_tuple(self.artifact_hashes, "artifact_hashes", nonempty=False)
        if self.summary:
            require_text(self.summary, "summary")
            _reject_user_terms(self.summary, "summary")


@dataclass(frozen=True, kw_only=True)
class UserFacingVocabulary(CanonicalContract):
    SCHEMA_VERSION: ClassVar[str] = RESEARCH_CLOSURE_SCHEMA_VERSION

    product_name: str = "AlphaForge（千因研究院）"
    specialist_group_label: str = "研究小组"
    route_label: str = "研究路线"
    evidence_label: str = "机器证据"
    verdict_label: str = "评审结论"
    report_label: str = "正式研究报告"
    group_labels: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in (
            "product_name",
            "specialist_group_label",
            "route_label",
            "evidence_label",
            "verdict_label",
            "report_label",
        ):
            value = getattr(self, name)
            require_text(value, name)
            _reject_user_terms(value, name)
        labels = freeze_mapping(self.group_labels, "group_labels")
        for key, value in labels.items():
            require_identifier(key, "group_labels.key")
            require_text(value, "group_labels.value")
            _reject_user_terms(value, "group_labels.value")
        object.__setattr__(self, "group_labels", MappingProxyType(dict(sorted(labels.items()))))


USER_FACING_VOCABULARY = UserFacingVocabulary(
    group_labels={
        "fundamental": "基本面研究小组",
        "expectations_events": "预期与事件研究小组",
        "market_structure": "市场结构研究小组",
        "alternative_data": "另类数据研究小组",
        "macro_regime": "宏观状态研究小组",
        "meta_alpha": "元阿尔法研究小组",
    }
)
