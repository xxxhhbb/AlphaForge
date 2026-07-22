"""Versioned contracts for the AlphaForge research A-share data bridge."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import ClassVar, Mapping

from ..contracts import CanonicalContract, FailureCode
from ..contracts.base import (
    fail,
    freeze_mapping,
    require_hash,
    require_identifier,
    require_nonempty_tuple,
    require_text,
)


SCHEMA_VERSION = "alphaforge.research-real-data/v2"
APPROVED_PROVIDER_CLASS = "APPROVED_PROXY_DATA_VENDOR"
APPROVED_SOURCE_CLASS = "TUSHARE_COMPATIBLE_PROXY"
FORBIDDEN_SOURCE_CLASS = "OFFICIAL_TUSHARE"

REQUIRED_FEATURE_FIELDS = (
    "open",
    "high",
    "low",
    "close",
    "volume",
    "amount",
    "adj_factor",
    "market_cap",
    "industry",
    "status",
)
PRICE_CORE_FEATURE_FIELDS = (
    "open",
    "high",
    "low",
    "close",
    "volume",
    "amount",
    "adj_factor",
    "market_cap",
    "status",
)
DEFAULT_OPTIONAL_FEATURE_FIELDS = ("industry",)
REQUIRED_LABEL_FIELDS = ("future_return_5d",)


class ResearchDataStatus(str, Enum):
    READY = "READY"
    FIXTURE_ONLY = "FIXTURE_ONLY"
    UNAVAILABLE = "UNAVAILABLE"
    BLOCKED = "BLOCKED"


class ResearchDataFailureCode(str, Enum):
    ADAPTER_UNAVAILABLE = "ADAPTER_UNAVAILABLE"
    NETWORK_NOT_ENABLED = "NETWORK_NOT_ENABLED"
    SOURCE_LABEL_REJECTED = "SOURCE_LABEL_REJECTED"
    BELOW_MINIMUM_SCALE = "BELOW_MINIMUM_SCALE"
    INSUFFICIENT_LOOKBACK_HISTORY = "INSUFFICIENT_LOOKBACK_HISTORY"
    ABOVE_BOUNDED_SCALE = "ABOVE_BOUNDED_SCALE"
    MISSING_REQUIRED_FIELD = "MISSING_REQUIRED_FIELD"
    COVERAGE_BELOW_POLICY = "COVERAGE_BELOW_POLICY"
    CALENDAR_INCOMPLETE = "CALENDAR_INCOMPLETE"
    UNIVERSE_INCOMPLETE = "UNIVERSE_INCOMPLETE"
    FUTURE_MEMBERSHIP_LEAKAGE = "FUTURE_MEMBERSHIP_LEAKAGE"
    TIME_SEMANTICS_VIOLATION = "TIME_SEMANTICS_VIOLATION"
    FEATURE_LABEL_LEAKAGE = "FEATURE_LABEL_LEAKAGE"
    HASH_MISMATCH = "HASH_MISMATCH"


@dataclass(frozen=True, kw_only=True)
class ResearchDataPolicy(CanonicalContract):
    SCHEMA_VERSION: ClassVar[str] = SCHEMA_VERSION

    policy_id: str = "alphaforge.research-data.ashare.v2"
    min_symbols_per_session: int = 30
    max_symbols_per_session: int = 50
    min_sessions: int = 180
    recommended_sessions_for_50d: int = 260
    max_sessions: int = 320
    min_field_coverage: float = 0.95
    required_feature_fields: tuple[str, ...] = PRICE_CORE_FEATURE_FIELDS
    required_label_fields: tuple[str, ...] = REQUIRED_LABEL_FIELDS
    provider_class: str = APPROVED_PROVIDER_CLASS
    source_class: str = APPROVED_SOURCE_CLASS
    offline_default: bool = True

    def __post_init__(self) -> None:
        require_identifier(self.policy_id, "policy_id")
        if self.provider_class != APPROVED_PROVIDER_CLASS or self.source_class != APPROVED_SOURCE_CLASS:
            fail(FailureCode.AUXILIARY_MASQUERADE, "approved proxy source labels are required")
        if FORBIDDEN_SOURCE_CLASS in {self.provider_class, self.source_class}:
            fail(FailureCode.AUXILIARY_MASQUERADE, "proxy data cannot claim official Tushare")
        if not (1 <= self.min_symbols_per_session <= self.max_symbols_per_session):
            fail(FailureCode.INVALID_VALUE, "symbol policy bounds are inconsistent")
        if not (1 <= self.min_sessions <= self.recommended_sessions_for_50d <= self.max_sessions):
            fail(FailureCode.INVALID_VALUE, "session thresholds are inconsistent")
        if not (0.0 < self.min_field_coverage <= 1.0):
            fail(FailureCode.INVALID_VALUE, "min_field_coverage must lie in (0,1]")
        require_nonempty_tuple(self.required_feature_fields, "required_feature_fields")
        require_nonempty_tuple(self.required_label_fields, "required_label_fields")
        if any("future_return" in name.lower() for name in self.required_feature_fields):
            fail(FailureCode.UNAUTHORIZED_CAPABILITY, "future returns are label-only")
        if set(self.required_feature_fields) & set(self.required_label_fields):
            fail(FailureCode.UNAUTHORIZED_CAPABILITY, "feature and label fields must be disjoint")
        if self.offline_default is not True:
            fail(FailureCode.UNAUTHORIZED_CAPABILITY, "research data must default offline")


@dataclass(frozen=True, kw_only=True)
class ResearchDataRequest(CanonicalContract):
    SCHEMA_VERSION: ClassVar[str] = SCHEMA_VERSION

    request_id: str
    dataset_id: str
    lookback_sessions: int = 50
    allow_network: bool = False
    required_feature_fields: tuple[str, ...] = PRICE_CORE_FEATURE_FIELDS
    optional_feature_fields: tuple[str, ...] = DEFAULT_OPTIONAL_FEATURE_FIELDS

    def __post_init__(self) -> None:
        require_identifier(self.request_id, "request_id")
        require_identifier(self.dataset_id, "dataset_id")
        if isinstance(self.lookback_sessions, bool) or self.lookback_sessions < 1:
            fail(FailureCode.INVALID_VALUE, "lookback_sessions must be positive")
        if type(self.allow_network) is not bool:
            fail(FailureCode.INVALID_VALUE, "allow_network must be boolean")
        require_nonempty_tuple(self.required_feature_fields, "required_feature_fields")
        for name in self.required_feature_fields + self.optional_feature_fields:
            require_identifier(name, "feature_field")
            if "future_return" in name.lower():
                fail(FailureCode.UNAUTHORIZED_CAPABILITY, "future return is label-only")
        if len(set(self.required_feature_fields)) != len(self.required_feature_fields):
            fail(FailureCode.INVALID_VALUE, "required_feature_fields contains duplicates")
        if len(set(self.optional_feature_fields)) != len(self.optional_feature_fields):
            fail(FailureCode.INVALID_VALUE, "optional_feature_fields contains duplicates")
        if set(self.required_feature_fields) & set(self.optional_feature_fields):
            fail(FailureCode.INVALID_VALUE, "required and optional feature fields must be disjoint")


@dataclass(frozen=True, kw_only=True)
class ResearchDataManifest(CanonicalContract):
    SCHEMA_VERSION: ClassVar[str] = SCHEMA_VERSION

    manifest_id: str
    dataset_id: str
    provider_class: str
    source_class: str
    input_origin: str
    calendar_id: str
    calendar_hash: str
    universe_hash: str
    content_hash: str
    source_adapter_manifest_hash: str
    availability_policy_hash: str
    symbol_count: int
    minimum_symbols_per_session: int
    maximum_symbols_per_session: int
    session_count: int
    row_count: int
    feature_fields: tuple[str, ...]
    label_fields: tuple[str, ...]
    coverage_by_field: Mapping[str, float]
    recommended_50d_history_satisfied: bool
    network_used_at_capture: bool
    requested_required_feature_fields: tuple[str, ...] = ()
    requested_optional_feature_fields: tuple[str, ...] = ()
    available_optional_feature_fields: tuple[str, ...] = ()
    missing_optional_feature_fields: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name in ("manifest_id", "dataset_id", "calendar_id", "provider_class", "source_class", "input_origin"):
            require_identifier(getattr(self, name), name)
        if self.provider_class != APPROVED_PROVIDER_CLASS or self.source_class != APPROVED_SOURCE_CLASS:
            fail(FailureCode.AUXILIARY_MASQUERADE, "manifest must retain approved proxy labels")
        if FORBIDDEN_SOURCE_CLASS in {self.provider_class, self.source_class}:
            fail(FailureCode.AUXILIARY_MASQUERADE, "manifest cannot claim official Tushare")
        for name in ("calendar_hash", "universe_hash", "content_hash", "source_adapter_manifest_hash", "availability_policy_hash"):
            require_hash(getattr(self, name), name)
        for name in ("symbol_count", "minimum_symbols_per_session", "maximum_symbols_per_session", "session_count", "row_count"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                fail(FailureCode.INVALID_VALUE, f"{name} must be a non-negative integer")
        require_nonempty_tuple(self.feature_fields, "feature_fields")
        require_nonempty_tuple(self.label_fields, "label_fields")
        if any("future_return" in name.lower() for name in self.feature_fields):
            fail(FailureCode.UNAUTHORIZED_CAPABILITY, "future return cannot be a feature")
        coverage = freeze_mapping(self.coverage_by_field, "coverage_by_field", numeric_values=True)
        if any(value < 0.0 or value > 1.0 for value in coverage.values()):
            fail(FailureCode.INVALID_VALUE, "coverage must lie in [0,1]")
        object.__setattr__(self, "coverage_by_field", coverage)
        if type(self.recommended_50d_history_satisfied) is not bool or type(self.network_used_at_capture) is not bool:
            fail(FailureCode.INVALID_VALUE, "manifest flags must be boolean")
        for field_name in (
            self.requested_required_feature_fields
            + self.requested_optional_feature_fields
            + self.available_optional_feature_fields
            + self.missing_optional_feature_fields
        ):
            require_identifier(field_name, "manifest_feature_field")
        if set(self.available_optional_feature_fields) & set(self.missing_optional_feature_fields):
            fail(FailureCode.INVALID_VALUE, "optional field cannot be both available and missing")
        if set(self.available_optional_feature_fields) | set(self.missing_optional_feature_fields) != set(self.requested_optional_feature_fields):
            fail(FailureCode.INCOMPLETE_RESULT, "manifest must account for every requested optional field")
        for limitation in self.limitations:
            require_text(limitation, "limitations")


@dataclass(frozen=True, kw_only=True)
class ResearchDataQualification(CanonicalContract):
    SCHEMA_VERSION: ClassVar[str] = SCHEMA_VERSION

    request_hash: str
    status: ResearchDataStatus
    failure_codes: tuple[ResearchDataFailureCode, ...]
    message: str
    manifest: ResearchDataManifest | None = None
    dataset_card_hash: str | None = None

    def __post_init__(self) -> None:
        require_hash(self.request_hash, "request_hash")
        if isinstance(self.status, str):
            object.__setattr__(self, "status", ResearchDataStatus(self.status))
        require_text(self.message, "message")
        if any(not isinstance(code, ResearchDataFailureCode) for code in self.failure_codes):
            fail(FailureCode.INVALID_VALUE, "failure_codes must be typed")
        if self.status in {ResearchDataStatus.READY, ResearchDataStatus.FIXTURE_ONLY}:
            if self.manifest is None or self.dataset_card_hash is None:
                fail(FailureCode.INCOMPLETE_RESULT, "ready qualification requires manifest and dataset card")
            require_hash(self.dataset_card_hash, "dataset_card_hash")
        elif self.manifest is not None or self.dataset_card_hash is not None:
            fail(FailureCode.INVALID_STATE, "blocked qualification cannot carry trusted dataset artifacts")


__all__ = [
    "APPROVED_PROVIDER_CLASS",
    "APPROVED_SOURCE_CLASS",
    "FORBIDDEN_SOURCE_CLASS",
    "DEFAULT_OPTIONAL_FEATURE_FIELDS",
    "PRICE_CORE_FEATURE_FIELDS",
    "REQUIRED_FEATURE_FIELDS",
    "REQUIRED_LABEL_FIELDS",
    "ResearchDataFailureCode",
    "ResearchDataManifest",
    "ResearchDataPolicy",
    "ResearchDataQualification",
    "ResearchDataRequest",
    "ResearchDataStatus",
]
