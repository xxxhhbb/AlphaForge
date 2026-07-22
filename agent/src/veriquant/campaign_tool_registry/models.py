"""Campaign-scoped, desk-scoped research tool registry.

This package is an outer product-layer adapter.  It compiles tool visibility
from a confirmed Research Campaign and a deterministic CampaignRoute.  It does
not discover Vibe tools globally and does not grant official Evidence,
Promotion, Risk, Order, broker, or Ledger authority.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping

from ..contracts import ContractValidationError, FailureCode
from ..contracts.base import fail, require_hash, require_identifier, require_text
from ..research_campaign import (
    CampaignRoute,
    CampaignState,
    ResearchCampaign,
    require_campaign_executable,
)
from ..research_tool_contracts import (
    DeskToolPolicy,
    ResearchToolCapability,
    ResearchToolContractError,
    ResearchToolFailureCode,
    ResearchToolRequest,
    ResearchToolResult,
    ResearchToolVisibility,
    content_hash,
)


CAMPAIGN_TOOL_REGISTRY_SCHEMA_VERSION = "alphaforge.campaign-tool-registry/v1"
ALL_DESKS = frozenset(
    {
        "fundamental",
        "expectations_events",
        "market_structure",
        "alternative_data",
        "macro_regime",
        "meta_alpha",
    }
)

_TOOL_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_FORBIDDEN_TOOL_NAMES = frozenset(
    {
        "read_sealed",
        "sealed_reader",
        "calculate_official_evidence",
        "official_evidence",
        "official_metrics",
        "promote_candidate",
        "promotion_decision",
        "approve_risk",
        "risk_approval",
        "set_target_weights",
        "target_weights",
        "create_order",
        "order_plan",
        "append_ledger",
        "ledger_append",
        "broker",
        "live_runner",
        "live_routes",
        "mcp",
        "bash",
        "shell",
        "file_write",
        "file_read",
        "python_exec",
        "qveris_execute",
    }
)


class DeskToolAvailability(str, Enum):
    """Availability state for a desk-visible binding."""

    AVAILABLE = "AVAILABLE"
    UNAVAILABLE = "UNAVAILABLE"


def _as_capability(value: ResearchToolCapability | str) -> ResearchToolCapability:
    if isinstance(value, ResearchToolCapability):
        return value
    try:
        return ResearchToolCapability(value)
    except ValueError as exc:
        raise ResearchToolContractError(
            ResearchToolFailureCode.UNAUTHORIZED_CAPABILITY,
            "unknown tool capability",
            details={"capability": str(value)},
        ) from exc


def _as_visibility(value: ResearchToolVisibility | str) -> ResearchToolVisibility:
    if isinstance(value, ResearchToolVisibility):
        return value
    try:
        return ResearchToolVisibility(value)
    except ValueError as exc:
        raise ResearchToolContractError(
            ResearchToolFailureCode.UNAUTHORIZED_CAPABILITY,
            "unknown tool visibility",
            details={"visibility": str(value)},
        ) from exc


def _as_availability(value: DeskToolAvailability | str) -> DeskToolAvailability:
    if isinstance(value, DeskToolAvailability):
        return value
    try:
        return DeskToolAvailability(value)
    except ValueError as exc:
        raise ResearchToolContractError(
            ResearchToolFailureCode.PROVIDER_UNAVAILABLE,
            "unknown tool availability",
            details={"availability": str(value)},
        ) from exc


def _require_tool_name(value: str, field: str = "tool_name") -> None:
    if not isinstance(value, str) or _TOOL_NAME_RE.fullmatch(value) is None:
        raise ResearchToolContractError(
            ResearchToolFailureCode.INVALID_IDENTIFIER,
            f"{field} is not a safe tool name",
            details={"field": field, "value": value},
        )
    if value in _FORBIDDEN_TOOL_NAMES:
        raise ResearchToolContractError(
            ResearchToolFailureCode.UNAUTHORIZED_TOOL,
            "privileged or unsafe tool name cannot be registered",
            details={"tool_name": value},
        )


def _require_read_only_capability(tool_name: str, capability: ResearchToolCapability) -> None:
    if not capability.is_read_only or capability.is_privileged_financial_authority:
        raise ResearchToolContractError(
            ResearchToolFailureCode.UNAUTHORIZED_CAPABILITY,
            "Campaign tool bindings may expose only read-only research capabilities",
            details={"tool_name": tool_name, "capability": capability.value},
        )


def _sorted_text_tuple(value: tuple[str, ...], field: str, *, nonempty: bool = False) -> tuple[str, ...]:
    if not isinstance(value, tuple) or (nonempty and not value):
        raise ResearchToolContractError(
            ResearchToolFailureCode.MISSING_REQUIRED_FIELD,
            f"{field} must be a tuple",
            details={"field": field},
        )
    normalized: list[str] = []
    for item in value:
        require_text(item, field)
        normalized.append(item)
    return tuple(sorted(dict.fromkeys(normalized)))


@dataclass(frozen=True, kw_only=True)
class CampaignToolBinding:
    """One Campaign-scoped tool binding for one selected desk."""

    binding_id: str
    campaign_hash: str
    route_hash: str
    desk_id: str
    tool_name: str
    capability: ResearchToolCapability
    visibility: ResearchToolVisibility
    source_package: str
    provider_id: str
    allowed_data: tuple[str, ...]
    availability: DeskToolAvailability
    availability_reason: str
    context_hash: str
    schema_version: str = CAMPAIGN_TOOL_REGISTRY_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != CAMPAIGN_TOOL_REGISTRY_SCHEMA_VERSION:
            raise ResearchToolContractError(
                ResearchToolFailureCode.INVALID_SCHEMA_VERSION,
                "unsupported CampaignToolBinding schema",
            )
        require_identifier(self.binding_id, "binding_id")
        require_hash(self.campaign_hash, "campaign_hash")
        require_hash(self.route_hash, "route_hash")
        require_identifier(self.desk_id, "desk_id")
        _require_tool_name(self.tool_name)
        object.__setattr__(self, "capability", _as_capability(self.capability))
        object.__setattr__(self, "visibility", _as_visibility(self.visibility))
        _require_read_only_capability(self.tool_name, self.capability)
        require_identifier(self.source_package, "source_package")
        if self.source_package in _FORBIDDEN_TOOL_NAMES:
            raise ResearchToolContractError(
                ResearchToolFailureCode.UNAUTHORIZED_TOOL,
                "unsafe source package cannot be registered",
                details={"source_package": self.source_package},
            )
        require_identifier(self.provider_id, "provider_id")
        object.__setattr__(self, "allowed_data", _sorted_text_tuple(self.allowed_data, "allowed_data"))
        object.__setattr__(self, "availability", _as_availability(self.availability))
        require_text(self.availability_reason, "availability_reason")
        require_hash(self.context_hash, "context_hash")

    @property
    def artifact_hash(self) -> str:
        return content_hash(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "binding_id": self.binding_id,
            "campaign_hash": self.campaign_hash,
            "route_hash": self.route_hash,
            "desk_id": self.desk_id,
            "tool_name": self.tool_name,
            "capability": self.capability.value,
            "visibility": self.visibility.value,
            "source_package": self.source_package,
            "provider_id": self.provider_id,
            "allowed_data": self.allowed_data,
            "availability": self.availability.value,
            "availability_reason": self.availability_reason,
            "context_hash": self.context_hash,
        }


@dataclass(frozen=True, kw_only=True)
class DeskToolPack:
    """The complete tool set that one selected desk receives."""

    campaign_id: str
    campaign_hash: str
    route_hash: str
    desk_id: str
    artifact_namespace: str
    bindings: tuple[CampaignToolBinding, ...]
    policy: DeskToolPolicy
    schema_version: str = CAMPAIGN_TOOL_REGISTRY_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != CAMPAIGN_TOOL_REGISTRY_SCHEMA_VERSION:
            raise ResearchToolContractError(
                ResearchToolFailureCode.INVALID_SCHEMA_VERSION,
                "unsupported DeskToolPack schema",
            )
        require_identifier(self.campaign_id, "campaign_id")
        require_hash(self.campaign_hash, "campaign_hash")
        require_hash(self.route_hash, "route_hash")
        require_identifier(self.desk_id, "desk_id")
        require_identifier(self.artifact_namespace, "artifact_namespace")
        if not isinstance(self.bindings, tuple) or not self.bindings:
            raise ResearchToolContractError(
                ResearchToolFailureCode.MISSING_REQUIRED_FIELD,
                "DeskToolPack requires at least one binding",
            )
        ordered = tuple(sorted(self.bindings, key=lambda item: item.tool_name))
        tool_names = tuple(item.tool_name for item in ordered)
        if len(set(tool_names)) != len(tool_names):
            raise ResearchToolContractError(
                ResearchToolFailureCode.UNAUTHORIZED_TOOL,
                "DeskToolPack contains duplicate tool names",
                details={"desk_id": self.desk_id},
            )
        for binding in ordered:
            if binding.desk_id != self.desk_id:
                raise ResearchToolContractError(
                    ResearchToolFailureCode.DESK_MISMATCH,
                    "binding desk does not match pack desk",
                    details={"desk_id": self.desk_id, "binding_id": binding.binding_id},
                )
            if binding.campaign_hash != self.campaign_hash or binding.route_hash != self.route_hash:
                raise ResearchToolContractError(
                    ResearchToolFailureCode.CAMPAIGN_MISMATCH,
                    "binding does not match pack campaign or route",
                    details={"binding_id": binding.binding_id},
                )
        object.__setattr__(self, "bindings", ordered)
        if not isinstance(self.policy, DeskToolPolicy):
            raise ResearchToolContractError(ResearchToolFailureCode.INVALID_PAYLOAD, "policy must be DeskToolPolicy")
        if self.policy.campaign_id != self.campaign_id or self.policy.campaign_hash != self.campaign_hash:
            raise ResearchToolContractError(ResearchToolFailureCode.CAMPAIGN_MISMATCH, "policy campaign mismatch")
        if self.policy.desk_id != self.desk_id:
            raise ResearchToolContractError(ResearchToolFailureCode.DESK_MISMATCH, "policy desk mismatch")
        if tuple(sorted(self.policy.allowed_tools)) != tool_names:
            raise ResearchToolContractError(
                ResearchToolFailureCode.UNAUTHORIZED_TOOL,
                "policy allowed_tools must exactly match bindings",
                details={"desk_id": self.desk_id},
            )

    @property
    def available_tools(self) -> tuple[str, ...]:
        return tuple(
            binding.tool_name for binding in self.bindings if binding.availability is DeskToolAvailability.AVAILABLE
        )

    @property
    def unavailable_tools(self) -> tuple[str, ...]:
        return tuple(
            binding.tool_name for binding in self.bindings if binding.availability is DeskToolAvailability.UNAVAILABLE
        )

    @property
    def artifact_hash(self) -> str:
        return content_hash(self.to_dict())

    def require_binding(self, tool_name: str) -> CampaignToolBinding:
        _require_tool_name(tool_name)
        for binding in self.bindings:
            if binding.tool_name == tool_name:
                return binding
        raise ResearchToolContractError(
            ResearchToolFailureCode.UNAUTHORIZED_TOOL,
            "tool is not declared for this desk",
            details={"desk_id": self.desk_id, "tool_name": tool_name},
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "campaign_id": self.campaign_id,
            "campaign_hash": self.campaign_hash,
            "route_hash": self.route_hash,
            "desk_id": self.desk_id,
            "artifact_namespace": self.artifact_namespace,
            "bindings": tuple(binding.to_dict() for binding in self.bindings),
            "policy_hash": self.policy.artifact_hash,
            "policy": {
                "campaign_id": self.policy.campaign_id,
                "campaign_hash": self.policy.campaign_hash,
                "desk_id": self.policy.desk_id,
                "allowed_data": self.policy.allowed_data,
                "allowed_tools": self.policy.allowed_tools,
                "tool_capabilities": {
                    key: tuple(item.value for item in value)
                    for key, value in sorted(self.policy.tool_capabilities.items())
                },
                "offline_required": self.policy.offline_required,
            },
        }


@dataclass(frozen=True, kw_only=True)
class RouteAuthorityManifest:
    """Static manifest describing the authority boundary of a registry."""

    campaign_hash: str
    route_hash: str
    selected_desks: tuple[str, ...]
    registry_hash: str
    no_global_tool_registry: bool = True
    offline_required: bool = True
    official_authorities_excluded: tuple[str, ...] = (
        "sealed_reader",
        "official_evidence",
        "promotion",
        "risk_approval",
        "target_weights",
        "orders",
        "broker",
        "ledger",
        "shell",
        "file_read_write",
    )
    schema_version: str = CAMPAIGN_TOOL_REGISTRY_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != CAMPAIGN_TOOL_REGISTRY_SCHEMA_VERSION:
            raise ResearchToolContractError(
                ResearchToolFailureCode.INVALID_SCHEMA_VERSION,
                "unsupported RouteAuthorityManifest schema",
            )
        require_hash(self.campaign_hash, "campaign_hash")
        require_hash(self.route_hash, "route_hash")
        object.__setattr__(self, "selected_desks", _sorted_text_tuple(self.selected_desks, "selected_desks", nonempty=True))
        require_hash(self.registry_hash, "registry_hash")
        if self.no_global_tool_registry is not True or self.offline_required is not True:
            raise ResearchToolContractError(
                ResearchToolFailureCode.UNAUTHORIZED_CAPABILITY,
                "Campaign tool manifests must be isolated and offline-required",
            )
        object.__setattr__(
            self,
            "official_authorities_excluded",
            _sorted_text_tuple(self.official_authorities_excluded, "official_authorities_excluded", nonempty=True),
        )

    @property
    def artifact_hash(self) -> str:
        return content_hash(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "campaign_hash": self.campaign_hash,
            "route_hash": self.route_hash,
            "selected_desks": self.selected_desks,
            "registry_hash": self.registry_hash,
            "no_global_tool_registry": self.no_global_tool_registry,
            "offline_required": self.offline_required,
            "official_authorities_excluded": self.official_authorities_excluded,
        }


class CampaignToolRegistry:
    """Deterministic per-Campaign registry of desk-scoped tool packs."""

    def __init__(self, *, packs: tuple[DeskToolPack, ...]) -> None:
        if not isinstance(packs, tuple) or not packs:
            raise ResearchToolContractError(
                ResearchToolFailureCode.MISSING_REQUIRED_FIELD,
                "registry requires desk tool packs",
            )
        ordered = tuple(sorted(packs, key=lambda item: item.desk_id))
        desk_ids = tuple(pack.desk_id for pack in ordered)
        if len(set(desk_ids)) != len(desk_ids):
            raise ResearchToolContractError(ResearchToolFailureCode.DESK_MISMATCH, "duplicate desk packs")
        if ALL_DESKS <= set(desk_ids):
            raise ResearchToolContractError(
                ResearchToolFailureCode.UNAUTHORIZED_CAPABILITY,
                "Campaign tool registry must not open all desks by default",
            )
        campaign_hashes = {pack.campaign_hash for pack in ordered}
        route_hashes = {pack.route_hash for pack in ordered}
        if len(campaign_hashes) != 1 or len(route_hashes) != 1:
            raise ResearchToolContractError(
                ResearchToolFailureCode.CAMPAIGN_MISMATCH,
                "all desk packs must bind the same campaign and route",
            )
        self._packs = MappingProxyType({pack.desk_id: pack for pack in ordered})
        self.campaign_hash = next(iter(campaign_hashes))
        self.route_hash = next(iter(route_hashes))

    @property
    def desk_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._packs))

    @property
    def packs(self) -> Mapping[str, DeskToolPack]:
        return self._packs

    @property
    def artifact_hash(self) -> str:
        return content_hash(self.to_dict())

    @property
    def authority_manifest(self) -> RouteAuthorityManifest:
        return RouteAuthorityManifest(
            campaign_hash=self.campaign_hash,
            route_hash=self.route_hash,
            selected_desks=self.desk_ids,
            registry_hash=self.artifact_hash,
        )

    def pack_for_desk(self, desk_id: str) -> DeskToolPack:
        require_identifier(desk_id, "desk_id")
        try:
            return self._packs[desk_id]
        except KeyError as exc:
            raise ResearchToolContractError(
                ResearchToolFailureCode.UNAUTHORIZED_TOOL,
                "desk was not selected by the CampaignRoute",
                details={"desk_id": desk_id},
            ) from exc

    def binding_for(self, desk_id: str, tool_name: str) -> CampaignToolBinding:
        return self.pack_for_desk(desk_id).require_binding(tool_name)

    def tool_names_for_desk(self, desk_id: str, *, available_only: bool = False) -> tuple[str, ...]:
        pack = self.pack_for_desk(desk_id)
        if available_only:
            return pack.available_tools
        return tuple(binding.tool_name for binding in pack.bindings)

    def invoke(
        self,
        request: ResearchToolRequest,
        *,
        payload: Mapping[str, Any] | None = None,
    ) -> ResearchToolResult:
        if not isinstance(request, ResearchToolRequest):
            raise ResearchToolContractError(ResearchToolFailureCode.INVALID_PAYLOAD, "request must be ResearchToolRequest")
        binding = self.binding_for(request.desk_id, request.tool_name)
        pack = self.pack_for_desk(request.desk_id)
        pack.policy.authorize(request, binding.capability)
        if binding.availability is DeskToolAvailability.UNAVAILABLE:
            return ResearchToolResult(
                request=request,
                result_id=f"{binding.desk_id}:{binding.tool_name}:unavailable",
                visibility=binding.visibility,
                capability=binding.capability,
                payload={
                    "status": "unavailable",
                    "tool_name": binding.tool_name,
                    "source_package": binding.source_package,
                    "reason_code": ResearchToolFailureCode.PROVIDER_UNAVAILABLE.value,
                },
                failure_code=ResearchToolFailureCode.PROVIDER_UNAVAILABLE,
                message=binding.availability_reason,
            )
        clean_payload = dict(payload or {})
        result_payload = {
            "status": "available",
            "tool_name": binding.tool_name,
            "desk_id": binding.desk_id,
            "source_package": binding.source_package,
            "binding_hash": binding.artifact_hash,
            "content": clean_payload,
        }
        return ResearchToolResult(
            request=request,
            result_id=f"{binding.desk_id}:{binding.tool_name}:{binding.artifact_hash[7:19]}",
            visibility=binding.visibility,
            capability=binding.capability,
            payload=result_payload,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": CAMPAIGN_TOOL_REGISTRY_SCHEMA_VERSION,
            "campaign_hash": self.campaign_hash,
            "route_hash": self.route_hash,
            "desk_ids": self.desk_ids,
            "packs": tuple(pack.to_dict() for pack in self._packs.values()),
        }


@dataclass(frozen=True)
class _ToolSpec:
    desk_id: str
    tool_name: str
    capability: ResearchToolCapability
    source_package: str
    provider_id: str
    allowed_data: tuple[str, ...] = ()
    visibility: ResearchToolVisibility = ResearchToolVisibility.DRAFT_SUPPORT
    provider_key: str | None = None


def _specs() -> tuple[_ToolSpec, ...]:
    data = ResearchToolCapability.READ_FROZEN_DATA
    doc = ResearchToolCapability.READ_FROZEN_DOCUMENT
    visible = ResearchToolCapability.READ_VISIBLE_EVIDENCE
    design = ResearchToolCapability.DESIGN_EXPERIMENT
    lint = ResearchToolCapability.LINT_DRAFT
    return (
        _ToolSpec("fundamental", "dataset_pit_summary", data, "research_tool_contracts", "offline_fixture", ("price_volume",)),
        _ToolSpec("fundamental", "fundamentals_summary", data, "vibe_market_adapters", "frozen_vibe_market", ("fundamentals",)),
        _ToolSpec("fundamental", "financial_statements_summary", data, "vibe_market_adapters", "frozen_vibe_market", ("fundamentals",)),
        _ToolSpec("fundamental", "financial_rigor_lint", lint, "vibe_market_adapters", "frozen_vibe_market", ("fundamentals",)),
        _ToolSpec("fundamental", "query_factor_library", data, "factor_library", "static_catalog"),
        _ToolSpec("fundamental", "query_visible_evidence", visible, "research_explorer_tools", "visible_evidence_reader"),
        _ToolSpec("expectations_events", "frozen_document_summary", doc, "document_research_adapters", "frozen_document_provider", ("events",), provider_key="documents"),
        _ToolSpec("expectations_events", "news_summary", doc, "document_research_adapters", "frozen_document_provider", ("events",), provider_key="documents"),
        _ToolSpec("expectations_events", "research_report_summary", doc, "document_research_adapters", "frozen_document_provider", ("events",), provider_key="documents"),
        _ToolSpec("expectations_events", "filing_summary", doc, "document_research_adapters", "frozen_document_provider", ("events",), provider_key="documents"),
        _ToolSpec("expectations_events", "shareholder_count_summary", data, "vibe_market_adapters", "frozen_vibe_market", ("fundamentals",)),
        _ToolSpec("expectations_events", "lockup_event_summary", data, "vibe_market_adapters", "frozen_vibe_market", ("events",)),
        _ToolSpec("expectations_events", "event_time_resolver", design, "research_tool_contracts", "offline_fixture", ("events",)),
        _ToolSpec("market_structure", "price_volume_coverage", data, "research_tool_contracts", "offline_fixture", ("price_volume",)),
        _ToolSpec("market_structure", "margin_summary", data, "vibe_market_adapters", "frozen_vibe_market", ("market_structure",)),
        _ToolSpec("market_structure", "northbound_summary", data, "vibe_market_adapters", "frozen_vibe_market", ("market_structure",)),
        _ToolSpec("market_structure", "fund_flow_summary", data, "vibe_market_adapters", "frozen_vibe_market", ("market_structure",)),
        _ToolSpec("market_structure", "dragon_tiger_summary", data, "vibe_market_adapters", "frozen_vibe_market", ("events", "market_structure")),
        _ToolSpec("market_structure", "block_trade_summary", data, "vibe_market_adapters", "frozen_vibe_market", ("events", "market_structure")),
        _ToolSpec("market_structure", "cost_capacity_summary", visible, "research_explorer_tools", "visible_evidence_reader"),
        _ToolSpec("alternative_data", "provenance_summary", doc, "research_tool_contracts", "offline_fixture"),
        _ToolSpec("alternative_data", "coverage_bias_summary", data, "research_tool_contracts", "offline_fixture"),
        _ToolSpec("alternative_data", "frozen_citation_summary", doc, "document_research_adapters", "frozen_document_provider", provider_key="documents"),
        _ToolSpec("alternative_data", "data_source_assessment", design, "research_explorer_tools", "visible_evidence_reader"),
        _ToolSpec("macro_regime", "dataset_summary", data, "research_tool_contracts", "offline_fixture", ("price_volume",)),
        _ToolSpec("macro_regime", "frozen_macro_fixture", data, "macro_provider", "macro_fixture", ("macro_regime",), provider_key="macro"),
        _ToolSpec("macro_regime", "regime_summary", data, "macro_provider", "macro_fixture", ("macro_regime",), provider_key="macro"),
        _ToolSpec("meta_alpha", "query_factor_library", data, "factor_library", "static_catalog"),
        _ToolSpec("meta_alpha", "lineage_summary", visible, "quanta_research_adapters", "quanta_lineage_visible"),
        _ToolSpec("meta_alpha", "query_visible_evidence", visible, "research_explorer_tools", "visible_evidence_reader"),
        _ToolSpec("meta_alpha", "compare_candidates", visible, "research_explorer_tools", "visible_evidence_reader"),
        _ToolSpec("meta_alpha", "portfolio_gap_summary", visible, "research_explorer_tools", "visible_evidence_reader"),
        _ToolSpec("meta_alpha", "trial_budget_summary", visible, "research_explorer_tools", "visible_evidence_reader"),
    )


def _default_package_statuses() -> Mapping[str, bool]:
    return MappingProxyType(
        {
            "research_tool_contracts": True,
            "vibe_market_adapters": True,
            "quanta_research_adapters": True,
            "research_explorer_tools": True,
            "factor_library": True,
            "document_research_adapters": False,
            "macro_provider": False,
        }
    )


class CampaignToolPolicyCompiler:
    """Compile selected desk tool packs from a confirmed Campaign and route."""

    def __init__(
        self,
        *,
        package_statuses: Mapping[str, bool] | None = None,
        provider_availability: Mapping[str, bool] | None = None,
        offline_required: bool = True,
    ) -> None:
        statuses = dict(_default_package_statuses())
        statuses.update(dict(package_statuses or {}))
        self.package_statuses = MappingProxyType(dict(sorted(statuses.items())))
        self.provider_availability = MappingProxyType(dict(sorted(dict(provider_availability or {}).items())))
        if offline_required is not True:
            raise ResearchToolContractError(
                ResearchToolFailureCode.OFFLINE_NETWORK_DISABLED,
                "Campaign tool registry is offline-required",
            )
        self.offline_required = offline_required

    def compile(self, campaign: ResearchCampaign, route: CampaignRoute) -> CampaignToolRegistry:
        if not isinstance(campaign, ResearchCampaign):
            fail(FailureCode.INVALID_VALUE, "campaign must be ResearchCampaign")
        require_campaign_executable(campaign)
        if not isinstance(route, CampaignRoute):
            fail(FailureCode.INVALID_VALUE, "route must be CampaignRoute")
        if route.state is not CampaignState.ROUTED:
            fail(FailureCode.INVALID_STATE, "route must be ROUTED")
        if route.campaign_hash != campaign.artifact_hash:
            fail(FailureCode.INVALID_HASH, "route does not bind the Campaign hash")
        selected_desks = tuple(sorted(route.selected_desks))
        if ALL_DESKS <= set(selected_desks):
            fail(FailureCode.UNAUTHORIZED_CAPABILITY, "registry compiler must not open all six desks")
        for task in route.tasks:
            if task.requires_official_evidence or task.allows_promotion:
                fail(FailureCode.UNAUTHORIZED_CAPABILITY, "route task cannot expose Evidence or Promotion authority")
        task_namespace = {task.desk_id: task.artifact_namespace for task in route.tasks}
        specs_by_desk = _specs_by_desk()
        packs: list[DeskToolPack] = []
        for desk_id in selected_desks:
            if desk_id not in task_namespace:
                fail(FailureCode.DAG_VALIDATION_FAILED, "selected desk has no route task", desk_id=desk_id)
            specs = tuple(sorted(specs_by_desk.get(desk_id, ()), key=lambda item: item.tool_name))
            if not specs:
                fail(FailureCode.ADAPTER_UNAVAILABLE, "selected desk has no tool specs", desk_id=desk_id)
            bindings = tuple(self._binding_for_spec(campaign, route, spec) for spec in specs)
            policy = DeskToolPolicy(
                campaign_id=campaign.campaign_id,
                campaign_hash=campaign.artifact_hash,
                desk_id=desk_id,
                allowed_data=campaign.subject.allowed_data,
                allowed_tools=tuple(binding.tool_name for binding in bindings),
                tool_capabilities={binding.tool_name: (binding.capability,) for binding in bindings},
                offline_required=self.offline_required,
            )
            packs.append(
                DeskToolPack(
                    campaign_id=campaign.campaign_id,
                    campaign_hash=campaign.artifact_hash,
                    route_hash=route.artifact_hash,
                    desk_id=desk_id,
                    artifact_namespace=task_namespace[desk_id],
                    bindings=bindings,
                    policy=policy,
                )
            )
        return CampaignToolRegistry(packs=tuple(packs))

    def _binding_for_spec(
        self,
        campaign: ResearchCampaign,
        route: CampaignRoute,
        spec: _ToolSpec,
    ) -> CampaignToolBinding:
        _require_tool_name(spec.tool_name)
        _require_read_only_capability(spec.tool_name, spec.capability)
        missing_data = tuple(sorted(set(spec.allowed_data) - set(campaign.subject.allowed_data)))
        package_available = bool(self.package_statuses.get(spec.source_package, False))
        provider_key = spec.provider_key or spec.source_package
        provider_available = bool(self.provider_availability.get(provider_key, package_available))
        if missing_data:
            availability = DeskToolAvailability.UNAVAILABLE
            reason = "campaign_allowed_data_missing:" + ",".join(missing_data)
        elif not package_available:
            availability = DeskToolAvailability.UNAVAILABLE
            reason = "package_not_ready:" + spec.source_package
        elif not provider_available:
            availability = DeskToolAvailability.UNAVAILABLE
            reason = "provider_unavailable:" + provider_key
        else:
            availability = DeskToolAvailability.AVAILABLE
            reason = "available"
        context_hash = content_hash(
            {
                "campaign_hash": campaign.artifact_hash,
                "route_hash": route.artifact_hash,
                "desk_id": spec.desk_id,
                "tool_name": spec.tool_name,
                "allowed_data": campaign.subject.allowed_data,
                "source_package": spec.source_package,
            }
        )
        binding_id = "binding." + context_hash[7:23]
        return CampaignToolBinding(
            binding_id=binding_id,
            campaign_hash=campaign.artifact_hash,
            route_hash=route.artifact_hash,
            desk_id=spec.desk_id,
            tool_name=spec.tool_name,
            capability=spec.capability,
            visibility=spec.visibility,
            source_package=spec.source_package,
            provider_id=spec.provider_id,
            allowed_data=spec.allowed_data,
            availability=availability,
            availability_reason=reason,
            context_hash=context_hash,
        )


def _specs_by_desk() -> Mapping[str, tuple[_ToolSpec, ...]]:
    grouped: dict[str, list[_ToolSpec]] = {}
    for spec in _specs():
        grouped.setdefault(spec.desk_id, []).append(spec)
    return MappingProxyType({desk: tuple(items) for desk, items in sorted(grouped.items())})


def build_campaign_tool_registry(
    campaign: ResearchCampaign,
    route: CampaignRoute,
    *,
    package_statuses: Mapping[str, bool] | None = None,
    provider_availability: Mapping[str, bool] | None = None,
) -> CampaignToolRegistry:
    """Build a deterministic, offline, selected-desk tool registry."""

    return CampaignToolPolicyCompiler(
        package_statuses=package_statuses,
        provider_availability=provider_availability,
    ).compile(campaign, route)


__all__ = [
    "CAMPAIGN_TOOL_REGISTRY_SCHEMA_VERSION",
    "CampaignToolBinding",
    "CampaignToolPolicyCompiler",
    "CampaignToolRegistry",
    "DeskToolAvailability",
    "DeskToolPack",
    "RouteAuthorityManifest",
    "build_campaign_tool_registry",
]
