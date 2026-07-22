"""Immutable contracts for source-aware, bitemporal research features."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field, fields, is_dataclass
from datetime import date, datetime, timezone
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping


class BitemporalError(ValueError):
    """Raised when data cannot safely enter a point-in-time feature view."""


class EvidenceEligibility(str, Enum):
    FEATURE_ELIGIBLE = "FEATURE_ELIGIBLE"
    AUXILIARY_ONLY = "AUXILIARY_ONLY"


class UnknownTimePolicy(str, Enum):
    AUXILIARY_ONLY = "AUXILIARY_ONLY"
    FAIL_CLOSED = "FAIL_CLOSED"


class SourceKind(str, Enum):
    DAILY = "daily"
    DAILY_BASIC = "daily_basic"
    ADJ_FACTOR = "adj_factor"
    INCOME = "income"
    BALANCESHEET = "balancesheet"
    CASHFLOW = "cashflow"
    FINA_INDICATOR = "fina_indicator"
    FORECAST = "forecast"
    EXPRESS = "express"
    MONEYFLOW = "moneyflow"
    INDEX_DAILY = "index_daily"
    SHIBOR = "shibor"


def _aware(value: datetime, name: str) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise BitemporalError(f"{name} must be timezone-aware")


def _freeze_mapping(value: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise BitemporalError(f"{name} must be a mapping")
    copied: dict[str, Any] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not key:
            raise BitemporalError(f"{name} keys must be non-empty strings")
        if isinstance(item, float) and not math.isfinite(item):
            raise BitemporalError(f"{name}.{key} must be finite")
        if not (item is None or isinstance(item, (str, int, float, bool))):
            raise BitemporalError(f"{name}.{key} has unsupported type")
        copied[key] = item
    return MappingProxyType(dict(sorted(copied.items())))


def _canonical(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        _aware(value, "datetime")
        rendered = value.astimezone(timezone.utc).isoformat(timespec="microseconds")
        return rendered[:-6] + "Z"
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, float):
        if not math.isfinite(value):
            raise BitemporalError("canonical values must be finite")
        return 0.0 if value == 0.0 else value
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, Mapping):
        return {key: _canonical(value[key]) for key in sorted(value)}
    if isinstance(value, (tuple, list)):
        return [_canonical(item) for item in value]
    if is_dataclass(value):
        return {item.name: _canonical(getattr(value, item.name)) for item in fields(value)}
    raise BitemporalError(f"unsupported canonical value: {type(value).__name__}")


def canonical_hash(value: Any) -> str:
    payload = json.dumps(
        _canonical(value),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True, kw_only=True)
class NormalizedRecord:
    """One immutable source revision.

    ``event_time`` is when the economic event belongs, while ``published_at``
    and ``available_at`` are knowledge times. ``decision_time`` is populated
    only when a record is projected into a decision-specific view.
    """

    source: SourceKind
    entity_id: str
    event_time: datetime
    published_at: datetime
    available_at: datetime
    revision_id: str
    values: Mapping[str, str | int | float | bool | None]
    raw_hash: str
    decision_time: datetime | None = None
    eligibility: EvidenceEligibility = EvidenceEligibility.FEATURE_ELIGIBLE

    def __post_init__(self) -> None:
        if not isinstance(self.source, SourceKind):
            raise BitemporalError("source must be a registered SourceKind")
        if not isinstance(self.entity_id, str) or not self.entity_id.strip():
            raise BitemporalError("entity_id is required")
        if not isinstance(self.revision_id, str) or not self.revision_id.strip():
            raise BitemporalError("revision_id is required")
        for name in ("event_time", "published_at", "available_at"):
            _aware(getattr(self, name), name)
        if self.published_at > self.available_at:
            raise BitemporalError("published_at must not follow available_at")
        if self.decision_time is not None:
            _aware(self.decision_time, "decision_time")
            if self.available_at > self.decision_time:
                raise BitemporalError("record is unavailable at decision_time")
        if not isinstance(self.eligibility, EvidenceEligibility):
            raise BitemporalError("eligibility is invalid")
        if not isinstance(self.raw_hash, str) or not self.raw_hash.startswith("sha256:"):
            raise BitemporalError("raw_hash must be a canonical sha256 hash")
        object.__setattr__(self, "values", _freeze_mapping(self.values, "values"))

    @property
    def fact_key(self) -> tuple[str, str, datetime]:
        return (self.source.value, self.entity_id, self.event_time)

    @property
    def revision_key(self) -> tuple[str, str, datetime, str]:
        return (*self.fact_key, self.revision_id)

    @property
    def artifact_hash(self) -> str:
        return canonical_hash(self)


@dataclass(frozen=True, kw_only=True)
class NormalizationIssue:
    source_name: str
    row_number: int
    code: str
    message: str


@dataclass(frozen=True, kw_only=True)
class NormalizationBatch:
    source_name: str
    records: tuple[NormalizedRecord, ...]
    eligibility: EvidenceEligibility
    issues: tuple[NormalizationIssue, ...] = ()

    @property
    def artifact_hash(self) -> str:
        return canonical_hash(self)


@dataclass(frozen=True, kw_only=True)
class FeatureRow:
    entity_id: str
    event_time: datetime
    decision_time: datetime
    values: Mapping[str, int | float | None]
    revision_ids: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _aware(self.event_time, "event_time")
        _aware(self.decision_time, "decision_time")
        if self.event_time > self.decision_time:
            raise BitemporalError("feature event_time follows decision_time")
        if not self.entity_id:
            raise BitemporalError("feature entity_id is required")
        numeric: dict[str, int | float | None] = {}
        for key, value in self.values.items():
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, (int, float))
            ):
                raise BitemporalError(f"feature {key} must be numeric or null")
            if isinstance(value, float) and not math.isfinite(value):
                raise BitemporalError(f"feature {key} must be finite")
            numeric[key] = value
        object.__setattr__(self, "values", MappingProxyType(dict(sorted(numeric.items()))))
        object.__setattr__(
            self,
            "revision_ids",
            MappingProxyType(dict(sorted(self.revision_ids.items()))),
        )

    @property
    def artifact_hash(self) -> str:
        return canonical_hash(self)


@dataclass(frozen=True, kw_only=True)
class FactorFeatureView:
    """An inert numeric field view; it does not compile or execute FactorDAG."""

    decision_time: datetime
    fields: tuple[str, ...]
    rows: tuple[FeatureRow, ...]
    eligibility: EvidenceEligibility = EvidenceEligibility.FEATURE_ELIGIBLE

    def __post_init__(self) -> None:
        _aware(self.decision_time, "decision_time")
        if len(set(self.fields)) != len(self.fields) or any(not item for item in self.fields):
            raise BitemporalError("feature fields must be unique non-empty names")
        if tuple(sorted(self.rows, key=lambda row: (row.event_time, row.entity_id))) != self.rows:
            raise BitemporalError("feature rows must be canonically ordered")
        for row in self.rows:
            if row.decision_time != self.decision_time:
                raise BitemporalError("row decision_time does not match view")
            if tuple(row.values) != self.fields:
                raise BitemporalError("row fields do not match view fields")

    @property
    def artifact_hash(self) -> str:
        return canonical_hash(self)


def canonical_record_hash(records: tuple[NormalizedRecord, ...] | list[NormalizedRecord]) -> str:
    """Hash record content independently of ingestion order."""

    ordered = tuple(
        sorted(
            records,
            key=lambda item: (
                item.source.value,
                item.entity_id,
                item.event_time,
                item.available_at,
                item.published_at,
                item.revision_id,
                item.raw_hash,
            ),
        )
    )
    return canonical_hash(ordered)
