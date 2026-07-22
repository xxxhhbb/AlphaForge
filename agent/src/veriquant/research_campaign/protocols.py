"""Method-specific fixed protocols for Campaign-routed research.

These templates keep the product runtime from forcing every research question
through a factor-generation loop.  They are declarative contracts only: no
FactorDAG compilation, PIT evaluation, official Evidence, Promotion, Portfolio,
Risk, Execution, or Ledger mutation is performed here.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from types import MappingProxyType
from typing import Any, ClassVar, Mapping

from ..contracts import CanonicalContract, Component, ContractValidationError, FailureCode
from ..contracts.base import (
    canonicalize,
    fail,
    require_aware_datetime,
    require_hash,
    require_identifier,
    require_nonempty_tuple,
    require_text,
)
from .models import (
    RESEARCH_CAMPAIGN_SCHEMA_VERSION,
    CampaignRoute,
    CampaignState,
    MethodModel,
    ResearchCampaign,
    ResearchDepth,
    ResearchMode,
    require_campaign_executable,
)


METHOD_PROTOCOL_RULE_VERSION = "veriquant.method-protocol/v1"
DEFAULT_PROTOCOL_COMPILED_AT = datetime(2026, 7, 19, 11, 0, tzinfo=timezone.utc)


class StrEnum(str, Enum):
    """Python 3.10-compatible string enum with stable JSON values."""


class ResearchProtocolKind(StrEnum):
    QUICK_DRAFT_ONLY = "QUICK_DRAFT_ONLY"
    FACTOR_EXPLORATION_OR_IMPROVEMENT = "FACTOR_EXPLORATION_OR_IMPROVEMENT"
    IDEA_VALIDATION = "IDEA_VALIDATION"
    DIAGNOSTIC = "DIAGNOSTIC"
    DATA_SOURCE_EVALUATION = "DATA_SOURCE_EVALUATION"
    PAPER_REPLICATION = "PAPER_REPLICATION"
    ONLINE_MONITORING = "ONLINE_MONITORING"
    EVENT_STUDY_TOPIC = "EVENT_STUDY_TOPIC"


_FORBIDDEN_PROTOCOL_COMPONENTS = frozenset(
    {
        Component.PROMOTION_SERVICE,
        Component.ALPHA_SERVICE,
        Component.PORTFOLIO_SERVICE,
        Component.RISK_GATE,
        Component.ORDER_SERVICE,
        Component.EXECUTION_SERVICE,
        Component.LEDGER_SERVICE,
    }
)


@dataclass(frozen=True, kw_only=True)
class ProtocolStageSpec(CanonicalContract):
    SCHEMA_VERSION: ClassVar[str] = RESEARCH_CAMPAIGN_SCHEMA_VERSION

    stage_id: str
    stage_name: str
    description: str
    allowed_components: tuple[Component, ...]
    input_artifacts: tuple[str, ...] = ()
    output_artifacts: tuple[str, ...] = ()
    emits_factor_draft: bool = False
    requires_pit: bool = False
    requires_official_evidence: bool = False
    uses_registered_evidence: bool = False
    requires_human_decision: bool = False
    allows_position_or_order_change: bool = False

    def __post_init__(self) -> None:
        require_identifier(self.stage_id, "stage_id")
        require_text(self.stage_name, "stage_name")
        require_text(self.description, "description")
        _require_component_tuple(self.allowed_components, "allowed_components")
        forbidden = set(self.allowed_components) & _FORBIDDEN_PROTOCOL_COMPONENTS
        if forbidden:
            fail(
                FailureCode.UNAUTHORIZED_CAPABILITY,
                "method protocol stages cannot assign downstream authority",
                stage_id=self.stage_id,
                components=tuple(sorted(component.value for component in forbidden)),
            )
        _require_text_tuple(self.input_artifacts, "input_artifacts", nonempty=False)
        _require_text_tuple(self.output_artifacts, "output_artifacts", nonempty=False)
        for name in (
            "emits_factor_draft",
            "requires_pit",
            "requires_official_evidence",
            "uses_registered_evidence",
            "requires_human_decision",
            "allows_position_or_order_change",
        ):
            if not isinstance(getattr(self, name), bool):
                fail(FailureCode.INVALID_VALUE, f"{name} must be boolean", field=name)
        if self.allows_position_or_order_change:
            fail(FailureCode.UNAUTHORIZED_CAPABILITY, "method protocol cannot change positions or orders")


@dataclass(frozen=True, kw_only=True)
class MethodProtocolTemplate(CanonicalContract):
    SCHEMA_VERSION: ClassVar[str] = RESEARCH_CAMPAIGN_SCHEMA_VERSION

    template_id: str
    protocol_kind: ResearchProtocolKind
    research_modes: tuple[ResearchMode, ...]
    method_models: tuple[MethodModel, ...]
    stages: tuple[ProtocolStageSpec, ...]
    terminal_outputs: tuple[str, ...]
    allows_factor_generation: bool = False
    requires_official_evidence: bool = False
    human_decision_required: bool = False
    allowed_human_recommendations: tuple[str, ...] = ()
    allows_automatic_trading: bool = False
    allows_unbounded_search: bool = False

    def __post_init__(self) -> None:
        require_identifier(self.template_id, "template_id")
        if not isinstance(self.protocol_kind, ResearchProtocolKind):
            object.__setattr__(self, "protocol_kind", ResearchProtocolKind(self.protocol_kind))
        _require_enum_tuple(self.research_modes, ResearchMode, "research_modes")
        _require_enum_tuple(self.method_models, MethodModel, "method_models")
        require_nonempty_tuple(self.stages, "stages")
        if not all(isinstance(stage, ProtocolStageSpec) for stage in self.stages):
            fail(FailureCode.INVALID_VALUE, "stages must contain ProtocolStageSpec")
        stage_ids = tuple(stage.stage_id for stage in self.stages)
        if len(set(stage_ids)) != len(stage_ids):
            fail(FailureCode.INVALID_VALUE, "protocol stages contain duplicates")
        _require_text_tuple(self.terminal_outputs, "terminal_outputs")
        _require_text_tuple(self.allowed_human_recommendations, "allowed_human_recommendations", nonempty=False)
        for name in (
            "allows_factor_generation",
            "requires_official_evidence",
            "human_decision_required",
            "allows_automatic_trading",
            "allows_unbounded_search",
        ):
            if not isinstance(getattr(self, name), bool):
                fail(FailureCode.INVALID_VALUE, f"{name} must be boolean", field=name)
        if self.allows_automatic_trading:
            fail(FailureCode.UNAUTHORIZED_CAPABILITY, "method protocol cannot auto-trade")
        if self.allows_unbounded_search:
            fail(FailureCode.PROVIDER_BUDGET_EXCEEDED, "method protocol cannot allow unbounded search")
        if not self.allows_factor_generation and any(stage.emits_factor_draft for stage in self.stages):
            fail(FailureCode.UNAUTHORIZED_CAPABILITY, "non-factor protocol cannot emit factor drafts")
        if self.requires_official_evidence != any(stage.requires_official_evidence for stage in self.stages):
            fail(FailureCode.INVALID_VALUE, "template official-evidence flag must match its stages")

    @property
    def stage_ids(self) -> tuple[str, ...]:
        return tuple(stage.stage_id for stage in self.stages)

    @property
    def transition_table(self) -> Mapping[str, tuple[str, ...]]:
        ids = self.stage_ids
        transitions = {stage_id: (ids[index + 1],) if index + 1 < len(ids) else () for index, stage_id in enumerate(ids)}
        return MappingProxyType(transitions)


@dataclass(frozen=True, kw_only=True)
class CampaignTaskProtocolPlan(CanonicalContract):
    SCHEMA_VERSION: ClassVar[str] = RESEARCH_CAMPAIGN_SCHEMA_VERSION

    route_task_id: str
    desk_id: str
    method_model: MethodModel
    protocol_kind: ResearchProtocolKind
    protocol_template_hash: str
    stage_ids: tuple[str, ...]
    current_stage_id: str
    artifact_namespace: str
    may_generate_factor_draft: bool
    requires_official_evidence: bool
    human_decision_required: bool
    allowed_human_recommendations: tuple[str, ...] = ()
    allows_automatic_trading: bool = False

    def __post_init__(self) -> None:
        require_identifier(self.route_task_id, "route_task_id")
        require_identifier(self.desk_id, "desk_id")
        if not isinstance(self.method_model, MethodModel):
            object.__setattr__(self, "method_model", MethodModel(self.method_model))
        if not isinstance(self.protocol_kind, ResearchProtocolKind):
            object.__setattr__(self, "protocol_kind", ResearchProtocolKind(self.protocol_kind))
        require_hash(self.protocol_template_hash, "protocol_template_hash")
        _require_identifier_tuple(self.stage_ids, "stage_ids")
        require_identifier(self.current_stage_id, "current_stage_id")
        if self.current_stage_id not in self.stage_ids:
            fail(FailureCode.INVALID_STATE, "current stage must be part of protocol")
        require_identifier(self.artifact_namespace, "artifact_namespace")
        _require_text_tuple(self.allowed_human_recommendations, "allowed_human_recommendations", nonempty=False)
        for name in (
            "may_generate_factor_draft",
            "requires_official_evidence",
            "human_decision_required",
            "allows_automatic_trading",
        ):
            if not isinstance(getattr(self, name), bool):
                fail(FailureCode.INVALID_VALUE, f"{name} must be boolean", field=name)
        if self.allows_automatic_trading:
            fail(FailureCode.UNAUTHORIZED_CAPABILITY, "task protocol cannot auto-trade")


@dataclass(frozen=True, kw_only=True)
class CampaignProtocolPlan(CanonicalContract):
    SCHEMA_VERSION: ClassVar[str] = RESEARCH_CAMPAIGN_SCHEMA_VERSION

    plan_id: str
    campaign_hash: str
    route_hash: str
    protocol_version: str
    task_protocols: tuple[CampaignTaskProtocolPlan, ...]
    compiled_at: datetime
    state: CampaignState = CampaignState.ROUTED

    def __post_init__(self) -> None:
        require_identifier(self.plan_id, "plan_id")
        require_hash(self.campaign_hash, "campaign_hash")
        require_hash(self.route_hash, "route_hash")
        require_identifier(self.protocol_version, "protocol_version")
        require_nonempty_tuple(self.task_protocols, "task_protocols")
        if not all(isinstance(item, CampaignTaskProtocolPlan) for item in self.task_protocols):
            fail(FailureCode.INVALID_VALUE, "task_protocols must contain CampaignTaskProtocolPlan")
        route_task_ids = tuple(item.route_task_id for item in self.task_protocols)
        if len(set(route_task_ids)) != len(route_task_ids):
            fail(FailureCode.INVALID_VALUE, "task_protocols contain duplicate route_task_id")
        require_aware_datetime(self.compiled_at, "compiled_at")
        if self.state is not CampaignState.ROUTED:
            fail(FailureCode.INVALID_STATE, "CampaignProtocolPlan must be ROUTED")


class MethodProtocolRegistry:
    """Versioned registry of fixed research protocol templates."""

    def __init__(
        self,
        templates: tuple[MethodProtocolTemplate, ...] | None = None,
        *,
        version: str = METHOD_PROTOCOL_RULE_VERSION,
    ) -> None:
        require_identifier(version, "version")
        selected = templates or default_method_protocol_templates()
        if len({template.protocol_kind for template in selected}) != len(selected):
            fail(FailureCode.INVALID_VALUE, "protocol templates contain duplicate protocol kinds")
        self.version = version
        self._templates = MappingProxyType({template.protocol_kind: template for template in selected})

    @property
    def templates(self) -> Mapping[ResearchProtocolKind, MethodProtocolTemplate]:
        return self._templates

    def template_for(self, kind: ResearchProtocolKind) -> MethodProtocolTemplate:
        if not isinstance(kind, ResearchProtocolKind):
            kind = ResearchProtocolKind(kind)
        try:
            return self._templates[kind]
        except KeyError as exc:
            raise ContractValidationError(
                FailureCode.INVALID_VALUE,
                "unknown research protocol kind",
                details={"protocol_kind": kind.value},
            ) from exc

    def resolve(self, *, mode: ResearchMode, method_model: MethodModel, depth: ResearchDepth) -> MethodProtocolTemplate:
        if depth is ResearchDepth.QUICK_EXPLORATION:
            return self.template_for(ResearchProtocolKind.QUICK_DRAFT_ONLY)
        kind = _protocol_kind_for_mode(mode)
        template = self.template_for(kind)
        if mode not in template.research_modes:
            fail(FailureCode.INVALID_VALUE, "protocol template does not support Campaign mode", mode=mode.value)
        if method_model not in template.method_models:
            # A route can carry auxiliary falsification/regime method models; the
            # mode still selects the protocol family.  The task method is retained
            # for traceability but cannot change the protocol authority.
            if not (method_model is MethodModel.ADVERSARIAL_FALSIFICATION):
                fail(
                    FailureCode.INVALID_VALUE,
                    "protocol template does not support task method model",
                    mode=mode.value,
                    method_model=method_model.value,
                    protocol_kind=kind.value,
                )
        return template


class DeterministicMethodProtocolCompiler:
    """Compile a confirmed CampaignRoute into per-task fixed protocol plans."""

    provider_id = "deterministic_method_protocol_compiler"

    def __init__(self, *, registry: MethodProtocolRegistry | None = None) -> None:
        self.registry = registry or MethodProtocolRegistry()

    def compile(
        self,
        *,
        campaign: ResearchCampaign,
        route: CampaignRoute,
        compiled_at: datetime = DEFAULT_PROTOCOL_COMPILED_AT,
    ) -> CampaignProtocolPlan:
        require_campaign_executable(campaign)
        if not isinstance(route, CampaignRoute):
            fail(FailureCode.INVALID_VALUE, "route must be CampaignRoute")
        if route.campaign_hash != campaign.artifact_hash:
            fail(FailureCode.INVALID_HASH, "route must bind the Campaign hash")
        require_aware_datetime(compiled_at, "compiled_at")
        task_protocols: list[CampaignTaskProtocolPlan] = []
        for task in route.tasks:
            template = self.registry.resolve(
                mode=campaign.mode,
                method_model=task.method_model,
                depth=campaign.research_depth,
            )
            task_protocols.append(
                CampaignTaskProtocolPlan(
                    route_task_id=task.task_id,
                    desk_id=task.desk_id,
                    method_model=task.method_model,
                    protocol_kind=template.protocol_kind,
                    protocol_template_hash=template.artifact_hash,
                    stage_ids=template.stage_ids,
                    current_stage_id=template.stage_ids[0],
                    artifact_namespace=f"{task.artifact_namespace}.protocol",
                    may_generate_factor_draft=template.allows_factor_generation,
                    requires_official_evidence=template.requires_official_evidence,
                    human_decision_required=template.human_decision_required,
                    allowed_human_recommendations=template.allowed_human_recommendations,
                    allows_automatic_trading=False,
                )
            )
        plan = CampaignProtocolPlan(
            plan_id=_stable_id("protocol_plan", campaign.artifact_hash, route.artifact_hash, task_protocols),
            campaign_hash=campaign.artifact_hash,
            route_hash=route.artifact_hash,
            protocol_version=self.registry.version,
            task_protocols=tuple(task_protocols),
            compiled_at=compiled_at,
        )
        return plan


def validate_protocol_transition(template: MethodProtocolTemplate, from_stage_id: str, to_stage_id: str) -> None:
    if not isinstance(template, MethodProtocolTemplate):
        fail(FailureCode.INVALID_VALUE, "template must be MethodProtocolTemplate")
    require_identifier(from_stage_id, "from_stage_id")
    require_identifier(to_stage_id, "to_stage_id")
    transitions = template.transition_table
    if from_stage_id not in transitions:
        raise ContractValidationError(
            FailureCode.INVALID_STATE,
            "unknown protocol stage",
            details={"from_stage_id": from_stage_id, "template_id": template.template_id},
        )
    allowed = transitions[from_stage_id]
    if to_stage_id not in allowed:
        raise ContractValidationError(
            FailureCode.INVALID_STATE_TRANSITION,
            "illegal method protocol transition",
            details={"from_stage_id": from_stage_id, "to_stage_id": to_stage_id, "allowed": allowed},
        )


def default_method_protocol_templates() -> tuple[MethodProtocolTemplate, ...]:
    return (
        _quick_draft_template(),
        _factor_template(),
        _idea_validation_template(),
        _diagnostic_template(),
        _data_source_template(),
        _replication_template(),
        _monitoring_template(),
        _event_topic_template(),
    )


def _quick_draft_template() -> MethodProtocolTemplate:
    return MethodProtocolTemplate(
        template_id="protocol.quick_draft_only.v1",
        protocol_kind=ResearchProtocolKind.QUICK_DRAFT_ONLY,
        research_modes=tuple(mode for mode in ResearchMode),
        method_models=tuple(method for method in MethodModel),
        stages=(
            _stage("scope_question", "Scope question", "Clarify the user objective and frozen subject.", Component.LLM_RESEARCHER),
            _stage("draft_research_brief", "Draft research brief", "Produce a non-executable research brief.", Component.RESEARCH_DESK),
            _stage("draft_only_output", "Draft-only output", "Return DRAFT hypotheses or diagnostic outline only.", Component.RESEARCH_DESK),
            _stage("human_decision", "Human decision", "Ask the operator whether to stop or convert to a deeper Campaign.", Component.OPERATOR, human=True),
        ),
        terminal_outputs=("research_brief_draft", "candidate_draft_or_outline", "human_decision_required"),
        human_decision_required=True,
        allowed_human_recommendations=("STOP", "REVISE_CAMPAIGN", "CONFIRM_DEEPER_RESEARCH"),
    )


def _factor_template() -> MethodProtocolTemplate:
    return MethodProtocolTemplate(
        template_id="protocol.factor_exploration_or_improvement.v1",
        protocol_kind=ResearchProtocolKind.FACTOR_EXPLORATION_OR_IMPROVEMENT,
        research_modes=(
            ResearchMode.NEW_ALPHA_EXPLORATION,
            ResearchMode.IMPROVE_EXISTING_FACTOR,
            ResearchMode.PORTFOLIO_GAP_SEARCH,
        ),
        method_models=(
            MethodModel.HYPOTHESIS_DRIVEN,
            MethodModel.DATA_ANOMALY,
            MethodModel.RESIDUAL_INCREMENTAL,
            MethodModel.BOUNDED_EVOLUTION,
            MethodModel.ADVERSARIAL_FALSIFICATION,
        ),
        stages=(
            _stage("hypothesis", "Hypothesis", "State mechanism, prediction direction, and falsification criteria.", Component.RESEARCH_DESK),
            _stage("factor_construct", "Factor construct", "Construct a DRAFT FactorSpec without arbitrary code.", Component.RESEARCH_DESK, emits_factor=True),
            _stage("factor_dag_pit", "FactorDAG/PIT", "Convert to restricted FactorDAG and validate point-in-time availability.", Component.EVIDENCE_SERVICE, pit=True),
            _stage("official_evaluate", "Official evaluate", "Run deterministic official evaluation from frozen inputs.", Component.EVIDENCE_SERVICE, official=True),
            _stage("redacted_feedback", "Redacted feedback", "Return only research-visible feedback.", Component.CRITIC_SERVICE),
        ),
        terminal_outputs=("factor_spec_draft", "official_evidence_ref", "redacted_feedback"),
        allows_factor_generation=True,
        requires_official_evidence=True,
    )


def _idea_validation_template() -> MethodProtocolTemplate:
    return MethodProtocolTemplate(
        template_id="protocol.idea_validation.v1",
        protocol_kind=ResearchProtocolKind.IDEA_VALIDATION,
        research_modes=(ResearchMode.VALIDATE_IDEA,),
        method_models=(MethodModel.HYPOTHESIS_DRIVEN, MethodModel.ADVERSARIAL_FALSIFICATION),
        stages=(
            _stage("preregister_falsification", "Preregister falsification", "Freeze what would refute the idea before results.", Component.RESEARCH_DESK),
            _stage("resolve_factor_or_proxy", "Resolve factor or proxy", "Map the idea to an existing factor, proxy, or DRAFT spec.", Component.RESEARCH_DESK, emits_factor=True),
            _stage("pit_evaluate", "PIT/evaluate", "Run PIT-safe deterministic evaluation.", Component.EVIDENCE_SERVICE, pit=True, official=True),
            _stage("adversarial_tests", "Adversarial tests", "Apply registered robustness and placebo checks.", Component.CRITIC_SERVICE),
            _stage("validation_conclusion", "Validation conclusion", "Conclude support, rejection, or conditional validity.", Component.CRITIC_SERVICE),
        ),
        terminal_outputs=("falsification_record", "evidence_ref", "validation_conclusion"),
        allows_factor_generation=True,
        requires_official_evidence=True,
    )


def _diagnostic_template() -> MethodProtocolTemplate:
    return MethodProtocolTemplate(
        template_id="protocol.diagnostic.v1",
        protocol_kind=ResearchProtocolKind.DIAGNOSTIC,
        research_modes=(ResearchMode.DIAGNOSE_FACTOR_PORTFOLIO, ResearchMode.PRELAUNCH_REVIEW),
        method_models=(
            MethodModel.DIAGNOSTIC_ATTRIBUTION,
            MethodModel.REGIME_CONDITIONING,
            MethodModel.ADVERSARIAL_FALSIFICATION,
        ),
        stages=(
            _stage("scope_problem", "Scope problem", "Freeze the observed drawdown, decay, crowding, or review question.", Component.RESEARCH_DESK),
            _stage("retrieve_registered_evidence", "Retrieve registered evidence", "Read only registered/redacted evidence and portfolio summaries.", Component.EVIDENCE_SERVICE, registered=True),
            _stage("attribution_and_regime_split", "Attribution and regime split", "Split by attribution, cost, exposure, and market regime.", Component.CRITIC_SERVICE),
            _stage("adversarial_root_cause_checks", "Adversarial root-cause checks", "Test data, cost, crowding, and regime-change alternatives.", Component.CRITIC_SERVICE),
            _stage("ranked_diagnosis", "Ranked diagnosis", "Return ranked causes and operator-facing recommendations.", Component.CRITIC_SERVICE),
        ),
        terminal_outputs=("ranked_diagnosis", "redacted_registered_evidence_refs"),
        allows_factor_generation=False,
        requires_official_evidence=False,
    )


def _data_source_template() -> MethodProtocolTemplate:
    return MethodProtocolTemplate(
        template_id="protocol.data_source_evaluation.v1",
        protocol_kind=ResearchProtocolKind.DATA_SOURCE_EVALUATION,
        research_modes=(ResearchMode.NEW_DATA_SOURCE_EVALUATION,),
        method_models=(MethodModel.DATA_ANOMALY, MethodModel.ADVERSARIAL_FALSIFICATION),
        stages=(
            _stage("provenance", "Provenance", "Freeze source provenance, license, vendor, and timestamp semantics.", Component.DATA_SERVICE),
            _stage("availability_pit", "Availability/PIT", "Validate event, published, available, and decision-time semantics.", Component.DATA_SERVICE, pit=True),
            _stage("coverage_and_bias", "Coverage and bias", "Measure coverage, missingness, survivorship, and selection bias.", Component.DATA_SERVICE),
            _stage("sandbox_incrementality", "Sandbox incrementality", "Run sandbox incrementality only; do not create official Promotion.", Component.EVIDENCE_SERVICE, pit=True),
            _stage("dataset_assessment", "Dataset assessment", "Return dataset assessment with limits and required freezes.", Component.CRITIC_SERVICE),
        ),
        terminal_outputs=("dataset_assessment", "coverage_bias_report", "sandbox_incrementality_summary"),
    )


def _replication_template() -> MethodProtocolTemplate:
    return MethodProtocolTemplate(
        template_id="protocol.paper_replication.v1",
        protocol_kind=ResearchProtocolKind.PAPER_REPLICATION,
        research_modes=(ResearchMode.PAPER_STRATEGY_REPLICATION,),
        method_models=(MethodModel.REPLICATION, MethodModel.ADVERSARIAL_FALSIFICATION),
        stages=(
            _stage("extract_definition", "Extract definition", "Extract the paper or report definition and claimed timing.", Component.RESEARCH_DESK),
            _stage("map_data_and_time_semantics", "Map data and time semantics", "Map source fields, clocks, universe, and unavailable pieces.", Component.DATA_SERVICE),
            _stage("controlled_implementation", "Controlled implementation", "Implement only through restricted contracts or record impossibility.", Component.RESEARCH_DESK, emits_factor=True),
            _stage("reproduce", "Reproduce", "Run controlled reproduction or fail closed with evidence.", Component.EVIDENCE_SERVICE, pit=True, official=True),
            _stage("discrepancy_report", "Discrepancy report", "Report full, approximate, impossible, or failed reproduction.", Component.CRITIC_SERVICE),
        ),
        terminal_outputs=("replication_status", "discrepancy_report", "evidence_ref_or_blocker"),
        allows_factor_generation=True,
        requires_official_evidence=True,
    )


def _monitoring_template() -> MethodProtocolTemplate:
    return MethodProtocolTemplate(
        template_id="protocol.online_monitoring.v1",
        protocol_kind=ResearchProtocolKind.ONLINE_MONITORING,
        research_modes=(ResearchMode.MONITOR_DECAY_RETIREMENT,),
        method_models=(MethodModel.ONLINE_MONITORING, MethodModel.DIAGNOSTIC_ATTRIBUTION),
        stages=(
            _stage("compare_baseline", "Compare baseline", "Compare recent behavior with registered research baseline.", Component.EVIDENCE_SERVICE, registered=True),
            _stage("detect_drift", "Detect drift", "Detect alpha, cost, exposure, or data drift.", Component.CRITIC_SERVICE),
            _stage("diagnose", "Diagnose", "Diagnose likely cause without starting open-ended search.", Component.CRITIC_SERVICE),
            _stage("recommend", "Recommend", "Recommend keep, scale down, pause, or retire for human review.", Component.CRITIC_SERVICE),
            _stage("human_decision", "Human decision", "Require operator decision before any downstream action.", Component.OPERATOR, human=True),
        ),
        terminal_outputs=("monitoring_recommendation", "human_decision_required"),
        human_decision_required=True,
        allowed_human_recommendations=("KEEP", "SCALE_DOWN", "PAUSE", "RETIRE"),
    )


def _event_topic_template() -> MethodProtocolTemplate:
    return MethodProtocolTemplate(
        template_id="protocol.event_study_topic.v1",
        protocol_kind=ResearchProtocolKind.EVENT_STUDY_TOPIC,
        research_modes=(ResearchMode.EVENT_MARKET_TOPIC,),
        method_models=(MethodModel.EVENT_STUDY, MethodModel.REGIME_CONDITIONING, MethodModel.HYPOTHESIS_DRIVEN),
        stages=(
            _stage("event_definition", "Event definition", "Freeze event definition and event window.", Component.RESEARCH_DESK),
            _stage("map_event_time_semantics", "Map event time semantics", "Separate event_time, published_at, available_at, and decision_time.", Component.DATA_SERVICE),
            _stage("event_window_pit_evaluate", "Event-window PIT/evaluate", "Evaluate event window with PIT-safe clocks.", Component.EVIDENCE_SERVICE, pit=True, official=True),
            _stage("regime_conditioning", "Regime conditioning", "Split by market state known at decision time.", Component.CRITIC_SERVICE),
            _stage("event_topic_conclusion", "Event topic conclusion", "Return conditional event-study conclusion.", Component.CRITIC_SERVICE),
        ),
        terminal_outputs=("event_topic_conclusion", "evidence_ref"),
        requires_official_evidence=True,
    )


def _stage(
    stage_id: str,
    stage_name: str,
    description: str,
    component: Component,
    *,
    emits_factor: bool = False,
    pit: bool = False,
    official: bool = False,
    registered: bool = False,
    human: bool = False,
) -> ProtocolStageSpec:
    return ProtocolStageSpec(
        stage_id=stage_id,
        stage_name=stage_name,
        description=description,
        allowed_components=(component,),
        output_artifacts=(stage_id,),
        emits_factor_draft=emits_factor,
        requires_pit=pit,
        requires_official_evidence=official,
        uses_registered_evidence=registered,
        requires_human_decision=human,
    )


def _protocol_kind_for_mode(mode: ResearchMode) -> ResearchProtocolKind:
    if mode in {
        ResearchMode.NEW_ALPHA_EXPLORATION,
        ResearchMode.IMPROVE_EXISTING_FACTOR,
        ResearchMode.PORTFOLIO_GAP_SEARCH,
    }:
        return ResearchProtocolKind.FACTOR_EXPLORATION_OR_IMPROVEMENT
    if mode is ResearchMode.VALIDATE_IDEA:
        return ResearchProtocolKind.IDEA_VALIDATION
    if mode in {ResearchMode.DIAGNOSE_FACTOR_PORTFOLIO, ResearchMode.PRELAUNCH_REVIEW}:
        return ResearchProtocolKind.DIAGNOSTIC
    if mode is ResearchMode.NEW_DATA_SOURCE_EVALUATION:
        return ResearchProtocolKind.DATA_SOURCE_EVALUATION
    if mode is ResearchMode.PAPER_STRATEGY_REPLICATION:
        return ResearchProtocolKind.PAPER_REPLICATION
    if mode is ResearchMode.MONITOR_DECAY_RETIREMENT:
        return ResearchProtocolKind.ONLINE_MONITORING
    if mode is ResearchMode.EVENT_MARKET_TOPIC:
        return ResearchProtocolKind.EVENT_STUDY_TOPIC
    fail(FailureCode.INVALID_VALUE, "no method protocol exists for Campaign mode", mode=mode.value)


def _stable_id(prefix: str, *parts: Any) -> str:
    rendered = json.dumps(canonicalize(parts), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return f"{prefix}.{hashlib.sha256(rendered.encode('utf-8')).hexdigest()[:16]}"


def _require_text_tuple(value: tuple[str, ...], field_name: str, *, nonempty: bool = True) -> None:
    if not isinstance(value, tuple) or (nonempty and not value):
        fail(FailureCode.INVALID_VALUE, f"{field_name} must be a tuple", field=field_name)
    for item in value:
        require_text(item, field_name)
    if len(set(value)) != len(value):
        fail(FailureCode.INVALID_VALUE, f"{field_name} contains duplicates", field=field_name)


def _require_identifier_tuple(value: tuple[str, ...], field_name: str, *, nonempty: bool = True) -> None:
    if not isinstance(value, tuple) or (nonempty and not value):
        fail(FailureCode.INVALID_VALUE, f"{field_name} must be a tuple", field=field_name)
    for item in value:
        require_identifier(item, field_name)
    if len(set(value)) != len(value):
        fail(FailureCode.INVALID_VALUE, f"{field_name} contains duplicates", field=field_name)


def _require_component_tuple(value: tuple[Component, ...], field_name: str) -> None:
    if not isinstance(value, tuple) or not value:
        fail(FailureCode.INVALID_VALUE, f"{field_name} must be a non-empty tuple", field=field_name)
    for item in value:
        if not isinstance(item, Component):
            Component(item)


def _require_enum_tuple(value: tuple[Any, ...], enum_type: type[Enum], field_name: str) -> None:
    if not isinstance(value, tuple) or not value:
        fail(FailureCode.INVALID_VALUE, f"{field_name} must be a non-empty tuple", field=field_name)
    for item in value:
        if not isinstance(item, enum_type):
            enum_type(item)

