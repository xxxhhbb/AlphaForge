"""Research Campaign contracts for the VeriQuant product layer.

This package defines the user-facing research wrapper around the existing
deterministic research kernel.  It does not compile FactorDAGs, validate PIT
snapshots, calculate evidence, promote candidates, construct portfolios, or
write a Trial Ledger.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from types import MappingProxyType
from typing import Any, ClassVar, Mapping

from ..contracts import CanonicalContract, Component, ContractValidationError, FailureCode
from ..contracts.base import (
    fail,
    freeze_mapping,
    require_aware_datetime,
    require_hash,
    require_identifier,
    require_nonempty_tuple,
    require_text,
)


RESEARCH_CAMPAIGN_SCHEMA_VERSION = "veriquant.research-campaign/v1"


class StrEnum(str, Enum):
    """Python 3.10-compatible string enum with stable JSON values."""


class ResearchMode(StrEnum):
    NEW_ALPHA_EXPLORATION = "NEW_ALPHA_EXPLORATION"
    VALIDATE_IDEA = "VALIDATE_IDEA"
    IMPROVE_EXISTING_FACTOR = "IMPROVE_EXISTING_FACTOR"
    PORTFOLIO_GAP_SEARCH = "PORTFOLIO_GAP_SEARCH"
    DIAGNOSE_FACTOR_PORTFOLIO = "DIAGNOSE_FACTOR_PORTFOLIO"
    EVENT_MARKET_TOPIC = "EVENT_MARKET_TOPIC"
    PAPER_STRATEGY_REPLICATION = "PAPER_STRATEGY_REPLICATION"
    NEW_DATA_SOURCE_EVALUATION = "NEW_DATA_SOURCE_EVALUATION"
    PRELAUNCH_REVIEW = "PRELAUNCH_REVIEW"
    MONITOR_DECAY_RETIREMENT = "MONITOR_DECAY_RETIREMENT"


class ResearchDepth(StrEnum):
    QUICK_EXPLORATION = "QUICK_EXPLORATION"
    STANDARD_RESEARCH = "STANDARD_RESEARCH"
    STRICT_VALIDATION = "STRICT_VALIDATION"


class InteractionStyle(StrEnum):
    GUIDED = "GUIDED"
    STANDARD = "STANDARD"
    BATCH = "BATCH"


class MethodModel(StrEnum):
    HYPOTHESIS_DRIVEN = "HYPOTHESIS_DRIVEN"
    DATA_ANOMALY = "DATA_ANOMALY"
    RESIDUAL_INCREMENTAL = "RESIDUAL_INCREMENTAL"
    DIAGNOSTIC_ATTRIBUTION = "DIAGNOSTIC_ATTRIBUTION"
    EVENT_STUDY = "EVENT_STUDY"
    REGIME_CONDITIONING = "REGIME_CONDITIONING"
    BOUNDED_EVOLUTION = "BOUNDED_EVOLUTION"
    REPLICATION = "REPLICATION"
    ADVERSARIAL_FALSIFICATION = "ADVERSARIAL_FALSIFICATION"
    ONLINE_MONITORING = "ONLINE_MONITORING"


class CampaignState(StrEnum):
    DRAFT = "DRAFT"
    NEEDS_CONFIRMATION = "NEEDS_CONFIRMATION"
    CONFIRMED = "CONFIRMED"
    ROUTED = "ROUTED"
    RUNNING = "RUNNING"
    AWAITING_CHECKPOINT = "AWAITING_CHECKPOINT"
    VALIDATING = "VALIDATING"
    COMPLETED = "COMPLETED"
    STOPPED = "STOPPED"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"


class StopCondition(StrEnum):
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"
    NO_INCREMENTAL_CANDIDATE = "NO_INCREMENTAL_CANDIDATE"
    USER_STOPPED = "USER_STOPPED"
    VALIDATION_COMPLETED = "VALIDATION_COMPLETED"
    CHECKPOINT_REQUIRED = "CHECKPOINT_REQUIRED"
    PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
    DATA_UNAVAILABLE = "DATA_UNAVAILABLE"
    MAX_RUNTIME_REACHED = "MAX_RUNTIME_REACHED"


class CampaignFailureCode(StrEnum):
    CAMPAIGN_NOT_CONFIRMED = "CAMPAIGN_NOT_CONFIRMED"
    CAMPAIGN_CLARIFICATION_REQUIRED = "CAMPAIGN_CLARIFICATION_REQUIRED"
    QUICK_DEPTH_AUTHORITY_VIOLATION = "QUICK_DEPTH_AUTHORITY_VIOLATION"
    DUPLICATE_COMMITTED_TRIAL = "DUPLICATE_COMMITTED_TRIAL"


CAMPAIGN_TRANSITION_TABLE: Mapping[CampaignState, frozenset[CampaignState]] = MappingProxyType(
    {
        CampaignState.DRAFT: frozenset({CampaignState.NEEDS_CONFIRMATION, CampaignState.BLOCKED}),
        CampaignState.NEEDS_CONFIRMATION: frozenset(
            {CampaignState.CONFIRMED, CampaignState.STOPPED, CampaignState.BLOCKED}
        ),
        CampaignState.CONFIRMED: frozenset({CampaignState.ROUTED, CampaignState.STOPPED, CampaignState.BLOCKED}),
        CampaignState.ROUTED: frozenset({CampaignState.RUNNING, CampaignState.STOPPED, CampaignState.BLOCKED}),
        CampaignState.RUNNING: frozenset(
            {
                CampaignState.AWAITING_CHECKPOINT,
                CampaignState.VALIDATING,
                CampaignState.COMPLETED,
                CampaignState.STOPPED,
                CampaignState.BLOCKED,
                CampaignState.FAILED,
            }
        ),
        CampaignState.AWAITING_CHECKPOINT: frozenset(
            {CampaignState.RUNNING, CampaignState.VALIDATING, CampaignState.STOPPED, CampaignState.BLOCKED}
        ),
        CampaignState.VALIDATING: frozenset(
            {CampaignState.COMPLETED, CampaignState.STOPPED, CampaignState.BLOCKED, CampaignState.FAILED}
        ),
        CampaignState.COMPLETED: frozenset(),
        CampaignState.STOPPED: frozenset(),
        CampaignState.BLOCKED: frozenset(),
        CampaignState.FAILED: frozenset(),
    }
)


FACTOR_CATEGORY_WORDS = frozenset(
    {
        "MOMENTUM",
        "REVERSAL",
        "VALUE",
        "GROWTH",
        "QUALITY",
        "VOLATILITY",
        "LIQUIDITY",
        "FUNDAMENTAL",
        "EVENT",
        "MACRO",
        "动量",
        "反转",
        "价值",
        "基本面",
        "事件",
        "宏观",
    }
)


def _validate_enum(value: Any, enum_type: type[Enum], field_name: str) -> None:
    if not isinstance(value, enum_type):
        fail(FailureCode.INVALID_VALUE, f"{field_name} must be {enum_type.__name__}", field=field_name)


def _validate_string_tuple(value: tuple[str, ...], field_name: str, *, nonempty: bool = True) -> None:
    if not isinstance(value, tuple) or (nonempty and not value):
        fail(FailureCode.INVALID_VALUE, f"{field_name} must be a tuple", field=field_name)
    for item in value:
        require_text(item, field_name)
    if len(set(value)) != len(value):
        fail(FailureCode.INVALID_VALUE, f"{field_name} contains duplicates", field=field_name)


def _validate_hash_tuple(value: tuple[str, ...], field_name: str, *, nonempty: bool = False) -> None:
    _validate_string_tuple(value, field_name, nonempty=nonempty)
    for item in value:
        require_hash(item, field_name)


def _validate_subject_context(subject: "ResearchSubject") -> None:
    _validate_string_tuple(subject.universe, "subject.universe")
    require_text(subject.horizon, "subject.horizon")
    require_aware_datetime(subject.decision_time, "subject.decision_time")
    _validate_string_tuple(subject.allowed_data, "subject.allowed_data")


def _validate_common_campaign_fields(obj: Any) -> None:
    require_identifier(obj.campaign_id, "campaign_id")
    require_text(obj.question, "question")
    _validate_enum(obj.mode, ResearchMode, "mode")
    _validate_enum(obj.research_depth, ResearchDepth, "research_depth")
    _validate_enum(obj.interaction_style, InteractionStyle, "interaction_style")
    if not isinstance(obj.subject, ResearchSubject):
        fail(FailureCode.INVALID_VALUE, "subject must be ResearchSubject")
    _validate_subject_context(obj.subject)
    require_nonempty_tuple(obj.method_models, "method_models")
    for method in obj.method_models:
        _validate_enum(method, MethodModel, "method_models")
    _validate_string_tuple(obj.candidate_agents, "candidate_agents")
    if not isinstance(obj.budget, CampaignBudget):
        fail(FailureCode.INVALID_VALUE, "budget must be CampaignBudget")
    require_nonempty_tuple(obj.user_checkpoints, "user_checkpoints")
    if not all(isinstance(item, UserCheckpoint) for item in obj.user_checkpoints):
        fail(FailureCode.INVALID_VALUE, "user_checkpoints must contain UserCheckpoint")
    require_nonempty_tuple(obj.stop_conditions, "stop_conditions")
    for condition in obj.stop_conditions:
        _validate_enum(condition, StopCondition, "stop_conditions")
    for name in ("data_manifest_hash", "policy_hash", "code_hash"):
        require_hash(getattr(obj, name), name)


def validate_campaign_transition(from_state: CampaignState, to_state: CampaignState) -> None:
    _validate_enum(from_state, CampaignState, "from_state")
    _validate_enum(to_state, CampaignState, "to_state")
    allowed = CAMPAIGN_TRANSITION_TABLE.get(from_state, frozenset())
    if to_state not in allowed:
        raise ContractValidationError(
            FailureCode.INVALID_STATE_TRANSITION,
            f"illegal campaign transition {from_state.value} -> {to_state.value}",
            details={
                "from_state": from_state.value,
                "to_state": to_state.value,
                "allowed": tuple(sorted(state.value for state in allowed)),
            },
        )


def require_campaign_executable(campaign: "ResearchCampaign") -> None:
    if not isinstance(campaign, ResearchCampaign) or campaign.state is not CampaignState.CONFIRMED:
        raise ContractValidationError(
            FailureCode.INVALID_STATE,
            "only a CONFIRMED ResearchCampaign can start execution",
            details={"campaign_failure_code": CampaignFailureCode.CAMPAIGN_NOT_CONFIRMED.value},
        )


def require_new_trial(manifest: "CampaignRunManifest", trial_id: str) -> None:
    require_identifier(trial_id, "trial_id")
    if trial_id in manifest.committed_trial_ids:
        raise ContractValidationError(
            FailureCode.INVALID_STATE,
            "campaign recovery cannot create a committed Trial twice",
            details={"campaign_failure_code": CampaignFailureCode.DUPLICATE_COMMITTED_TRIAL.value},
        )


@dataclass(frozen=True, kw_only=True)
class ResearchSubject(CanonicalContract):
    SCHEMA_VERSION: ClassVar[str] = RESEARCH_CAMPAIGN_SCHEMA_VERSION

    subject_id: str
    description: str
    universe: tuple[str, ...]
    horizon: str
    decision_time: datetime
    allowed_data: tuple[str, ...]
    existing_factor_ids: tuple[str, ...] = ()
    portfolio_id: str | None = None
    tags: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        require_identifier(self.subject_id, "subject_id")
        require_text(self.description, "description")
        _validate_subject_context(self)
        _validate_string_tuple(self.existing_factor_ids, "existing_factor_ids", nonempty=False)
        if self.portfolio_id is not None:
            require_identifier(self.portfolio_id, "portfolio_id")
        _validate_string_tuple(self.tags, "tags", nonempty=False)


@dataclass(frozen=True, kw_only=True)
class CampaignBudget(CanonicalContract):
    SCHEMA_VERSION: ClassVar[str] = RESEARCH_CAMPAIGN_SCHEMA_VERSION

    max_factor_families: int
    max_trials: int
    max_llm_calls: int
    max_runtime_seconds: int
    expires_at: datetime | None = None

    def __post_init__(self) -> None:
        for name in ("max_factor_families", "max_trials", "max_llm_calls", "max_runtime_seconds"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                fail(FailureCode.INVALID_VALUE, f"{name} must be a positive integer", field=name)
        if self.max_trials < self.max_factor_families:
            fail(FailureCode.INVALID_VALUE, "max_trials must cover max_factor_families")
        if self.expires_at is not None:
            require_aware_datetime(self.expires_at, "expires_at")


@dataclass(frozen=True, kw_only=True)
class UserCheckpoint(CanonicalContract):
    SCHEMA_VERSION: ClassVar[str] = RESEARCH_CAMPAIGN_SCHEMA_VERSION

    checkpoint_id: str
    stage: str
    required: bool = True
    description: str = ""

    def __post_init__(self) -> None:
        require_identifier(self.checkpoint_id, "checkpoint_id")
        require_identifier(self.stage, "checkpoint.stage")
        if not isinstance(self.required, bool):
            fail(FailureCode.INVALID_VALUE, "checkpoint.required must be boolean")
        if self.description:
            require_text(self.description, "checkpoint.description")


@dataclass(frozen=True, kw_only=True)
class CampaignClarificationQuestion(CanonicalContract):
    """A machine-readable question that must be resolved before confirmation."""

    SCHEMA_VERSION: ClassVar[str] = RESEARCH_CAMPAIGN_SCHEMA_VERSION

    question_id: str
    question: str
    required: bool = True
    field_name: str | None = None

    def __post_init__(self) -> None:
        require_identifier(self.question_id, "question_id")
        require_text(self.question, "question")
        if not isinstance(self.required, bool):
            fail(FailureCode.INVALID_VALUE, "required must be boolean")
        if self.field_name is not None:
            require_identifier(self.field_name, "field_name")


@dataclass(frozen=True, kw_only=True)
class ResearchCampaignDraft(CanonicalContract):
    SCHEMA_VERSION: ClassVar[str] = RESEARCH_CAMPAIGN_SCHEMA_VERSION
    ALLOWED_PRODUCERS: ClassVar[frozenset[Component]] = frozenset(
        {Component.LLM_RESEARCHER, Component.RESEARCH_DESK, Component.OPERATOR}
    )

    campaign_id: str
    question: str
    mode: ResearchMode
    research_depth: ResearchDepth
    interaction_style: InteractionStyle
    subject: ResearchSubject
    method_models: tuple[MethodModel, ...]
    candidate_agents: tuple[str, ...]
    budget: CampaignBudget
    user_checkpoints: tuple[UserCheckpoint, ...]
    stop_conditions: tuple[StopCondition, ...]
    data_manifest_hash: str
    policy_hash: str
    code_hash: str
    created_at: datetime
    proposed_by: Component
    assumptions: tuple[str, ...] = ()
    state: CampaignState = CampaignState.DRAFT
    clarification_questions: tuple[CampaignClarificationQuestion, ...] = ()
    unresolved_fields: tuple[str, ...] = ()
    resolution_answers: Mapping[str, str] = field(default_factory=dict)
    resolved_by: Component | None = None

    def __post_init__(self) -> None:
        _validate_common_campaign_fields(self)
        require_aware_datetime(self.created_at, "created_at")
        if self.proposed_by not in {Component.LLM_RESEARCHER, Component.RESEARCH_DESK, Component.OPERATOR}:
            fail(FailureCode.INVALID_ARTIFACT_PRODUCER, "invalid campaign draft proposer")
        _validate_string_tuple(self.assumptions, "assumptions", nonempty=False)
        if self.state not in {CampaignState.DRAFT, CampaignState.NEEDS_CONFIRMATION}:
            fail(FailureCode.INVALID_STATE, "Campaign draft may only be DRAFT or NEEDS_CONFIRMATION")
        if not isinstance(self.clarification_questions, tuple) or not all(
            isinstance(item, CampaignClarificationQuestion) for item in self.clarification_questions
        ):
            fail(FailureCode.INVALID_VALUE, "clarification_questions must contain CampaignClarificationQuestion")
        _validate_string_tuple(self.unresolved_fields, "unresolved_fields", nonempty=False)
        if tuple(sorted(self.unresolved_fields)) != self.unresolved_fields:
            fail(FailureCode.INVALID_VALUE, "unresolved_fields must be unique and sorted")
        question_fields = {
            item.field_name for item in self.clarification_questions if item.required and item.field_name is not None
        }
        if not set(self.unresolved_fields).issubset(question_fields):
            fail(FailureCode.INVALID_VALUE, "unresolved_fields must reference required clarification fields")
        frozen_answers = freeze_mapping(self.resolution_answers, "resolution_answers")
        for key, value in frozen_answers.items():
            require_identifier(key, "resolution_answers.key")
            require_text(value, f"resolution_answers.{key}")
        object.__setattr__(self, "resolution_answers", frozen_answers)
        if frozen_answers and self.resolved_by is not Component.OPERATOR:
            fail(FailureCode.INVALID_ARTIFACT_PRODUCER, "only OPERATOR may resolve Campaign clarifications")
        if self.resolved_by is not None and self.resolved_by is not Component.OPERATOR:
            fail(FailureCode.INVALID_ARTIFACT_PRODUCER, "invalid Campaign clarification resolver")

    @classmethod
    def from_payload(
        cls,
        payload: Mapping[str, Any],
        *,
        component: Component | str | None = None,
    ) -> "ResearchCampaignDraft":
        draft = super().from_payload(payload, component=component)
        if component is not None and draft.proposed_by is not Component(component):
            fail(
                FailureCode.INVALID_ARTIFACT_PRODUCER,
                "campaign draft proposer must match the submitting component",
                component=Component(component).value,
                proposed_by=draft.proposed_by.value,
            )
        if component is not None and Component(component) is not Component.OPERATOR and (
            draft.resolution_answers or draft.resolved_by is not None
        ):
            fail(
                FailureCode.UNAUTHORIZED_CAPABILITY,
                "LLM/Research Desk cannot resolve or confirm Campaign semantics",
            )
        return draft

    def resolve_clarifications(
        self,
        answers: Mapping[str, str],
        *,
        resolved_by: Component = Component.OPERATOR,
    ) -> "ResearchCampaignDraft":
        """Return a new hash-bound draft with explicit operator answers applied."""

        if resolved_by is not Component.OPERATOR:
            fail(FailureCode.INVALID_ARTIFACT_PRODUCER, "only OPERATOR may resolve Campaign clarifications")
        frozen_answers = freeze_mapping(answers, "answers")
        unknown = set(frozen_answers) - set(self.unresolved_fields)
        missing = set(self.unresolved_fields) - set(frozen_answers)
        if unknown:
            fail(FailureCode.UNKNOWN_FIELD, "answers contain unresolved-field mismatch", fields=tuple(sorted(unknown)))
        if missing:
            fail(
                FailureCode.MISSING_REQUIRED_FIELD,
                "all unresolved Campaign fields require explicit answers",
                fields=tuple(sorted(missing)),
            )
        normalized = {key: str(value).strip() for key, value in frozen_answers.items()}
        for key, value in normalized.items():
            require_text(value, f"answers.{key}")
        subject = _subject_with_resolution(self.subject, normalized)
        return ResearchCampaignDraft(
            campaign_id=self.campaign_id,
            question=self.question,
            mode=self.mode,
            research_depth=self.research_depth,
            interaction_style=self.interaction_style,
            subject=subject,
            method_models=self.method_models,
            candidate_agents=self.candidate_agents,
            budget=self.budget,
            user_checkpoints=self.user_checkpoints,
            stop_conditions=self.stop_conditions,
            data_manifest_hash=self.data_manifest_hash,
            policy_hash=self.policy_hash,
            code_hash=self.code_hash,
            created_at=self.created_at,
            proposed_by=self.proposed_by,
            assumptions=self.assumptions,
            state=self.state,
            clarification_questions=self.clarification_questions,
            unresolved_fields=(),
            resolution_answers=normalized,
            resolved_by=resolved_by,
        )

    def ready_for_confirmation(self) -> "ResearchCampaignDraft":
        _require_resolved_campaign_semantics(self)
        validate_campaign_transition(self.state, CampaignState.NEEDS_CONFIRMATION)
        return ResearchCampaignDraft(
            campaign_id=self.campaign_id,
            question=self.question,
            mode=self.mode,
            research_depth=self.research_depth,
            interaction_style=self.interaction_style,
            subject=self.subject,
            method_models=self.method_models,
            candidate_agents=self.candidate_agents,
            budget=self.budget,
            user_checkpoints=self.user_checkpoints,
            stop_conditions=self.stop_conditions,
            data_manifest_hash=self.data_manifest_hash,
            policy_hash=self.policy_hash,
            code_hash=self.code_hash,
            created_at=self.created_at,
            proposed_by=self.proposed_by,
            assumptions=self.assumptions,
            state=CampaignState.NEEDS_CONFIRMATION,
            clarification_questions=self.clarification_questions,
            unresolved_fields=self.unresolved_fields,
            resolution_answers=self.resolution_answers,
            resolved_by=self.resolved_by,
        )


@dataclass(frozen=True, kw_only=True)
class ResearchCampaign(CanonicalContract):
    SCHEMA_VERSION: ClassVar[str] = RESEARCH_CAMPAIGN_SCHEMA_VERSION
    ALLOWED_PRODUCERS: ClassVar[frozenset[Component]] = frozenset({Component.OPERATOR})

    campaign_id: str
    draft_hash: str
    question: str
    mode: ResearchMode
    research_depth: ResearchDepth
    interaction_style: InteractionStyle
    subject: ResearchSubject
    method_models: tuple[MethodModel, ...]
    candidate_agents: tuple[str, ...]
    budget: CampaignBudget
    user_checkpoints: tuple[UserCheckpoint, ...]
    stop_conditions: tuple[StopCondition, ...]
    data_manifest_hash: str
    policy_hash: str
    code_hash: str
    confirmed_at: datetime
    confirmed_by: Component = Component.OPERATOR
    state: CampaignState = CampaignState.CONFIRMED
    clarification_questions: tuple[CampaignClarificationQuestion, ...] = ()
    resolution_answers: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _validate_common_campaign_fields(self)
        require_hash(self.draft_hash, "draft_hash")
        require_aware_datetime(self.confirmed_at, "confirmed_at")
        if self.confirmed_by is not Component.OPERATOR:
            fail(FailureCode.INVALID_ARTIFACT_PRODUCER, "ResearchCampaign must be confirmed by OPERATOR")
        if self.state is not CampaignState.CONFIRMED:
            fail(FailureCode.INVALID_STATE, "ResearchCampaign must start as CONFIRMED")
        if not isinstance(self.clarification_questions, tuple) or not all(
            isinstance(item, CampaignClarificationQuestion) for item in self.clarification_questions
        ):
            fail(FailureCode.INVALID_VALUE, "clarification_questions must contain CampaignClarificationQuestion")
        frozen_answers = freeze_mapping(self.resolution_answers, "resolution_answers")
        for key, value in frozen_answers.items():
            require_identifier(key, "resolution_answers.key")
            require_text(value, f"resolution_answers.{key}")
        object.__setattr__(self, "resolution_answers", frozen_answers)

    @classmethod
    def confirm(
        cls,
        draft: ResearchCampaignDraft,
        *,
        confirmed_at: datetime,
        confirmed_by: Component = Component.OPERATOR,
    ) -> "ResearchCampaign":
        if not isinstance(draft, ResearchCampaignDraft):
            fail(FailureCode.INVALID_VALUE, "draft must be ResearchCampaignDraft")
        if draft.state is not CampaignState.NEEDS_CONFIRMATION:
            fail(FailureCode.INVALID_STATE, "draft must be NEEDS_CONFIRMATION before confirmation")
        _require_resolved_campaign_semantics(draft)
        validate_campaign_transition(draft.state, CampaignState.CONFIRMED)
        return cls(
            campaign_id=draft.campaign_id,
            draft_hash=draft.artifact_hash,
            question=draft.question,
            mode=draft.mode,
            research_depth=draft.research_depth,
            interaction_style=draft.interaction_style,
            subject=draft.subject,
            method_models=draft.method_models,
            candidate_agents=draft.candidate_agents,
            budget=draft.budget,
            user_checkpoints=draft.user_checkpoints,
            stop_conditions=draft.stop_conditions,
            data_manifest_hash=draft.data_manifest_hash,
            policy_hash=draft.policy_hash,
            code_hash=draft.code_hash,
            confirmed_at=confirmed_at,
            confirmed_by=confirmed_by,
            clarification_questions=draft.clarification_questions,
            resolution_answers=draft.resolution_answers,
        )


def _require_resolved_campaign_semantics(draft: ResearchCampaignDraft) -> None:
    missing = set(draft.unresolved_fields)
    if draft.mode is ResearchMode.IMPROVE_EXISTING_FACTOR and not draft.subject.existing_factor_ids:
        missing.add("parent_factor_id")
    if draft.mode in {ResearchMode.PORTFOLIO_GAP_SEARCH, ResearchMode.DIAGNOSE_FACTOR_PORTFOLIO} and not (
        draft.subject.portfolio_id or draft.subject.existing_factor_ids
    ):
        missing.add("portfolio_or_existing_factor_ids")
    if draft.mode in {ResearchMode.PRELAUNCH_REVIEW, ResearchMode.MONITOR_DECAY_RETIREMENT} and not (
        draft.subject.existing_factor_ids
    ):
        missing.add("factor_id")
    if missing:
        raise ContractValidationError(
            FailureCode.MISSING_REQUIRED_FIELD,
            "Campaign has unresolved research semantics and cannot be confirmed",
            details={
                "campaign_failure_code": CampaignFailureCode.CAMPAIGN_CLARIFICATION_REQUIRED.value,
                "unresolved_fields": tuple(sorted(missing)),
            },
        )


def _subject_with_resolution(subject: ResearchSubject, answers: Mapping[str, str]) -> ResearchSubject:
    horizon = subject.horizon
    universe = subject.universe
    existing = subject.existing_factor_ids
    portfolio_id = subject.portfolio_id
    if "forecast_horizons" in answers:
        value = answers["forecast_horizons"].lower().strip()
        if value.endswith("d"):
            value = value[:-1]
        if not value.isdigit() or int(value) < 1:
            fail(FailureCode.INVALID_VALUE, "forecast_horizons answer must be a positive day count")
        horizon = f"{int(value)}d"
    if "parent_factor_id" in answers:
        require_identifier(answers["parent_factor_id"], "parent_factor_id")
        existing = (answers["parent_factor_id"],)
    if "factor_id" in answers:
        require_identifier(answers["factor_id"], "factor_id")
        existing = (answers["factor_id"],)
    if "portfolio_or_existing_factor_ids" in answers:
        value = answers["portfolio_or_existing_factor_ids"]
        if value.startswith("portfolio_id:"):
            portfolio_id = value.split(":", 1)[1].strip()
            require_identifier(portfolio_id, "portfolio_id")
        elif value.startswith("factor_id:") or value.startswith("factor_ids:"):
            raw = value.split(":", 1)[1]
            existing = tuple(item.strip() for item in raw.split(",") if item.strip())
            _validate_string_tuple(existing, "existing_factor_ids")
            for item in existing:
                require_identifier(item, "existing_factor_ids")
        else:
            fail(
                FailureCode.INVALID_VALUE,
                "portfolio context answer must use portfolio_id:<id> or factor_ids:<id,...>",
            )
    if "universe" in answers:
        universe = tuple(item.strip() for item in answers["universe"].split(",") if item.strip())
        _validate_string_tuple(universe, "subject.universe")
    return ResearchSubject(
        subject_id=subject.subject_id,
        description=subject.description,
        universe=universe,
        horizon=horizon,
        decision_time=subject.decision_time,
        allowed_data=subject.allowed_data,
        existing_factor_ids=existing,
        portfolio_id=portfolio_id,
        tags=subject.tags,
    )


@dataclass(frozen=True, kw_only=True)
class CampaignTaskPlan(CanonicalContract):
    SCHEMA_VERSION: ClassVar[str] = RESEARCH_CAMPAIGN_SCHEMA_VERSION

    task_id: str
    desk_id: str
    method_model: MethodModel
    input_from: tuple[str, ...] = ()
    required: bool = True
    artifact_namespace: str
    budget_units: int = 1
    requires_official_evidence: bool = False
    allows_promotion: bool = False

    def __post_init__(self) -> None:
        require_identifier(self.task_id, "task_id")
        require_identifier(self.desk_id, "desk_id")
        _validate_enum(self.method_model, MethodModel, "method_model")
        _validate_string_tuple(self.input_from, "input_from", nonempty=False)
        if not isinstance(self.required, bool):
            fail(FailureCode.INVALID_VALUE, "required must be boolean")
        require_identifier(self.artifact_namespace, "artifact_namespace")
        if isinstance(self.budget_units, bool) or not isinstance(self.budget_units, int) or self.budget_units < 1:
            fail(FailureCode.INVALID_VALUE, "budget_units must be a positive integer")
        if not isinstance(self.requires_official_evidence, bool) or not isinstance(self.allows_promotion, bool):
            fail(FailureCode.INVALID_VALUE, "task authority fields must be boolean")


@dataclass(frozen=True, kw_only=True)
class RoutedDeskBudget(CanonicalContract):
    SCHEMA_VERSION: ClassVar[str] = RESEARCH_CAMPAIGN_SCHEMA_VERSION

    desk_id: str
    max_factor_families: int
    max_trials: int
    max_llm_calls: int
    max_tasks: int

    def __post_init__(self) -> None:
        require_identifier(self.desk_id, "desk_id")
        for name in ("max_factor_families", "max_trials", "max_llm_calls", "max_tasks"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                fail(FailureCode.INVALID_VALUE, f"{name} must be a positive integer", field=name)


@dataclass(frozen=True, kw_only=True)
class CampaignRouteCheckpoint(CanonicalContract):
    SCHEMA_VERSION: ClassVar[str] = RESEARCH_CAMPAIGN_SCHEMA_VERSION

    checkpoint_id: str
    stage: str
    after_task_ids: tuple[str, ...] = ()
    required: bool = True

    def __post_init__(self) -> None:
        require_identifier(self.checkpoint_id, "checkpoint_id")
        require_identifier(self.stage, "checkpoint.stage")
        _validate_string_tuple(self.after_task_ids, "after_task_ids", nonempty=False)
        if not isinstance(self.required, bool):
            fail(FailureCode.INVALID_VALUE, "checkpoint.required must be boolean")


@dataclass(frozen=True, kw_only=True)
class CampaignRoute(CanonicalContract):
    SCHEMA_VERSION: ClassVar[str] = RESEARCH_CAMPAIGN_SCHEMA_VERSION

    route_id: str
    campaign_hash: str
    mode: ResearchMode
    research_depth: ResearchDepth
    selected_desks: tuple[str, ...]
    method_models: tuple[MethodModel, ...]
    tasks: tuple[CampaignTaskPlan, ...]
    routed_at: datetime
    route_reason: str
    desk_budgets: tuple[RoutedDeskBudget, ...] = ()
    checkpoint_plan: tuple[CampaignRouteCheckpoint, ...] = ()
    state: CampaignState = CampaignState.ROUTED

    def __post_init__(self) -> None:
        require_identifier(self.route_id, "route_id")
        require_hash(self.campaign_hash, "campaign_hash")
        _validate_enum(self.mode, ResearchMode, "mode")
        _validate_enum(self.research_depth, ResearchDepth, "research_depth")
        _validate_string_tuple(self.selected_desks, "selected_desks")
        for method in self.method_models:
            _validate_enum(method, MethodModel, "method_models")
        require_nonempty_tuple(self.method_models, "method_models")
        require_nonempty_tuple(self.tasks, "tasks")
        if not all(isinstance(task, CampaignTaskPlan) for task in self.tasks):
            fail(FailureCode.INVALID_VALUE, "tasks must contain CampaignTaskPlan")
        require_aware_datetime(self.routed_at, "routed_at")
        require_text(self.route_reason, "route_reason")
        if not isinstance(self.desk_budgets, tuple):
            fail(FailureCode.INVALID_VALUE, "desk_budgets must be a tuple")
        if not all(isinstance(item, RoutedDeskBudget) for item in self.desk_budgets):
            fail(FailureCode.INVALID_VALUE, "desk_budgets must contain RoutedDeskBudget")
        if not isinstance(self.checkpoint_plan, tuple):
            fail(FailureCode.INVALID_VALUE, "checkpoint_plan must be a tuple")
        if not all(isinstance(item, CampaignRouteCheckpoint) for item in self.checkpoint_plan):
            fail(FailureCode.INVALID_VALUE, "checkpoint_plan must contain CampaignRouteCheckpoint")
        if self.state is not CampaignState.ROUTED:
            fail(FailureCode.INVALID_STATE, "CampaignRoute must be ROUTED")
        task_desks = {task.desk_id for task in self.tasks}
        if task_desks - set(self.selected_desks):
            fail(FailureCode.INVALID_VALUE, "tasks reference a desk that was not selected")
        budget_desks = {budget.desk_id for budget in self.desk_budgets}
        if budget_desks - set(self.selected_desks):
            fail(FailureCode.INVALID_VALUE, "desk_budgets reference a desk that was not selected")
        if len(budget_desks) != len(self.desk_budgets):
            fail(FailureCode.INVALID_VALUE, "desk_budgets contain duplicate desks")
        task_ids = {task.task_id for task in self.tasks}
        for checkpoint in self.checkpoint_plan:
            missing = set(checkpoint.after_task_ids) - task_ids
            if missing:
                fail(
                    FailureCode.DAG_VALIDATION_FAILED,
                    "checkpoint_plan references unknown task ids",
                    checkpoint_id=checkpoint.checkpoint_id,
                    task_ids=tuple(sorted(missing)),
                )
        if self.research_depth is ResearchDepth.QUICK_EXPLORATION:
            for task in self.tasks:
                if task.requires_official_evidence or task.allows_promotion:
                    raise ContractValidationError(
                        FailureCode.UNAUTHORIZED_CAPABILITY,
                        "QUICK_EXPLORATION cannot require official evidence or promotion",
                        details={
                            "campaign_failure_code": CampaignFailureCode.QUICK_DEPTH_AUTHORITY_VIOLATION.value,
                            "task_id": task.task_id,
                        },
                    )


@dataclass(frozen=True, kw_only=True)
class CampaignRunManifest(CanonicalContract):
    SCHEMA_VERSION: ClassVar[str] = RESEARCH_CAMPAIGN_SCHEMA_VERSION

    run_id: str
    campaign_hash: str
    route_hash: str
    state: CampaignState
    current_checkpoint_id: str | None
    campaign_event_log_hash: str
    committed_trial_ids: tuple[str, ...] = ()
    trial_ledger_ref: str | None = None
    artifact_hashes: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        require_identifier(self.run_id, "run_id")
        require_hash(self.campaign_hash, "campaign_hash")
        require_hash(self.route_hash, "route_hash")
        _validate_enum(self.state, CampaignState, "state")
        if self.current_checkpoint_id is not None:
            require_identifier(self.current_checkpoint_id, "current_checkpoint_id")
        require_hash(self.campaign_event_log_hash, "campaign_event_log_hash")
        _validate_string_tuple(self.committed_trial_ids, "committed_trial_ids", nonempty=False)
        if self.trial_ledger_ref is not None:
            require_identifier(self.trial_ledger_ref, "trial_ledger_ref")
            if self.trial_ledger_ref == self.campaign_event_log_hash:
                fail(FailureCode.INVALID_VALUE, "Campaign events and Trial Ledger references must be separate")
        if len(set(self.committed_trial_ids)) != len(self.committed_trial_ids):
            raise ContractValidationError(
                FailureCode.INVALID_STATE,
                "committed Trial ids must be unique for recovery",
                details={"campaign_failure_code": CampaignFailureCode.DUPLICATE_COMMITTED_TRIAL.value},
            )
        hashes = dict(self.artifact_hashes)
        for key, value in hashes.items():
            require_identifier(key, "artifact_hashes.key")
            require_hash(value, f"artifact_hashes.{key}")
        object.__setattr__(self, "artifact_hashes", MappingProxyType(dict(sorted(hashes.items()))))


@dataclass(frozen=True, kw_only=True)
class CampaignOutcome(CanonicalContract):
    SCHEMA_VERSION: ClassVar[str] = RESEARCH_CAMPAIGN_SCHEMA_VERSION

    outcome_id: str
    campaign_hash: str
    run_manifest_hash: str
    research_depth: ResearchDepth
    state: CampaignState
    candidate_hashes: tuple[str, ...] = ()
    official_evidence_hashes: tuple[str, ...] = ()
    promotion_decision_hash: str | None = None
    summary: str

    def __post_init__(self) -> None:
        require_identifier(self.outcome_id, "outcome_id")
        require_hash(self.campaign_hash, "campaign_hash")
        require_hash(self.run_manifest_hash, "run_manifest_hash")
        _validate_enum(self.research_depth, ResearchDepth, "research_depth")
        if self.state not in {
            CampaignState.COMPLETED,
            CampaignState.STOPPED,
            CampaignState.BLOCKED,
            CampaignState.FAILED,
        }:
            fail(FailureCode.INVALID_STATE, "CampaignOutcome must be terminal")
        _validate_hash_tuple(self.candidate_hashes, "candidate_hashes", nonempty=False)
        _validate_hash_tuple(self.official_evidence_hashes, "official_evidence_hashes", nonempty=False)
        if self.promotion_decision_hash is not None:
            require_hash(self.promotion_decision_hash, "promotion_decision_hash")
        require_text(self.summary, "summary")
        if self.research_depth is ResearchDepth.QUICK_EXPLORATION:
            if self.official_evidence_hashes or self.promotion_decision_hash is not None:
                raise ContractValidationError(
                    FailureCode.UNAUTHORIZED_CAPABILITY,
                    "QUICK_EXPLORATION outcomes cannot include official evidence or promotion",
                    details={
                        "campaign_failure_code": CampaignFailureCode.QUICK_DEPTH_AUTHORITY_VIOLATION.value,
                    },
                )


def freeze_campaign_payload(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    """Return an immutable shallow copy for integration boundaries."""

    return freeze_mapping(payload, "campaign_payload")
