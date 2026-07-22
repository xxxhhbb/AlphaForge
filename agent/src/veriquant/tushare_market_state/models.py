"""Typed, fail-closed contracts for frozen Tushare market-state metadata."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping

from ..contracts import CanonicalContract, FailureCode
from ..contracts.base import (
    fail,
    require_aware_datetime,
    require_finite_number,
    require_hash,
    require_identifier,
    require_text,
    require_time_order,
)


SCHEMA_VERSION = "veriquant.tushare-market-state/v1"
SOURCE = "TUSHARE_COMPATIBLE_PROXY_SDK"
AUTHORITY_CLASS = "AUXILIARY_RESEARCH_EVIDENCE"
_ASHARE_CODE = re.compile(r"^[0-9]{6}\.(?:SH|SZ)$")
_SAFE_DATASET_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_ALLOWED_APIS = frozenset({"stock_basic", "suspend_d", "stk_limit", "adj_factor"})


class MarketStateFailureCode(str, Enum):
    INVALID_REQUEST = "INVALID_REQUEST"
    API_OUTSIDE_ALLOWLIST = "API_OUTSIDE_ALLOWLIST"
    SDK_FAILURE = "SDK_FAILURE"
    INCOMPLETE_METADATA = "INCOMPLETE_METADATA"
    INVALID_RECORD = "INVALID_RECORD"
    FUTURE_DATA = "FUTURE_DATA"
    HASH_MISMATCH = "HASH_MISMATCH"
    PATH_TRAVERSAL = "PATH_TRAVERSAL"
    SNAPSHOT_CORRUPT = "SNAPSHOT_CORRUPT"
    SECRET_MATERIAL = "SECRET_MATERIAL"


class MarketStateError(RuntimeError):
    """Sanitized package error; provider exception values are never retained."""

    def __init__(
        self,
        code: MarketStateFailureCode,
        message: str,
        *,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.details = MappingProxyType(dict(details or {}))


def market_state_fail(
    code: MarketStateFailureCode,
    message: str,
    **details: Any,
) -> None:
    raise MarketStateError(code, message, details=details)


def parse_date(value: str, field_name: str) -> date:
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise MarketStateError(
            MarketStateFailureCode.INVALID_REQUEST,
            f"{field_name} must be YYYY-MM-DD",
            details={"field": field_name},
        ) from exc


def require_code(value: str, field_name: str = "symbol") -> None:
    if not isinstance(value, str) or _ASHARE_CODE.fullmatch(value) is None:
        market_state_fail(
            MarketStateFailureCode.INVALID_REQUEST,
            f"{field_name} must be a six-digit SH/SZ code",
            field=field_name,
        )


def require_dataset_id(value: str) -> None:
    if not isinstance(value, str) or _SAFE_DATASET_ID.fullmatch(value) is None:
        market_state_fail(
            MarketStateFailureCode.PATH_TRAVERSAL,
            "dataset_id is not safe for content-addressed storage",
        )


def freeze_scalar_mapping(value: Mapping[str, Any], field_name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        market_state_fail(MarketStateFailureCode.INVALID_RECORD, f"{field_name} must be an object")
    output: dict[str, Any] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not key:
            market_state_fail(MarketStateFailureCode.INVALID_RECORD, f"{field_name} keys must be text")
        if item is not None and type(item) not in (str, int, float, bool):
            market_state_fail(MarketStateFailureCode.INVALID_RECORD, f"{field_name} values must be scalar")
        if isinstance(item, float):
            require_finite_number(item, f"{field_name}.{key}")
        output[key] = item
    return MappingProxyType(dict(sorted(output.items())))


@dataclass(frozen=True, kw_only=True)
class MarketStateInputRow(CanonicalContract):
    """One already-frozen bar row whose tradability state must be supplied."""

    SCHEMA_VERSION = SCHEMA_VERSION

    symbol: str
    trade_date: str
    event_time: datetime
    close: float

    def __post_init__(self) -> None:
        require_code(self.symbol)
        parsed = parse_date(self.trade_date, "trade_date")
        require_aware_datetime(self.event_time, "event_time")
        if self.event_time.date() != parsed:
            market_state_fail(MarketStateFailureCode.INVALID_REQUEST, "event_time date must equal trade_date")
        require_finite_number(self.close, "close")
        if self.close <= 0:
            market_state_fail(MarketStateFailureCode.INVALID_REQUEST, "close must be positive")


@dataclass(frozen=True, kw_only=True)
class MarketStateRequest(CanonicalContract):
    """Bounded request bound to the existing frozen price input and identities."""

    SCHEMA_VERSION = SCHEMA_VERSION

    dataset_id: str
    codes: tuple[str, ...]
    start_date: str
    end_date: str
    decision_time: datetime
    rows: tuple[MarketStateInputRow, ...]
    source_snapshot_hash: str
    calendar_hash: str
    universe_hash: str
    bar_capture_hash: str
    source: str = SOURCE
    max_codes: int = 3
    max_rows: int = 1_000
    max_span_days: int = 366

    def __post_init__(self) -> None:
        require_dataset_id(self.dataset_id)
        if self.source != SOURCE:
            market_state_fail(MarketStateFailureCode.INVALID_REQUEST, "source marker is immutable")
        if not isinstance(self.codes, tuple) or not self.codes:
            market_state_fail(MarketStateFailureCode.INVALID_REQUEST, "codes must be a non-empty tuple")
        if tuple(sorted(set(self.codes))) != self.codes:
            market_state_fail(MarketStateFailureCode.INVALID_REQUEST, "codes must be sorted and unique")
        for code in self.codes:
            require_code(code, "codes")
        for name, value, ceiling in (
            ("max_codes", self.max_codes, 3),
            ("max_rows", self.max_rows, 1_000),
            ("max_span_days", self.max_span_days, 366),
        ):
            if type(value) is not int or not 1 <= value <= ceiling:
                market_state_fail(MarketStateFailureCode.INVALID_REQUEST, f"{name} exceeds its immutable bound")
        if len(self.codes) > self.max_codes:
            market_state_fail(MarketStateFailureCode.INVALID_REQUEST, "code count exceeds max_codes")
        start = parse_date(self.start_date, "start_date")
        end = parse_date(self.end_date, "end_date")
        if start > end or (end - start).days + 1 > self.max_span_days:
            market_state_fail(MarketStateFailureCode.INVALID_REQUEST, "date range is reversed or unbounded")
        require_aware_datetime(self.decision_time, "decision_time")
        for name in ("source_snapshot_hash", "calendar_hash", "universe_hash", "bar_capture_hash"):
            require_hash(getattr(self, name), name)
        if not isinstance(self.rows, tuple) or not self.rows or len(self.rows) > self.max_rows:
            market_state_fail(MarketStateFailureCode.INVALID_REQUEST, "rows are empty or exceed max_rows")
        keys: list[tuple[str, str]] = []
        for row in self.rows:
            if not isinstance(row, MarketStateInputRow):
                market_state_fail(MarketStateFailureCode.INVALID_REQUEST, "rows must be typed")
            day = parse_date(row.trade_date, "trade_date")
            if row.symbol not in self.codes or day < start or day > end:
                market_state_fail(MarketStateFailureCode.INVALID_REQUEST, "row lies outside request scope")
            if row.event_time > self.decision_time:
                market_state_fail(MarketStateFailureCode.FUTURE_DATA, "row event_time follows decision_time")
            keys.append((row.symbol, row.trade_date))
        if keys != sorted(keys) or len(set(keys)) != len(keys):
            market_state_fail(MarketStateFailureCode.INVALID_REQUEST, "rows must be sorted and unique")
        if set(self.codes) != {row.symbol for row in self.rows}:
            market_state_fail(MarketStateFailureCode.INCOMPLETE_METADATA, "rows must cover every code")


@dataclass(frozen=True, kw_only=True)
class SdkCallCapture(CanonicalContract):
    """Canonical response from one fixed SDK operation."""

    SCHEMA_VERSION = SCHEMA_VERSION

    api_name: str
    params: Mapping[str, Any]
    rows: tuple[Mapping[str, Any], ...]

    def __post_init__(self) -> None:
        if self.api_name not in _ALLOWED_APIS:
            market_state_fail(MarketStateFailureCode.API_OUTSIDE_ALLOWLIST, "SDK API is outside allowlist")
        object.__setattr__(self, "params", freeze_scalar_mapping(self.params, "params"))
        if not isinstance(self.rows, tuple):
            market_state_fail(MarketStateFailureCode.INVALID_RECORD, "captured rows must be a tuple")
        object.__setattr__(
            self,
            "rows",
            tuple(freeze_scalar_mapping(row, "rows") for row in self.rows),
        )


@dataclass(frozen=True, kw_only=True)
class MarketStateCapture(CanonicalContract):
    """Complete fixed-call capture; it contains no credentials or endpoint URL."""

    SCHEMA_VERSION = SCHEMA_VERSION

    request_hash: str
    calls: tuple[SdkCallCapture, ...]
    source: str = SOURCE

    def __post_init__(self) -> None:
        require_hash(self.request_hash, "request_hash")
        if self.source != SOURCE:
            market_state_fail(MarketStateFailureCode.INVALID_RECORD, "capture source marker changed")
        if not isinstance(self.calls, tuple) or not self.calls:
            market_state_fail(MarketStateFailureCode.INCOMPLETE_METADATA, "SDK capture is empty")
        if any(not isinstance(call, SdkCallCapture) for call in self.calls):
            market_state_fail(MarketStateFailureCode.INVALID_RECORD, "capture calls must be typed")


@dataclass(frozen=True, kw_only=True)
class NormalizedMarketStateRow(CanonicalContract):
    """Row-level metadata ready for deterministic RowMarketState conversion."""

    SCHEMA_VERSION = SCHEMA_VERSION

    symbol: str
    trade_date: str
    event_time: datetime
    listed_at: datetime
    published_at: datetime
    available_at: datetime
    revision: str
    suspended: bool
    delisted: bool
    limit_up: bool
    limit_down: bool
    up_limit: float
    down_limit: float
    adj_factor: float

    def __post_init__(self) -> None:
        require_code(self.symbol)
        if self.event_time.date() != parse_date(self.trade_date, "trade_date"):
            market_state_fail(MarketStateFailureCode.INVALID_RECORD, "event_time date must equal trade_date")
        require_aware_datetime(self.listed_at, "listed_at")
        require_time_order(
            ("event_time", self.event_time),
            ("published_at", self.published_at),
            ("available_at", self.available_at),
        )
        if self.listed_at > self.event_time:
            market_state_fail(MarketStateFailureCode.INVALID_RECORD, "row precedes listing date")
        require_hash(self.revision, "revision")
        for name in ("suspended", "delisted", "limit_up", "limit_down"):
            if type(getattr(self, name)) is not bool:
                fail(FailureCode.INVALID_VALUE, f"{name} must be boolean")
        for name in ("up_limit", "down_limit", "adj_factor"):
            require_finite_number(getattr(self, name), name)
            if getattr(self, name) <= 0:
                market_state_fail(MarketStateFailureCode.INVALID_RECORD, f"{name} must be positive")
        if self.down_limit >= self.up_limit or (self.limit_up and self.limit_down):
            market_state_fail(MarketStateFailureCode.INVALID_RECORD, "price-limit state is inconsistent")

    def to_row_market_state(self):
        """Convert without adding a new public import to existing package initializers."""

        from ..real_research.reader import RowMarketState

        return RowMarketState(
            symbol=self.symbol,
            event_time=self.event_time,
            listed_at=self.listed_at,
            published_at=self.published_at,
            available_at=self.available_at,
            revision=self.revision,
            suspended=self.suspended,
            delisted=self.delisted,
            limit_up=self.limit_up,
            limit_down=self.limit_down,
        )


@dataclass(frozen=True, kw_only=True)
class FrozenMarketStateManifest(CanonicalContract):
    """Identity binding for one immutable normalized market-state vintage."""

    SCHEMA_VERSION = SCHEMA_VERSION

    dataset_id: str
    request_hash: str
    provider_capture_hash: str
    source_snapshot_hash: str
    calendar_hash: str
    universe_hash: str
    bar_capture_hash: str
    records_hash: str
    vintage_hash: str
    revision: str
    decision_time: datetime
    row_count: int
    api_names: tuple[str, ...]
    authority_class: str = AUTHORITY_CLASS
    source: str = SOURCE
    promotion_eligible: bool = False
    official_v1_tushare_smoke: bool = False
    historical_availability_attested: bool = False

    def __post_init__(self) -> None:
        require_dataset_id(self.dataset_id)
        for name in (
            "request_hash",
            "provider_capture_hash",
            "source_snapshot_hash",
            "calendar_hash",
            "universe_hash",
            "bar_capture_hash",
            "records_hash",
            "vintage_hash",
            "revision",
        ):
            require_hash(getattr(self, name), name)
        require_aware_datetime(self.decision_time, "decision_time")
        if type(self.row_count) is not int or self.row_count <= 0:
            market_state_fail(MarketStateFailureCode.INCOMPLETE_METADATA, "row_count must be positive")
        if self.api_names != tuple(sorted(_ALLOWED_APIS)):
            market_state_fail(MarketStateFailureCode.INCOMPLETE_METADATA, "all required SDK APIs must be captured")
        if self.authority_class != AUTHORITY_CLASS or self.source != SOURCE:
            market_state_fail(MarketStateFailureCode.INVALID_RECORD, "authority/source marker changed")
        if self.promotion_eligible or self.official_v1_tushare_smoke or self.historical_availability_attested:
            market_state_fail(
                MarketStateFailureCode.INVALID_RECORD,
                "compatible-proxy market state cannot claim official or attested status",
            )


@dataclass(frozen=True, kw_only=True)
class FrozenMarketStateHandle(CanonicalContract):
    SCHEMA_VERSION = SCHEMA_VERSION

    dataset_id: str
    vintage_hash: str
    manifest_hash: str
    provider_capture_hash: str
    records_hash: str
    manifest_path: str

    def __post_init__(self) -> None:
        require_dataset_id(self.dataset_id)
        for name in ("vintage_hash", "manifest_hash", "provider_capture_hash", "records_hash"):
            require_hash(getattr(self, name), name)
        require_text(self.manifest_path, "manifest_path")


@dataclass(frozen=True, kw_only=True)
class FrozenMarketStateBundle:
    manifest: FrozenMarketStateManifest
    capture: MarketStateCapture
    rows: tuple[NormalizedMarketStateRow, ...]
    handle: FrozenMarketStateHandle

    def __post_init__(self) -> None:
        if not self.rows:
            market_state_fail(MarketStateFailureCode.INCOMPLETE_METADATA, "frozen bundle has no rows")
        if self.capture.artifact_hash != self.manifest.provider_capture_hash:
            market_state_fail(MarketStateFailureCode.HASH_MISMATCH, "capture hash is not bound")
        if self.handle.vintage_hash != self.manifest.vintage_hash:
            market_state_fail(MarketStateFailureCode.HASH_MISMATCH, "handle vintage hash is not bound")
