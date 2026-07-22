"""Campaign routing and method registry for confirmed Research Campaigns.

The router is a product-layer planner.  It selects specialist desks and method
models for a confirmed Campaign, but it does not compile FactorDAGs, read sealed
data, calculate official Evidence, decide Promotion, construct portfolios,
approve risk, create orders, or write ledgers.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Any, ClassVar, Mapping

from ..contracts import CanonicalContract, ContractValidationError, FailureCode
from ..contracts.base import (
    canonicalize,
    fail,
    freeze_mapping,
    require_aware_datetime,
    require_hash,
    require_identifier,
    require_nonempty_tuple,
    require_text,
)
from .models import (
    RESEARCH_CAMPAIGN_SCHEMA_VERSION,
    CampaignRoute,
    CampaignRouteCheckpoint,
    CampaignState,
    CampaignTaskPlan,
    MethodModel,
    ResearchCampaign,
    ResearchMode,
    RoutedDeskBudget,
    require_campaign_executable,
)


CAMPAIGN_ROUTER_RULE_VERSION = "veriquant.campaign-router/v1"
DEFAULT_ROUTED_AT = datetime(2026, 7, 19, 10, 30, tzinfo=timezone.utc)
DEFAULT_HASH = "sha256:" + "0" * 64
MAX_DEFAULT_DESKS = 6


@dataclass(frozen=True, kw_only=True)
class DataAvailability(CanonicalContract):
    SCHEMA_VERSION: ClassVar[str] = RESEARCH_CAMPAIGN_SCHEMA_VERSION

    available_data: tuple[str, ...]
    universe_ids: tuple[str, ...]
    data_manifest_hash: str = DEFAULT_HASH
    calendar_ids: tuple[str, ...] = ()
    blocked_reasons: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require_text_tuple(self.available_data, "available_data")
        _require_text_tuple(self.universe_ids, "universe_ids")
        require_hash(self.data_manifest_hash, "data_manifest_hash")
        _require_text_tuple(self.calendar_ids, "calendar_ids", nonempty=False)
        _require_text_tuple(self.blocked_reasons, "blocked_reasons", nonempty=False)


@dataclass(frozen=True, kw_only=True)
class RegisteredResearchContext(CanonicalContract):
    SCHEMA_VERSION: ClassVar[str] = RESEARCH_CAMPAIGN_SCHEMA_VERSION

    factor_ids: tuple[str, ...] = ()
    portfolio_ids: tuple[str, ...] = ()
    factor_domains: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require_identifier_tuple(self.factor_ids, "factor_ids", nonempty=False)
        _require_identifier_tuple(self.portfolio_ids, "portfolio_ids", nonempty=False)
        domains = freeze_mapping(self.factor_domains, "factor_domains")
        for factor_id, desk_id in domains.items():
            require_identifier(factor_id, "factor_domains.factor_id")
            require_identifier(desk_id, "factor_domains.desk_id")
        object.__setattr__(self, "factor_domains", MappingProxyType(dict(sorted(domains.items()))))


@dataclass(frozen=True, kw_only=True)
class DeskCapabilityManifest(CanonicalContract):
    SCHEMA_VERSION: ClassVar[str] = RESEARCH_CAMPAIGN_SCHEMA_VERSION

    desk_id: str
    method_models: tuple[MethodModel, ...]
    data_scopes: tuple[str, ...]
    tool_allowlist: tuple[str, ...]
    max_tasks: int = 1
    max_llm_calls: int = 4
    max_trial_families: int = 1
    enabled: bool = True

    def __post_init__(self) -> None:
        require_identifier(self.desk_id, "desk_id")
        _require_method_tuple(self.method_models, "method_models")
        _require_text_tuple(self.data_scopes, "data_scopes")
        _require_identifier_tuple(self.tool_allowlist, "tool_allowlist", nonempty=False)
        for name in ("max_tasks", "max_llm_calls", "max_trial_families"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                fail(FailureCode.INVALID_VALUE, f"{name} must be a positive integer", field=name)
        if not isinstance(self.enabled, bool):
            fail(FailureCode.INVALID_VALUE, "enabled must be boolean")
        forbidden = {
            "read_sealed",
            "calculate_official_evidence",
            "promote_candidate",
            "approve_risk",
            "set_target_weights",
            "create_order",
            "append_ledger",
            "shell",
            "python_exec",
        }
        blocked_tools = set(self.tool_allowlist) & forbidden
        if blocked_tools:
            fail(
                FailureCode.UNAUTHORIZED_CAPABILITY,
                "desk capability manifest contains privileged tools",
                desk_id=self.desk_id,
                tools=tuple(sorted(blocked_tools)),
            )


@dataclass(frozen=True, kw_only=True)
class CampaignRoutingRule(CanonicalContract):
    SCHEMA_VERSION: ClassVar[str] = RESEARCH_CAMPAIGN_SCHEMA_VERSION

    mode: ResearchMode
    method_models: tuple[MethodModel, ...]
    default_desks: tuple[str, ...]
    description: str

    def __post_init__(self) -> None:
        if not isinstance(self.mode, ResearchMode):
            object.__setattr__(self, "mode", ResearchMode(self.mode))
        _require_method_tuple(self.method_models, "method_models")
        _require_identifier_tuple(self.default_desks, "default_desks")
        if len(self.default_desks) > MAX_DEFAULT_DESKS:
            fail(FailureCode.INVALID_VALUE, "default routing cannot select all desks")
        require_text(self.description, "description")


@dataclass(frozen=True, kw_only=True)
class CampaignRoutingRequest(CanonicalContract):
    SCHEMA_VERSION: ClassVar[str] = RESEARCH_CAMPAIGN_SCHEMA_VERSION

    campaign: ResearchCampaign
    data_availability: DataAvailability
    registered_context: RegisteredResearchContext
    desk_capabilities: tuple[DeskCapabilityManifest, ...]
    routed_at: datetime = DEFAULT_ROUTED_AT
    router_version: str = CAMPAIGN_ROUTER_RULE_VERSION

    def __post_init__(self) -> None:
        if not isinstance(self.campaign, ResearchCampaign):
            fail(FailureCode.INVALID_VALUE, "campaign must be ResearchCampaign")
        require_campaign_executable(self.campaign)
        if not isinstance(self.data_availability, DataAvailability):
            fail(FailureCode.INVALID_VALUE, "data_availability must be DataAvailability")
        if not isinstance(self.registered_context, RegisteredResearchContext):
            fail(FailureCode.INVALID_VALUE, "registered_context must be RegisteredResearchContext")
        require_nonempty_tuple(self.desk_capabilities, "desk_capabilities")
        if not all(isinstance(item, DeskCapabilityManifest) for item in self.desk_capabilities):
            fail(FailureCode.INVALID_VALUE, "desk_capabilities must contain DeskCapabilityManifest")
        if len({item.desk_id for item in self.desk_capabilities}) != len(self.desk_capabilities):
            fail(FailureCode.INVALID_VALUE, "desk_capabilities contain duplicate desks")
        require_aware_datetime(self.routed_at, "routed_at")
        require_identifier(self.router_version, "router_version")


@dataclass(frozen=True, kw_only=True)
class CampaignRoutingResult(CanonicalContract):
    SCHEMA_VERSION: ClassVar[str] = RESEARCH_CAMPAIGN_SCHEMA_VERSION

    request_hash: str
    router_version: str
    state: CampaignState
    route: CampaignRoute | None
    context_allowed_desks: tuple[str, ...] = ()
    blocked_reasons: tuple[str, ...] = ()
    downgrade_suggestions: tuple[str, ...] = ()
    failure_code: FailureCode | None = None

    def __post_init__(self) -> None:
        require_hash(self.request_hash, "request_hash")
        require_identifier(self.router_version, "router_version")
        if not isinstance(self.state, CampaignState):
            object.__setattr__(self, "state", CampaignState(self.state))
        if self.route is not None and not isinstance(self.route, CampaignRoute):
            fail(FailureCode.INVALID_VALUE, "route must be CampaignRoute")
        _require_identifier_tuple(self.context_allowed_desks, "context_allowed_desks", nonempty=False)
        _require_text_tuple(self.blocked_reasons, "blocked_reasons", nonempty=False)
        _require_text_tuple(self.downgrade_suggestions, "downgrade_suggestions", nonempty=False)
        if self.failure_code is not None and not isinstance(self.failure_code, FailureCode):
            object.__setattr__(self, "failure_code", FailureCode(self.failure_code))
        if self.state is CampaignState.ROUTED:
            if self.route is None:
                fail(FailureCode.INVALID_STATE, "ROUTED result requires a CampaignRoute")
            if self.context_allowed_desks != self.route.selected_desks:
                fail(FailureCode.INVALID_VALUE, "context_allowed_desks must match route.selected_desks")
            if self.blocked_reasons or self.failure_code is not None:
                fail(FailureCode.INVALID_STATE, "ROUTED result cannot include blockers")
        elif self.state is CampaignState.BLOCKED:
            if self.route is not None:
                fail(FailureCode.INVALID_STATE, "BLOCKED result cannot include a route")
            if not self.blocked_reasons or self.failure_code is None:
                fail(FailureCode.INVALID_STATE, "BLOCKED result requires blocker details and failure_code")
        else:
            fail(FailureCode.INVALID_STATE, "router results may only be ROUTED or BLOCKED")


class CampaignMethodRegistry:
    """Versioned default mapping from research mode to methods and desks."""

    def __init__(
        self,
        rules: tuple[CampaignRoutingRule, ...] | None = None,
        *,
        version: str = CAMPAIGN_ROUTER_RULE_VERSION,
    ) -> None:
        require_identifier(version, "version")
        self.version = version
        selected_rules = rules or _default_routing_rules()
        if len({rule.mode for rule in selected_rules}) != len(selected_rules):
            fail(FailureCode.INVALID_VALUE, "routing rules contain duplicate modes")
        self._rules = MappingProxyType({rule.mode: rule for rule in selected_rules})

    def rule_for(self, mode: ResearchMode) -> CampaignRoutingRule:
        if not isinstance(mode, ResearchMode):
            mode = ResearchMode(mode)
        try:
            return self._rules[mode]
        except KeyError as exc:
            raise ContractValidationError(
                FailureCode.INVALID_VALUE,
                "no routing rule exists for Campaign mode",
                details={"mode": mode.value},
            ) from exc

    @property
    def rules(self) -> Mapping[ResearchMode, CampaignRoutingRule]:
        return self._rules


class DeterministicCampaignRouter:
    """Offline, deterministic router for confirmed Campaigns."""

    provider_id = "deterministic_campaign_router"

    def __init__(self, *, registry: CampaignMethodRegistry | None = None) -> None:
        self.registry = registry or CampaignMethodRegistry()

    def route(
        self,
        campaign: ResearchCampaign,
        *,
        data_availability: DataAvailability,
        registered_context: RegisteredResearchContext | None = None,
        desk_capabilities: tuple[DeskCapabilityManifest, ...] | None = None,
        routed_at: datetime = DEFAULT_ROUTED_AT,
    ) -> CampaignRoutingResult:
        request = CampaignRoutingRequest(
            campaign=campaign,
            data_availability=data_availability,
            registered_context=registered_context or RegisteredResearchContext(),
            desk_capabilities=desk_capabilities or default_desk_capabilities(),
            routed_at=routed_at,
            router_version=self.registry.version,
        )
        return self.route_request(request)

    def route_request(self, request: CampaignRoutingRequest) -> CampaignRoutingResult:
        if not isinstance(request, CampaignRoutingRequest):
            fail(FailureCode.INVALID_VALUE, "request must be CampaignRoutingRequest")
        blockers, suggestions = _preflight_blockers(request)
        if blockers:
            return _blocked_result(
                request=request,
                blocked_reasons=tuple(blockers),
                downgrade_suggestions=tuple(suggestions),
                failure_code=FailureCode.UPSTREAM_BLOCKED,
            )

        rule = self.registry.rule_for(request.campaign.mode)
        method_models = tuple(dict.fromkeys((*rule.method_models, *request.campaign.method_models)))
        selected_desks = _select_desks(request=request, rule=rule, method_models=method_models)
        if not selected_desks:
            return _blocked_result(
                request=request,
                blocked_reasons=("no_enabled_desk_supports_required_methods",),
                downgrade_suggestions=("enable a matching desk or narrow the Campaign method models",),
                failure_code=FailureCode.ADAPTER_UNAVAILABLE,
            )

        desk_budgets = _allocate_desk_budgets(request.campaign, selected_desks)
        tasks = _build_tasks(
            method_models=method_models,
            selected_desks=selected_desks,
            desk_budgets=desk_budgets,
            desk_capabilities=request.desk_capabilities,
        )
        checkpoint_plan = _build_checkpoint_plan(request.campaign.user_checkpoints, tasks)
        route = CampaignRoute(
            route_id=_stable_id("route", request.artifact_hash, selected_desks, method_models, tasks),
            campaign_hash=request.campaign.artifact_hash,
            mode=request.campaign.mode,
            research_depth=request.campaign.research_depth,
            selected_desks=selected_desks,
            method_models=method_models,
            tasks=tasks,
            routed_at=request.routed_at,
            route_reason=(
                f"{request.campaign.mode.value} routed by {self.provider_id}; "
                "only selected desks receive Campaign context; Evidence and Promotion are out of router authority."
            ),
            desk_budgets=desk_budgets,
            checkpoint_plan=checkpoint_plan,
        )
        validate_campaign_route_dag(route)
        _validate_budget_bounds(request.campaign, route)
        return CampaignRoutingResult(
            request_hash=request.artifact_hash,
            router_version=request.router_version,
            state=CampaignState.ROUTED,
            route=route,
            context_allowed_desks=route.selected_desks,
        )


def default_desk_capabilities() -> tuple[DeskCapabilityManifest, ...]:
    read_only_tools = ("read_campaign_brief", "write_factor_draft", "write_feedback_note")
    return (
        DeskCapabilityManifest(
            desk_id="fundamental",
            method_models=(
                MethodModel.HYPOTHESIS_DRIVEN,
                MethodModel.DATA_ANOMALY,
                MethodModel.ADVERSARIAL_FALSIFICATION,
                MethodModel.REPLICATION,
            ),
            data_scopes=("price_volume", "fundamentals", "registered_factor_summary"),
            tool_allowlist=read_only_tools,
        ),
        DeskCapabilityManifest(
            desk_id="expectations_events",
            method_models=(
                MethodModel.HYPOTHESIS_DRIVEN,
                MethodModel.EVENT_STUDY,
                MethodModel.ADVERSARIAL_FALSIFICATION,
            ),
            data_scopes=("price_volume", "fundamentals", "events", "macro_regime"),
            tool_allowlist=read_only_tools,
        ),
        DeskCapabilityManifest(
            desk_id="market_structure",
            method_models=(
                MethodModel.HYPOTHESIS_DRIVEN,
                MethodModel.DATA_ANOMALY,
                MethodModel.DIAGNOSTIC_ATTRIBUTION,
                MethodModel.REGIME_CONDITIONING,
                MethodModel.BOUNDED_EVOLUTION,
                MethodModel.ADVERSARIAL_FALSIFICATION,
            ),
            data_scopes=("price_volume", "registered_factor_summary", "liquidity"),
            tool_allowlist=read_only_tools,
        ),
        DeskCapabilityManifest(
            desk_id="alternative_data",
            method_models=(
                MethodModel.HYPOTHESIS_DRIVEN,
                MethodModel.DATA_ANOMALY,
                MethodModel.ADVERSARIAL_FALSIFICATION,
            ),
            data_scopes=("price_volume", "alternative_data"),
            tool_allowlist=read_only_tools,
        ),
        DeskCapabilityManifest(
            desk_id="macro_regime",
            method_models=(
                MethodModel.HYPOTHESIS_DRIVEN,
                MethodModel.DIAGNOSTIC_ATTRIBUTION,
                MethodModel.EVENT_STUDY,
                MethodModel.REGIME_CONDITIONING,
                MethodModel.ADVERSARIAL_FALSIFICATION,
            ),
            data_scopes=("price_volume", "macro_regime"),
            tool_allowlist=read_only_tools,
        ),
        DeskCapabilityManifest(
            desk_id="meta_alpha",
            method_models=(
                MethodModel.RESIDUAL_INCREMENTAL,
                MethodModel.DIAGNOSTIC_ATTRIBUTION,
                MethodModel.BOUNDED_EVOLUTION,
                MethodModel.ADVERSARIAL_FALSIFICATION,
                MethodModel.ONLINE_MONITORING,
            ),
            data_scopes=("price_volume", "registered_factor_summary"),
            tool_allowlist=read_only_tools,
        ),
    )


def validate_campaign_route_dag(route: CampaignRoute) -> None:
    task_ids = [task.task_id for task in route.tasks]
    if len(set(task_ids)) != len(task_ids):
        fail(FailureCode.DAG_VALIDATION_FAILED, "route DAG contains duplicate task ids")
    known = set(task_ids)
    for task in route.tasks:
        missing = set(task.input_from) - known
        if missing:
            fail(
                FailureCode.DAG_VALIDATION_FAILED,
                "route DAG references unknown dependencies",
                task_id=task.task_id,
                missing=tuple(sorted(missing)),
            )

    visiting: set[str] = set()
    visited: set[str] = set()
    deps = {task.task_id: task.input_from for task in route.tasks}

    def visit(task_id: str) -> None:
        if task_id in visited:
            return
        if task_id in visiting:
            fail(FailureCode.DAG_VALIDATION_FAILED, "route DAG contains a cycle", task_id=task_id)
        visiting.add(task_id)
        for dependency in deps[task_id]:
            visit(dependency)
        visiting.remove(task_id)
        visited.add(task_id)

    for task_id in task_ids:
        visit(task_id)


def _default_routing_rules() -> tuple[CampaignRoutingRule, ...]:
    return (
        CampaignRoutingRule(
            mode=ResearchMode.NEW_ALPHA_EXPLORATION,
            method_models=(
                MethodModel.HYPOTHESIS_DRIVEN,
                MethodModel.DATA_ANOMALY,
                MethodModel.ADVERSARIAL_FALSIFICATION,
            ),
            default_desks=("fundamental", "market_structure", "meta_alpha"),
            description="new alpha exploration uses two or three relevant proposal desks, not the full six-desk set",
        ),
        CampaignRoutingRule(
            mode=ResearchMode.VALIDATE_IDEA,
            method_models=(MethodModel.HYPOTHESIS_DRIVEN, MethodModel.ADVERSARIAL_FALSIFICATION),
            default_desks=("fundamental", "market_structure"),
            description="idea validation starts with the mechanism desk plus market structure falsification",
        ),
        CampaignRoutingRule(
            mode=ResearchMode.IMPROVE_EXISTING_FACTOR,
            method_models=(
                MethodModel.BOUNDED_EVOLUTION,
                MethodModel.DIAGNOSTIC_ATTRIBUTION,
                MethodModel.ADVERSARIAL_FALSIFICATION,
            ),
            default_desks=("meta_alpha", "market_structure"),
            description="factor improvement uses registered parent-domain desks plus meta-alpha and market structure",
        ),
        CampaignRoutingRule(
            mode=ResearchMode.PORTFOLIO_GAP_SEARCH,
            method_models=(
                MethodModel.RESIDUAL_INCREMENTAL,
                MethodModel.HYPOTHESIS_DRIVEN,
                MethodModel.ADVERSARIAL_FALSIFICATION,
            ),
            default_desks=("meta_alpha", "market_structure"),
            description="portfolio gap search starts from residual context plus registered related-domain desks",
        ),
        CampaignRoutingRule(
            mode=ResearchMode.DIAGNOSE_FACTOR_PORTFOLIO,
            method_models=(
                MethodModel.DIAGNOSTIC_ATTRIBUTION,
                MethodModel.REGIME_CONDITIONING,
                MethodModel.ADVERSARIAL_FALSIFICATION,
            ),
            default_desks=("meta_alpha", "market_structure", "macro_regime"),
            description="diagnosis routes to meta-alpha, market structure, and macro regime analysis",
        ),
        CampaignRoutingRule(
            mode=ResearchMode.EVENT_MARKET_TOPIC,
            method_models=(
                MethodModel.EVENT_STUDY,
                MethodModel.REGIME_CONDITIONING,
                MethodModel.HYPOTHESIS_DRIVEN,
            ),
            default_desks=("expectations_events", "macro_regime", "market_structure"),
            description="event topics route through event, macro, and market structure desks",
        ),
        CampaignRoutingRule(
            mode=ResearchMode.PAPER_STRATEGY_REPLICATION,
            method_models=(MethodModel.REPLICATION, MethodModel.ADVERSARIAL_FALSIFICATION),
            default_desks=("fundamental", "market_structure"),
            description="replication routes to definitional mapping and market-structure feasibility",
        ),
        CampaignRoutingRule(
            mode=ResearchMode.NEW_DATA_SOURCE_EVALUATION,
            method_models=(MethodModel.DATA_ANOMALY, MethodModel.ADVERSARIAL_FALSIFICATION),
            default_desks=("alternative_data", "market_structure"),
            description="new data-source evaluation is coverage and safety oriented",
        ),
        CampaignRoutingRule(
            mode=ResearchMode.PRELAUNCH_REVIEW,
            method_models=(MethodModel.DIAGNOSTIC_ATTRIBUTION, MethodModel.ADVERSARIAL_FALSIFICATION),
            default_desks=("market_structure", "meta_alpha"),
            description="prelaunch review focuses on costs, capacity, and robustness",
        ),
        CampaignRoutingRule(
            mode=ResearchMode.MONITOR_DECAY_RETIREMENT,
            method_models=(MethodModel.ONLINE_MONITORING, MethodModel.DIAGNOSTIC_ATTRIBUTION),
            default_desks=("meta_alpha", "market_structure", "macro_regime"),
            description="monitoring routes only to diagnostic desks and cannot launch open-ended search",
        ),
    )


def _preflight_blockers(request: CampaignRoutingRequest) -> tuple[list[str], list[str]]:
    blockers = list(request.data_availability.blocked_reasons)
    suggestions: list[str] = []
    campaign = request.campaign
    available_data = set(request.data_availability.available_data)
    missing_data = sorted(set(campaign.subject.allowed_data) - available_data)
    if missing_data:
        blockers.append("missing_data:" + ",".join(missing_data))
        suggestions.append("freeze the missing data scopes before routing; do not silently substitute providers")
    if request.data_availability.data_manifest_hash != campaign.data_manifest_hash:
        blockers.append("data_manifest_hash_mismatch")
        suggestions.append("confirm a Campaign against the frozen DataAvailability manifest hash")
    available_universes = set(request.data_availability.universe_ids)
    missing_universe = sorted(set(campaign.subject.universe) - available_universes)
    if missing_universe:
        blockers.append("missing_universe:" + ",".join(missing_universe))
        suggestions.append("freeze the requested historical universe before routing")

    registered = request.registered_context
    factor_ids = set(registered.factor_ids)
    portfolio_ids = set(registered.portfolio_ids)
    subject_factor_ids = set(campaign.subject.existing_factor_ids)
    if campaign.mode is ResearchMode.IMPROVE_EXISTING_FACTOR:
        if not subject_factor_ids:
            blockers.append("missing_parent_factor_id")
            suggestions.append("ask the operator for parent_factor_id before bounded evolution")
        elif not subject_factor_ids <= factor_ids:
            blockers.append("unregistered_parent_factor:" + ",".join(sorted(subject_factor_ids - factor_ids)))
            suggestions.append("register the parent factor before routing improvement work")
    if campaign.mode is ResearchMode.PORTFOLIO_GAP_SEARCH:
        has_registered_portfolio = campaign.subject.portfolio_id in portfolio_ids if campaign.subject.portfolio_id else False
        has_registered_factors = bool(subject_factor_ids) and subject_factor_ids <= factor_ids
        if not has_registered_portfolio and not has_registered_factors:
            blockers.append("missing_registered_portfolio_or_factor_scope")
            suggestions.append("provide registered portfolio_id or existing_factor_ids for residual/incremental search")
    if campaign.subject.portfolio_id and campaign.subject.portfolio_id not in portfolio_ids:
        blockers.append(f"unregistered_portfolio:{campaign.subject.portfolio_id}")
        suggestions.append("register or freeze the referenced portfolio before routing")
    if subject_factor_ids and not subject_factor_ids <= factor_ids:
        blockers.append("unregistered_factor_scope:" + ",".join(sorted(subject_factor_ids - factor_ids)))
        suggestions.append("register referenced factors before routing")
    return blockers, suggestions


def _select_desks(
    *,
    request: CampaignRoutingRequest,
    rule: CampaignRoutingRule,
    method_models: tuple[MethodModel, ...],
) -> tuple[str, ...]:
    capabilities = {item.desk_id: item for item in request.desk_capabilities}
    domain_preferred = _domain_desks(request)
    if domain_preferred and request.campaign.mode is ResearchMode.NEW_ALPHA_EXPLORATION:
        # A clearly scoped new-alpha question should not inherit unrelated
        # default desks merely to fill a fixed desk count. This previously sent
        # pure price momentum questions to the fundamental desk.
        primary_preferred = domain_preferred
    else:
        primary_preferred = tuple(dict.fromkeys((*domain_preferred, *rule.default_desks)))
    backup_preferred = tuple(
        desk_id for desk_id in request.campaign.candidate_agents if desk_id not in set(primary_preferred)
    )
    budget = request.campaign.budget
    target_desks = min(
        MAX_DEFAULT_DESKS,
        max(1, len(primary_preferred)),
        budget.max_factor_families,
        budget.max_trials,
        budget.max_llm_calls,
    )
    selected: list[str] = []
    available_data = set(request.data_availability.available_data)
    for desk_id in (*primary_preferred, *backup_preferred):
        if len(selected) >= target_desks:
            break
        capability = capabilities.get(desk_id)
        if capability is None or not capability.enabled:
            continue
        if not set(capability.method_models) & set(method_models):
            continue
        if not set(capability.data_scopes) & available_data:
            continue
        selected.append(desk_id)
    return tuple(selected)


def _domain_desks(request: CampaignRoutingRequest) -> tuple[str, ...]:
    desks: list[str] = []
    if request.campaign.mode is ResearchMode.EVENT_MARKET_TOPIC:
        return ()
    for factor_id in request.campaign.subject.existing_factor_ids:
        desk_id = request.registered_context.factor_domains.get(factor_id)
        if desk_id:
            desks.append(desk_id)
    tags = set(request.campaign.subject.tags)
    if tags & {"momentum", "reversal", "volatility", "volume", "liquidity", "moneyflow"}:
        desks.extend(("market_structure", "meta_alpha"))
    if tags & {"value", "quality", "growth", "cashflow"}:
        desks.extend(("fundamental", "market_structure", "meta_alpha"))
    if {"earnings", "earnings_revision"} & tags:
        desks.extend(("expectations_events", "fundamental", "market_structure"))
    if tags & {"event"}:
        desks.extend(("expectations_events", "market_structure"))
    if tags & {"macro"}:
        desks.extend(("macro_regime", "market_structure", "meta_alpha"))
    if tags & {"alternative_data"}:
        desks.extend(("alternative_data", "expectations_events", "market_structure"))
    if "high_volatility" in tags:
        desks.append("macro_regime")
    return tuple(dict.fromkeys(desks))


def _allocate_desk_budgets(
    campaign: ResearchCampaign,
    selected_desks: tuple[str, ...],
) -> tuple[RoutedDeskBudget, ...]:
    n = len(selected_desks)
    families = _allocate_positive(campaign.budget.max_factor_families, n)
    trials = _allocate_positive(campaign.budget.max_trials, n)
    llm_calls = _allocate_positive(campaign.budget.max_llm_calls, n)
    return tuple(
        RoutedDeskBudget(
            desk_id=desk_id,
            max_factor_families=families[index],
            max_trials=trials[index],
            max_llm_calls=llm_calls[index],
            max_tasks=1,
        )
        for index, desk_id in enumerate(selected_desks)
    )


def _build_tasks(
    *,
    method_models: tuple[MethodModel, ...],
    selected_desks: tuple[str, ...],
    desk_budgets: tuple[RoutedDeskBudget, ...],
    desk_capabilities: tuple[DeskCapabilityManifest, ...],
) -> tuple[CampaignTaskPlan, ...]:
    budgets = {budget.desk_id: budget for budget in desk_budgets}
    tasks: list[CampaignTaskPlan] = []
    capabilities = {item.desk_id: item for item in desk_capabilities}
    for index, desk_id in enumerate(selected_desks):
        capability = capabilities[desk_id]
        method = next(method for method in method_models if method in capability.method_models)
        tasks.append(
            CampaignTaskPlan(
                task_id=f"task.{desk_id}.{index + 1}",
                desk_id=desk_id,
                method_model=method,
                input_from=(),
                required=True,
                artifact_namespace=f"campaign.route.{desk_id}",
                budget_units=budgets[desk_id].max_trials,
                requires_official_evidence=False,
                allows_promotion=False,
            )
        )
    return tuple(tasks)


def _build_checkpoint_plan(
    checkpoints: tuple[Any, ...],
    tasks: tuple[CampaignTaskPlan, ...],
) -> tuple[CampaignRouteCheckpoint, ...]:
    task_ids = tuple(task.task_id for task in tasks)
    route_checkpoints: list[CampaignRouteCheckpoint] = []
    for checkpoint in checkpoints:
        after_task_ids = () if checkpoint.stage == "after_research_brief" else task_ids
        route_checkpoints.append(
            CampaignRouteCheckpoint(
                checkpoint_id=checkpoint.checkpoint_id,
                stage=checkpoint.stage,
                required=checkpoint.required,
                after_task_ids=after_task_ids,
            )
        )
    return tuple(route_checkpoints)


def _blocked_result(
    *,
    request: CampaignRoutingRequest,
    blocked_reasons: tuple[str, ...],
    downgrade_suggestions: tuple[str, ...],
    failure_code: FailureCode,
) -> CampaignRoutingResult:
    return CampaignRoutingResult(
        request_hash=request.artifact_hash,
        router_version=request.router_version,
        state=CampaignState.BLOCKED,
        route=None,
        context_allowed_desks=(),
        blocked_reasons=blocked_reasons,
        downgrade_suggestions=downgrade_suggestions,
        failure_code=failure_code,
    )


def _validate_budget_bounds(campaign: ResearchCampaign, route: CampaignRoute) -> None:
    if len(route.selected_desks) > MAX_DEFAULT_DESKS:
        fail(FailureCode.PROVIDER_BUDGET_EXCEEDED, "router selected too many desks")
    if len(route.tasks) > campaign.budget.max_trials:
        fail(FailureCode.PROVIDER_BUDGET_EXCEEDED, "route task count exceeds Campaign trial budget")
    if sum(item.max_factor_families for item in route.desk_budgets) > campaign.budget.max_factor_families:
        fail(FailureCode.PROVIDER_BUDGET_EXCEEDED, "desk family budgets exceed Campaign budget")
    if sum(item.max_trials for item in route.desk_budgets) > campaign.budget.max_trials:
        fail(FailureCode.PROVIDER_BUDGET_EXCEEDED, "desk trial budgets exceed Campaign budget")
    if sum(item.max_llm_calls for item in route.desk_budgets) > campaign.budget.max_llm_calls:
        fail(FailureCode.PROVIDER_BUDGET_EXCEEDED, "desk LLM-call budgets exceed Campaign budget")
    for task in route.tasks:
        if task.requires_official_evidence or task.allows_promotion:
            fail(FailureCode.UNAUTHORIZED_CAPABILITY, "router task cannot own Evidence or Promotion authority")


def _allocate_positive(total: int, count: int) -> tuple[int, ...]:
    if count < 1 or total < count:
        fail(FailureCode.PROVIDER_BUDGET_EXCEEDED, "budget cannot cover selected desks")
    base, remainder = divmod(total, count)
    return tuple(base + (1 if index < remainder else 0) for index in range(count))


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


def _require_method_tuple(value: tuple[MethodModel, ...], field_name: str) -> None:
    if not isinstance(value, tuple) or not value:
        fail(FailureCode.INVALID_VALUE, f"{field_name} must be a non-empty tuple", field=field_name)
    for item in value:
        if not isinstance(item, MethodModel):
            MethodModel(item)
