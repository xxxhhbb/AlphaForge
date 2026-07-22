"""Controlled bridge from Vibe's loader layer to PIT-frozen A-share snapshots."""

from __future__ import annotations

import importlib.util
import os
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Mapping, Protocol

from ...contracts import ContractValidationError, DatasetCard, FailureCode
from ...contracts.base import fail
from ..bitemporal import canonical_content_hash
from .calendar import FrozenTradingCalendar
from .models import (
    SHANGHAI,
    AShareDataRequest,
    AShareFailure,
    AShareLoadResult,
    AShareSnapshotRow,
    AvailabilityPolicy,
    FrozenAShareSnapshot,
    HistoricalUniverseResult,
)


class AShareMarketProvider(Protocol):
    provider_name: str
    offline_fixture: bool

    def fetch(self, request: AShareDataRequest) -> Mapping[str, Any]: ...


class HistoricalUniverseProvider(Protocol):
    def fetch(
        self,
        *,
        request: AShareDataRequest,
        decision_dates: tuple[datetime, ...],
    ) -> HistoricalUniverseResult: ...


class RepositoryMarketDataProvider:
    """Calls the existing shared loader layer; it contains no network code."""

    provider_name = "vibe-market-data"
    offline_fixture = False

    def fetch(self, request: AShareDataRequest) -> Mapping[str, Any]:
        if request.adjustment != "raw":
            fail(
                FailureCode.ADAPTER_UNAVAILABLE,
                "the repository Tushare loader currently exposes raw daily bars only",
            )
        if importlib.util.find_spec("tushare") is None:
            fail(
                FailureCode.ADAPTER_UNAVAILABLE,
                "Tushare dependency is unavailable; fallback providers are not permitted",
            )
        try:
            from src.config.accessor import get_env_config

            token = get_env_config().data.tushare_token.strip()
        except Exception as exc:
            fail(
                FailureCode.ADAPTER_UNAVAILABLE,
                f"Tushare credential configuration unavailable: {type(exc).__name__}",
            )
        if not token or token == "your-tushare-token":
            fail(FailureCode.ADAPTER_UNAVAILABLE, "Tushare credentials are not configured")
        from src.market_data import fetch_market_data

        return fetch_market_data(
            codes=list(request.codes),
            start_date=request.start_time.date().isoformat(),
            end_date=request.end_time.date().isoformat(),
            source=request.source,
            interval=request.frequency,
            max_rows=0,
        )


def _provider_time(value: Any, *, daily_close: bool) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        parsed = datetime.combine(value, time(15, 0))
    elif isinstance(value, str):
        candidate = value[:-1] + "+00:00" if value.endswith("Z") else value
        try:
            parsed = datetime.fromisoformat(candidate)
        except ValueError:
            parsed = datetime.strptime(value[:10], "%Y-%m-%d")
    else:
        fail(FailureCode.INVALID_TIMESTAMP, "provider row has no parseable event time")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=SHANGHAI)
    if daily_close and parsed.hour == 0 and parsed.minute == 0:
        parsed = parsed.astimezone(SHANGHAI).replace(hour=15)
    return parsed


def _records(value: Any) -> list[Mapping[str, Any]]:
    if isinstance(value, Mapping) and "data" in value:
        value = value["data"]
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, Mapping)]


