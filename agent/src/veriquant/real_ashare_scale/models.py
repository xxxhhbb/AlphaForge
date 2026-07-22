"""Versioned contracts for bounded, frozen real A-share scale inputs.

These contracts describe data identity and qualification only.  They do not
implement PIT selection, factor evaluation, promotion, portfolio construction,
execution, or accounting.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum
from types import MappingProxyType
from typing import Any, ClassVar, Mapping

from ..contracts import CanonicalContract, FailureCode
from ..contracts.base import (
    canonicalize,
    fail,
    require_aware_datetime,
    require_hash,
    require_identifier,
    require_nonempty_tuple,
    require_text,
)


REAL_ASHARE_SCALE_SCHEMA_VERSION = "alphaforge.real-ashare-scale/v2"
APPROVED_PROVIDER_CLASS = "APPROVED_PROXY_DATA_VENDOR"
APPROVED_SOURCE_CLASS = "TUSHARE_COMPATIBLE_PROXY"
FORBIDDEN_OFFICIAL_SOURCE = "OFFICIAL_TUSHARE"


class StableStringEnum(str, Enum):
    """String enum compatible with the repository's Python baseline."""


class ScaleQualificationStatus(StableStringEnum):
    QUALIFIED = "QUALIFIED"
    IMPLEMENTED_NOT_RUN = "IMPLEMENTED_NOT_RUN"
    UNAVAILABLE = "UNAVAILABLE"
    FAILED = "FAILED"


class ScaleQualificationFailureCode(StableStringEnum):
    ADAPTER_UNAVAILABLE = "ADAPTER_UNAVAILABLE"
    SOURCE_LABEL_REJECTED = "SOURCE_LABEL_REJECTED"
    SYNTHETIC_NOT_REAL = "SYNTHETIC_NOT_REAL"
    MISSING_REQUIRED_FIELD = "MISSING_REQUIRED_FIELD"
    HASH_MISMATCH = "HASH_MISMATCH"
    BELOW_MINIMUM_SCALE = "BELOW_MINIMUM_SCALE"
    ABOVE_BOUNDED_SCALE = "ABOVE_BOUNDED_SCALE"
    COVERAGE_BELOW_POLICY = "COVERAGE_BELOW_POLICY"
    FUTURE_MEMBERSHIP_LEAKAGE = "FUTURE_MEMBERSHIP_LEAKAGE"
    TIME_SEMANTICS_VIOLATION = "TIME_SEMANTICS_VIOLATION"
    FEATURE_LABEL_LEAKAGE = "FEATURE_LABEL_LEAKAGE"
    VINTAGE_PLAN_INCOMPLETE = "VINTAGE_PLAN_INCOMPLETE"
    REVISION_HISTORY_INVALID = "REVISION_HISTORY_INVALID"


class InputOrigin(StableStringEnum):
    FROZEN_REAL = "FROZEN_REAL"
    TEST_FIXTURE = "TEST_FIXTURE"


