"""Typed contracts for the Tencent A-share operational rehearsal.

This package deliberately models Tencent data as an auxiliary operational
input.  It is not a replacement for the frozen Tushare V1 acceptance source.
"""

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
    require_text,
)


SOURCE_MARKER = "TENCENT_OPERATIONAL_REHEARSAL"
UNIVERSE_POLICY = "FIXED_OPERATIONAL_SAMPLE"
AUXILIARY_AUTHORITY = "AUXILIARY_RESEARCH_EVIDENCE"
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_ASHARE_CODE = re.compile(r"^[0-9]{6}\.(?:SH|SZ)$")


class TencentRehearsalFailureCode(str, Enum):
    NETWORK_DISABLED = "NETWORK_DISABLED"
    INVALID_REQUEST = "INVALID_REQUEST"
    PATH_TRAVERSAL = "PATH_TRAVERSAL"
    CALENDAR_MISSING = "CALENDAR_MISSING"
    CALENDAR_INVALID = "CALENDAR_INVALID"
    TRANSPORT_UNAVAILABLE = "TRANSPORT_UNAVAILABLE"
    FETCH_FAILED = "FETCH_FAILED"
    EMPTY_RESULT = "EMPTY_RESULT"
    ROW_LIMIT_EXCEEDED = "ROW_LIMIT_EXCEEDED"
    INVALID_RECORD = "INVALID_RECORD"
    SECRET_MATERIAL = "SECRET_MATERIAL"
    HASH_MISMATCH = "HASH_MISMATCH"
    SNAPSHOT_CORRUPT = "SNAPSHOT_CORRUPT"
    PIT_INELIGIBLE = "PIT_INELIGIBLE"


