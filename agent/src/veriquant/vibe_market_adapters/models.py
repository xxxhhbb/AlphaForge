"""Frozen provider records for Vibe market research adapter output."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping

from ..research_tool_contracts import (
    ResearchToolContractError,
    ResearchToolFailureCode,
    content_hash,
)


def _raise(code: ResearchToolFailureCode, message: str) -> None:
    raise ResearchToolContractError(code, message)


def _ensure_tz(value: datetime, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        _raise(ResearchToolFailureCode.INVALID_TIMESTAMP, f"{field_name} must be timezone-aware")
    return value


def normalize_datetime(value: Any, field_name: str) -> datetime:
    if value is None:
        _raise(ResearchToolFailureCode.MISSING_AVAILABLE_AT, "provider record is missing time semantics")
    if isinstance(value, datetime):
        return _ensure_tz(value, field_name)
    if not isinstance(value, str) or not value.strip():
        _raise(ResearchToolFailureCode.MISSING_AVAILABLE_AT, "provider record is missing time semantics")
    raw = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise ResearchToolContractError(
            ResearchToolFailureCode.INVALID_TIMESTAMP,
            f"{field_name} must be ISO-8601",
        ) from exc
    return _ensure_tz(parsed, field_name)


def canonical_time(value: datetime) -> str:
    rendered = _ensure_tz(value, "datetime").astimezone(timezone.utc).isoformat(timespec="microseconds")
    return rendered[:-6] + "Z"


@dataclass(frozen=True, kw_only=True)
class FrozenMarketRecord:
    """One provider row after it has been frozen and hash-addressed."""

    record_id: str
    provider: str
    source_class: str
    event_time: datetime
    published_at: datetime
    available_at: datetime
    retrieved_at: datetime
    payload: Mapping[str, Any]
    source_payload: Mapping[str, Any] = field(default_factory=dict)
    manifest_hash: str | None = None

    def __post_init__(self) -> None:
        for field_name in ("event_time", "published_at", "available_at", "retrieved_at"):
            _ensure_tz(getattr(self, field_name), field_name)
        if self.event_time > self.published_at or self.published_at > self.available_at:
            _raise(ResearchToolFailureCode.INVALID_TIME_ORDER, "provider record time order is invalid")
        if self.available_at > self.retrieved_at:
            _raise(ResearchToolFailureCode.INVALID_TIME_ORDER, "provider record retrieved_at precedes available_at")
        if not isinstance(self.payload, Mapping):
            _raise(ResearchToolFailureCode.INVALID_PAYLOAD, "provider payload must be a mapping")
        if not isinstance(self.source_payload, Mapping):
            _raise(ResearchToolFailureCode.INVALID_PAYLOAD, "source_payload must be a mapping")

    @property
    def source_hash(self) -> str:
        return content_hash(self.source_payload or self.payload)

    @property
    def payload_hash(self) -> str:
        return content_hash(self.payload)


@dataclass(frozen=True, kw_only=True)
class FrozenMarketResponse:
    """Provider response with all rows frozen before desk-visible conversion."""

    provider: str
    source_class: str
    records: tuple[FrozenMarketRecord, ...]
    manifest: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.manifest, Mapping):
            _raise(ResearchToolFailureCode.INVALID_PAYLOAD, "manifest must be a mapping")
        for record in self.records:
            if not isinstance(record, FrozenMarketRecord):
                _raise(ResearchToolFailureCode.INVALID_PAYLOAD, "records must be FrozenMarketRecord")

    @property
    def manifest_hash(self) -> str:
        return content_hash(self.manifest)
