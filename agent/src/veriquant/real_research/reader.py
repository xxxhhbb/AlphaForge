"""Frozen snapshot reader protocol and offline in-memory implementation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import ClassVar, Mapping, Protocol, runtime_checkable

from veriquant.contracts import CanonicalContract, FailureCode
from veriquant.contracts.base import (
    fail,
    require_aware_datetime,
    require_hash,
    require_identifier,
    require_nonempty_tuple,
    require_text,
    require_time_order,
)
from veriquant.data import canonical_content_hash
from veriquant.data.ashare.models import FrozenAShareSnapshot

from .models import BRIDGE_SCHEMA_VERSION


@dataclass(frozen=True, kw_only=True)
class RowMarketState(CanonicalContract):
    """Tradability metadata required before a row can enter research."""

    SCHEMA_VERSION: ClassVar[str] = BRIDGE_SCHEMA_VERSION

    symbol: str
    event_time: datetime
    listed_at: datetime
    published_at: datetime
    available_at: datetime
    revision: str
    suspended: bool | None
    delisted: bool | None
    limit_up: bool | None
    limit_down: bool | None

    def __post_init__(self) -> None:
        require_identifier(self.symbol, "symbol")
        require_aware_datetime(self.event_time, "event_time")
        require_aware_datetime(self.listed_at, "listed_at")
        require_time_order(
            ("event_time", self.event_time),
            ("published_at", self.published_at),
            ("available_at", self.available_at),
        )
        require_text(self.revision, "revision")
        for name in ("suspended", "delisted", "limit_up", "limit_down"):
            if getattr(self, name) is not None and type(getattr(self, name)) is not bool:
                fail(FailureCode.INVALID_VALUE, f"{name} must be boolean or None")


@dataclass(frozen=True, kw_only=True)
class FrozenSnapshotValidationManifest(CanonicalContract):
    """Reader-side validation manifest for a Prompt 10 FrozenAShareSnapshot."""

    SCHEMA_VERSION: ClassVar[str] = BRIDGE_SCHEMA_VERSION

    snapshot_id: str
    content_hash: str
    source_snapshot_hash: str
    reader_schema_version: str
    trading_calendar: tuple[datetime, ...]
    trading_calendar_hash: str
    symbol_universe: tuple[str, ...]
    published_at: datetime
    available_at: datetime
    revision: str
    data_start: datetime
    data_end: datetime
    coverage_by_field: Mapping[str, float]

    def __post_init__(self) -> None:
        require_identifier(self.snapshot_id, "snapshot_id")
        require_hash(self.content_hash, "content_hash")
        require_hash(self.source_snapshot_hash, "source_snapshot_hash")
        require_text(self.reader_schema_version, "reader_schema_version")
        require_nonempty_tuple(self.trading_calendar, "trading_calendar")
        for timestamp in self.trading_calendar:
            require_aware_datetime(timestamp, "trading_calendar")
        require_hash(self.trading_calendar_hash, "trading_calendar_hash")
        require_nonempty_tuple(self.symbol_universe, "symbol_universe")
        if len(set(self.symbol_universe)) != len(self.symbol_universe):
            fail(FailureCode.INVALID_VALUE, "symbol_universe must be unique")
        for symbol in self.symbol_universe:
            require_identifier(symbol, "symbol_universe")
        require_time_order(
            ("data_start", self.data_start),
            ("published_at", self.published_at),
            ("available_at", self.available_at),
        )
        if self.data_start > self.data_end:
            fail(FailureCode.INVALID_TIME_ORDER, "data_start must not follow data_end")
        require_aware_datetime(self.data_end, "data_end")
        coverage = dict(self.coverage_by_field)
        for key, value in coverage.items():
            require_text(key, "coverage_by_field.key")
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0.0 or value > 1.0:
                fail(FailureCode.INVALID_VALUE, "coverage values must lie in [0,1]")
        object.__setattr__(self, "coverage_by_field", MappingProxyType(dict(sorted(coverage.items()))))


@dataclass(frozen=True, kw_only=True)
class FrozenSnapshotEnvelope(CanonicalContract):
    """A frozen snapshot plus required bridge metadata."""

    SCHEMA_VERSION: ClassVar[str] = BRIDGE_SCHEMA_VERSION

    manifest: FrozenSnapshotValidationManifest
    snapshot: FrozenAShareSnapshot
    row_market_state: tuple[RowMarketState, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.manifest, FrozenSnapshotValidationManifest):
            fail(FailureCode.INVALID_VALUE, "manifest must be FrozenSnapshotValidationManifest")
        if not isinstance(self.snapshot, FrozenAShareSnapshot):
            fail(FailureCode.INVALID_VALUE, "snapshot must be FrozenAShareSnapshot")
        if self.manifest.source_snapshot_hash != self.snapshot.snapshot_hash:
            fail(FailureCode.INVALID_HASH, "reader manifest does not bind FrozenAShareSnapshot hash")
        if self.manifest.content_hash != self.snapshot.snapshot_hash:
            fail(FailureCode.INVALID_HASH, "snapshot content hash mismatch")
        if not self.row_market_state or any(not isinstance(item, RowMarketState) for item in self.row_market_state):
            fail(FailureCode.INVALID_VALUE, "row_market_state is required")
        state_keys = {(item.symbol, item.event_time) for item in self.row_market_state}
        row_keys = {(row.symbol, row.event_time) for row in self.snapshot.rows}
        if not row_keys <= state_keys:
            fail(FailureCode.INCOMPLETE_RESULT, "snapshot rows are missing market-state metadata")


@runtime_checkable
class FrozenSnapshotReader(Protocol):
    """Read-only protocol for frozen A-share snapshots.

    Implementations must not download data, mutate the Prompt 10 snapshot store,
    expose secrets, or call pyqlib.
    """

    def read_snapshot(self, snapshot_id: str) -> FrozenSnapshotEnvelope: ...


class InMemoryFrozenSnapshotReader:
    """Deterministic fixture reader used by tests and offline development."""

    def __init__(self, snapshots: Mapping[str, FrozenSnapshotEnvelope]) -> None:
        if not isinstance(snapshots, Mapping) or not snapshots:
            fail(FailureCode.INVALID_VALUE, "snapshots mapping is required")
        self._snapshots = dict(snapshots)
        for key, envelope in self._snapshots.items():
            require_identifier(key, "snapshot_id")
            if not isinstance(envelope, FrozenSnapshotEnvelope):
                fail(FailureCode.INVALID_VALUE, "snapshots must contain FrozenSnapshotEnvelope values")
            if envelope.manifest.snapshot_id != key:
                fail(FailureCode.INVALID_IDENTIFIER, "snapshot mapping key does not match manifest")

    def read_snapshot(self, snapshot_id: str) -> FrozenSnapshotEnvelope:
        require_identifier(snapshot_id, "snapshot_id")
        try:
            return self._snapshots[snapshot_id]
        except KeyError as exc:
            raise ValueError(f"unknown frozen snapshot: {snapshot_id}") from exc


def build_reader_manifest(
    *,
    snapshot_id: str,
    snapshot: FrozenAShareSnapshot,
    trading_calendar: tuple[datetime, ...],
    row_market_state: tuple[RowMarketState, ...],
    revision: str,
    reader_schema_version: str = "frozen-ashare-reader/v1",
) -> FrozenSnapshotValidationManifest:
    """Create a deterministic validation manifest for a frozen snapshot."""

    require_identifier(snapshot_id, "snapshot_id")
    if not isinstance(snapshot, FrozenAShareSnapshot):
        fail(FailureCode.INVALID_VALUE, "snapshot must be FrozenAShareSnapshot")
    require_nonempty_tuple(trading_calendar, "trading_calendar")
    for timestamp in trading_calendar:
        require_aware_datetime(timestamp, "trading_calendar")
    if not row_market_state:
        fail(FailureCode.INVALID_VALUE, "row_market_state is required")
    require_text(revision, "revision")
    return FrozenSnapshotValidationManifest(
        snapshot_id=snapshot_id,
        content_hash=snapshot.snapshot_hash,
        source_snapshot_hash=snapshot.snapshot_hash,
        reader_schema_version=reader_schema_version,
        trading_calendar=tuple(sorted(trading_calendar)),
        trading_calendar_hash=canonical_content_hash(tuple(sorted(trading_calendar))),
        symbol_universe=tuple(sorted({row.symbol for row in snapshot.rows})),
        published_at=max(row.published_at for row in snapshot.rows),
        available_at=max(row.available_at for row in snapshot.rows),
        revision=revision,
        data_start=min(row.event_time for row in snapshot.rows),
        data_end=max(row.event_time for row in snapshot.rows),
        coverage_by_field=snapshot.coverage_by_field,
    )
