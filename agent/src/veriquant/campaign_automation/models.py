"""Stable contracts for Campaign automation.

Automation state is operational state only. Trial attempts, official metrics,
promotion, portfolio, risk, execution, and accounting truth remain in their
existing deterministic services.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from types import MappingProxyType
from typing import Any, ClassVar, Mapping

from ..contracts import CanonicalContract, FailureCode
from ..contracts.base import (
    fail,
    freeze_mapping,
    require_aware_datetime,
    require_hash,
    require_identifier,
    require_text,
)
from ..research_campaign import CampaignRuntimeStatus, ResearchMode


CAMPAIGN_AUTOMATION_RULE_VERSION = "alphaforge.campaign-automation/v1"
DEFAULT_AUTOMATION_DECISION_TIME = datetime(2026, 7, 19, 9, 30, tzinfo=timezone.utc)


class AutomationRuntimeMode(str, Enum):
    DETERMINISTIC_OFFLINE = "DETERMINISTIC_OFFLINE"
    REAL_LLM = "REAL_LLM"


class AutomationStatus(str, Enum):
    COMPLETED = "COMPLETED"
    PARTIAL = "PARTIAL"
    AWAITING_CHECKPOINT = "AWAITING_CHECKPOINT"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"


class DataVendorClass(str, Enum):
    DETERMINISTIC_OFFLINE_FIXTURE = "DETERMINISTIC_OFFLINE_FIXTURE"
    APPROVED_PROXY_DATA_VENDOR = "APPROVED_PROXY_DATA_VENDOR"


@dataclass(frozen=True, kw_only=True)
class AutomationRequest(CanonicalContract):
    SCHEMA_VERSION: ClassVar[str] = CAMPAIGN_AUTOMATION_RULE_VERSION

    question: str
    run_id: str
    runtime_mode: AutomationRuntimeMode = AutomationRuntimeMode.DETERMINISTIC_OFFLINE
    universe: tuple[str, ...] = ("CN_ASHARE_APPROVED_PIT_SAMPLE",)
    horizon: str = "20d"
    allowed_data: tuple[str, ...] = ("price_volume", "fundamentals", "registered_factor_summary")
    decision_time: datetime = DEFAULT_AUTOMATION_DECISION_TIME
    seed: int = 7
    max_factor_families: int = 3
    max_trials: int = 12
    max_llm_calls: int = 12
    output_root: str = "../.autobuild/veriquant/campaign_automation"
    provider_available: bool = False
    real_llm_from_env: bool = False
    data_vendor_class: DataVendorClass = DataVendorClass.DETERMINISTIC_OFFLINE_FIXTURE

    def __post_init__(self) -> None:
        require_text(self.question, "question")
        require_identifier(self.run_id, "run_id")
        if not isinstance(self.runtime_mode, AutomationRuntimeMode):
            object.__setattr__(self, "runtime_mode", AutomationRuntimeMode(self.runtime_mode))
        if not isinstance(self.data_vendor_class, DataVendorClass):
            object.__setattr__(self, "data_vendor_class", DataVendorClass(self.data_vendor_class))
        _require_text_tuple(self.universe, "universe")
        require_text(self.horizon, "horizon")
        _require_text_tuple(self.allowed_data, "allowed_data")
        require_aware_datetime(self.decision_time, "decision_time")
        for name in ("seed", "max_factor_families", "max_trials", "max_llm_calls"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                fail(FailureCode.INVALID_VALUE, f"{name} must be a positive integer")
        if self.max_trials < self.max_factor_families:
            fail(FailureCode.INVALID_VALUE, "max_trials must cover max_factor_families")
        require_text(self.output_root, "output_root")
        if ".." in self.run_id or "/" in self.run_id or "\\" in self.run_id:
            fail(FailureCode.INVALID_IDENTIFIER, "run_id must not contain path traversal")
        if not isinstance(self.provider_available, bool):
            fail(FailureCode.INVALID_VALUE, "provider_available must be boolean")
        if not isinstance(self.real_llm_from_env, bool):
            fail(FailureCode.INVALID_VALUE, "real_llm_from_env must be boolean")


@dataclass(frozen=True, kw_only=True)
class AutomationDataManifest(CanonicalContract):
    SCHEMA_VERSION: ClassVar[str] = CAMPAIGN_AUTOMATION_RULE_VERSION

    manifest_id: str
    vendor_class: DataVendorClass
    source_label: str
    official_tushare_source: bool
    universe: tuple[str, ...]
    horizon: str
    decision_time: datetime
    allowed_data: tuple[str, ...]
    calendar_id: str
    content_refs: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        require_identifier(self.manifest_id, "manifest_id")
        if not isinstance(self.vendor_class, DataVendorClass):
            object.__setattr__(self, "vendor_class", DataVendorClass(self.vendor_class))
        require_identifier(self.source_label, "source_label")
        if self.official_tushare_source:
            fail(FailureCode.UNAUTHORIZED_CAPABILITY, "approved proxy must not be labeled OFFICIAL_TUSHARE")
        _require_text_tuple(self.universe, "universe")
        require_text(self.horizon, "horizon")
        require_aware_datetime(self.decision_time, "decision_time")
        _require_text_tuple(self.allowed_data, "allowed_data")
        require_identifier(self.calendar_id, "calendar_id")
        refs = freeze_mapping(self.content_refs, "content_refs")
        for key, value in refs.items():
            require_identifier(key, "content_refs.key")
            require_hash(value, f"content_refs.{key}")
        object.__setattr__(self, "content_refs", MappingProxyType(dict(sorted(refs.items()))))


@dataclass(frozen=True, kw_only=True)
class AutomationReport(CanonicalContract):
    SCHEMA_VERSION: ClassVar[str] = CAMPAIGN_AUTOMATION_RULE_VERSION

    report_id: str
    run_id: str
    campaign_hash: str
    route_hash: str
    protocol_plan_hash: str
    runtime_manifest_hash: str
    data_manifest_hash: str
    mode: ResearchMode
    runtime_mode: AutomationRuntimeMode
    status: AutomationStatus
    selected_desks: tuple[str, ...]
    completed_desks: tuple[str, ...]
    blocked_desks: tuple[str, ...]
    not_selected_desks: tuple[str, ...]
    stage_count: int
    official_evidence_claimed: bool
    promotion_claimed: bool
    generated_at: datetime
    summary: str

    def __post_init__(self) -> None:
        require_identifier(self.report_id, "report_id")
        require_identifier(self.run_id, "run_id")
        for name in (
            "campaign_hash",
            "route_hash",
            "protocol_plan_hash",
            "runtime_manifest_hash",
            "data_manifest_hash",
        ):
            require_hash(getattr(self, name), name)
        if not isinstance(self.mode, ResearchMode):
            object.__setattr__(self, "mode", ResearchMode(self.mode))
        if not isinstance(self.runtime_mode, AutomationRuntimeMode):
            object.__setattr__(self, "runtime_mode", AutomationRuntimeMode(self.runtime_mode))
        if not isinstance(self.status, AutomationStatus):
            object.__setattr__(self, "status", AutomationStatus(self.status))
        _require_identifier_tuple(self.selected_desks, "selected_desks", nonempty=False)
        _require_identifier_tuple(self.completed_desks, "completed_desks", nonempty=False)
        _require_identifier_tuple(self.blocked_desks, "blocked_desks", nonempty=False)
        _require_identifier_tuple(self.not_selected_desks, "not_selected_desks", nonempty=False)
        if isinstance(self.stage_count, bool) or not isinstance(self.stage_count, int) or self.stage_count < 0:
            fail(FailureCode.INVALID_VALUE, "stage_count must be a non-negative integer")
        if self.official_evidence_claimed or self.promotion_claimed:
            fail(FailureCode.UNAUTHORIZED_CAPABILITY, "automation reports cannot claim official evidence or promotion")
        require_aware_datetime(self.generated_at, "generated_at")
        require_text(self.summary, "summary")
        _reject_forbidden_text(self.summary, "summary")


@dataclass(frozen=True, kw_only=True)
class AutomationResult(CanonicalContract):
    SCHEMA_VERSION: ClassVar[str] = CAMPAIGN_AUTOMATION_RULE_VERSION

    request_hash: str
    status: AutomationStatus
    run_id: str
    campaign_hash: str | None
    route_hash: str | None
    protocol_plan_hash: str | None
    runtime_manifest_hash: str | None
    data_manifest_hash: str | None
    report_hash: str | None
    output_dir: str
    json_report_path: str | None = None
    markdown_report_path: str | None = None
    failure_code: FailureCode | None = None
    message: str = ""

    def __post_init__(self) -> None:
        require_hash(self.request_hash, "request_hash")
        if not isinstance(self.status, AutomationStatus):
            object.__setattr__(self, "status", AutomationStatus(self.status))
        require_identifier(self.run_id, "run_id")
        for name in (
            "campaign_hash",
            "route_hash",
            "protocol_plan_hash",
            "runtime_manifest_hash",
            "data_manifest_hash",
            "report_hash",
        ):
            value = getattr(self, name)
            if value is not None:
                require_hash(value, name)
        require_text(self.output_dir, "output_dir")
        for name in ("json_report_path", "markdown_report_path"):
            value = getattr(self, name)
            if value is not None:
                require_text(value, name)
        if self.failure_code is not None and not isinstance(self.failure_code, FailureCode):
            object.__setattr__(self, "failure_code", FailureCode(self.failure_code))
        if self.message:
            require_text(self.message, "message")
            _reject_forbidden_text(self.message, "message")


def automation_status_from_runtime(status: CampaignRuntimeStatus) -> AutomationStatus:
    if status is CampaignRuntimeStatus.COMPLETED:
        return AutomationStatus.COMPLETED
    if status is CampaignRuntimeStatus.PARTIAL:
        return AutomationStatus.PARTIAL
    if status is CampaignRuntimeStatus.AWAITING_CHECKPOINT:
        return AutomationStatus.AWAITING_CHECKPOINT
    return AutomationStatus.BLOCKED


def _require_text_tuple(value: tuple[str, ...], field_name: str, *, nonempty: bool = True) -> None:
    if not isinstance(value, tuple) or (nonempty and not value):
        fail(FailureCode.INVALID_VALUE, f"{field_name} must be a tuple")
    for item in value:
        require_text(item, field_name)
    if len(set(value)) != len(value):
        fail(FailureCode.INVALID_VALUE, f"{field_name} contains duplicates")


def _require_identifier_tuple(value: tuple[str, ...], field_name: str, *, nonempty: bool = True) -> None:
    if not isinstance(value, tuple) or (nonempty and not value):
        fail(FailureCode.INVALID_VALUE, f"{field_name} must be a tuple")
    for item in value:
        require_identifier(item, field_name)
    if len(set(value)) != len(value):
        fail(FailureCode.INVALID_VALUE, f"{field_name} contains duplicates")


def _reject_forbidden_text(value: str, field_name: str) -> None:
    lowered = value.lower()
    for token in (
        "sealed",
        "holdout",
        "api_key",
        "authorization",
        "bearer ",
        "secret",
        "broker",
        "target_weights",
        "risk_approval",
        "promotion_verdict",
        "promote_challenger",
        "official_metrics",
    ):
        if token in lowered:
            fail(FailureCode.UNAUTHORIZED_CAPABILITY, f"{field_name} contains forbidden text")