class TencentRehearsalError(RuntimeError):
    """Typed, sanitized failure raised at the rehearsal boundary."""

    def __init__(
        self,
        code: TencentRehearsalFailureCode,
        message: str,
        *,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.details = MappingProxyType(dict(details or {}))


def rehearsal_fail(
    code: TencentRehearsalFailureCode,
    message: str,
    **details: Any,
) -> None:
    raise TencentRehearsalError(code, message, details=details)


def require_safe_id(value: str, field_name: str) -> None:
    if not isinstance(value, str) or _SAFE_ID.fullmatch(value) is None:
        rehearsal_fail(
            TencentRehearsalFailureCode.PATH_TRAVERSAL,
            f"{field_name} is not a safe artifact identifier",
            field=field_name,
        )


def _parse_date(value: str, field_name: str) -> date:
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise TencentRehearsalError(
            TencentRehearsalFailureCode.INVALID_REQUEST,
            f"{field_name} must be YYYY-MM-DD",
            details={"field": field_name},
        ) from exc


class AdjustmentSafetyVerdict(str, Enum):
    RAW_VERIFIED = "RAW_VERIFIED"
    QFQ_ONLY = "QFQ_ONLY"
    UNKNOWN = "UNKNOWN"

    @property
    def may_be_pit_eligible(self) -> bool:
        return self is AdjustmentSafetyVerdict.RAW_VERIFIED


class DataAvailabilityStatus(str, Enum):
    AVAILABLE = "AVAILABLE"
    OFFLINE_DISABLED = "OFFLINE_DISABLED"
    UNAVAILABLE = "UNAVAILABLE"
    INVALID = "INVALID"


@dataclass(frozen=True, kw_only=True)
class TencentBoundedRequest(CanonicalContract):
    dataset_id: str
    request_id: str
    codes: tuple[str, ...]
    start_date: str
    end_date: str
    calendar_id: str
    calendar_hash: str
    interval: str = "1D"
    max_codes: int = 10
    max_rows_per_code: int = 500
    max_total_rows: int = 5_000
    max_span_days: int = 366
    source: str = SOURCE_MARKER
    universe_policy: str = UNIVERSE_POLICY

    def __post_init__(self) -> None:
        require_safe_id(self.dataset_id, "dataset_id")
        require_safe_id(self.request_id, "request_id")
        require_safe_id(self.calendar_id, "calendar_id")
        require_hash(self.calendar_hash, "calendar_hash")
        if self.source != SOURCE_MARKER or self.universe_policy != UNIVERSE_POLICY:
            rehearsal_fail(
                TencentRehearsalFailureCode.INVALID_REQUEST,
                "source and universe policy are immutable for this rehearsal",
            )
        if self.interval != "1D":
            rehearsal_fail(
                TencentRehearsalFailureCode.INVALID_REQUEST,
                "only bounded daily bars are supported",
            )
        if not isinstance(self.codes, tuple) or not self.codes:
            rehearsal_fail(
                TencentRehearsalFailureCode.INVALID_REQUEST,
                "codes must be a non-empty tuple",
            )
        if tuple(sorted(set(self.codes))) != self.codes:
            rehearsal_fail(
                TencentRehearsalFailureCode.INVALID_REQUEST,
                "codes must be sorted and unique",
            )
        if any(_ASHARE_CODE.fullmatch(code) is None for code in self.codes):
            rehearsal_fail(
                TencentRehearsalFailureCode.INVALID_REQUEST,
                "only six-digit SH/SZ symbols are permitted",
            )
        for name, value, ceiling in (
            ("max_codes", self.max_codes, 10),
            ("max_rows_per_code", self.max_rows_per_code, 500),
            ("max_total_rows", self.max_total_rows, 5_000),
            ("max_span_days", self.max_span_days, 366),
        ):
            if type(value) is not int or not 1 <= value <= ceiling:
                rehearsal_fail(
                    TencentRehearsalFailureCode.INVALID_REQUEST,
                    f"{name} exceeds the immutable rehearsal bound",
                    field=name,
                )
        if len(self.codes) > self.max_codes:
            rehearsal_fail(
                TencentRehearsalFailureCode.INVALID_REQUEST,
                "code count exceeds max_codes",
            )
        start = _parse_date(self.start_date, "start_date")
        end = _parse_date(self.end_date, "end_date")
        if start > end or (end - start).days + 1 > self.max_span_days:
            rehearsal_fail(
                TencentRehearsalFailureCode.INVALID_REQUEST,
                "date range is reversed or exceeds max_span_days",
            )


@dataclass(frozen=True, kw_only=True)
class TencentBar(CanonicalContract):
    code: str
    trade_date: str
    available_at: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float

    def __post_init__(self) -> None:
        if _ASHARE_CODE.fullmatch(self.code) is None:
            rehearsal_fail(
                TencentRehearsalFailureCode.INVALID_RECORD,
                "record symbol is not a supported SH/SZ code",
            )
        trade_date = _parse_date(self.trade_date, "trade_date")
        require_aware_datetime(self.available_at, "available_at")
        if self.available_at.date() < trade_date:
            rehearsal_fail(
                TencentRehearsalFailureCode.INVALID_RECORD,
                "available_at precedes trade_date",
            )
        for name in ("open", "high", "low", "close", "volume"):
            require_finite_number(getattr(self, name), name)
        if min(self.open, self.high, self.low, self.close) <= 0 or self.volume < 0:
            rehearsal_fail(
                TencentRehearsalFailureCode.INVALID_RECORD,
                "prices must be positive and volume non-negative",
            )
        if self.high < max(self.open, self.low, self.close) or self.low > min(
            self.open, self.high, self.close
        ):
            rehearsal_fail(
                TencentRehearsalFailureCode.INVALID_RECORD,
                "OHLC invariants are violated",
            )


@dataclass(frozen=True, kw_only=True)
class TencentFetchPayload:
    raw_bytes: bytes
    records: tuple[TencentBar, ...]
    adjustment_verdict: AdjustmentSafetyVerdict
    raw_capture_kind: str
    availability_semantics_verified: bool
    adjustment_attestation_hash: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.raw_bytes, bytes) or not self.raw_bytes:
            rehearsal_fail(
                TencentRehearsalFailureCode.EMPTY_RESULT,
                "transport returned no raw content",
            )
        if not isinstance(self.records, tuple) or not self.records:
            rehearsal_fail(
                TencentRehearsalFailureCode.EMPTY_RESULT,
                "transport returned no records",
            )
        require_text(self.raw_capture_kind, "raw_capture_kind")
        if type(self.availability_semantics_verified) is not bool:
            fail(FailureCode.INVALID_VALUE, "availability_semantics_verified must be boolean")
        if self.adjustment_verdict is AdjustmentSafetyVerdict.RAW_VERIFIED:
            if self.adjustment_attestation_hash is None:
                rehearsal_fail(
                    TencentRehearsalFailureCode.INVALID_RECORD,
                    "RAW_VERIFIED requires a content-addressed adjustment attestation",
                )
            require_hash(self.adjustment_attestation_hash, "adjustment_attestation_hash")
        elif self.adjustment_attestation_hash is not None:
            require_hash(self.adjustment_attestation_hash, "adjustment_attestation_hash")