class VibeAshareLoader:
    """Normalizes explicit Tushare responses and applies availability as-of filtering."""

    def __init__(
        self,
        *,
        provider: AShareMarketProvider | None = None,
        availability_policy: AvailabilityPolicy | None = None,
        trading_calendar: FrozenTradingCalendar | None = None,
        clock=lambda: datetime.now(timezone.utc),
    ) -> None:
        self.provider = provider or RepositoryMarketDataProvider()
        self.policy = availability_policy or AvailabilityPolicy()
        self.trading_calendar = trading_calendar
        self.clock = clock

    def fetch(self, request: AShareDataRequest) -> AShareLoadResult | AShareFailure:
        if not isinstance(request, AShareDataRequest):
            fail(FailureCode.INVALID_VALUE, "typed AShareDataRequest is required")
        fixture = bool(getattr(self.provider, "offline_fixture", False))
        if not fixture and os.environ.get("ENABLE_ASHARE_NETWORK") != "1":
            return AShareFailure(
                request_hash=request.artifact_hash,
                code=FailureCode.ADAPTER_UNAVAILABLE,
                status="UNAVAILABLE",
                message="A-share network adapter is disabled; set ENABLE_ASHARE_NETWORK=1 explicitly",
                provider="vibe-market-data",
            )
        if not fixture and self.trading_calendar is None:
            return AShareFailure(
                request_hash=request.artifact_hash,
                code=FailureCode.INCOMPLETE_RESULT,
                status="UNAVAILABLE",
                message=(
                    "real A-share PIT snapshots require a frozen SSE/SZSE trading-calendar manifest"
                ),
                provider=str(getattr(self.provider, "provider_name", "ashare-provider")),
            )
        try:
            payload = self.provider.fetch(request)
        except ContractValidationError as exc:
            code = exc.code if exc.code in {FailureCode.ADAPTER_UNAVAILABLE, FailureCode.INCOMPLETE_RESULT} else FailureCode.ADAPTER_EXECUTION_FAILED
            return AShareFailure(
                request_hash=request.artifact_hash,
                code=code,
                status="UNAVAILABLE" if code is FailureCode.ADAPTER_UNAVAILABLE else "FAILED",
                message=str(exc),
                provider=str(getattr(self.provider, "provider_name", "ashare-provider")),
                retryable=False,
            )
        except Exception as exc:
            return AShareFailure(
                request_hash=request.artifact_hash,
                code=FailureCode.ADAPTER_EXECUTION_FAILED,
                status="FAILED",
                message=f"A-share provider failed: {type(exc).__name__}",
                provider=str(getattr(self.provider, "provider_name", "ashare-provider")),
                retryable=True,
            )
        candidates: list[AShareSnapshotRow] = []
        excluded = 0
        for symbol in request.codes:
            for raw in _records(payload.get(symbol, [])):
                stamp = raw.get("trade_date", raw.get("datetime", raw.get("date", raw.get("index"))))
                event_time = _provider_time(stamp, daily_close=True)
                if not (request.start_time.date() <= event_time.date() <= request.end_time.date()):
                    excluded += 1
                    continue
                published_at = _provider_time(raw.get("published_at", event_time), daily_close=False)
                available_raw = raw.get("available_at")
                if self.trading_calendar is not None and (available_raw is None or not fixture):
                    available_at = self.policy.daily_available_at(
                        event_time,
                        trading_calendar=self.trading_calendar,
                    )
                elif available_raw is not None:
                    available_at = _provider_time(available_raw, daily_close=False)
                else:
                    available_at = self.policy.daily_available_at(event_time)
                if event_time > published_at:
                    published_at = event_time
                if published_at > available_at or available_at > request.decision_time:
                    excluded += 1
                    continue
                values = {field: raw.get(field) for field in request.fields}
                try:
                    row = AShareSnapshotRow(
                        symbol=symbol,
                        event_time=event_time,
                        published_at=published_at,
                        available_at=available_at,
                        values=values,
                    )
                except Exception:
                    excluded += 1
                    continue
                candidates.append(row)
        selected: dict[tuple[str, datetime], AShareSnapshotRow] = {}
        for row in candidates:
            key = (row.symbol, row.event_time)
            current = selected.get(key)
            if current is None or (row.available_at, row.published_at, row.artifact_hash) > (
                current.available_at,
                current.published_at,
                current.artifact_hash,
            ):
                selected[key] = row
        rows = tuple(sorted(selected.values(), key=lambda row: (row.event_time, row.symbol)))
        if not rows:
            return AShareFailure(
                request_hash=request.artifact_hash,
                code=FailureCode.INCOMPLETE_RESULT,
                status="UNAVAILABLE",
                message="provider returned no PIT-visible daily rows",
                provider=str(getattr(self.provider, "provider_name", "ashare-provider")),
            )
        return AShareLoadResult(
            request_hash=request.artifact_hash,
            provider=str(getattr(self.provider, "provider_name", "ashare-provider")),
            fetched_at=self.clock(),
            rows=rows,
            excluded_rows=excluded,
        )


