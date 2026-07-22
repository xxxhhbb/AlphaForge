"""Thin Campaign-routed LLM runtime contracts.

This package is an integration facade over the existing Research Campaign and
LLM Desk contracts.  It does not add Evidence, Promotion, Portfolio, Risk,
Execution, or journal authority; it only validates Campaign routing boundaries
and exposes a small runtime/checkpoint/manifest protocol for selected desks.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Any, ClassVar, Mapping

from ..contracts import CanonicalContract, Component, FailureCode
from ..contracts.base import fail, require_hash, require_identifier
from ..llm_desks.models import DeskId
from ..llm_desks.safety import assert_no_forbidden_content, assert_safe_tool_name
from ..research_campaign.models import CampaignRoute, CampaignState, ResearchCampaign
from ..research_campaign.protocols import CampaignProtocolPlan
from ..research_campaign.runtime import (
    BranchRuntimeStatus,
    CampaignBranchResult,
    CampaignCommittedStage,
    CampaignRuntimeControl,
    CampaignRuntimeRecoveryState,
    CampaignRuntimeStatus,
    CampaignStageResult,
    StageRuntimeStatus,
)


CAMPAIGN_LLM_RUNTIME_SCHEMA_VERSION = "veriquant.campaign-llm-runtime/v1"
CAMPAIGN_LLM_RUNTIME_RULE_VERSION = "veriquant.campaign-llm-runtime/v1"
CAMPAIGN_LLM_RUNTIME_MODE = "DETERMINISTIC_OFFLINE_WRAPPER"
ALLOWED_CAMPAIGN_RUNTIME_MODES = frozenset({CAMPAIGN_LLM_RUNTIME_MODE, "REAL_LLM"})

ALL_CAMPAIGN_DESKS: tuple[str, ...] = tuple(desk.value for desk in DeskId)


class StrEnum(str, Enum):
    """Python 3.10-compatible string enum with stable JSON values."""


class CampaignRuntimeFailureCode(StrEnum):
    CAMPAIGN_NOT_CONFIRMED = "CAMPAIGN_NOT_CONFIRMED"
    ROUTE_NOT_ROUTED = "ROUTE_NOT_ROUTED"
    ROUTE_PLAN_MISMATCH = "ROUTE_PLAN_MISMATCH"
    DESK_NOT_SELECTED = "DESK_NOT_SELECTED"
    CONTEXT_REJECTED = "CONTEXT_REJECTED"
    TOOL_REJECTED = "TOOL_REJECTED"
    PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
    BRANCH_FAILED = "BRANCH_FAILED"
    PRIVILEGED_OUTPUT_REJECTED = "PRIVILEGED_OUTPUT_REJECTED"
    CHECKPOINT_REJECTED = "CHECKPOINT_REJECTED"
    CANCELLED = "CANCELLED"


@dataclass(frozen=True, kw_only=True)
class CampaignDeskRunPlan(CanonicalContract):
    """Validated runtime input for one routed Campaign run.

    The route and protocol plan carry the selected desks.  Optional desk context
    and tool scopes must be declared only for selected desks, and they are
    checked before anything is handed to the lower-level executor.
    """

    SCHEMA_VERSION: ClassVar[str] = CAMPAIGN_LLM_RUNTIME_SCHEMA_VERSION

    run_id: str
    campaign: Any
    route: CampaignRoute
    protocol_plan: CampaignProtocolPlan
    runtime_mode: str = CAMPAIGN_LLM_RUNTIME_MODE
    desk_context_hashes: Mapping[str, str] = field(default_factory=dict)
    desk_tool_whitelists: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    desk_budgets: Mapping[str, Mapping[str, int]] = field(default_factory=dict)
    desk_context_payloads: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        require_identifier(self.run_id, "run_id")
        require_identifier(self.runtime_mode, "runtime_mode")
        if not isinstance(self.route, CampaignRoute):
            fail(FailureCode.INVALID_VALUE, "route must be CampaignRoute")
        if not isinstance(self.protocol_plan, CampaignProtocolPlan):
            fail(FailureCode.INVALID_VALUE, "protocol_plan must be CampaignProtocolPlan")
        _validate_runtime_mode(self.runtime_mode)
        selected = _selected_desk_set(self.route)
        _validate_hash_mapping(self.desk_context_hashes, "desk_context_hashes", selected=selected)
        _validate_tool_mapping(self.desk_tool_whitelists, "desk_tool_whitelists", selected=selected)
        _validate_budget_mapping(self.desk_budgets, "desk_budgets", selected=selected)
        _validate_context_payloads(self.desk_context_payloads, selected=selected)
        object.__setattr__(self, "desk_context_hashes", _freeze_sorted_mapping(self.desk_context_hashes))
        object.__setattr__(
            self,
            "desk_tool_whitelists",
            MappingProxyType({key: tuple(value) for key, value in sorted(self.desk_tool_whitelists.items())}),
        )
        object.__setattr__(
            self,
            "desk_budgets",
            MappingProxyType(
                {key: MappingProxyType(dict(sorted(value.items()))) for key, value in sorted(self.desk_budgets.items())}
            ),
        )
        object.__setattr__(
            self,
            "desk_context_payloads",
            MappingProxyType(
                {key: MappingProxyType(dict(sorted(value.items()))) for key, value in sorted(self.desk_context_payloads.items())}
            ),
        )

    @property
    def selected_desks(self) -> tuple[str, ...]:
        return tuple(self.route.selected_desks)

    @property
    def not_selected_desks(self) -> tuple[str, ...]:
        selected = set(self.route.selected_desks)
        return tuple(desk for desk in ALL_CAMPAIGN_DESKS if desk not in selected)


@dataclass(frozen=True, kw_only=True)
class CampaignRuntimeCheckpoint(CanonicalContract):
    """Checkpoint/cancel/resume envelope for the facade runtime."""

    SCHEMA_VERSION: ClassVar[str] = CAMPAIGN_LLM_RUNTIME_SCHEMA_VERSION

    committed_stages: tuple[CampaignCommittedStage, ...] = ()
    cancel_requested: bool = False
    stop_at_next_checkpoint: bool = False
    checkpoint_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.committed_stages, tuple):
            fail(FailureCode.INVALID_VALUE, "committed_stages must be a tuple")
        if not all(isinstance(item, CampaignCommittedStage) for item in self.committed_stages):
            fail(FailureCode.INVALID_VALUE, "committed_stages must contain CampaignCommittedStage")
        keys = tuple(item.stage_key for item in self.committed_stages)
        if len(set(keys)) != len(keys):
            fail(FailureCode.INVALID_STATE, "checkpoint contains duplicate committed stage keys")
        if not isinstance(self.cancel_requested, bool) or not isinstance(self.stop_at_next_checkpoint, bool):
            fail(FailureCode.INVALID_VALUE, "checkpoint control flags must be boolean")
        if self.checkpoint_id is not None:
            require_identifier(self.checkpoint_id, "checkpoint_id")

    def to_recovery_state(self) -> CampaignRuntimeRecoveryState:
        return CampaignRuntimeRecoveryState(committed_stages=self.committed_stages)

    def to_control(self) -> CampaignRuntimeControl:
        return CampaignRuntimeControl(
            cancel_requested=self.cancel_requested,
            stop_at_next_checkpoint=self.stop_at_next_checkpoint,
        )

    @classmethod
    def from_manifest(cls, manifest: "CampaignRuntimeManifest") -> "CampaignRuntimeCheckpoint":
        committed: list[CampaignCommittedStage] = []
        for branch in manifest.branch_results:
            for stage in branch.stage_results:
                if stage.status in {StageRuntimeStatus.BLOCKED, StageRuntimeStatus.CANCELLED, StageRuntimeStatus.SKIPPED}:
                    continue
                committed.append(
                    CampaignCommittedStage(
                        stage_key=f"{stage.route_task_id}:{stage.stage_id}",
                        result=stage,
                    )
                )
        return cls(committed_stages=tuple(committed), checkpoint_id=manifest.awaiting_checkpoint_id)


@dataclass(frozen=True, kw_only=True)
class CampaignRuntimeManifest(CanonicalContract):
    """Stable hashable facade manifest for Campaign-routed desk execution."""

    SCHEMA_VERSION: ClassVar[str] = CAMPAIGN_LLM_RUNTIME_SCHEMA_VERSION

    run_id: str
    campaign_hash: str
    route_hash: str
    protocol_plan_hash: str
    runtime_version: str
    runtime_mode: str
    status: CampaignRuntimeStatus
    selected_desks: tuple[str, ...]
    not_selected_desks: tuple[str, ...]
    branch_results: tuple[CampaignBranchResult, ...]
    artifact_hashes: Mapping[str, str] = field(default_factory=dict)
    base_manifest_hash: str
    checkpoint_hash: str | None = None
    awaiting_checkpoint_id: str | None = None
    cancelled: bool = False

    def __post_init__(self) -> None:
        require_identifier(self.run_id, "run_id")
        for name in ("campaign_hash", "route_hash", "protocol_plan_hash", "base_manifest_hash"):
            require_hash(getattr(self, name), name)
        require_identifier(self.runtime_version, "runtime_version")
        require_identifier(self.runtime_mode, "runtime_mode")
        _validate_runtime_mode(self.runtime_mode)
        if not isinstance(self.status, CampaignRuntimeStatus):
            object.__setattr__(self, "status", CampaignRuntimeStatus(self.status))
        _validate_desk_tuple(self.selected_desks, "selected_desks", nonempty=True)
        _validate_desk_tuple(self.not_selected_desks, "not_selected_desks", nonempty=False)
        if set(self.selected_desks) & set(self.not_selected_desks):
            fail(FailureCode.INVALID_VALUE, "selected and not_selected desks overlap")
        if not isinstance(self.branch_results, tuple):
            fail(FailureCode.INVALID_VALUE, "branch_results must be a tuple")
        if not all(isinstance(item, CampaignBranchResult) for item in self.branch_results):
            fail(FailureCode.INVALID_VALUE, "branch_results must contain CampaignBranchResult")
        if tuple(item.route_task_id for item in self.branch_results) != tuple(
            sorted(item.route_task_id for item in self.branch_results)
        ):
            fail(FailureCode.INVALID_STATE, "branch_results must be stable sorted")
        branch_desks = {branch.desk_id for branch in self.branch_results}
        if branch_desks != set(self.selected_desks):
            fail(FailureCode.INVALID_STATE, "branch results must match selected desks exactly")
        hashes = dict(self.artifact_hashes)
        for key, value in hashes.items():
            require_identifier(key, "artifact_hashes.key")
            require_hash(value, f"artifact_hashes.{key}")
        object.__setattr__(self, "artifact_hashes", MappingProxyType(dict(sorted(hashes.items()))))
        if self.checkpoint_hash is not None:
            require_hash(self.checkpoint_hash, "checkpoint_hash")
        if self.awaiting_checkpoint_id is not None:
            require_identifier(self.awaiting_checkpoint_id, "awaiting_checkpoint_id")
        if not isinstance(self.cancelled, bool):
            fail(FailureCode.INVALID_VALUE, "cancelled must be boolean")
        _validate_manifest_visible_boundary(self)


def require_confirmed_campaign(campaign: Any) -> ResearchCampaign:
    if not isinstance(campaign, ResearchCampaign) or campaign.state is not CampaignState.CONFIRMED:
        fail(
            FailureCode.INVALID_STATE,
            "Campaign LLM runtime requires a CONFIRMED ResearchCampaign",
            campaign_runtime_failure_code=CampaignRuntimeFailureCode.CAMPAIGN_NOT_CONFIRMED.value,
        )
    return campaign


def validate_plan_bindings(plan: CampaignDeskRunPlan) -> ResearchCampaign:
    campaign = require_confirmed_campaign(plan.campaign)
    route = plan.route
    protocol_plan = plan.protocol_plan
    if route.state is not CampaignState.ROUTED:
        fail(
            FailureCode.INVALID_STATE,
            "Campaign LLM runtime requires a ROUTED CampaignRoute",
            campaign_runtime_failure_code=CampaignRuntimeFailureCode.ROUTE_NOT_ROUTED.value,
        )
    if route.campaign_hash != campaign.artifact_hash:
        fail(
            FailureCode.INVALID_HASH,
            "route does not bind the confirmed Campaign hash",
            campaign_runtime_failure_code=CampaignRuntimeFailureCode.ROUTE_PLAN_MISMATCH.value,
        )
    if protocol_plan.campaign_hash != campaign.artifact_hash or protocol_plan.route_hash != route.artifact_hash:
        fail(
            FailureCode.INVALID_HASH,
            "protocol plan does not bind the Campaign and route hashes",
            campaign_runtime_failure_code=CampaignRuntimeFailureCode.ROUTE_PLAN_MISMATCH.value,
        )
    selected = set(route.selected_desks)
    task_desks = {task.desk_id for task in route.tasks}
    if task_desks != selected:
        fail(
            FailureCode.INVALID_VALUE,
            "route tasks must exactly match selected desks",
            campaign_runtime_failure_code=CampaignRuntimeFailureCode.DESK_NOT_SELECTED.value,
        )
    task_namespaces = tuple(task.artifact_namespace for task in route.tasks)
    if len(set(task_namespaces)) != len(task_namespaces):
        fail(FailureCode.ARTIFACT_COLLISION, "selected desks must use isolated artifact namespaces")
    for task in route.tasks:
        if task.allows_promotion:
            fail(
                FailureCode.UNAUTHORIZED_CAPABILITY,
                "Campaign LLM runtime cannot run tasks with downstream approval authority",
                campaign_runtime_failure_code=CampaignRuntimeFailureCode.PRIVILEGED_OUTPUT_REJECTED.value,
            )
    protocol_task_ids = {item.route_task_id for item in protocol_plan.task_protocols}
    route_task_ids = {task.task_id for task in route.tasks}
    if protocol_task_ids != route_task_ids:
        fail(
            FailureCode.INVALID_VALUE,
            "protocol plan tasks must exactly match route tasks",
            campaign_runtime_failure_code=CampaignRuntimeFailureCode.ROUTE_PLAN_MISMATCH.value,
        )
    for item in protocol_plan.task_protocols:
        if item.allows_automatic_trading:
            fail(
                FailureCode.UNAUTHORIZED_CAPABILITY,
                "Campaign LLM runtime cannot run protocols with automatic trading authority",
                campaign_runtime_failure_code=CampaignRuntimeFailureCode.PRIVILEGED_OUTPUT_REJECTED.value,
            )
    return campaign


def validate_branch_authority(branches: tuple[CampaignBranchResult, ...]) -> None:
    """Validate lower-level output before wrapping it in the facade manifest."""

    for branch in branches:
        if branch.status is BranchRuntimeStatus.COMPLETED and branch.failure_code is not None:
            fail(FailureCode.INVALID_STATE, "completed branch cannot carry a failure code")
        if branch.desk_id not in ALL_CAMPAIGN_DESKS:
            fail(FailureCode.INVALID_IDENTIFIER, "unknown desk returned by runtime", desk_id=branch.desk_id)
        for stage in branch.stage_results:
            if stage.produced_by in {Component.LLM_RESEARCHER, Component.RESEARCH_DESK} and stage.status not in {
                StageRuntimeStatus.DRAFT,
                StageRuntimeStatus.EXPLANATION,
            }:
                fail(
                    FailureCode.UNAUTHORIZED_CAPABILITY,
                    "LLM desk output must remain DRAFT or explanation only",
                    campaign_runtime_failure_code=CampaignRuntimeFailureCode.PRIVILEGED_OUTPUT_REJECTED.value,
                )
            _reject_privileged_visible_text(stage.message, where="stage.message")
        _reject_privileged_visible_text(branch.message, where="branch.message")


def _validate_runtime_mode(mode: str) -> None:
    require_identifier(mode, "runtime_mode")
    if mode not in ALLOWED_CAMPAIGN_RUNTIME_MODES:
        fail(
            FailureCode.INVALID_VALUE,
            "runtime_mode must be a declared deterministic or REAL_LLM mode",
            runtime_mode=mode,
            allowed=tuple(sorted(ALLOWED_CAMPAIGN_RUNTIME_MODES)),
        )


def _selected_desk_set(route: CampaignRoute) -> frozenset[str]:
    _validate_desk_tuple(route.selected_desks, "route.selected_desks", nonempty=True)
    return frozenset(route.selected_desks)


def _validate_desk_tuple(value: tuple[str, ...], field_name: str, *, nonempty: bool) -> None:
    if not isinstance(value, tuple) or (nonempty and not value):
        fail(FailureCode.INVALID_VALUE, f"{field_name} must be a tuple")
    if len(set(value)) != len(value):
        fail(FailureCode.INVALID_VALUE, f"{field_name} contains duplicate desks")
    for desk_id in value:
        if desk_id not in ALL_CAMPAIGN_DESKS:
            fail(FailureCode.INVALID_IDENTIFIER, "unknown Campaign Research Desk", desk_id=desk_id)


def _require_selected_desk_key(desk_id: str, field_name: str, *, selected: frozenset[str]) -> None:
    if desk_id not in ALL_CAMPAIGN_DESKS:
        fail(FailureCode.INVALID_IDENTIFIER, "unknown Campaign Research Desk", field=field_name, desk_id=desk_id)
    if desk_id not in selected:
        fail(
            FailureCode.UNAUTHORIZED_CAPABILITY,
            "unselected desks cannot receive runtime context, tools, budgets, or artifact scopes",
            field=field_name,
            desk_id=desk_id,
            campaign_runtime_failure_code=CampaignRuntimeFailureCode.DESK_NOT_SELECTED.value,
        )


def _validate_hash_mapping(value: Mapping[str, str], field_name: str, *, selected: frozenset[str]) -> None:
    if not isinstance(value, Mapping):
        fail(FailureCode.INVALID_VALUE, f"{field_name} must be a mapping")
    for desk_id, hash_value in value.items():
        _require_selected_desk_key(desk_id, field_name, selected=selected)
        require_hash(hash_value, f"{field_name}.{desk_id}")


def _validate_tool_mapping(value: Mapping[str, tuple[str, ...]], field_name: str, *, selected: frozenset[str]) -> None:
    if not isinstance(value, Mapping):
        fail(FailureCode.INVALID_VALUE, f"{field_name} must be a mapping")
    for desk_id, tools in value.items():
        _require_selected_desk_key(desk_id, field_name, selected=selected)
        if not isinstance(tools, tuple):
            fail(FailureCode.INVALID_VALUE, f"{field_name}.{desk_id} must be a tuple")
        for tool in tools:
            require_identifier(tool, f"{field_name}.{desk_id}.tool")
            assert_safe_tool_name(tool)


def _validate_budget_mapping(value: Mapping[str, Mapping[str, int]], field_name: str, *, selected: frozenset[str]) -> None:
    if not isinstance(value, Mapping):
        fail(FailureCode.INVALID_VALUE, f"{field_name} must be a mapping")
    for desk_id, budget in value.items():
        _require_selected_desk_key(desk_id, field_name, selected=selected)
        if not isinstance(budget, Mapping):
            fail(FailureCode.INVALID_VALUE, f"{field_name}.{desk_id} must be a mapping")
        for key, amount in budget.items():
            require_identifier(key, f"{field_name}.{desk_id}.key")
            if isinstance(amount, bool) or not isinstance(amount, int) or amount < 0:
                fail(FailureCode.INVALID_VALUE, f"{field_name}.{desk_id}.{key} must be a non-negative integer")


def _validate_context_payloads(
    value: Mapping[str, Mapping[str, Any]],
    *,
    selected: frozenset[str],
) -> None:
    if not isinstance(value, Mapping):
        fail(FailureCode.INVALID_VALUE, "desk_context_payloads must be a mapping")
    for desk_id, payload in value.items():
        _require_selected_desk_key(desk_id, "desk_context_payloads", selected=selected)
        if not isinstance(payload, Mapping):
            fail(FailureCode.INVALID_VALUE, f"desk_context_payloads.{desk_id} must be a mapping")
        try:
            assert_no_forbidden_content(payload, where=f"desk_context_payloads.{desk_id}")
        except Exception as exc:
            if hasattr(exc, "code"):
                raise
            raise


def _freeze_sorted_mapping(value: Mapping[str, str]) -> Mapping[str, str]:
    return MappingProxyType(dict(sorted(value.items())))


_VISIBLE_FORBIDDEN_TOKENS = (
    "sealed",
    "holdout",
    "authorization",
    "bearer ",
    "api_key",
    "apikey",
    "private_key",
    "target_weight",
    "target_weights",
    "risk_approval",
    "broker",
    "ledger",
    "promotion",
    "promote_challenger",
    "promotion_decision",
    "promotion_verdict",
)


def _reject_privileged_visible_text(value: str | None, *, where: str) -> None:
    if value is None:
        return
    lowered = value.lower()
    for token in _VISIBLE_FORBIDDEN_TOKENS:
        if token in lowered:
            fail(
                FailureCode.UNAUTHORIZED_CAPABILITY,
                "runtime-visible text contains forbidden downstream, secret, or sealed material",
                where=where,
                token=token,
                campaign_runtime_failure_code=CampaignRuntimeFailureCode.PRIVILEGED_OUTPUT_REJECTED.value,
            )


def _validate_manifest_visible_boundary(manifest: CampaignRuntimeManifest) -> None:
    validate_branch_authority(manifest.branch_results)
    for text in (
        manifest.run_id,
        manifest.runtime_version,
        manifest.runtime_mode,
        manifest.awaiting_checkpoint_id,
    ):
        _reject_privileged_visible_text(text, where="manifest")
    for key in manifest.artifact_hashes:
        _reject_privileged_visible_text(key, where="artifact_hashes.key")


__all__ = [
    "ALLOWED_CAMPAIGN_RUNTIME_MODES",
    "ALL_CAMPAIGN_DESKS",
    "CAMPAIGN_LLM_RUNTIME_MODE",
    "CAMPAIGN_LLM_RUNTIME_RULE_VERSION",
    "CAMPAIGN_LLM_RUNTIME_SCHEMA_VERSION",
    "CampaignDeskRunPlan",
    "CampaignRuntimeCheckpoint",
    "CampaignRuntimeFailureCode",
    "CampaignRuntimeManifest",
    "require_confirmed_campaign",
    "validate_branch_authority",
    "validate_plan_bindings",
]
