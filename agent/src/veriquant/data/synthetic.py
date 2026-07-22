"""Tiny, reproducible, fully offline bitemporal market fixture."""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Mapping

from ..contracts import PointInTimeRecord
from .bitemporal import DatasetRegistry, FieldRole, UniverseMembership


FEATURE_FIELDS = (
    "close",
    "volume",
    "industry",
    "market_cap",
    "book_value",
    "macro_rate",
)
LABEL_FIELD = "future_return_5d"


@dataclass(frozen=True, kw_only=True)
class SyntheticMarketFixture:
    dataset_id: str
    records: tuple[PointInTimeRecord, ...]
    memberships: tuple[UniverseMembership, ...]
    field_roles: Mapping[str, FieldRole]
    calendar: tuple[datetime, ...]
    seed: int

    def register(self, registry: DatasetRegistry):
        return registry.register(
            dataset_id=self.dataset_id,
            records=self.records,
            memberships=self.memberships,
            field_roles=self.field_roles,
            calendar=self.calendar,
            seed=self.seed,
        )


def _business_sessions(start: datetime, count: int) -> tuple[datetime, ...]:
    sessions: list[datetime] = []
    cursor = start
    while len(sessions) < count:
        if cursor.weekday() < 5:
            sessions.append(cursor.replace(hour=16, minute=0, second=0, microsecond=0))
        cursor += timedelta(days=1)
    return tuple(sessions)