class TushareHistoricalUniverseProvider:
    """Historical CSI membership as-of each date; never falls back to current names."""

    source = "tushare-index-weight"

    def __init__(self, *, client: Any | None = None, lookback_days: int = 62) -> None:
        self.client = client
        self.lookback_days = lookback_days

    def _client(self, request: AShareDataRequest) -> Any | AShareFailure:
        if self.client is not None:
            return self.client
        if os.environ.get("ENABLE_ASHARE_NETWORK") != "1":
            return AShareFailure(
                request_hash=request.artifact_hash,
                code=FailureCode.ADAPTER_UNAVAILABLE,
                status="UNAVAILABLE",
                message="historical-universe network adapter is disabled",
                provider=self.source,
            )
        try:
            from src.config.accessor import get_env_config
            import tushare as ts

            token = get_env_config().data.tushare_token.strip()
            if not token or token == "your-tushare-token":
                return AShareFailure(
                    request_hash=request.artifact_hash,
                    code=FailureCode.ADAPTER_UNAVAILABLE,
                    status="UNAVAILABLE",
                    message="Tushare credentials are not configured",
                    provider=self.source,
                )
            return ts.pro_api(token)
        except Exception as exc:
            return AShareFailure(
                request_hash=request.artifact_hash,
                code=FailureCode.ADAPTER_UNAVAILABLE,
                status="UNAVAILABLE",
                message=f"Tushare universe client unavailable: {type(exc).__name__}",
                provider=self.source,
            )

    @staticmethod
    def _table_rows(table: Any) -> list[Mapping[str, Any]]:
        if table is None:
            return []
        if isinstance(table, list):
            return [item for item in table if isinstance(item, Mapping)]
        if hasattr(table, "to_dict"):
            try:
                return list(table.to_dict(orient="records"))
            except Exception:
                return []
        return []

    def fetch(
        self,
        *,
        request: AShareDataRequest,
        decision_dates: tuple[datetime, ...],
    ) -> HistoricalUniverseResult:
        if not decision_dates:
            fail(FailureCode.INVALID_VALUE, "decision_dates are required")
        client = self._client(request)
        if isinstance(client, AShareFailure):
            return HistoricalUniverseResult(
                status="UNAVAILABLE",
                index_code=request.index_code,
                snapshots={},
                source=self.source,
                failure=client,
            )
        first = min(item.date() for item in decision_dates) - timedelta(days=self.lookback_days)
        last = max(item.date() for item in decision_dates)
        try:
            table = client.index_weight(
                index_code=request.index_code,
                start_date=first.strftime("%Y%m%d"),
                end_date=last.strftime("%Y%m%d"),
            )
            rows = self._table_rows(table)
        except Exception as exc:
            failure = AShareFailure(
                request_hash=request.artifact_hash,
                code=FailureCode.ADAPTER_EXECUTION_FAILED,
                status="UNAVAILABLE",
                message=f"historical index_weight unavailable: {type(exc).__name__}",
                provider=self.source,
                retryable=True,
            )
            return HistoricalUniverseResult(
                status="UNAVAILABLE",
                index_code=request.index_code,
                snapshots={},
                source=self.source,
                failure=failure,
            )
        by_date: dict[date, set[str]] = {}
        for row in rows:
            try:
                trade_date = datetime.strptime(str(row["trade_date"])[:10].replace("-", ""), "%Y%m%d").date()
                code = str(row["con_code"])
            except (KeyError, ValueError):
                continue
            if trade_date <= last:
                by_date.setdefault(trade_date, set()).add(code)
        snapshots: dict[str, tuple[str, ...]] = {}
        missing: list[str] = []
        available_dates = sorted(by_date)
        for item in sorted(decision_dates):
            eligible = [value for value in available_dates if value <= item.date()]
            if not eligible:
                missing.append(item.date().isoformat())
                continue
            selected = eligible[-1]
            snapshots[item.date().isoformat()] = tuple(sorted(by_date[selected]))
        if missing:
            failure = AShareFailure(
                request_hash=request.artifact_hash,
                code=FailureCode.INCOMPLETE_RESULT,
                status="DEGRADED",
                message="historical universe is missing one or more decision dates",
                provider=self.source,
            )
            return HistoricalUniverseResult(
                status="DEGRADED",
                index_code=request.index_code,
                snapshots=snapshots,
                source=self.source,
                failure=failure,
            )
        return HistoricalUniverseResult(
            status="AVAILABLE",
            index_code=request.index_code,
            snapshots=snapshots,
            source=self.source,
        )