@dataclass(frozen=True, kw_only=True)
class TencentSnapshotManifest(CanonicalContract):
    request: TencentBoundedRequest
    request_hash: str
    fetched_at: datetime
    frozen_at: datetime
    raw_content_hash: str
    normalized_content_hash: str
    calendar_hash: str
    row_count: int
    rows_by_code: Mapping[str, int]
    adjustment_verdict: AdjustmentSafetyVerdict
    adjustment_attestation_hash: str | None
    raw_capture_kind: str
    availability_semantics_verified: bool
    pit_eligible: bool
    authority_class: str = AUXILIARY_AUTHORITY
    source: str = SOURCE_MARKER
    universe_policy: str = UNIVERSE_POLICY
    historical_universe: bool = False
    promotion_eligible: bool = False
    official_v1_tushare_smoke: bool = False
    raw_path: str = "raw.bin"
    records_path: str = "records.json"

    def __post_init__(self) -> None:
        if not isinstance(self.request, TencentBoundedRequest):
            fail(FailureCode.INVALID_VALUE, "request must be typed")
        for name in ("request_hash", "raw_content_hash", "normalized_content_hash", "calendar_hash"):
            require_hash(getattr(self, name), name)
        require_aware_datetime(self.fetched_at, "fetched_at")
        require_aware_datetime(self.frozen_at, "frozen_at")
        if self.fetched_at > self.frozen_at:
            rehearsal_fail(
                TencentRehearsalFailureCode.INVALID_RECORD,
                "fetched_at must not follow frozen_at",
            )
        if self.request_hash != self.request.artifact_hash:
            rehearsal_fail(TencentRehearsalFailureCode.HASH_MISMATCH, "request hash mismatch")
        if self.calendar_hash != self.request.calendar_hash:
            rehearsal_fail(TencentRehearsalFailureCode.HASH_MISMATCH, "calendar hash mismatch")
        if type(self.row_count) is not int or self.row_count <= 0:
            rehearsal_fail(TencentRehearsalFailureCode.EMPTY_RESULT, "row_count must be positive")
        counts = dict(self.rows_by_code)
        if set(counts) != set(self.request.codes) or any(type(v) is not int or v <= 0 for v in counts.values()):
            rehearsal_fail(
                TencentRehearsalFailureCode.INVALID_RECORD,
                "rows_by_code must cover every fixed-sample symbol",
            )
        if sum(counts.values()) != self.row_count:
            rehearsal_fail(TencentRehearsalFailureCode.HASH_MISMATCH, "row count mismatch")
        object.__setattr__(self, "rows_by_code", MappingProxyType(dict(sorted(counts.items()))))
        if self.source != SOURCE_MARKER or self.universe_policy != UNIVERSE_POLICY:
            rehearsal_fail(TencentRehearsalFailureCode.INVALID_RECORD, "immutable source policy changed")
        if self.authority_class != AUXILIARY_AUTHORITY:
            rehearsal_fail(
                TencentRehearsalFailureCode.INVALID_RECORD,
                "Tencent rehearsal artifacts must remain auxiliary",
            )
        if self.historical_universe or self.promotion_eligible or self.official_v1_tushare_smoke:
            rehearsal_fail(
                TencentRehearsalFailureCode.INVALID_RECORD,
                "fixed-sample Tencent artifacts cannot claim historical universe, promotion, or Tushare smoke",
            )
        expected_pit = (
            self.adjustment_verdict is AdjustmentSafetyVerdict.RAW_VERIFIED
            and self.availability_semantics_verified
            and self.adjustment_attestation_hash is not None
        )
        if self.pit_eligible != expected_pit:
            rehearsal_fail(
                TencentRehearsalFailureCode.PIT_INELIGIBLE,
                "PIT eligibility does not match adjustment and availability proof",
            )
        for path_value in (self.raw_path, self.records_path):
            if path_value not in {"raw.bin", "records.json"}:
                rehearsal_fail(TencentRehearsalFailureCode.PATH_TRAVERSAL, "artifact filename is invalid")


