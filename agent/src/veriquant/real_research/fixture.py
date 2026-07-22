"""Offline deterministic A-share fixture for the real research bridge."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta
from types import MappingProxyType
from typing import ClassVar
from zoneinfo import ZoneInfo

from veriquant.contracts import Component, DatasetCard
from veriquant.data import canonical_content_hash
from veriquant.data.ashare.models import (
    AShareDataRequest,
    AShareSnapshotRow,
    AvailabilityPolicy,
    FrozenAShareSnapshot,
)

from .bridge import build_default_bridge_request
from .models import ResearchSplitManifest
from .reader import (
    FrozenSnapshotEnvelope,
    InMemoryFrozenSnapshotReader,
    RowMarketState,
    build_reader_manifest,
)


SHANGHAI = ZoneInfo("Asia/Shanghai")


@dataclass(frozen=True, kw_only=True)
class OfflineRealResearchFixture:
    snapshot_id: str
    reader: InMemoryFrozenSnapshotReader
    snapshot: FrozenAShareSnapshot
    envelope: FrozenSnapshotEnvelope
    split_manifest: ResearchSplitManifest
    decision_time: datetime
    feature_fields: tuple[str, ...]
    symbols: tuple[str, ...]
    calendar: tuple[datetime, ...]
    SCHEMA_VERSION: ClassVar[str] = "offline-real-research-fixture/v1"


def _business_sessions(start: datetime, count: int) -> tuple[datetime, ...]:
    sessions: list[datetime] = []
    cursor = start.date()
    while len(sessions) < count:
        if cursor.weekday() < 5:
            sessions.append(datetime.combine(cursor, time(15, 0), tzinfo=SHANGHAI))
        cursor += timedelta(days=1)
    return tuple(sessions)


def _next_open(calendar: tuple[datetime, ...], index: int) -> datetime:
    if index + 1 < len(calendar):
        next_day = calendar[index + 1].date()
    else:
        next_day = calendar[index].date() + timedelta(days=1)
        while next_day.weekday() >= 5:
            next_day += timedelta(days=1)
    return datetime.combine(next_day, time(9, 25), tzinfo=SHANGHAI)


def _base_values(symbol_index: int, day_index: int) -> dict[str, float]:
    close = 10.0 + symbol_index * 1.7 + day_index * 0.13 + ((symbol_index + day_index) % 3) * 0.03
    open_ = close * 0.997
    high = close * 1.012
    low = close * 0.989
    volume = 100000.0 + symbol_index * 9000.0 + day_index * 700.0
    amount = close * volume
    return {
        "open": round(open_, 4),
        "high": round(high, 4),
        "low": round(low, 4),
        "close": round(close, 4),
        "volume": round(volume, 4),
        "amount": round(amount, 4),
    }


def _historical_universe(
    *, calendar: tuple[datetime, ...], symbols: tuple[str, ...]
) -> MappingProxyType:
    mapping: dict[str, tuple[str, ...]] = {}
    for index, event_time in enumerate(calendar):
        members = list(symbols[:8])
        if index >= 9:
            members.append(symbols[9])
        if index >= 10:
            members.append(symbols[8])
        if index >= 13 and symbols[4] in members:
            members.remove(symbols[4])
        mapping[event_time.isoformat()] = tuple(sorted(members))
    return MappingProxyType(mapping)


def build_offline_real_research_fixture(
    *,
    snapshot_id: str = "fixture-ashare-real",
    revision: str = "r1",
    revision_close_delta: float = 0.7,
) -> OfflineRealResearchFixture:
    """Create a small frozen A-share fixture without Tushare, pyqlib, or network."""

    calendar = _business_sessions(datetime(2024, 1, 2, 15, 0, tzinfo=SHANGHAI), 18)
    symbols = tuple(f"00000{index}.SZ" for index in range(1, 10)) + ("000010.SZ",)
    fields = ("open", "high", "low", "close", "volume", "amount")
    feature_fields = ("close", "volume", "amount")
    freeze_decision = _next_open(calendar, len(calendar) - 1)
    request = AShareDataRequest(
        dataset_id="real-ashare-fixture",
        codes=symbols,
        index_code="000300.SH",
        start_time=calendar[0],
        end_time=calendar[-1],
        frequency="1D",
        adjustment="qfq",
        source="tushare",
        decision_time=freeze_decision,
        fields=fields,
        data_version=revision,
        cache_policy="freeze",
    )

    rows: list[AShareSnapshotRow] = []
    states: dict[tuple[str, datetime], RowMarketState] = {}
    for day_index, event_time in enumerate(calendar):
        available_at = _next_open(calendar, day_index)
        for symbol_index, symbol in enumerate(symbols):
            values = _base_values(symbol_index, day_index)
            if symbol == symbols[1] and day_index == 5:
                values = {**values, "amount": None}
            row = AShareSnapshotRow(
                symbol=symbol,
                event_time=event_time,
                published_at=event_time,
                available_at=available_at,
                values=MappingProxyType(values),
            )
            rows.append(row)
            listed_at = calendar[0] - timedelta(days=90)
            if symbol == symbols[8]:
                listed_at = calendar[10]
            states[(symbol, event_time)] = RowMarketState(
                symbol=symbol,
                event_time=event_time,
                listed_at=listed_at,
                published_at=event_time,
                available_at=available_at,
                revision=revision,
                suspended=True if symbol == symbols[2] and day_index == 6 else False,
                delisted=True if symbol == symbols[4] and day_index == 12 else False,
                limit_up=True if symbol == symbols[6] and day_index == 7 else False,
                limit_down=False,
            )
            if symbol == symbols[5] and day_index == 5:
                states[(symbol, event_time)] = RowMarketState(
                    symbol=symbol,
                    event_time=event_time,
                    listed_at=listed_at,
                    published_at=event_time,
                    available_at=available_at,
                    revision=revision,
                    suspended=False,
                    delisted=False,
                    limit_up=None,
                    limit_down=False,
                )

    revision_available = _next_open(calendar, 8)
    original_values = _base_values(0, 4)
    revised_values = {
        **original_values,
        "close": round(original_values["close"] + revision_close_delta, 4),
    }
    revised_values["amount"] = round(revised_values["close"] * revised_values["volume"], 4)
    rows.append(
        AShareSnapshotRow(
            symbol=symbols[0],
            event_time=calendar[4],
            published_at=calendar[4] + timedelta(days=2),
            available_at=revision_available,
            values=MappingProxyType(revised_values),
        )
    )

    delayed_values = _base_values(1, 5)
    rows.append(
        AShareSnapshotRow(
            symbol=symbols[1],
            event_time=calendar[5],
            published_at=calendar[5] + timedelta(days=1),
            available_at=_next_open(calendar, 9),
            values=MappingProxyType(delayed_values),
        )
    )

    rows_tuple = tuple(sorted(rows, key=lambda row: (row.event_time, row.symbol, row.available_at)))
    historical_universe = _historical_universe(calendar=calendar, symbols=symbols)
    coverage = {
        field: sum(1 for row in rows_tuple if row.values.get(field) is not None) / len(rows_tuple)
        for field in fields
    }
    snapshot_hash = canonical_content_hash(
        {
            "request": request,
            "rows": rows_tuple,
            "historical_universe": historical_universe,
            "coverage": coverage,
            "revision": revision,
        }
    )
    dataset_card = DatasetCard(
        dataset_id=request.dataset_id,
        source="offline-frozen-ashare-fixture",
        universe=symbols,
        horizon="daily",
        decision_time=freeze_decision,
        availability_assumptions=(
            "daily bars become available at the next frozen session open",
            "historical index membership is resolved per event date",
            "row-level suspended/delisted/limit/listing-age metadata is required",
        ),
        fields=fields,
        event_time=calendar[0],
        published_at=max(row.published_at for row in rows_tuple),
        available_at=max(row.available_at for row in rows_tuple),
        snapshot_hash=snapshot_hash,
        record_count=len(rows_tuple),
        produced_by=Component.DATA_SERVICE,
    )
    snapshot = FrozenAShareSnapshot(
        request=request,
        dataset_card=dataset_card,
        snapshot_hash=snapshot_hash,
        rows=rows_tuple,
        historical_universe=historical_universe,
        coverage_by_field=MappingProxyType(coverage),
        availability_policy=AvailabilityPolicy(),
        provider="offline-fixture",
        fetched_at=freeze_decision,
        excluded_rows=0,
    )
    state_tuple = tuple(sorted(states.values(), key=lambda item: (item.event_time, item.symbol)))
    manifest = build_reader_manifest(
        snapshot_id=snapshot_id,
        snapshot=snapshot,
        trading_calendar=calendar,
        row_market_state=state_tuple,
        revision=revision,
    )
    envelope = FrozenSnapshotEnvelope(
        manifest=manifest,
        snapshot=snapshot,
        row_market_state=state_tuple,
    )
    split = ResearchSplitManifest(
        split_id="fixture-time-split",
        train_start=calendar[2],
        train_end=calendar[7],
        validation_start=calendar[8],
        validation_end=calendar[10],
        sealed_start=calendar[11],
        sealed_end=calendar[13],
    )
    bridge_decision = _next_open(calendar, 16)
    reader = InMemoryFrozenSnapshotReader({snapshot_id: envelope})
    # Construct once to ensure the fixture default label configuration is valid;
    # callers may still override the returned request fields in tests.
    build_default_bridge_request(
        snapshot_id=snapshot_id,
        decision_time=bridge_decision,
        split_manifest=split,
        feature_fields=feature_fields,
    )
    return OfflineRealResearchFixture(
        snapshot_id=snapshot_id,
        reader=reader,
        snapshot=snapshot,
        envelope=envelope,
        split_manifest=split,
        decision_time=bridge_decision,
        feature_fields=feature_fields,
        symbols=symbols,
        calendar=calendar,
    )
