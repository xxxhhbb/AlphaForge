"""Stable contracts for Campaign data binding."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
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
from ..research_campaign import CampaignState, ResearchCampaign, ResearchCampaignDraft


CAMPAIGN_DATA_BINDING_SCHEMA_VERSION = "alphaforge.campaign-data-binding/v1"


class DataVendorClass(str, Enum):
    DETERMINISTIC_OFFLINE_FIXTURE = "DETERMINISTIC_OFFLINE_FIXTURE"
    APPROVED_PROXY_DATA_VENDOR = "APPROVED_PROXY_DATA_VENDOR"


class CampaignDataBindingStatus(str, Enum):
    BOUND = "BOUND"
    BLOCKED = "BLOCKED"
    UNAVAILABLE = "UNAVAILABLE"


class CampaignDataBindingFailure(str, Enum):
    CAMPAIGN_NOT_CONFIRMED = "CAMPAIGN_NOT_CONFIRMED"
    DATA_PROVIDER_UNAVAILABLE = "DATA_PROVIDER_UNAVAILABLE"
    OFFICIAL_TUSHARE_MISLABEL = "OFFICIAL_TUSHARE_MISLABEL"
    DATA_SCOPE_MISMATCH = "DATA_SCOPE_MISMATCH"


@dataclass(frozen=True, kw_only=True)
class CampaignDataBindingRequest(CanonicalContract):
    SCHEMA_VERSION: ClassVar[str] = CAMPAIGN_DATA_BINDING_SCHEMA_VERSION

    campaign: ResearchCampaign
    vendor_class: DataVendorClass = DataVendorClass.DETERMINISTIC_OFFLINE_FIXTURE
    provider_source_label: str = "DETERMINISTIC_OFFLINE_FIXTURE"
    calendar_id: str = "cn_ashare_sse_szse"
    provider_available: bool = True
    content_refs: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if isinstance(self.campaign, ResearchCampaignDraft):
            fail(
                FailureCode.INVALID_STATE,
                "data binding requires a confirmed Campaign",
                data_binding_failure=CampaignDataBindingFailure.CAMPAIGN_NOT_CONFIRMED.value,
            )
        if not isinstance(self.campaign, ResearchCampaign):
            fail(FailureCode.INVALID_VALUE, "campaign must be ResearchCampaign")
        if self.campaign.state is not CampaignState.CONFIRMED:
            fail(
                FailureCode.INVALID_STATE,
                "data binding requires a confirmed Campaign",
                data_binding_failure=CampaignDataBindingFailure.CAMPAIGN_NOT_CONFIRMED.value,
            )
        if not isinstance(self.vendor_class, DataVendorClass):
            object.__setattr__(self, "vendor_class", DataVendorClass(self.vendor_class))
        require_identifier(self.provider_source_label, "provider_source_label")
        if self.vendor_class is DataVendorClass.APPROVED_PROXY_DATA_VENDOR and self.provider_source_label != "APPROVED_PROXY_DATA_VENDOR":
            fail(FailureCode.INVALID_VALUE, "approved proxy must use APPROVED_PROXY_DATA_VENDOR source label")
        if "OFFICIAL_TUSHARE" in self.provider_source_label:
            fail(
                FailureCode.UNAUTHORIZED_CAPABILITY,
                "compatible proxy cannot be labeled official Tushare",
                data_binding_failure=CampaignDataBindingFailure.OFFICIAL_TUSHARE_MISLABEL.value,
            )
        require_identifier(self.calendar_id, "calendar_id")
        if not isinstance(self.provider_available, bool):
            fail(FailureCode.INVALID_VALUE, "provider_available must be boolean")
        refs = freeze_mapping(self.content_refs, "content_refs")
        for key, value in refs.items():
            require_identifier(key, "content_refs.key")
            require_hash(value, f"content_refs.{key}")
        object.__setattr__(self, "content_refs", MappingProxyType(dict(sorted(refs.items()))))


@dataclass(frozen=True, kw_only=True)
class CampaignDatasetBundle(CanonicalContract):
    SCHEMA_VERSION: ClassVar[str] = CAMPAIGN_DATA_BINDING_SCHEMA_VERSION

    bundle_id: str
    campaign_hash: str
    vendor_class: DataVendorClass
    provider_source_label: str
    official_tushare_source: bool
    universe: tuple[str, ...]
    horizon: str
    decision_time: datetime
    allowed_data: tuple[str, ...]
    calendar_id: str
    manifest_hash: str
    research_visible_summary_hash: str
    content_refs: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        require_identifier(self.bundle_id, "bundle_id")
        require_hash(self.campaign_hash, "campaign_hash")
        if not isinstance(self.vendor_class, DataVendorClass):
            object.__setattr__(self, "vendor_class", DataVendorClass(self.vendor_class))
        require_identifier(self.provider_source_label, "provider_source_label")
        if self.official_tushare_source:
            fail(
                FailureCode.UNAUTHORIZED_CAPABILITY,
                "approved proxy data must not be represented as OFFICIAL_TUSHARE",
                data_binding_failure=CampaignDataBindingFailure.OFFICIAL_TUSHARE_MISLABEL.value,
            )
        _require_text_tuple(self.universe, "universe")
        require_text(self.horizon, "horizon")
        require_aware_datetime(self.decision_time, "decision_time")
        _require_text_tuple(self.allowed_data, "allowed_data")
        require_identifier(self.calendar_id, "calendar_id")
        require_hash(self.manifest_hash, "manifest_hash")
        require_hash(self.research_visible_summary_hash, "research_visible_summary_hash")
        refs = freeze_mapping(self.content_refs, "content_refs")
        for key, value in refs.items():
            require_identifier(key, "content_refs.key")
            require_hash(value, f"content_refs.{key}")
        object.__setattr__(self, "content_refs", MappingProxyType(dict(sorted(refs.items()))))


@dataclass(frozen=True, kw_only=True)
class CampaignDataBindingResult(CanonicalContract):
    SCHEMA_VERSION: ClassVar[str] = CAMPAIGN_DATA_BINDING_SCHEMA_VERSION

    request_hash: str
    status: CampaignDataBindingStatus
    bundle: CampaignDatasetBundle | None = None
    failure: CampaignDataBindingFailure | None = None
    failure_code: FailureCode | None = None
    message: str = ""

    def __post_init__(self) -> None:
        require_hash(self.request_hash, "request_hash")
        if not isinstance(self.status, CampaignDataBindingStatus):
            object.__setattr__(self, "status", CampaignDataBindingStatus(self.status))
        if self.bundle is not None and not isinstance(self.bundle, CampaignDatasetBundle):
            fail(FailureCode.INVALID_VALUE, "bundle must be CampaignDatasetBundle")
        if self.failure is not None and not isinstance(self.failure, CampaignDataBindingFailure):
            object.__setattr__(self, "failure", CampaignDataBindingFailure(self.failure))
        if self.failure_code is not None and not isinstance(self.failure_code, FailureCode):
            object.__setattr__(self, "failure_code", FailureCode(self.failure_code))
        if self.status is CampaignDataBindingStatus.BOUND:
            if self.bundle is None or self.failure is not None or self.failure_code is not None:
                fail(FailureCode.INVALID_STATE, "BOUND result requires bundle and no failure")
        elif self.failure is None or self.failure_code is None:
            fail(FailureCode.INVALID_STATE, "non-bound data binding requires typed failure")
        if self.message:
            require_text(self.message, "message")
            _reject_forbidden(self.message)


def _require_text_tuple(value: tuple[str, ...], field_name: str) -> None:
    if not isinstance(value, tuple) or not value:
        fail(FailureCode.INVALID_VALUE, f"{field_name} must be a non-empty tuple")
    for item in value:
        require_text(item, field_name)
    if len(set(value)) != len(value):
        fail(FailureCode.INVALID_VALUE, f"{field_name} contains duplicates")


def _reject_forbidden(value: str) -> None:
    lowered = value.lower()
    for token in ("sealed", "promotion", "risk_approval", "target_weights", "orders", "broker", "ledger", "api_key"):
        if token in lowered:
            fail(FailureCode.UNAUTHORIZED_CAPABILITY, "data binding message contains forbidden content")