@dataclass(frozen=True, kw_only=True)
class TencentSnapshotHandle(CanonicalContract):
    dataset_id: str
    snapshot_id: str
    manifest_hash: str
    raw_content_hash: str
    normalized_content_hash: str
    calendar_hash: str
    manifest_path: str

    def __post_init__(self) -> None:
        require_safe_id(self.dataset_id, "dataset_id")
        if not re.fullmatch(r"[0-9a-f]{64}", self.snapshot_id):
            rehearsal_fail(TencentRehearsalFailureCode.PATH_TRAVERSAL, "snapshot_id is invalid")
        for name in ("manifest_hash", "raw_content_hash", "normalized_content_hash", "calendar_hash"):
            require_hash(getattr(self, name), name)
        require_text(self.manifest_path, "manifest_path")


@dataclass(frozen=True, kw_only=True)
class TencentMarketVintage(CanonicalContract):
    dataset_id: str
    source_snapshot_hash: str
    decision_time: datetime
    calendar_hash: str
    adjustment_verdict: AdjustmentSafetyVerdict
    pit_eligible: bool
    rows: tuple[TencentBar, ...]
    vintage_hash: str
    authority_class: str = AUXILIARY_AUTHORITY
    source: str = SOURCE_MARKER

    def __post_init__(self) -> None:
        require_safe_id(self.dataset_id, "dataset_id")
        require_hash(self.source_snapshot_hash, "source_snapshot_hash")
        require_hash(self.calendar_hash, "calendar_hash")
        require_hash(self.vintage_hash, "vintage_hash")
        require_aware_datetime(self.decision_time, "decision_time")
        if not isinstance(self.rows, tuple) or not self.rows:
            rehearsal_fail(TencentRehearsalFailureCode.EMPTY_RESULT, "vintage has no visible rows")
        if any(row.available_at > self.decision_time for row in self.rows):
            rehearsal_fail(TencentRehearsalFailureCode.INVALID_RECORD, "future availability leaked into vintage")
        if self.adjustment_verdict is not AdjustmentSafetyVerdict.RAW_VERIFIED and self.pit_eligible:
            rehearsal_fail(TencentRehearsalFailureCode.PIT_INELIGIBLE, "adjusted data cannot be PIT eligible")
        if self.authority_class != AUXILIARY_AUTHORITY or self.source != SOURCE_MARKER:
            rehearsal_fail(TencentRehearsalFailureCode.INVALID_RECORD, "vintage authority changed")


@dataclass(frozen=True, kw_only=True)
class TencentDataAvailability(CanonicalContract):
    status: DataAvailabilityStatus
    network_enabled: bool
    transport_available: bool
    message: str
    request_hash: str | None = None
    snapshot_hash: str | None = None
    failure_code: TencentRehearsalFailureCode | None = None
    source: str = SOURCE_MARKER

    def __post_init__(self) -> None:
        for name in ("network_enabled", "transport_available"):
            if type(getattr(self, name)) is not bool:
                fail(FailureCode.INVALID_VALUE, f"{name} must be boolean")
        require_text(self.message, "message")
        for name in ("request_hash", "snapshot_hash"):
            value = getattr(self, name)
            if value is not None:
                require_hash(value, name)
        if self.source != SOURCE_MARKER:
            rehearsal_fail(TencentRehearsalFailureCode.INVALID_RECORD, "availability source changed")