def build_synthetic_market_fixture(
    *,
    seed: int = 7,
    dataset_id: str = "synthetic-market-v1",
    session_count: int = 60,
    market_availability_lag_steps: int = 0,
    fundamental_revision_delay_steps: int = 0,
    future_price_shock_start: int | None = None,
    future_price_shock_multiplier: float = 1.0,
) -> SyntheticMarketFixture:
    """Build a small frozen fixture without network, files, or external providers."""

    if session_count < 35:
        raise ValueError("session_count must provide at least 35 sessions")
    if market_availability_lag_steps < 0 or fundamental_revision_delay_steps < 0:
        raise ValueError("availability lags cannot be negative")
    if future_price_shock_start is not None and not 0 <= future_price_shock_start < session_count:
        raise ValueError("future_price_shock_start is outside the fixture")
    if not math.isfinite(future_price_shock_multiplier) or future_price_shock_multiplier <= 0:
        raise ValueError("future_price_shock_multiplier must be finite and positive")

    calendar = _business_sessions(datetime(2024, 1, 2, tzinfo=timezone.utc), session_count)
    symbols = ("AAA", "BBB", "CCC", "DDD")
    industries = {"AAA": "TECH", "BBB": "FIN", "CCC": "TECH", "DDD": "INDUSTRIAL"}
    bases = {"AAA": 100.0, "BBB": 65.0, "CCC": 42.0, "DDD": 82.0}
    trends = {"AAA": 0.0018, "BBB": -0.0004, "CCC": 0.0025, "DDD": 0.0007}
    shares = {"AAA": 10_000_000, "BBB": 14_000_000, "CCC": 8_000_000, "DDD": 11_000_000}
    rng = random.Random(seed)

    closes: dict[str, list[float]] = {symbol: [] for symbol in symbols}
    for symbol_index, symbol in enumerate(symbols):
        for index in range(session_count):
            oscillation = 0.012 * math.sin((index + 2 * symbol_index) / 4.5)
            deterministic_noise = rng.uniform(-0.0015, 0.0015)
            value = bases[symbol] * (1.0 + trends[symbol] * index + oscillation + deterministic_noise)
            if future_price_shock_start is not None and index >= future_price_shock_start:
                value *= future_price_shock_multiplier
            closes[symbol].append(round(value, 6))

    records: list[PointInTimeRecord] = []

    def add_record(
        *,
        record_id: str,
        symbol: str,
        field_name: str,
        value,
        event_time: datetime,
        published_at: datetime,
        available_at: datetime,
    ) -> None:
        records.append(
            PointInTimeRecord(
                record_id=record_id,
                dataset_id=dataset_id,
                entity_id=symbol,
                field_name=field_name,
                value=value,
                event_time=event_time,
                published_at=published_at,
                available_at=available_at,
                source="synthetic-fixture",
            )
        )

    # Daily market and macro features. A positive lag moves the availability
    # timestamp forward by exactly that many fixture sessions. Trailing records
    # whose availability would exceed the frozen calendar are intentionally absent.
    for index, event_time in enumerate(calendar):
        available_index = index + market_availability_lag_steps
        if available_index >= session_count:
            continue
        available_at = calendar[available_index] + timedelta(minutes=5)
        published_at = event_time + timedelta(minutes=1)
        macro_value = None if index == 26 else round(0.02 + 0.00005 * index, 6)
        for symbol_index, symbol in enumerate(symbols):
            close = closes[symbol][index]
            volume = 900_000 + 15_000 * index + 70_000 * symbol_index
            if (symbol, index) in {("AAA", 11), ("CCC", 29)}:
                volume = None
            add_record(
                record_id=f"{symbol}.close.{index:03d}",
                symbol=symbol,
                field_name="close",
                value=close,
                event_time=event_time,
                published_at=published_at,
                available_at=available_at,
            )
            add_record(
                record_id=f"{symbol}.volume.{index:03d}",
                symbol=symbol,
                field_name="volume",
                value=volume,
                event_time=event_time,
                published_at=published_at,
                available_at=available_at,
            )
            market_cap = None if symbol == "CCC" and index < 23 else round(close * shares[symbol], 2)
            add_record(
                record_id=f"{symbol}.market_cap.{index:03d}",
                symbol=symbol,
                field_name="market_cap",
                value=market_cap,
                event_time=event_time,
                published_at=published_at,
                available_at=available_at,
            )
            # Macro data starts after three sessions, creating an explicit coverage ramp.
            if index >= 3:
                add_record(
                    record_id=f"{symbol}.macro_rate.{index:03d}",
                    symbol=symbol,
                    field_name="macro_rate",
                    value=macro_value,
                    event_time=event_time,
                    published_at=published_at,
                    available_at=available_at,
                )

    # Industry is static but PIT-addressed and carried forward by the snapshotter.
    for symbol in symbols:
        add_record(
            record_id=f"{symbol}.industry.initial",
            symbol=symbol,
            field_name="industry",
            value=industries[symbol],
            event_time=calendar[0],
            published_at=calendar[0] + timedelta(minutes=1),
            available_at=calendar[0] + timedelta(minutes=2),
        )

    # Financial observations deliberately separate fiscal event time, publication,
    # availability, and a later revision of the same event.
    fiscal_event = calendar[0] - timedelta(days=32)
    first_available_index = 3
    revision_base_index = 14
    revision_available_index = min(
        revision_base_index + fundamental_revision_delay_steps, session_count - 1
    )
    second_event_index = 27
    second_available_index = 36
    fundamentals = {
        "AAA": (24.0, 24.8, 26.0),
        "BBB": (18.0, 17.6, 17.9),
        "CCC": (None, None, 11.5),
        "DDD": (None, 20.2, 20.9),
    }
    for symbol, (initial_value, revised_value, second_value) in fundamentals.items():
        add_record(
            record_id=f"{symbol}.book_value.initial",
            symbol=symbol,
            field_name="book_value",
            value=initial_value,
            event_time=fiscal_event,
            published_at=calendar[first_available_index] + timedelta(hours=-3),
            available_at=calendar[first_available_index] + timedelta(hours=-2),
        )
        add_record(
            record_id=f"{symbol}.book_value.revision",
            symbol=symbol,
            field_name="book_value",
            value=revised_value,
            event_time=fiscal_event,
            published_at=calendar[revision_base_index] + timedelta(hours=-3),
            available_at=calendar[revision_available_index] + timedelta(hours=-2),
        )
        add_record(
            record_id=f"{symbol}.book_value.next",
            symbol=symbol,
            field_name="book_value",
            value=second_value,
            event_time=calendar[second_event_index],
            published_at=calendar[second_available_index] + timedelta(hours=-3),
            available_at=calendar[second_available_index] + timedelta(hours=-2),
        )

    # Labels are computed from future prices but are unavailable until the horizon
    # closes and are catalogued LABEL, so the feature snapshot/parser reject them.
    for symbol in symbols:
        for index in range(session_count - 5):
            value = round(closes[symbol][index + 5] / closes[symbol][index] - 1.0, 10)
            add_record(
                record_id=f"{symbol}.future_return_5d.{index:03d}",
                symbol=symbol,
                field_name=LABEL_FIELD,
                value=value,
                event_time=calendar[index],
                published_at=calendar[index + 5] + timedelta(minutes=1),
                available_at=calendar[index + 5] + timedelta(minutes=5),
            )

    memberships = (
        UniverseMembership(
            dataset_id=dataset_id,
            symbol="AAA",
            member_from=calendar[0],
            member_until=None,
            published_at=calendar[0],
            available_at=calendar[0],
        ),
        UniverseMembership(
            dataset_id=dataset_id,
            symbol="BBB",
            member_from=calendar[0],
            member_until=calendar[42],
            published_at=calendar[0],
            available_at=calendar[0],
        ),
        UniverseMembership(
            dataset_id=dataset_id,
            symbol="CCC",
            member_from=calendar[20],
            member_until=None,
            published_at=calendar[18],
            available_at=calendar[18] + timedelta(minutes=1),
        ),
        UniverseMembership(
            dataset_id=dataset_id,
            symbol="DDD",
            member_from=calendar[0],
            member_until=None,
            published_at=calendar[0],
            available_at=calendar[0],
        ),
    )
    roles = {field_name: FieldRole.FEATURE for field_name in FEATURE_FIELDS}
    roles[LABEL_FIELD] = FieldRole.LABEL
    return SyntheticMarketFixture(
        dataset_id=dataset_id,
        records=tuple(records),
        memberships=memberships,
        field_roles=roles,
        calendar=calendar,
        seed=seed,
    )