class VibeAshareSnapshotAdapter:
    def __init__(
        self,
        *,
        loader: VibeAshareLoader | None = None,
        universe_provider: HistoricalUniverseProvider | None = None,
        availability_policy: AvailabilityPolicy | None = None,
        store: Any | None = None,
        trading_calendar: FrozenTradingCalendar | None = None,
    ) -> None:
        self.policy = availability_policy or AvailabilityPolicy()
        if trading_calendar is not None and not isinstance(trading_calendar, FrozenTradingCalendar):
            fail(FailureCode.INVALID_VALUE, "trading_calendar must be FrozenTradingCalendar")
        self.loader = loader or VibeAshareLoader(
            availability_policy=self.policy,
            trading_calendar=trading_calendar,
        )
        loader_calendar = getattr(self.loader, "trading_calendar", None)
        if (
            trading_calendar is not None
            and loader_calendar is not None
            and trading_calendar.artifact_hash != loader_calendar.artifact_hash
        ):
            fail(FailureCode.INVALID_VALUE, "loader and adapter calendars differ")
        if trading_calendar is not None and loader_calendar is None:
            setattr(self.loader, "trading_calendar", trading_calendar)
        self.trading_calendar = trading_calendar or loader_calendar
        self.universe_provider = universe_provider or TushareHistoricalUniverseProvider()
        self.store = store

    def build(self, request: AShareDataRequest) -> FrozenAShareSnapshot | AShareFailure:
        loaded = self.loader.fetch(request)
        if isinstance(loaded, AShareFailure):
            if self.store is not None:
                self.store.record_failure(request=request, failure=loaded, fetched_at=datetime.now(timezone.utc))
            return loaded
        decision_dates = tuple(sorted({row.event_time for row in loaded.rows}))
        universe = self.universe_provider.fetch(request=request, decision_dates=decision_dates)
        if universe.status != "AVAILABLE":
            failure = universe.failure
            if self.store is not None and failure is not None:
                self.store.record_failure(request=request, failure=failure, fetched_at=loaded.fetched_at)
            return failure
        active_rows = tuple(
            row
            for row in loaded.rows
            if row.symbol in universe.snapshots[row.event_time.date().isoformat()]
        )
        if not active_rows:
            failure = AShareFailure(
                request_hash=request.artifact_hash,
                code=FailureCode.INCOMPLETE_RESULT,
                status="UNAVAILABLE",
                message="historical universe removed all requested rows",
                provider=loaded.provider,
            )
            if self.store is not None:
                self.store.record_failure(request=request, failure=failure, fetched_at=loaded.fetched_at)
            return failure
        expected = len(active_rows)
        coverage = {
            field: sum(row.values[field] is not None for row in active_rows) / expected
            for field in request.fields
        }
        material = {
            "request_hash": request.artifact_hash,
            "rows": active_rows,
            "historical_universe": universe.snapshots,
            "coverage": coverage,
            "availability_policy": self.policy,
            "provider": loaded.provider,
            "trading_calendar": self.trading_calendar,
        }
        snapshot_hash = canonical_content_hash(material)
        row_symbols = tuple(sorted({row.symbol for row in active_rows}))
        card = DatasetCard(
            dataset_id=request.dataset_id,
            source=f"vibe:{request.source}",
            universe=row_symbols,
            horizon=request.frequency,
            decision_time=request.decision_time,
            availability_assumptions=self.policy.assumptions(
                request.fields,
                trading_calendar=self.trading_calendar,
            ),
            fields=request.fields,
            event_time=min(row.event_time for row in active_rows),
            published_at=max(row.published_at for row in active_rows),
            available_at=max(row.available_at for row in active_rows),
            snapshot_hash=snapshot_hash,
            record_count=len(active_rows),
        )
        snapshot = FrozenAShareSnapshot(
            request=request,
            dataset_card=card,
            snapshot_hash=snapshot_hash,
            rows=active_rows,
            historical_universe=universe.snapshots,
            coverage_by_field=coverage,
            availability_policy=self.policy,
            provider=loaded.provider,
            fetched_at=loaded.fetched_at,
            excluded_rows=loaded.excluded_rows,
            trading_calendar=self.trading_calendar,
        )
        if self.store is not None:
            self.store.persist(snapshot)
        return snapshot




