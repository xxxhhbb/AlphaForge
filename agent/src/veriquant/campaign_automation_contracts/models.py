"""Typed contracts for Campaign automation integration.

This package is intentionally thin.  It binds product-layer Campaign objects to
data binding, stage execution, durable run storage, and report publication
without becoming a second Trial Ledger or a source of financial truth.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
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
    require_nonempty_tuple,
    require_text,
)
from ..research_campaign import CampaignState, ResearchDepth


CAMPAIGN_AUTOMATION_SCHEMA_VERSION = "alphaforge.campaign-automation/v1"


class StrEnum(str, Enum):
    """Python 3.10-compatible string enum with stable JSON values."""


class AutomationProviderMode(StrEnum):
    DETERMINISTIC = "DETERMINISTIC"
    REAL_LLM = "REAL_LLM"


class AutomationRunStatus(StrEnum):
    NOT_STARTED = "NOT_STARTED"
    BOUND = "BOUND"
    RUNNING = "RUNNING"
    AWAITING_CHECKPOINT = "AWAITING_CHECKPOINT"
    COMPLETED = "COMPLETED"
    RUN_COMPLETED_NO_ALPHA = "RUN_COMPLETED_NO_ALPHA"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class AutomationStageStatus(StrEnum):
    DRAFT = "DRAFT"
    EXPLANATION = "EXPLANATION"
    CONTROL_REFERENCE = "CONTROL_REFERENCE"
    BLOCKED = "BLOCKED"
    SKIPPED_UPSTREAM = "SKIPPED_UPSTREAM"
    CANCELLED = "CANCELLED"


class AutomationFailureCode(StrEnum):
    CAMPAIGN_NOT_CONFIRMED = "CAMPAIGN_NOT_CONFIRMED"
    DATA_UNAVAILABLE = "DATA_UNAVAILABLE"
    PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
    STAGE_FAILED_CLOSED = "STAGE_FAILED_CLOSED"
    CHECKPOINT_CORRUPT = "CHECKPOINT_CORRUPT"
    DUPLICATE_STAGE_COMMIT = "DUPLICATE_STAGE_COMMIT"
    PATH_TRAVERSAL = "PATH_TRAVERSAL"
    AUTHORITY_VIOLATION = "AUTHORITY_VIOLATION"
    QUICK_DEPTH_AUTHORITY_VIOLATION = "QUICK_DEPTH_AUTHORITY_VIOLATION"


class DataVendorKind(StrEnum):
    SYNTHETIC_FIXTURE = "SYNTHETIC_FIXTURE"
    APPROVED_PROXY_DATA_VENDOR = "APPROVED_PROXY_DATA_VENDOR"
    INTERNAL_FROZEN_FIXTURE = "INTERNAL_FROZEN_FIXTURE"


_TERMINAL_STATUSES = frozenset(
    {
        AutomationRunStatus.COMPLETED,
        AutomationRunStatus.RUN_COMPLETED_NO_ALPHA,
        AutomationRunStatus.BLOCKED,
        AutomationRunStatus.FAILED,
        AutomationRunStatus.CANCELLED,
    }
)

_LLM_COMPONENTS = frozenset({Component.LLM_RESEARCHER, Component.RESEARCH_DESK})
_LLM_ALLOWED_STAGE_STATUSES = frozenset({AutomationStageStatus.DRAFT, AutomationStageStatus.EXPLANATION})


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


def require_safe_artifact_locator(locator: str, field_name: str = "artifact_locator") -> None:
    require_text(locator, field_name)
    normalized = locator.replace("\\", "/")
    drive_absolute = len(normalized) >= 3 and normalized[1:3] == ":/"
    if (
        normalized.startswith("/")
        or drive_absolute
        or "://" in normalized
        or any(part in {"", ".", ".."} for part in normalized.split("/"))
    ):
        fail(
            FailureCode.INVALID_VALUE,
            f"{field_name} must be a relative, traversal-free artifact locator",
            automation_failure_code=AutomationFailureCode.PATH_TRAVERSAL.value,
        )


@dataclass(frozen=True, kw_only=True)
class ArtifactRef:
    SCHEMA_VERSION: ClassVar[str] = CAMPAIGN_AUTOMATION_SCHEMA_VERSION

    ref_id: str
    artifact_type: str
    content_hash: str
    locator: str
    produced_by: Component

    def __post_init__(self) -> None:
        require_identifier(self.ref_id, "ref_id")
        require_identifier(self.artifact_type, "artifact_type")
        require_hash(self.content_hash, "content_hash")
        require_safe_artifact_locator(self.locator, "locator")
        if not isinstance(self.produced_by, Component):
            object.__setattr__(self, "produced_by", Component(self.produced_by))


@dataclass(frozen=True, kw_only=True)
class CampaignAutomationRequest(CanonicalContract):
    SCHEMA_VERSION: ClassVar[str] = CAMPAIGN_AUTOMATION_SCHEMA_VERSION
    ALLOWED_PRODUCERS: ClassVar[frozenset[Component]] = frozenset({Component.OPERATOR})

    run_id: str
    campaign_hash: str
    route_hash: str
    protocol_plan_hash: str
    campaign_state: CampaignState
    research_depth: ResearchDepth
    provider_mode: AutomationProviderMode
    requested_at: datetime
    requested_by: Component = Component.OPERATOR
    allow_real_provider: bool = False
    operator_confirmed: bool = True
    request_tags: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        require_identifier(self.run_id, "run_id")
        for name in ("campaign_hash", "route_hash", "protocol_plan_hash"):
            require_hash(getattr(self, name), name)
        if not isinstance(self.campaign_state, CampaignState):
            object.__setattr__(self, "campaign_state", CampaignState(self.campaign_state))
        if not isinstance(self.research_depth, ResearchDepth):
            object.__setattr__(self, "research_depth", ResearchDepth(self.research_depth))
        if not isinstance(self.provider_mode, AutomationProviderMode):
            object.__setattr__(self, "provider_mode", AutomationProviderMode(self.provider_mode))
        require_aware_datetime(self.requested_at, "requested_at")
        if self.requested_by is not Component.OPERATOR:
            fail(FailureCode.INVALID_ARTIFACT_PRODUCER, "automation request must be operator-confirmed")
        if self.campaign_state is not CampaignState.CONFIRMED or not self.operator_confirmed:
            fail(
                FailureCode.INVALID_STATE,
                "automation can start only from an operator-confirmed Campaign",
                automation_failure_code=AutomationFailureCode.CAMPAIGN_NOT_CONFIRMED.value,
            )
        if self.provider_mode is AutomationProviderMode.REAL_LLM and not self.allow_real_provider:
            fail(
                FailureCode.UNAUTHORIZED_CAPABILITY,
                "REAL_LLM automation requires explicit server-side provider authorization",
                automation_failure_code=AutomationFailureCode.PROVIDER_UNAVAILABLE.value,
            )
        _validate_string_tuple(self.request_tags, "request_tags", nonempty=False)


@dataclass(frozen=True, kw_only=True)
class CampaignDataBindingRef(CanonicalContract):
    SCHEMA_VERSION: ClassVar[str] = CAMPAIGN_AUTOMATION_SCHEMA_VERSION

    binding_id: str
    campaign_hash: str
    dataset_id: str
    universe_id: str
    horizon: str
    decision_time: datetime
    vendor_kind: DataVendorKind
    dataset_manifest_hash: str
    snapshot_hash: str
    calendar_hash: str
    availability_policy_hash: str
    source_label: str

    def __post_init__(self) -> None:
        for name in ("binding_id", "dataset_id", "universe_id"):
            require_identifier(getattr(self, name), name)
        require_text(self.horizon, "horizon")
        require_aware_datetime(self.decision_time, "decision_time")
        if not isinstance(self.vendor_kind, DataVendorKind):
            object.__setattr__(self, "vendor_kind", DataVendorKind(self.vendor_kind))
        for name in ("campaign_hash", "dataset_manifest_hash", "snapshot_hash", "calendar_hash", "availability_policy_hash"):
            require_hash(getattr(self, name), name)
        require_text(self.source_label, "source_label")
        if "OFFICIAL_TUSHARE" in self.source_label.upper():
            fail(FailureCode.INVALID_VALUE, "approved proxy data must not be labeled OFFICIAL_TUSHARE")


@dataclass(frozen=True, kw_only=True)
class CampaignStageCommit(CanonicalContract):
    SCHEMA_VERSION: ClassVar[str] = CAMPAIGN_AUTOMATION_SCHEMA_VERSION

    run_id: str
    route_task_id: str
    desk_id: str
    stage_id: str
    stage_index: int
    status: AutomationStageStatus
    produced_by: Component
    input_hash: str
    output_hash: str | None
    prompt_hash: str | None = None
    trace_hash: str | None = None
    failure_code: AutomationFailureCode | None = None
    committed_trial_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name in ("run_id", "route_task_id", "desk_id", "stage_id"):
            require_identifier(getattr(self, name), name)
        if isinstance(self.stage_index, bool) or not isinstance(self.stage_index, int) or self.stage_index < 0:
            fail(FailureCode.INVALID_VALUE, "stage_index must be a non-negative integer")
        if not isinstance(self.status, AutomationStageStatus):
            object.__setattr__(self, "status", AutomationStageStatus(self.status))
        if not isinstance(self.produced_by, Component):
            object.__setattr__(self, "produced_by", Component(self.produced_by))
        require_hash(self.input_hash, "input_hash")
        for name in ("output_hash", "prompt_hash", "trace_hash"):
            value = getattr(self, name)
            if value is not None:
                require_hash(value, name)
        if self.failure_code is not None and not isinstance(self.failure_code, AutomationFailureCode):
            object.__setattr__(self, "failure_code", AutomationFailureCode(self.failure_code))
        _validate_string_tuple(self.committed_trial_ids, "committed_trial_ids", nonempty=False)
        if self.produced_by in _LLM_COMPONENTS and self.status not in _LLM_ALLOWED_STAGE_STATUSES:
            fail(
                FailureCode.UNAUTHORIZED_CAPABILITY,
                "LLM/Research Desk stages may commit only DRAFT or EXPLANATION",
                automation_failure_code=AutomationFailureCode.AUTHORITY_VIOLATION.value,
            )
        if self.status in {AutomationStageStatus.DRAFT, AutomationStageStatus.EXPLANATION, AutomationStageStatus.CONTROL_REFERENCE}:
            if self.output_hash is None:
                fail(FailureCode.INVALID_HASH, "successful stage commit requires output_hash")
        if self.status in {AutomationStageStatus.BLOCKED, AutomationStageStatus.SKIPPED_UPSTREAM, AutomationStageStatus.CANCELLED}:
            if self.failure_code is None:
                fail(FailureCode.INVALID_VALUE, "non-success stage commit requires failure_code")

    @property
    def stage_key(self) -> str:
        return f"{self.route_task_id}:{self.stage_id}"


@dataclass(frozen=True, kw_only=True)
class CampaignAutomationSnapshot(CanonicalContract):
    SCHEMA_VERSION: ClassVar[str] = CAMPAIGN_AUTOMATION_SCHEMA_VERSION

    run_id: str
    automation_request_hash: str
    data_binding_hash: str | None
    status: AutomationRunStatus
    committed_stage_hashes: tuple[str, ...] = ()
    runtime_manifest_hash: str | None = None
    current_checkpoint_id: str | None = None
    failure_codes: tuple[AutomationFailureCode, ...] = ()
    artifact_refs: tuple[ArtifactRef, ...] = ()

    def __post_init__(self) -> None:
        require_identifier(self.run_id, "run_id")
        require_hash(self.automation_request_hash, "automation_request_hash")
        if self.data_binding_hash is not None:
            require_hash(self.data_binding_hash, "data_binding_hash")
        if not isinstance(self.status, AutomationRunStatus):
            object.__setattr__(self, "status", AutomationRunStatus(self.status))
        _validate_hash_tuple(self.committed_stage_hashes, "committed_stage_hashes", nonempty=False)
        if self.runtime_manifest_hash is not None:
            require_hash(self.runtime_manifest_hash, "runtime_manifest_hash")
        if self.current_checkpoint_id is not None:
            require_identifier(self.current_checkpoint_id, "current_checkpoint_id")
        if not isinstance(self.failure_codes, tuple):
            fail(FailureCode.INVALID_VALUE, "failure_codes must be a tuple")
        for code in self.failure_codes:
            if not isinstance(code, AutomationFailureCode):
                fail(FailureCode.INVALID_VALUE, "failure_codes must contain AutomationFailureCode")
        if not isinstance(self.artifact_refs, tuple) or not all(isinstance(item, ArtifactRef) for item in self.artifact_refs):
            fail(FailureCode.INVALID_VALUE, "artifact_refs must contain ArtifactRef")


@dataclass(frozen=True, kw_only=True)
class CampaignPublicationManifest(CanonicalContract):
    SCHEMA_VERSION: ClassVar[str] = CAMPAIGN_AUTOMATION_SCHEMA_VERSION

    run_id: str
    automation_snapshot_hash: str
    research_depth: ResearchDepth
    status: AutomationRunStatus
    machine_report_hash: str
    markdown_report_hash: str
    published_refs: tuple[ArtifactRef, ...]
    redaction_policy_hash: str
    official_evidence_hashes: tuple[str, ...] = ()
    promotion_decision_hash: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        require_identifier(self.run_id, "run_id")
        for name in ("automation_snapshot_hash", "machine_report_hash", "markdown_report_hash", "redaction_policy_hash"):
            require_hash(getattr(self, name), name)
        if not isinstance(self.research_depth, ResearchDepth):
            object.__setattr__(self, "research_depth", ResearchDepth(self.research_depth))
        if not isinstance(self.status, AutomationRunStatus):
            object.__setattr__(self, "status", AutomationRunStatus(self.status))
        if self.status not in _TERMINAL_STATUSES:
            fail(FailureCode.INVALID_STATE, "publication requires a terminal automation status")
        if not isinstance(self.published_refs, tuple) or not all(isinstance(item, ArtifactRef) for item in self.published_refs):
            fail(FailureCode.INVALID_VALUE, "published_refs must contain ArtifactRef")
        _validate_hash_tuple(self.official_evidence_hashes, "official_evidence_hashes", nonempty=False)
        if self.promotion_decision_hash is not None:
            require_hash(self.promotion_decision_hash, "promotion_decision_hash")
        if self.research_depth is ResearchDepth.QUICK_EXPLORATION:
            if self.official_evidence_hashes or self.promotion_decision_hash is not None:
                fail(
                    FailureCode.UNAUTHORIZED_CAPABILITY,
                    "QUICK_EXPLORATION publication cannot include official evidence or promotion",
                    automation_failure_code=AutomationFailureCode.QUICK_DEPTH_AUTHORITY_VIOLATION.value,
                )
        object.__setattr__(self, "metadata", freeze_mapping(self.metadata, "metadata"))


def freeze_automation_payload(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    """Return an immutable shallow payload copy for integration boundaries."""

    return freeze_mapping(payload, "automation_payload")
