"""Typed contracts for the offline A-share research bridge.

These contracts describe a data bridge only.  They do not compute official
evidence, promote candidates, calibrate alpha, construct portfolios, approve
risk, create orders, or write ledgers.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import ClassVar, Mapping

from veriquant.contracts import CanonicalContract, DatasetCard, FailureCode
from veriquant.contracts.base import (
    fail,
    freeze_mapping,
    require_aware_datetime,
    require_finite_number,
    require_hash,
    require_identifier,
    require_nonempty_tuple,
    require_text,
    require_time_order,
)


BRIDGE_SCHEMA_VERSION = "veriquant.real-research/v1"
PARTITIONS = frozenset({"train", "validation", "sealed"})


def _partition(value: str, field_name: str = "partition") -> None:
    if value not in PARTITIONS:
        fail(FailureCode.INVALID_VALUE, f"{field_name} must be train, validation, or sealed")


def _non_negative_int(value: int, field_name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        fail(FailureCode.INVALID_VALUE, f"{field_name} must be a non-negative integer")


def _positive_int(value: int, field_name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        fail(FailureCode.INVALID_VALUE, f"{field_name} must be a positive integer")


def _hash_mapping(value: Mapping[str, str], field_name: str) -> Mapping[str, str]:
    if not isinstance(value, Mapping):
        fail(FailureCode.INVALID_VALUE, f"{field_name} must be a mapping")
    output: dict[str, str] = {}
    for key, item in sorted(value.items()):
        require_text(key, f"{field_name}.key")
        require_hash(item, f"{field_name}.{key}")
        output[key] = item
    return MappingProxyType(output)


@dataclass(frozen=True, kw_only=True)
class LabelSpecification(CanonicalContract):
    """Forward-return label definition with explicit execution lag."""

    SCHEMA_VERSION: ClassVar[str] = BRIDGE_SCHEMA_VERSION

    label_id: str
    name: str
    price_field: str
    forward_days: int
    execution_lag_days: int
    return_type: str
    label_start: datetime
    label_end: datetime
    availability_assumption: str

    def __post_init__(self) -> None:
        require_identifier(self.label_id, "label_id")
        require_text(self.name, "name")
        require_text(self.price_field, "price_field")
        _positive_int(self.forward_days, "forward_days")
        _non_negative_int(self.execution_lag_days, "execution_lag_days")
        if self.return_type != "simple_forward_return":
            fail(FailureCode.INVALID_VALUE, "only simple_forward_return is supported")
        require_time_order(("label_start", self.label_start), ("label_end", self.label_end))
        require_text(self.availability_assumption, "availability_assumption")


@dataclass(frozen=True, kw_only=True)
class ResearchSplitManifest(CanonicalContract):
    """Time ranges for visible and sealed evaluation partitions."""

    SCHEMA_VERSION: ClassVar[str] = BRIDGE_SCHEMA_VERSION

    split_id: str
    train_start: datetime
    train_end: datetime
    validation_start: datetime
    validation_end: datetime
    sealed_start: datetime
    sealed_end: datetime
    policy_version: str = "time-ordered-train-validation-sealed/v1"

    def __post_init__(self) -> None:
        require_identifier(self.split_id, "split_id")
        require_time_order(
            ("train_start", self.train_start),
            ("train_end", self.train_end),
            ("validation_start", self.validation_start),
            ("validation_end", self.validation_end),
            ("sealed_start", self.sealed_start),
            ("sealed_end", self.sealed_end),
        )
        if self.train_end >= self.validation_start or self.validation_end >= self.sealed_start:
            fail(FailureCode.INVALID_TIME_ORDER, "split partitions must be strictly disjoint")
        require_text(self.policy_version, "policy_version")

    def partition_for(self, event_time: datetime) -> str | None:
        require_aware_datetime(event_time, "event_time")
        if self.train_start <= event_time <= self.train_end:
            return "train"
        if self.validation_start <= event_time <= self.validation_end:
            return "validation"
        if self.sealed_start <= event_time <= self.sealed_end:
            return "sealed"
        return None


@dataclass(frozen=True, kw_only=True)
class UniverseManifest(CanonicalContract):
    """Historical A-share universe view used by the bridge."""

    SCHEMA_VERSION: ClassVar[str] = BRIDGE_SCHEMA_VERSION

    universe_id: str
    index_code: str
    decision_time: datetime
    symbols: tuple[str, ...]
    historical_universe: Mapping[str, tuple[str, ...]]
    source_snapshot_hash: str
    policy_version: str = "historical-membership-as-of-event-date/v1"

    def __post_init__(self) -> None:
        require_identifier(self.universe_id, "universe_id")
        require_identifier(self.index_code, "index_code")
        require_aware_datetime(self.decision_time, "decision_time")
        require_nonempty_tuple(self.symbols, "symbols")
        if len(set(self.symbols)) != len(self.symbols):
            fail(FailureCode.INVALID_VALUE, "universe symbols must be unique")
        for symbol in self.symbols:
            require_identifier(symbol, "symbols")
        if not isinstance(self.historical_universe, Mapping) or not self.historical_universe:
            fail(FailureCode.INVALID_VALUE, "historical_universe is required")
        universe: dict[str, tuple[str, ...]] = {}
        for key, members in sorted(self.historical_universe.items()):
            require_text(key, "historical_universe.date")
            require_nonempty_tuple(members, f"historical_universe.{key}")
            if len(set(members)) != len(members):
                fail(FailureCode.INVALID_VALUE, "historical universe has duplicate members")
            for symbol in members:
                require_identifier(symbol, "historical_universe.symbol")
            universe[key] = tuple(sorted(members))
        object.__setattr__(self, "historical_universe", MappingProxyType(universe))
        require_hash(self.source_snapshot_hash, "source_snapshot_hash")
        require_text(self.policy_version, "policy_version")


@dataclass(frozen=True, kw_only=True)
class DataQualityManifest(CanonicalContract):
    """Fail-closed row accounting for PIT and market-state filters."""

    SCHEMA_VERSION: ClassVar[str] = BRIDGE_SCHEMA_VERSION

    quality_id: str
    rows_seen: int
    rows_used: int
    rows_rejected: int
    rejection_reasons: Mapping[str, int]
    coverage_by_field: Mapping[str, float]
    required_market_state_fields: tuple[str, ...]
    policy_version: str = "ashare-bridge-quality/v1"

    def __post_init__(self) -> None:
        require_identifier(self.quality_id, "quality_id")
        for name in ("rows_seen", "rows_used", "rows_rejected"):
            _non_negative_int(getattr(self, name), name)
        if self.rows_seen != self.rows_used + self.rows_rejected:
            fail(FailureCode.INVALID_VALUE, "rows_seen must equal rows_used + rows_rejected")
        reasons = freeze_mapping(self.rejection_reasons, "rejection_reasons", numeric_values=True)
        normalized_reasons: dict[str, int] = {}
        for key, value in reasons.items():
            if isinstance(value, bool) or int(value) != value or value < 0:
                fail(FailureCode.INVALID_VALUE, "rejection reason counts must be non-negative integers")
            normalized_reasons[key] = int(value)
        if sum(normalized_reasons.values()) != self.rows_rejected:
            fail(FailureCode.INVALID_VALUE, "rejection reason counts must sum to rows_rejected")
        object.__setattr__(self, "rejection_reasons", MappingProxyType(dict(sorted(normalized_reasons.items()))))
        coverage = freeze_mapping(self.coverage_by_field, "coverage_by_field", numeric_values=True)
        for key, value in coverage.items():
            if value < 0.0 or value > 1.0:
                fail(FailureCode.INVALID_VALUE, "coverage values must lie in [0,1]")
        object.__setattr__(self, "coverage_by_field", MappingProxyType(dict(sorted(coverage.items()))))
        require_nonempty_tuple(self.required_market_state_fields, "required_market_state_fields")
        for field in self.required_market_state_fields:
            require_identifier(field, "required_market_state_fields")
        require_text(self.policy_version, "policy_version")


@dataclass(frozen=True, kw_only=True)
class BridgeRunManifest(CanonicalContract):
    """Audit manifest for one bridge materialization."""

    SCHEMA_VERSION: ClassVar[str] = BRIDGE_SCHEMA_VERSION

    run_id: str
    bridge_version: str
    snapshot_id: str
    snapshot_hash: str
    input_hash: str
    output_hash: str
    label_spec_hash: str
    split_manifest_hash: str
    universe_manifest_hash: str
    data_quality_hash: str
    generated_at: datetime
    sealed_metrics_recorded: bool
    qlib_status: str = "NOT_RUN"

    def __post_init__(self) -> None:
        require_identifier(self.run_id, "run_id")
        require_text(self.bridge_version, "bridge_version")
        require_identifier(self.snapshot_id, "snapshot_id")
        for name in (
            "snapshot_hash",
            "input_hash",
            "output_hash",
            "label_spec_hash",
            "split_manifest_hash",
            "universe_manifest_hash",
            "data_quality_hash",
        ):
            require_hash(getattr(self, name), name)
        require_aware_datetime(self.generated_at, "generated_at")
        if type(self.sealed_metrics_recorded) is not bool:
            fail(FailureCode.INVALID_VALUE, "sealed_metrics_recorded must be boolean")
        if self.sealed_metrics_recorded:
            fail(FailureCode.UNAUTHORIZED_CAPABILITY, "bridge audit must not record sealed metrics")
        if self.qlib_status not in {"NOT_RUN", "UNAVAILABLE", "AUXILIARY_VISIBLE_REQUEST"}:
            fail(FailureCode.INVALID_VALUE, "unknown qlib_status")


@dataclass(frozen=True, kw_only=True)
class ResearchFeatureRow(CanonicalContract):
    """One PIT-visible feature row for FactorDAG/Evidence consumers."""

    SCHEMA_VERSION: ClassVar[str] = BRIDGE_SCHEMA_VERSION

    partition: str
    event_time: datetime
    symbol: str
    values: Mapping[str, float]
    source_row_hash: str
    available_at: datetime

    def __post_init__(self) -> None:
        _partition(self.partition)
        require_time_order(("event_time", self.event_time), ("available_at", self.available_at))
        require_identifier(self.symbol, "symbol")
        values = freeze_mapping(self.values, "values", numeric_values=True)
        if not values:
            fail(FailureCode.INVALID_VALUE, "feature row values are required")
        object.__setattr__(self, "values", MappingProxyType(dict(sorted(values.items()))))
        require_hash(self.source_row_hash, "source_row_hash")


@dataclass(frozen=True, kw_only=True)
class ResearchLabelRow(CanonicalContract):
    """Label row kept separate from features so it cannot be used as input."""

    SCHEMA_VERSION: ClassVar[str] = BRIDGE_SCHEMA_VERSION

    partition: str
    event_time: datetime
    symbol: str
    value: float
    entry_time: datetime
    exit_time: datetime
    available_at: datetime
    label_spec_hash: str

    def __post_init__(self) -> None:
        _partition(self.partition)
        require_identifier(self.symbol, "symbol")
        require_time_order(
            ("event_time", self.event_time),
            ("entry_time", self.entry_time),
            ("exit_time", self.exit_time),
            ("available_at", self.available_at),
        )
        require_finite_number(self.value, "value")
        require_hash(self.label_spec_hash, "label_spec_hash")


@dataclass(frozen=True, kw_only=True)
class PITRejection(CanonicalContract):
    """Auditable rejected source row."""

    SCHEMA_VERSION: ClassVar[str] = BRIDGE_SCHEMA_VERSION

    event_time: datetime
    symbol: str
    reason: str
    source_row_hash: str

    def __post_init__(self) -> None:
        require_aware_datetime(self.event_time, "event_time")
        require_identifier(self.symbol, "symbol")
        require_identifier(self.reason, "reason")
        require_hash(self.source_row_hash, "source_row_hash")


@dataclass(frozen=True, kw_only=True)
class ResearchDatasetBundle(CanonicalContract):
    """Bridge output consumed by Desks, FactorDAG, and deterministic evidence."""

    SCHEMA_VERSION: ClassVar[str] = BRIDGE_SCHEMA_VERSION

    bundle_id: str
    dataset_card: DatasetCard
    decision_time: datetime
    feature_fields: tuple[str, ...]
    feature_rows: tuple[ResearchFeatureRow, ...]
    label_rows: tuple[ResearchLabelRow, ...]
    rejections: tuple[PITRejection, ...]
    label_specification: LabelSpecification
    split_manifest: ResearchSplitManifest
    universe_manifest: UniverseManifest
    data_quality_manifest: DataQualityManifest
    bridge_run_manifest: BridgeRunManifest

    def __post_init__(self) -> None:
        require_identifier(self.bundle_id, "bundle_id")
        if not isinstance(self.dataset_card, DatasetCard):
            fail(FailureCode.INVALID_VALUE, "dataset_card must be DatasetCard")
        require_aware_datetime(self.decision_time, "decision_time")
        require_nonempty_tuple(self.feature_fields, "feature_fields")
        if len(set(self.feature_fields)) != len(self.feature_fields):
            fail(FailureCode.INVALID_VALUE, "feature_fields must be unique")
        for field in self.feature_fields:
            require_identifier(field, "feature_fields")
        if not isinstance(self.feature_rows, tuple) or not self.feature_rows:
            fail(FailureCode.INCOMPLETE_RESULT, "bundle requires feature rows")
        if not isinstance(self.label_rows, tuple) or not self.label_rows:
            fail(FailureCode.INCOMPLETE_RESULT, "bundle requires label rows")
        if any(not isinstance(row, ResearchFeatureRow) for row in self.feature_rows):
            fail(FailureCode.INVALID_VALUE, "feature_rows contain invalid rows")
        if any(not isinstance(row, ResearchLabelRow) for row in self.label_rows):
            fail(FailureCode.INVALID_VALUE, "label_rows contain invalid rows")
        if any(not isinstance(row, PITRejection) for row in self.rejections):
            fail(FailureCode.INVALID_VALUE, "rejections contain invalid rows")
        for contract in (
            self.label_specification,
            self.split_manifest,
            self.universe_manifest,
            self.data_quality_manifest,
            self.bridge_run_manifest,
        ):
            if not isinstance(contract, CanonicalContract):
                fail(FailureCode.INVALID_VALUE, "bundle manifests must be canonical contracts")
        if self.bridge_run_manifest.sealed_metrics_recorded:
            fail(FailureCode.UNAUTHORIZED_CAPABILITY, "bundle cannot record sealed metrics")
        if self.bridge_run_manifest.output_hash != bundle_payload_hash(
            feature_rows=self.feature_rows,
            label_rows=self.label_rows,
            rejections=self.rejections,
            label_specification=self.label_specification,
            split_manifest=self.split_manifest,
            universe_manifest=self.universe_manifest,
            data_quality_manifest=self.data_quality_manifest,
        ):
            fail(FailureCode.INVALID_HASH, "bridge_run_manifest output_hash does not match bundle payload")


def bundle_payload_hash(
    *,
    feature_rows: tuple[ResearchFeatureRow, ...],
    label_rows: tuple[ResearchLabelRow, ...],
    rejections: tuple[PITRejection, ...],
    label_specification: LabelSpecification,
    split_manifest: ResearchSplitManifest,
    universe_manifest: UniverseManifest,
    data_quality_manifest: DataQualityManifest,
) -> str:
    from veriquant.data import canonical_content_hash

    return canonical_content_hash(
        {
            "feature_rows": feature_rows,
            "label_rows": label_rows,
            "rejections": rejections,
            "label_specification": label_specification,
            "split_manifest": split_manifest,
            "universe_manifest": universe_manifest,
            "data_quality_manifest": data_quality_manifest,
        }
    )


@dataclass(frozen=True, kw_only=True)
class QlibBridgeRequest(CanonicalContract):
    """Visible-only Qlib request materialized from a ResearchDatasetBundle."""

    SCHEMA_VERSION: ClassVar[str] = BRIDGE_SCHEMA_VERSION

    bundle_hash: str
    qlib_manifest_hash: str
    visible_rows_hash: str
    visible_train_rows: int
    visible_validation_rows: int
    provider_uri: str
    status: str

    def __post_init__(self) -> None:
        require_hash(self.bundle_hash, "bundle_hash")
        require_hash(self.qlib_manifest_hash, "qlib_manifest_hash")
        require_hash(self.visible_rows_hash, "visible_rows_hash")
        _non_negative_int(self.visible_train_rows, "visible_train_rows")
        _non_negative_int(self.visible_validation_rows, "visible_validation_rows")
        require_text(self.provider_uri, "provider_uri")
        if self.status not in {"AUXILIARY_VISIBLE_REQUEST", "UNAVAILABLE"}:
            fail(FailureCode.INVALID_VALUE, "unknown Qlib bridge request status")


@dataclass(frozen=True, kw_only=True)
class QlibBridgeUnavailable(CanonicalContract):
    """Typed result for unavailable optional pyqlib/default real runner."""

    SCHEMA_VERSION: ClassVar[str] = BRIDGE_SCHEMA_VERSION

    adapter: str
    status: str
    code: FailureCode
    message: str
    bundle_hash: str

    def __post_init__(self) -> None:
        require_identifier(self.adapter, "adapter")
        if self.status != "UNAVAILABLE":
            fail(FailureCode.INVALID_VALUE, "unavailable result must have UNAVAILABLE status")
        if self.code not in {FailureCode.ADAPTER_UNAVAILABLE, FailureCode.QLIB_DATA_UNAVAILABLE}:
            fail(FailureCode.INVALID_VALUE, "unexpected Qlib unavailable code")
        require_text(self.message, "message")
        require_hash(self.bundle_hash, "bundle_hash")
