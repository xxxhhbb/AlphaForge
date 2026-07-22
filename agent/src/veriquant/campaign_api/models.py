"""Product-facing Campaign API DTOs.

This package is intentionally API-framework neutral.  It exposes stable,
Chinese-first DTOs for future FastAPI and UI wiring, while the financial
authority remains in ``veriquant.research_campaign`` and downstream services.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from types import MappingProxyType
from typing import Any, ClassVar, Mapping

from ..contracts import CanonicalContract, Component, FailureCode
from ..contracts.base import (
    fail,
    freeze_mapping,
    require_aware_datetime,
    require_hash,
    require_identifier,
    require_text,
)
from ..research_campaign import (
    CampaignRoute,
    CampaignState,
    CampaignTaskProtocolPlan,
    InteractionStyle,
    MethodModel,
    ResearchCampaign,
    ResearchCampaignDraft,
    ResearchDepth,
    ResearchMode,
)


CAMPAIGN_API_SCHEMA_VERSION = "alphaforge.campaign-api/v1"
DEFAULT_DECISION_TIME = datetime(2026, 7, 19, 9, 30, tzinfo=timezone.utc)
DEFAULT_HASH = "sha256:" + "0" * 64


class StrEnum(str, Enum):
    """Python 3.10-compatible stable string enum."""


class CampaignApiStatus(StrEnum):
    DRAFT_CREATED = "DRAFT_CREATED"
    NEEDS_CLARIFICATION = "NEEDS_CLARIFICATION"
    CONFIRMED = "CONFIRMED"
    ROUTED = "ROUTED"
    BLOCKED = "BLOCKED"
    REJECTED = "REJECTED"


class CampaignApiFailureCode(StrEnum):
    MISSING_CONFIRMATION_INPUT = "MISSING_CONFIRMATION_INPUT"
    UNCONFIRMED_CAMPAIGN = "UNCONFIRMED_CAMPAIGN"
    QUICK_EXPLORATION_AUTHORITY = "QUICK_EXPLORATION_AUTHORITY"
    PROMPT_INJECTION_REDACTED = "PROMPT_INJECTION_REDACTED"
    ROUTE_BLOCKED = "ROUTE_BLOCKED"
    INVALID_CAMPAIGN_REQUEST = "INVALID_CAMPAIGN_REQUEST"


RESEARCH_MODE_LABELS: Mapping[str, str] = MappingProxyType(
    {
        ResearchMode.NEW_ALPHA_EXPLORATION.value: "新 Alpha 探索",
        ResearchMode.VALIDATE_IDEA.value: "验证我的想法",
        ResearchMode.IMPROVE_EXISTING_FACTOR.value: "改进现有因子",
        ResearchMode.PORTFOLIO_GAP_SEARCH.value: "补充组合缺口",
        ResearchMode.DIAGNOSE_FACTOR_PORTFOLIO.value: "诊断因子/组合",
        ResearchMode.EVENT_MARKET_TOPIC.value: "事件与市场专题",
        ResearchMode.PAPER_STRATEGY_REPLICATION.value: "论文/策略复现",
        ResearchMode.NEW_DATA_SOURCE_EVALUATION.value: "新数据源评估",
        ResearchMode.PRELAUNCH_REVIEW.value: "上线前评审",
        ResearchMode.MONITOR_DECAY_RETIREMENT.value: "监控、衰减与退役",
    }
)

RESEARCH_DEPTH_LABELS: Mapping[str, str] = MappingProxyType(
    {
        ResearchDepth.QUICK_EXPLORATION.value: "快速探索",
        ResearchDepth.STANDARD_RESEARCH.value: "标准研究",
        ResearchDepth.STRICT_VALIDATION.value: "严格验证",
    }
)

INTERACTION_STYLE_LABELS: Mapping[str, str] = MappingProxyType(
    {
        InteractionStyle.GUIDED.value: "引导式",
        InteractionStyle.STANDARD.value: "标准式",
        InteractionStyle.BATCH.value: "批处理式",
    }
)

METHOD_MODEL_LABELS: Mapping[str, str] = MappingProxyType(
    {
        MethodModel.HYPOTHESIS_DRIVEN.value: "假设驱动",
        MethodModel.DATA_ANOMALY.value: "数据异常驱动",
        MethodModel.RESIDUAL_INCREMENTAL.value: "残差与增量研究",
        MethodModel.DIAGNOSTIC_ATTRIBUTION.value: "诊断归因",
        MethodModel.EVENT_STUDY.value: "事件研究",
        MethodModel.REGIME_CONDITIONING.value: "市场状态条件",
        MethodModel.BOUNDED_EVOLUTION.value: "受限演化",
        MethodModel.REPLICATION.value: "复现",
        MethodModel.ADVERSARIAL_FALSIFICATION.value: "对抗证伪",
        MethodModel.ONLINE_MONITORING.value: "在线监控",
    }
)


@dataclass(frozen=True, kw_only=True)
class CampaignCatalogView(CanonicalContract):
    SCHEMA_VERSION: ClassVar[str] = CAMPAIGN_API_SCHEMA_VERSION

    research_modes: Mapping[str, str] = field(default_factory=lambda: dict(RESEARCH_MODE_LABELS))
    research_depths: Mapping[str, str] = field(default_factory=lambda: dict(RESEARCH_DEPTH_LABELS))
    interaction_styles: Mapping[str, str] = field(default_factory=lambda: dict(INTERACTION_STYLE_LABELS))
    method_models: Mapping[str, str] = field(default_factory=lambda: dict(METHOD_MODEL_LABELS))

    def __post_init__(self) -> None:
        for name in ("research_modes", "research_depths", "interaction_styles", "method_models"):
            object.__setattr__(self, name, MappingProxyType(dict(sorted(freeze_mapping(getattr(self, name), name).items()))))


@dataclass(frozen=True, kw_only=True)
class CampaignDraftRequest(CanonicalContract):
    SCHEMA_VERSION: ClassVar[str] = CAMPAIGN_API_SCHEMA_VERSION

    user_text: str
    decision_time: datetime = DEFAULT_DECISION_TIME
    universe: tuple[str, ...] = ("CN_ASHARE_APPROVED_PIT_SAMPLE",)
    horizon: str = "20d"
    allowed_data: tuple[str, ...] = ("price_volume", "fundamentals")
    data_manifest_hash: str = DEFAULT_HASH
    policy_hash: str = DEFAULT_HASH
    code_hash: str = DEFAULT_HASH
    existing_factor_ids: tuple[str, ...] = ()
    portfolio_id: str | None = None
    preferred_depth: ResearchDepth | None = None
    interaction_style: InteractionStyle | None = None

    def __post_init__(self) -> None:
        require_text(self.user_text, "user_text")
        require_aware_datetime(self.decision_time, "decision_time")
        _require_text_tuple(self.universe, "universe")
        require_text(self.horizon, "horizon")
        _require_text_tuple(self.allowed_data, "allowed_data")
        for name in ("data_manifest_hash", "policy_hash", "code_hash"):
            require_hash(getattr(self, name), name)
        _require_text_tuple(self.existing_factor_ids, "existing_factor_ids", nonempty=False)
        if self.portfolio_id is not None:
            require_identifier(self.portfolio_id, "portfolio_id")
        if self.preferred_depth is not None and not isinstance(self.preferred_depth, ResearchDepth):
            object.__setattr__(self, "preferred_depth", ResearchDepth(self.preferred_depth))
        if self.interaction_style is not None and not isinstance(self.interaction_style, InteractionStyle):
            object.__setattr__(self, "interaction_style", InteractionStyle(self.interaction_style))


@dataclass(frozen=True, kw_only=True)
class CampaignConfirmationRequest(CanonicalContract):
    SCHEMA_VERSION: ClassVar[str] = CAMPAIGN_API_SCHEMA_VERSION

    draft: ResearchCampaignDraft
    confirmed_at: datetime = DEFAULT_DECISION_TIME
    confirmed_by: Component = Component.OPERATOR
    answers: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.draft, ResearchCampaignDraft):
            fail(FailureCode.INVALID_VALUE, "draft must be ResearchCampaignDraft")
        require_aware_datetime(self.confirmed_at, "confirmed_at")
        if not isinstance(self.confirmed_by, Component):
            object.__setattr__(self, "confirmed_by", Component(self.confirmed_by))
        frozen_answers = freeze_mapping(self.answers, "answers")
        for key, value in frozen_answers.items():
            require_identifier(key, "answers.key")
            require_text(value, f"answers.{key}")
        object.__setattr__(self, "answers", frozen_answers)


@dataclass(frozen=True, kw_only=True)
class CampaignRunIntent(CanonicalContract):
    SCHEMA_VERSION: ClassVar[str] = CAMPAIGN_API_SCHEMA_VERSION

    campaign_hash: str
    route_hash: str
    protocol_plan_hash: str
    selected_desks: tuple[str, ...]
    method_models: tuple[MethodModel, ...]
    protocol_tasks: tuple[str, ...]
    allows_official_evidence: bool
    allows_promotion: bool
    requires_checkpoint: bool
    run_authority: str = "CAMPAIGN_CONFIRMED_ROUTED_ONLY"

    def __post_init__(self) -> None:
        for name in ("campaign_hash", "route_hash", "protocol_plan_hash"):
            require_hash(getattr(self, name), name)
        _require_text_tuple(self.selected_desks, "selected_desks")
        _require_text_tuple(self.protocol_tasks, "protocol_tasks")
        if not isinstance(self.method_models, tuple) or not self.method_models:
            fail(FailureCode.INVALID_VALUE, "method_models must be a non-empty tuple")
        for method in self.method_models:
            if not isinstance(method, MethodModel):
                MethodModel(method)
        for name in ("allows_official_evidence", "allows_promotion", "requires_checkpoint"):
            if not isinstance(getattr(self, name), bool):
                fail(FailureCode.INVALID_VALUE, f"{name} must be boolean")
        if self.allows_promotion:
            fail(FailureCode.UNAUTHORIZED_CAPABILITY, "Campaign API run intent never owns Promotion authority")
        require_identifier(self.run_authority, "run_authority")


@dataclass(frozen=True, kw_only=True)
class CampaignApiResponse(CanonicalContract):
    SCHEMA_VERSION: ClassVar[str] = CAMPAIGN_API_SCHEMA_VERSION

    request_hash: str
    status: CampaignApiStatus
    message: str
    display_labels: Mapping[str, str] = field(default_factory=dict)
    draft_hash: str | None = None
    campaign_hash: str | None = None
    route_hash: str | None = None
    run_intent_hash: str | None = None
    payload_hashes: Mapping[str, str] = field(default_factory=dict)
    clarification_questions: tuple[str, ...] = ()
    prompt_injection_removed: bool = False
    failure_code: CampaignApiFailureCode | None = None
    upstream_failure_code: FailureCode | None = None
    can_execute: bool = False

    def __post_init__(self) -> None:
        require_hash(self.request_hash, "request_hash")
        if not isinstance(self.status, CampaignApiStatus):
            object.__setattr__(self, "status", CampaignApiStatus(self.status))
        require_text(self.message, "message")
        _freeze_label_mapping(self.display_labels, "display_labels", self)
        for name in ("draft_hash", "campaign_hash", "route_hash", "run_intent_hash"):
            value = getattr(self, name)
            if value is not None:
                require_hash(value, name)
        hashes = freeze_mapping(self.payload_hashes, "payload_hashes")
        for key, value in hashes.items():
            require_identifier(key, "payload_hashes.key")
            require_hash(value, f"payload_hashes.{key}")
        object.__setattr__(self, "payload_hashes", MappingProxyType(dict(sorted(hashes.items()))))
        _require_text_tuple(self.clarification_questions, "clarification_questions", nonempty=False)
        if not isinstance(self.prompt_injection_removed, bool):
            fail(FailureCode.INVALID_VALUE, "prompt_injection_removed must be boolean")
        if self.failure_code is not None and not isinstance(self.failure_code, CampaignApiFailureCode):
            object.__setattr__(self, "failure_code", CampaignApiFailureCode(self.failure_code))
        if self.upstream_failure_code is not None and not isinstance(self.upstream_failure_code, FailureCode):
            object.__setattr__(self, "upstream_failure_code", FailureCode(self.upstream_failure_code))
        if not isinstance(self.can_execute, bool):
            fail(FailureCode.INVALID_VALUE, "can_execute must be boolean")
        if self.status in {CampaignApiStatus.DRAFT_CREATED, CampaignApiStatus.NEEDS_CLARIFICATION} and self.can_execute:
            fail(FailureCode.UNAUTHORIZED_CAPABILITY, "draft responses cannot be executable")


def labels_for(
    *,
    mode: ResearchMode | None = None,
    depth: ResearchDepth | None = None,
    interaction: InteractionStyle | None = None,
    methods: tuple[MethodModel, ...] = (),
) -> Mapping[str, str]:
    labels: dict[str, str] = {}
    if mode is not None:
        labels[f"mode.{mode.value}"] = RESEARCH_MODE_LABELS[mode.value]
    if depth is not None:
        labels[f"depth.{depth.value}"] = RESEARCH_DEPTH_LABELS[depth.value]
    if interaction is not None:
        labels[f"interaction.{interaction.value}"] = INTERACTION_STYLE_LABELS[interaction.value]
    for method in methods:
        labels[f"method.{method.value}"] = METHOD_MODEL_LABELS[method.value]
    return MappingProxyType(dict(sorted(labels.items())))


def _freeze_label_mapping(value: Mapping[str, str], field_name: str, owner: Any) -> None:
    labels = freeze_mapping(value, field_name)
    for key, item in labels.items():
        require_identifier(key, f"{field_name}.key")
        require_text(item, f"{field_name}.{key}")
    object.__setattr__(owner, field_name, MappingProxyType(dict(sorted(labels.items()))))


def _require_text_tuple(value: tuple[str, ...], field_name: str, *, nonempty: bool = True) -> None:
    if not isinstance(value, tuple) or (nonempty and not value):
        fail(FailureCode.INVALID_VALUE, f"{field_name} must be a tuple", field=field_name)
    for item in value:
        require_text(item, field_name)
    if len(set(value)) != len(value):
        fail(FailureCode.INVALID_VALUE, f"{field_name} contains duplicates", field=field_name)
