"""Typed A-share request, availability, historical-universe, and snapshot models."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from types import MappingProxyType
from typing import Mapping
from zoneinfo import ZoneInfo

from ...contracts import CanonicalContract, DatasetCard, FailureCode
from ...contracts.base import (
    fail,
    freeze_mapping,
    require_aware_datetime,
    require_finite_number,
    require_hash,
    require_identifier,
    require_nonempty_tuple,
    require_text,
)

from .calendar import FrozenTradingCalendar

SHANGHAI = ZoneInfo("Asia/Shanghai")
OHLCV_FIELDS = frozenset({"open", "high", "low", "close", "volume", "amount"})
FORBIDDEN_NAMES = ("sealed", "test", "official", "promotion")


@dataclass(frozen=True, kw_only=True)
class AShareDataRequest(CanonicalContract):
    """Closed request schema; it intentionally has no loader/callback field."""

    dataset_id: str
    codes: tuple[str, ...]
    index_code: str
    start_time: datetime
    end_time: datetime
    frequency: str
    adjustment: str
    source: str
    decision_time: datetime
    fields: tuple[str, ...]
    data_version: str
    cache_policy: str

    def __post_init__(self) -> None:
        require_identifier(self.dataset_id, "dataset_id")
        require_nonempty_tuple(self.codes, "codes")
        for code in self.codes:
            require_identifier(code, "codes")
        if len(set(self.codes)) != len(self.codes):
            fail(FailureCode.INVALID_VALUE, "codes contain duplicates")
        require_identifier(self.index_code, "index_code")
        for name in ("start_time", "end_time", "decision_time"):
            require_aware_datetime(getattr(self, name), name)
        if self.start_time > self.end_time or self.end_time > self.decision_time:
            fail(
                FailureCode.INVALID_TIME_ORDER,
                "A-share request requires start_time <= end_time <= decision_time",
            )
        if self.frequency != "1D":
            fail(FailureCode.INVALID_VALUE, "Prompt 10 A-share adapter supports daily bars only")
        if self.adjustment not in {"raw", "qfq", "hfq"}:
            fail(FailureCode.INVALID_VALUE, "adjustment must be raw, qfq, or hfq")
        if self.source != "tushare":
            fail(FailureCode.INVALID_VALUE, "real A-share source must be explicitly tushare")
        require_nonempty_tuple(self.fields, "fields")
        if len(set(self.fields)) != len(self.fields) or not set(self.fields) <= OHLCV_FIELDS:
            fail(FailureCode.UNKNOWN_FIELD, "A-share fields must be unique allowlisted OHLCV fields")
        for field_name in self.fields:
            if any(token in field_name.lower() for token in FORBIDDEN_NAMES):
                fail(FailureCode.UNAUTHORIZED_CAPABILITY, "forbidden field name")
        require_text(self.data_version, "data_version")
        if self.cache_policy not in {"freeze", "reuse-identical"}:
            fail(FailureCode.INVALID_VALUE, "cache_policy must be freeze or reuse-identical")


@dataclass(frozen=True, kw_only=True)
class AvailabilityPolicy(CanonicalContract):
    policy_version: str = "ashare-availability/v1"
    daily_policy_version: str = "daily-close-next-session-open/v1"
    fundamental_policy_version: str = "announcement-available-at/v1"
    next_open_hour: int = 9
    next_open_minute: int = 25

    def __post_init__(self) -> None:
        for name in ("policy_version", "daily_policy_version", "fundamental_policy_version"):
            require_text(getattr(self, name), name)
        if not (0 <= self.next_open_hour <= 23 and 0 <= self.next_open_minute <= 59):
            fail(FailureCode.INVALID_VALUE, "next-open clock is invalid")

    def daily_available_at(
        self,
        event_time: datetime,
        *,
        trading_calendar: FrozenTradingCalendar | None = None,
    ) -> datetime:
        require_aware_datetime(event_time, "event_time")
        if trading_calendar is not None:
            if not isinstance(trading_calendar, FrozenTradingCalendar):
                fail(FailureCode.INVALID_VALUE, "trading_calendar must be FrozenTradingCalendar")
            return trading_calendar.next_session_open(
                event_time,
                hour=self.next_open_hour,
                minute=self.next_open_minute,
            )
        local = event_time.astimezone(SHANGHAI)
        next_day = local.date() + timedelta(days=1)
        while next_day.weekday() >= 5:
            next_day += timedelta(days=1)
        return datetime(
            next_day.year,
            next_day.month,
            next_day.day,
            self.next_open_hour,
            self.next_open_minute,
            tzinfo=SHANGHAI,
        )

    def fundamental_available_at(
        self, *, event_time: datetime, published_at: datetime | None
    ) -> datetime:
        require_aware_datetime(event_time, "event_time")
        if published_at is None:
            fail(
                FailureCode.MISSING_REQUIRED_FIELD,
                "fundamental data requires ann_date/published_at",
            )
        require_aware_datetime(published_at, "published_at")
        if published_at < event_time:
            fail(FailureCode.INVALID_TIME_ORDER, "fundamental publication precedes event")
        return published_at

    def assumptions(
        self,
        fields: tuple[str, ...],
        *,
        trading_calendar: FrozenTradingCalendar | None = None,
    ) -> tuple[str, ...]:
        if trading_calendar is not None:
            if not isinstance(trading_calendar, FrozenTradingCalendar):
                fail(FailureCode.INVALID_VALUE, "trading_calendar must be FrozenTradingCalendar")
            calendar_assumption = (
                f"calendar={trading_calendar.calendar_id}:{trading_calendar.artifact_hash}"
            )
        else:
            calendar_assumption = "calendar=unfrozen-weekday-fallback"
        return tuple(
            [
                f"policy={self.policy_version}",
                f"daily_ohlcv={self.daily_policy_version}",
                f"fundamental={self.fundamental_policy_version}",
                calendar_assumption,
            ]
            + [f"field:{field_name}={self.daily_policy_version}" for field_name in fields]
        )


@dataclass(frozen=True, kw_only=True)
class AShareFailure(CanonicalContract):
    request_hash: str
    code: FailureCode
    status: str
    message: str
    provider: str
    retryable: bool = False

    def __post_init__(self) -> None:
        require_hash(self.request_hash, "request_hash")
        if self.code not in {
            FailureCode.ADAPTER_UNAVAILABLE,
            FailureCode.ADAPTER_EXECUTION_FAILED,
            FailureCode.INVALID_TIME_ORDER,
            FailureCode.INCOMPLETE_RESULT,
            FailureCode.QLIB_DATA_UNAVAILABLE,
        }:
            fail(FailureCode.INVALID_VALUE, "failure code is not data-adapter scoped")
        if self.status not in {"UNAVAILABLE", "DEGRADED", "FAILED"}:
            fail(FailureCode.INVALID_VALUE, "unknown A-share failure status")
        require_text(self.message, "message")
        require_identifier(self.provider, "provider")
        if type(self.retryable) is not bool:
            fail(FailureCode.INVALID_VALUE, "retryable must be boolean")


@dataclass(frozen=True, kw_only=True)
class AShareSnapshotRow(CanonicalContract):
    symbol: str
    event_time: datetime
    published_at: datetime
    available_at: datetime
    values: Mapping[str, int | float | None]

    def __post_init__(self) -> None:
        require_identifier(self.symbol, "symbol")
        for name in ("event_time", "published_at", "available_at"):
            require_aware_datetime(getattr(self, name), name)
        if self.event_time > self.published_at or self.published_at > self.available_at:
            fail(FailureCode.INVALID_TIME_ORDER, "row event/publication/availability order is invalid")
        frozen = freeze_mapping(self.values, "values")
        for key, value in frozen.items():
            if key not in OHLCV_FIELDS:
                fail(FailureCode.UNKNOWN_FIELD, f"unknown A-share row field: {key}")
            if value is not None:
                require_finite_number(value, f"values.{key}")
        object.__setattr__(self, "values", frozen)


def freeze_universe_mapping(
    value: Mapping[str, tuple[str, ...]], field_name: str = "historical_universe"
) -> Mapping[str, tuple[str, ...]]:
    if not isinstance(value, Mapping) or not value:
        fail(FailureCode.INVALID_VALUE, f"{field_name} must be a non-empty mapping")
    output: dict[str, tuple[str, ...]] = {}
    for key, symbols in sorted(value.items()):
        require_text(key, f"{field_name}.date")
        require_nonempty_tuple(symbols, f"{field_name}.{key}")
        if len(set(symbols)) != len(symbols):
            fail(FailureCode.INVALID_VALUE, "historical universe contains duplicates")
        for symbol in symbols:
            require_identifier(symbol, f"{field_name}.{key}")
        output[key] = tuple(sorted(symbols))
    return MappingProxyType(output)


@dataclass(frozen=True, kw_only=True)
class HistoricalUniverseResult(CanonicalContract):
    status: str
    index_code: str
    snapshots: Mapping[str, tuple[str, ...]]
    source: str
    failure: AShareFailure | None = None

    def __post_init__(self) -> None:
        if self.status not in {"AVAILABLE", "DEGRADED", "UNAVAILABLE"}:
            fail(FailureCode.INVALID_VALUE, "unknown historical-universe status")
        require_identifier(self.index_code, "index_code")
        require_identifier(self.source, "source")
        if self.status == "AVAILABLE":
            object.__setattr__(self, "snapshots", freeze_universe_mapping(self.snapshots))
            if self.failure is not None:
                fail(FailureCode.INVALID_STATE, "available universe cannot carry failure")
        else:
            if not isinstance(self.failure, AShareFailure):
                fail(FailureCode.INVALID_STATE, "degraded/unavailable universe requires failure")
            if self.snapshots:
                object.__setattr__(self, "snapshots", freeze_universe_mapping(self.snapshots))
            else:
                object.__setattr__(self, "snapshots", MappingProxyType({}))


@dataclass(frozen=True, kw_only=True)
class AShareLoadResult(CanonicalContract):
    request_hash: str
    provider: str
    fetched_at: datetime
    rows: tuple[AShareSnapshotRow, ...]
    excluded_rows: int

    def __post_init__(self) -> None:
        require_hash(self.request_hash, "request_hash")
        require_identifier(self.provider, "provider")
        require_aware_datetime(self.fetched_at, "fetched_at")
        if not isinstance(self.rows, tuple) or not self.rows:
            fail(FailureCode.INCOMPLETE_RESULT, "A-share loader produced no PIT-visible rows")
        if not all(isinstance(row, AShareSnapshotRow) for row in self.rows):
            fail(FailureCode.INVALID_VALUE, "load result contains invalid rows")
        if isinstance(self.excluded_rows, bool) or self.excluded_rows < 0:
            fail(FailureCode.INVALID_VALUE, "excluded_rows must be non-negative")


@dataclass(frozen=True, kw_only=True)
class FrozenAShareSnapshot(CanonicalContract):
    request: AShareDataRequest
    dataset_card: DatasetCard
    snapshot_hash: str
    rows: tuple[AShareSnapshotRow, ...]
    historical_universe: Mapping[str, tuple[str, ...]]
    coverage_by_field: Mapping[str, float]
    availability_policy: AvailabilityPolicy
    provider: str
    fetched_at: datetime
    excluded_rows: int
    trading_calendar: FrozenTradingCalendar | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.request, AShareDataRequest):
            fail(FailureCode.INVALID_VALUE, "snapshot requires AShareDataRequest")
        if not isinstance(self.dataset_card, DatasetCard):
            fail(FailureCode.INVALID_VALUE, "snapshot requires DatasetCard")
        require_hash(self.snapshot_hash, "snapshot_hash")
        if self.dataset_card.snapshot_hash != self.snapshot_hash:
            fail(FailureCode.INVALID_HASH, "DatasetCard does not bind snapshot hash")
        if not self.rows or any(row.available_at > self.request.decision_time for row in self.rows):
            fail(FailureCode.INVALID_TIME_ORDER, "snapshot contains unavailable/future rows")
        object.__setattr__(self, "historical_universe", freeze_universe_mapping(self.historical_universe))
        coverage = freeze_mapping(self.coverage_by_field, "coverage_by_field", numeric_values=True)
        if any(value < 0.0 or value > 1.0 for value in coverage.values()):
            fail(FailureCode.INVALID_VALUE, "coverage must lie in [0,1]")
        object.__setattr__(self, "coverage_by_field", coverage)
        if not isinstance(self.availability_policy, AvailabilityPolicy):
            fail(FailureCode.INVALID_VALUE, "snapshot requires AvailabilityPolicy")
        if self.trading_calendar is not None and not isinstance(
            self.trading_calendar, FrozenTradingCalendar
        ):
            fail(FailureCode.INVALID_VALUE, "snapshot requires FrozenTradingCalendar")
        require_identifier(self.provider, "provider")
        require_aware_datetime(self.fetched_at, "fetched_at")