def stable_content_hash(value: Any) -> str:
    """Hash a canonical tree without wall-clock or generated-at fields."""

    rendered = json.dumps(
        canonicalize(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    return "sha256:" + hashlib.sha256(rendered.encode("utf-8")).hexdigest()


def _require_date(value: str, field_name: str) -> None:
    require_text(value, field_name)
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        fail(FailureCode.INVALID_TIMESTAMP, f"{field_name} must be YYYY-MM-DD")
        raise AssertionError("unreachable") from exc
    if parsed.isoformat() != value:
        fail(FailureCode.INVALID_TIMESTAMP, f"{field_name} must be canonical YYYY-MM-DD")


def _require_sorted_unique_dates(value: tuple[str, ...], field_name: str) -> None:
    require_nonempty_tuple(value, field_name)
    for item in value:
        _require_date(item, field_name)
    if tuple(sorted(value)) != value or len(set(value)) != len(value):
        fail(FailureCode.INVALID_VALUE, f"{field_name} must be sorted and unique")


def _freeze_values(value: Mapping[str, Any], field_name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        fail(FailureCode.INVALID_VALUE, f"{field_name} must be a mapping")
    output: dict[str, Any] = {}
    for key, item in sorted(value.items()):
        require_identifier(key, f"{field_name}.key")
        if isinstance(item, float) and not math.isfinite(item):
            fail(FailureCode.NON_FINITE_NUMBER, f"{field_name}.{key} must be finite")
        if item is not None and not isinstance(item, (str, int, float, bool)):
            fail(FailureCode.INVALID_VALUE, f"{field_name}.{key} has unsupported value type")
        output[key] = item
    return MappingProxyType(output)


def validate_source_labels(provider_class: str, source_class: str) -> None:
    require_identifier(provider_class, "provider_class")
    require_identifier(source_class, "source_class")
    if provider_class != APPROVED_PROVIDER_CLASS or source_class != APPROVED_SOURCE_CLASS:
        fail(
            FailureCode.AUXILIARY_MASQUERADE,
            "real A-share input must remain APPROVED_PROXY_DATA_VENDOR / TUSHARE_COMPATIBLE_PROXY",
            scale_failure_code=ScaleQualificationFailureCode.SOURCE_LABEL_REJECTED.value,
        )
    if FORBIDDEN_OFFICIAL_SOURCE in {provider_class, source_class}:
        fail(
            FailureCode.AUXILIARY_MASQUERADE,
            "approved proxy input cannot claim OFFICIAL_TUSHARE",
            scale_failure_code=ScaleQualificationFailureCode.SOURCE_LABEL_REJECTED.value,
        )


@dataclass(frozen=True, kw_only=True)
class ScaleQualificationPolicy(CanonicalContract):
    SCHEMA_VERSION: ClassVar[str] = REAL_ASHARE_SCALE_SCHEMA_VERSION

    policy_id: str = "alphaforge.real-ashare-scale-policy.v1"
    min_symbols: int = 20
    max_symbols: int = 50
    min_sessions: int = 120
    max_sessions: int = 250
    replay_sessions: int = 20
    min_required_field_coverage: float = 0.80
    required_feature_fields: tuple[str, ...] = (
        "close",
        "volume",
        "industry",
        "market_cap",
        "adv",
        "benchmark_return",
    )
    allowed_label_fields: tuple[str, ...] = ("future_return_5d",)
    provider_class: str = APPROVED_PROVIDER_CLASS
    source_class: str = APPROVED_SOURCE_CLASS
    offline_default: bool = True

    def __post_init__(self) -> None:
        require_identifier(self.policy_id, "policy_id")
        validate_source_labels(self.provider_class, self.source_class)
        if not (1 <= self.min_symbols <= self.max_symbols <= 50):
            fail(FailureCode.INVALID_VALUE, "symbol policy must be bounded by 50")
        if not (1 <= self.min_sessions <= self.max_sessions <= 250):
            fail(FailureCode.INVALID_VALUE, "session policy must be bounded by 250")
        if not (1 <= self.replay_sessions <= self.min_sessions):
            fail(FailureCode.INVALID_VALUE, "replay_sessions must not exceed min_sessions")
        if not (0.0 < self.min_required_field_coverage <= 1.0):
            fail(FailureCode.INVALID_VALUE, "field coverage threshold must lie in (0,1]")
        for field_name in self.required_feature_fields + self.allowed_label_fields:
            require_identifier(field_name, "field")
        if len(set(self.required_feature_fields)) != len(self.required_feature_fields):
            fail(FailureCode.INVALID_VALUE, "required feature fields contain duplicates")
        if len(set(self.allowed_label_fields)) != len(self.allowed_label_fields):
            fail(FailureCode.INVALID_VALUE, "allowed label fields contain duplicates")
        if set(self.required_feature_fields) & set(self.allowed_label_fields):
            fail(FailureCode.UNAUTHORIZED_CAPABILITY, "labels cannot be features")
        if any("future_return" in name.lower() for name in self.required_feature_fields):
            fail(FailureCode.UNAUTHORIZED_CAPABILITY, "future return is label-only")
        if self.offline_default is not True:
            fail(FailureCode.UNAUTHORIZED_CAPABILITY, "qualification must default offline")


@dataclass(frozen=True, kw_only=True)
class HistoricalUniverseSnapshot(CanonicalContract):
    SCHEMA_VERSION: ClassVar[str] = REAL_ASHARE_SCALE_SCHEMA_VERSION

    session: str
    symbols: tuple[str, ...]

    def __post_init__(self) -> None:
        _require_date(self.session, "session")
        require_nonempty_tuple(self.symbols, "symbols")
        for symbol in self.symbols:
            require_identifier(symbol, "symbols")
        if tuple(sorted(self.symbols)) != self.symbols or len(set(self.symbols)) != len(self.symbols):
            fail(FailureCode.INVALID_VALUE, "historical universe symbols must be sorted and unique")


@dataclass(frozen=True, kw_only=True)
class ScaleInputRow(CanonicalContract):
    SCHEMA_VERSION: ClassVar[str] = REAL_ASHARE_SCALE_SCHEMA_VERSION

    symbol: str
    session: str
    event_time: datetime
    available_at: datetime
    revision_id: str
    features: Mapping[str, Any]
    labels: Mapping[str, Any]

    def __post_init__(self) -> None:
        require_identifier(self.symbol, "symbol")
        _require_date(self.session, "session")
        require_aware_datetime(self.event_time, "event_time")
        require_aware_datetime(self.available_at, "available_at")
        if self.event_time > self.available_at:
            fail(FailureCode.INVALID_TIME_ORDER, "event_time must not follow available_at")
        require_identifier(self.revision_id, "revision_id")
        features = _freeze_values(self.features, "features")
        labels = _freeze_values(self.labels, "labels")
        if not features:
            fail(FailureCode.MISSING_REQUIRED_FIELD, "row features are required")
        if any("future_return" in key.lower() for key in features):
            fail(
                FailureCode.UNAUTHORIZED_CAPABILITY,
                "future return is label-only and cannot appear in features",
                scale_failure_code=ScaleQualificationFailureCode.FEATURE_LABEL_LEAKAGE.value,
            )
        if any(not key.lower().startswith("future_return") for key in labels):
            fail(FailureCode.UNKNOWN_FIELD, "only future-return labels are accepted")
        if set(features) & set(labels):
            fail(FailureCode.UNAUTHORIZED_CAPABILITY, "feature and label namespaces must be disjoint")
        object.__setattr__(self, "features", features)
        object.__setattr__(self, "labels", labels)


@dataclass(frozen=True, kw_only=True)
class FrozenDataVintage(CanonicalContract):
    SCHEMA_VERSION: ClassVar[str] = REAL_ASHARE_SCALE_SCHEMA_VERSION

    vintage_id: str
    session: str
    available_at: datetime
    decision_time: datetime
    revision_id: str
    content_hash: str
    supersedes_vintage_id: str | None = None

    def __post_init__(self) -> None:
        require_identifier(self.vintage_id, "vintage_id")
        _require_date(self.session, "session")
        require_aware_datetime(self.available_at, "available_at")
        require_aware_datetime(self.decision_time, "decision_time")
        if self.available_at > self.decision_time:
            fail(FailureCode.INVALID_TIME_ORDER, "vintage available_at must not follow decision_time")
        require_identifier(self.revision_id, "revision_id")
        require_hash(self.content_hash, "content_hash")
        if self.supersedes_vintage_id is not None:
            require_identifier(self.supersedes_vintage_id, "supersedes_vintage_id")
            if self.supersedes_vintage_id == self.vintage_id:
                fail(FailureCode.INVALID_VALUE, "a vintage cannot supersede itself")


@dataclass(frozen=True, kw_only=True)
class FrozenAshareScaleInput(CanonicalContract):
    """Bounded adapter output; raw data remains outside the trusted authorities."""

    SCHEMA_VERSION: ClassVar[str] = REAL_ASHARE_SCALE_SCHEMA_VERSION

    dataset_id: str
    provider_class: str
    source_class: str
    input_origin: InputOrigin
    capture_time: datetime
    capture_network_used: bool
    calendar_id: str
    calendar_sessions: tuple[str, ...]
    calendar_hash: str
    historical_universe: tuple[HistoricalUniverseSnapshot, ...]
    universe_hash: str
    rows: tuple[ScaleInputRow, ...]
    data_hash: str
    feature_fields: tuple[str, ...]
    label_fields: tuple[str, ...]
    availability_policy_hash: str
    vintages: tuple[FrozenDataVintage, ...]
    adapter_manifest_hash: str

    def __post_init__(self) -> None:
        require_identifier(self.dataset_id, "dataset_id")
        validate_source_labels(self.provider_class, self.source_class)
        if not isinstance(self.input_origin, InputOrigin):
            object.__setattr__(self, "input_origin", InputOrigin(self.input_origin))
        require_aware_datetime(self.capture_time, "capture_time")
        if type(self.capture_network_used) is not bool:
            fail(FailureCode.INVALID_VALUE, "capture_network_used must be boolean")
        require_identifier(self.calendar_id, "calendar_id")
        _require_sorted_unique_dates(self.calendar_sessions, "calendar_sessions")
        for name in ("calendar_hash", "universe_hash", "data_hash", "availability_policy_hash", "adapter_manifest_hash"):
            require_hash(getattr(self, name), name)
        require_nonempty_tuple(self.historical_universe, "historical_universe")
        require_nonempty_tuple(self.rows, "rows")
        require_nonempty_tuple(self.feature_fields, "feature_fields")
        require_nonempty_tuple(self.label_fields, "label_fields")
        require_nonempty_tuple(self.vintages, "vintages")
        if any(not isinstance(item, HistoricalUniverseSnapshot) for item in self.historical_universe):
            fail(FailureCode.INVALID_VALUE, "historical_universe contains invalid items")
        if any(not isinstance(item, ScaleInputRow) for item in self.rows):
            fail(FailureCode.INVALID_VALUE, "rows contains invalid items")
        if any(not isinstance(item, FrozenDataVintage) for item in self.vintages):
            fail(FailureCode.INVALID_VALUE, "vintages contains invalid items")
        for field_name in self.feature_fields + self.label_fields:
            require_identifier(field_name, "field")
        if len(set(self.feature_fields)) != len(self.feature_fields) or len(set(self.label_fields)) != len(self.label_fields):
            fail(FailureCode.INVALID_VALUE, "feature and label fields must be unique")
        if set(self.feature_fields) & set(self.label_fields):
            fail(FailureCode.UNAUTHORIZED_CAPABILITY, "labels cannot be declared as features")
        if any("future_return" in name.lower() for name in self.feature_fields):
            fail(FailureCode.UNAUTHORIZED_CAPABILITY, "future return is label-only")


@dataclass(frozen=True, kw_only=True)
class FieldCoverage(CanonicalContract):
    SCHEMA_VERSION: ClassVar[str] = REAL_ASHARE_SCALE_SCHEMA_VERSION

    field_name: str
    present_count: int
    total_count: int
    coverage: float

    def __post_init__(self) -> None:
        require_identifier(self.field_name, "field_name")
        if any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in (self.present_count, self.total_count)):
            fail(FailureCode.INVALID_VALUE, "coverage counts must be non-negative integers")
        if self.present_count > self.total_count or not (0.0 <= self.coverage <= 1.0):
            fail(FailureCode.INVALID_VALUE, "field coverage is invalid")


@dataclass(frozen=True, kw_only=True)
class HistoricalUniverseCoverage(CanonicalContract):
    SCHEMA_VERSION: ClassVar[str] = REAL_ASHARE_SCALE_SCHEMA_VERSION

    calendar_session_count: int
    membership_session_count: int
    union_symbols: tuple[str, ...]
    minimum_members_per_session: int
    maximum_members_per_session: int
    session_coverage: float
    membership_hash: str
    future_membership_leakage: bool

    def __post_init__(self) -> None:
        for name in (
            "calendar_session_count",
            "membership_session_count",
            "minimum_members_per_session",
            "maximum_members_per_session",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                fail(FailureCode.INVALID_VALUE, f"{name} must be a non-negative integer")
        for symbol in self.union_symbols:
            require_identifier(symbol, "union_symbols")
        if tuple(sorted(self.union_symbols)) != self.union_symbols or len(set(self.union_symbols)) != len(self.union_symbols):
            fail(FailureCode.INVALID_VALUE, "union_symbols must be sorted and unique")
        if not (0.0 <= self.session_coverage <= 1.0):
            fail(FailureCode.INVALID_VALUE, "session_coverage must lie in [0,1]")
        require_hash(self.membership_hash, "membership_hash")
        if type(self.future_membership_leakage) is not bool:
            fail(FailureCode.INVALID_VALUE, "future_membership_leakage must be boolean")


@dataclass(frozen=True, kw_only=True)
class TimeSemanticsReport(CanonicalContract):
    SCHEMA_VERSION: ClassVar[str] = REAL_ASHARE_SCALE_SCHEMA_VERSION

    row_count: int
    event_available_order_violations: int
    as_of_visible_observations: int
    future_event_violations: int
    availability_policy_hash: str

    def __post_init__(self) -> None:
        for name in ("row_count", "event_available_order_violations", "as_of_visible_observations", "future_event_violations"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                fail(FailureCode.INVALID_VALUE, f"{name} must be a non-negative integer")
        require_hash(self.availability_policy_hash, "availability_policy_hash")


@dataclass(frozen=True, kw_only=True)
class FrozenAshareScaleManifest(CanonicalContract):
    SCHEMA_VERSION: ClassVar[str] = REAL_ASHARE_SCALE_SCHEMA_VERSION

    dataset_id: str
    provider_class: str
    source_class: str
    input_origin: InputOrigin
    capture_time: datetime
    capture_network_used: bool
    calendar_id: str
    calendar_hash: str
    universe_hash: str
    data_hash: str
    adapter_manifest_hash: str
    availability_policy_hash: str
    symbol_count: int
    session_count: int
    row_count: int
    feature_fields: tuple[str, ...]
    label_fields: tuple[str, ...]
    field_coverage: tuple[FieldCoverage, ...]
    universe_coverage: HistoricalUniverseCoverage
    time_semantics: TimeSemanticsReport
    vintage_count: int
    future_return_feature_present: bool = False

    def __post_init__(self) -> None:
        require_identifier(self.dataset_id, "dataset_id")
        validate_source_labels(self.provider_class, self.source_class)
        if not isinstance(self.input_origin, InputOrigin):
            object.__setattr__(self, "input_origin", InputOrigin(self.input_origin))
        require_aware_datetime(self.capture_time, "capture_time")
        require_identifier(self.calendar_id, "calendar_id")
        for name in ("calendar_hash", "universe_hash", "data_hash", "adapter_manifest_hash", "availability_policy_hash"):
            require_hash(getattr(self, name), name)
        for name in ("symbol_count", "session_count", "row_count", "vintage_count"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                fail(FailureCode.INVALID_VALUE, f"{name} must be a non-negative integer")
        if self.future_return_feature_present:
            fail(FailureCode.UNAUTHORIZED_CAPABILITY, "future return cannot be a feature")


@dataclass(frozen=True, kw_only=True)
class DataVintagePlan(CanonicalContract):
    SCHEMA_VERSION: ClassVar[str] = REAL_ASHARE_SCALE_SCHEMA_VERSION

    plan_id: str
    manifest_hash: str
    replay_session_count: int
    vintages: tuple[FrozenDataVintage, ...]
    first_decision_time: datetime
    last_decision_time: datetime

    def __post_init__(self) -> None:
        require_identifier(self.plan_id, "plan_id")
        require_hash(self.manifest_hash, "manifest_hash")
        if isinstance(self.replay_session_count, bool) or self.replay_session_count < 1:
            fail(FailureCode.INVALID_VALUE, "replay_session_count must be positive")
        if len(self.vintages) != self.replay_session_count:
            fail(FailureCode.INCOMPLETE_RESULT, "vintage plan count is incomplete")
        if len({item.session for item in self.vintages}) != len(self.vintages):
            fail(FailureCode.INVALID_VALUE, "vintage plan requires one decision vintage per session")
        ordered = tuple(sorted(self.vintages, key=lambda item: (item.decision_time, item.vintage_id)))
        if ordered != self.vintages:
            fail(FailureCode.INVALID_VALUE, "vintage plan must be deterministically ordered")
        require_aware_datetime(self.first_decision_time, "first_decision_time")
        require_aware_datetime(self.last_decision_time, "last_decision_time")
        if self.first_decision_time != self.vintages[0].decision_time or self.last_decision_time != self.vintages[-1].decision_time:
            fail(FailureCode.INVALID_TIME_ORDER, "vintage plan boundary clocks do not match")


@dataclass(frozen=True, kw_only=True)
class ScaleQualificationVerdict(CanonicalContract):
    SCHEMA_VERSION: ClassVar[str] = REAL_ASHARE_SCALE_SCHEMA_VERSION

    status: ScaleQualificationStatus
    eligible: bool
    policy_hash: str
    manifest_hash: str | None
    vintage_plan_hash: str | None
    failure_codes: tuple[ScaleQualificationFailureCode, ...]
    reasons: tuple[str, ...]
    operational_smoke_status: str

    def __post_init__(self) -> None:
        if not isinstance(self.status, ScaleQualificationStatus):
            object.__setattr__(self, "status", ScaleQualificationStatus(self.status))
        if type(self.eligible) is not bool:
            fail(FailureCode.INVALID_VALUE, "eligible must be boolean")
        require_hash(self.policy_hash, "policy_hash")
        for name in ("manifest_hash", "vintage_plan_hash"):
            value = getattr(self, name)
            if value is not None:
                require_hash(value, name)
        for item in self.failure_codes:
            if not isinstance(item, ScaleQualificationFailureCode):
                fail(FailureCode.INVALID_VALUE, "failure_codes contains an invalid code")
        for reason in self.reasons:
            require_text(reason, "reasons")
        require_identifier(self.operational_smoke_status, "operational_smoke_status")
        if self.status is ScaleQualificationStatus.QUALIFIED:
            if not self.eligible or self.failure_codes or self.manifest_hash is None or self.vintage_plan_hash is None:
                fail(FailureCode.INVALID_STATE, "qualified verdict requires complete, failure-free evidence")
        elif self.eligible:
            fail(FailureCode.INVALID_STATE, "non-qualified verdict cannot be eligible")


@dataclass(frozen=True, kw_only=True)
class ScaleQualificationOutcome(CanonicalContract):
    SCHEMA_VERSION: ClassVar[str] = REAL_ASHARE_SCALE_SCHEMA_VERSION

    verdict: ScaleQualificationVerdict
    manifest: FrozenAshareScaleManifest | None
    vintage_plan: DataVintagePlan | None

    def __post_init__(self) -> None:
        if self.verdict.manifest_hash != (None if self.manifest is None else self.manifest.artifact_hash):
            fail(FailureCode.INVALID_HASH, "verdict manifest hash does not bind manifest")
        if self.verdict.vintage_plan_hash != (None if self.vintage_plan is None else self.vintage_plan.artifact_hash):
            fail(FailureCode.INVALID_HASH, "verdict vintage plan hash does not bind plan")


__all__ = [
    "APPROVED_PROVIDER_CLASS",
    "APPROVED_SOURCE_CLASS",
    "DataVintagePlan",
    "FieldCoverage",
    "FrozenAshareScaleInput",
    "FrozenAshareScaleManifest",
    "FrozenDataVintage",
    "HistoricalUniverseCoverage",
    "HistoricalUniverseSnapshot",
    "InputOrigin",
    "REAL_ASHARE_SCALE_SCHEMA_VERSION",
    "ScaleInputRow",
    "ScaleQualificationFailureCode",
    "ScaleQualificationOutcome",
    "ScaleQualificationPolicy",
    "ScaleQualificationStatus",
    "ScaleQualificationVerdict",
    "TimeSemanticsReport",
    "stable_content_hash",
    "validate_source_labels",
]
