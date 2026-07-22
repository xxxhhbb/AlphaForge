from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from src.veriquant.contracts import DatasetCard, FailureCode
from src.veriquant.data.ashare import (
    AShareDataRequest,
    AShareSnapshotRow,
    AvailabilityPolicy,
    FrozenAShareSnapshot,
    FrozenSnapshotStore,
    FrozenTradingCalendar,
    FrozenTradingCalendarStore,
    HistoricalUniverseResult,
    VibeAshareLoader,
    VibeAshareSnapshotAdapter,
)
from src.veriquant.data.ashare.calendar import calendar_session_hash
from src.veriquant.data.bitemporal import canonical_content_hash
from src.veriquant.real_ashare_data.reader import (
    FrozenAShareSnapshotReader,
    HistoricalUniverseSnapshot,
    MarketVintage,
    RealDataAvailability,
    SnapshotIntegrityError,
)


SH = ZoneInfo("Asia/Shanghai")


def ts(day: str, hour: int = 15, minute: int = 0) -> datetime:
    year, month, dom = (int(part) for part in day.split("-"))
    return datetime(year, month, dom, hour, minute, tzinfo=SH)


def calendar() -> FrozenTradingCalendar:
    sessions = ("2024-02-08", "2024-02-19", "2024-02-20", "2024-02-21")
    return FrozenTradingCalendar(
        calendar_id="cn-ashare-sse-szse",
        venue_codes=("SSE", "SZSE"),
        source="fixture-exchange-calendars",
        source_calendar="XSHG",
        source_version="agent-a-test",
        policy_version="sse-szse-shared-session-calendar/v1",
        session_start="2024-02-08",
        session_end="2024-02-21",
        session_dates=sessions,
        session_content_hash=calendar_session_hash(sessions),
    )


def request(
    *,
    dataset_id: str = "real-ashare-fixture",
    decision_time: datetime = ts("2024-02-20", 10),
    end_time: datetime | None = None,
) -> AShareDataRequest:
    return AShareDataRequest(
        dataset_id=dataset_id,
        codes=("000001.SZ", "600000.SH", "000002.SZ"),
        index_code="000300.SH",
        start_time=ts("2024-02-08"),
        end_time=end_time or ts("2024-02-19"),
        frequency="1D",
        adjustment="raw",
        source="tushare",
        decision_time=decision_time,
        fields=("open", "high", "low", "close", "volume"),
        data_version="fixture/v1",
        cache_policy="reuse-identical",
    )


class FixtureBars:
    provider_name = "offline-bars"
    offline_fixture = True

    def __init__(self, *, feb8_revision_close: float | None = None, future_note: str | None = None) -> None:
        self.feb8_revision_close = feb8_revision_close
        self.future_note = future_note
        self.calls = 0

    def fetch(self, ignored_request: AShareDataRequest):
        self.calls += 1
        rows_000001 = [
            {
                "trade_date": "2024-02-08",
                "available_at": ts("2024-02-19", 9, 25),
                "open": 10.0,
                "high": 11.0,
                "low": 9.5,
                "close": 10.5,
                "volume": 1000.0,
            },
            {
                "trade_date": "2024-02-19",
                "available_at": ts("2024-02-20", 9, 25),
                "open": 10.5,
                "high": 12.0,
                "low": 10.0,
                "close": 11.0,
                "volume": 1100.0,
            },
        ]
        if self.feb8_revision_close is not None:
            rows_000001.append(
                {
                    "trade_date": "2024-02-08",
                    "published_at": ts("2024-02-20", 9, 30),
                    "available_at": ts("2024-02-20", 9, 30),
                    "open": 10.0,
                    "high": 11.0,
                    "low": 9.5,
                    "close": self.feb8_revision_close,
                    "volume": 1000.0,
                    "note": self.future_note,
                }
            )
        return {
            "000001.SZ": rows_000001,
            "600000.SH": [
                {
                    "trade_date": "2024-02-08",
                    "available_at": ts("2024-02-19", 9, 25),
                    "open": 20.0,
                    "high": 21.0,
                    "low": 19.0,
                    "close": 20.5,
                    "volume": 2000.0,
                },
                {
                    "trade_date": "2024-02-19",
                    "available_at": ts("2024-02-20", 9, 25),
                    "open": 20.5,
                    "high": 22.0,
                    "low": 20.0,
                    "close": 21.0,
                    "volume": 2200.0,
                },
            ],
            "000002.SZ": [
                {
                    "trade_date": "2024-02-19",
                    "available_at": ts("2024-02-20", 9, 25),
                    "open": 30.0,
                    "high": 33.0,
                    "low": 29.0,
                    "close": 32.0,
                    "volume": 3200.0,
                }
            ],
        }


class FixtureUniverse:
    source = "offline-universe"

    def __init__(self, *, include_future_membership: bool = False) -> None:
        self.include_future_membership = include_future_membership

    def fetch(self, *, request: AShareDataRequest, decision_dates: tuple[datetime, ...]) -> HistoricalUniverseResult:
        snapshots: dict[str, tuple[str, ...]] = {}
        for item in decision_dates:
            day = item.date().isoformat()
            if day == "2024-02-08":
                snapshots[day] = ("000001.SZ", "600000.SH")
            elif day == "2024-02-19":
                snapshots[day] = ("000001.SZ", "600000.SH", "000002.SZ")
            else:
                snapshots[day] = ("000001.SZ",)
        if self.include_future_membership:
            snapshots["2024-02-21"] = ("000001.SZ", "688001.SH")
        return HistoricalUniverseResult(
            status="AVAILABLE",
            index_code=request.index_code,
            snapshots=snapshots,
            source=self.source,
        )


def persist_calendar(tmp_path: Path) -> tuple[FrozenTradingCalendar, Path, Path]:
    calendar_root = tmp_path / "calendars"
    cal = calendar()
    persisted = FrozenTradingCalendarStore(calendar_root).persist(cal)
    manifest_path = Path(persisted.directory) / "manifest.json"
    return cal, calendar_root, manifest_path


def persist_snapshot(
    tmp_path: Path,
    *,
    bars: FixtureBars | None = None,
    universe: FixtureUniverse | None = None,
    req: AShareDataRequest | None = None,
) -> tuple[FrozenAShareSnapshotReader, object, FrozenAShareSnapshot, Path]:
    cal, calendar_root, _calendar_manifest = persist_calendar(tmp_path)
    snapshot_root = tmp_path / "snapshots"
    actual_request = req or request()
    built = VibeAshareSnapshotAdapter(
        loader=VibeAshareLoader(
            provider=bars or FixtureBars(),
            trading_calendar=cal,
            clock=lambda: ts("2024-02-20", 10),
        ),
        universe_provider=universe or FixtureUniverse(),
        store=FrozenSnapshotStore(snapshot_root),
        trading_calendar=cal,
    ).build(actual_request)
    assert isinstance(built, FrozenAShareSnapshot)
    manifest_path = snapshot_root / actual_request.dataset_id / built.snapshot_hash.removeprefix("sha256:") / "manifest.json"
    reader = FrozenAShareSnapshotReader(snapshot_root=snapshot_root, calendar_root=calendar_root)
    handle = reader.open(manifest_path)
    return reader, handle, built, manifest_path


def manual_snapshot_with_future_universe(tmp_path: Path) -> tuple[FrozenAShareSnapshotReader, object, FrozenAShareSnapshot]:
    cal, calendar_root, _ = persist_calendar(tmp_path)
    req = request(dataset_id="future-universe-fixture", decision_time=ts("2024-02-21", 16))
    rows = (
        AShareSnapshotRow(
            symbol="000001.SZ",
            event_time=ts("2024-02-08"),
            published_at=ts("2024-02-08"),
            available_at=ts("2024-02-19", 9, 25),
            values={"open": 10.0, "high": 11.0, "low": 9.5, "close": 10.5, "volume": 1000.0},
        ),
    )
    historical_universe = {
        "2024-02-08": ("000001.SZ",),
        "2024-02-21": ("000001.SZ", "688001.SH"),
    }
    coverage = {field: 1.0 for field in req.fields}
    policy = AvailabilityPolicy()
    material = {
        "request_hash": req.artifact_hash,
        "rows": rows,
        "historical_universe": historical_universe,
        "coverage": coverage,
        "availability_policy": policy,
        "provider": "offline-bars",
        "trading_calendar": cal,
    }
    snapshot_hash = canonical_content_hash(material)
    card = DatasetCard(
        dataset_id=req.dataset_id,
        source="vibe:tushare",
        universe=("000001.SZ",),
        horizon=req.frequency,
        decision_time=req.decision_time,
        availability_assumptions=policy.assumptions(req.fields, trading_calendar=cal),
        fields=req.fields,
        event_time=rows[0].event_time,
        published_at=rows[0].published_at,
        available_at=rows[0].available_at,
        snapshot_hash=snapshot_hash,
        record_count=len(rows),
    )
    snapshot = FrozenAShareSnapshot(
        request=req,
        dataset_card=card,
        snapshot_hash=snapshot_hash,
        rows=rows,
        historical_universe=historical_universe,
        coverage_by_field=coverage,
        availability_policy=policy,
        provider="offline-bars",
        fetched_at=ts("2024-02-20", 10),
        excluded_rows=0,
        trading_calendar=cal,
    )
    snapshot_root = tmp_path / "manual-snapshots"
    FrozenSnapshotStore(snapshot_root).persist(snapshot)
    reader = FrozenAShareSnapshotReader(snapshot_root=snapshot_root, calendar_root=calendar_root)
    manifest = snapshot_root / req.dataset_id / snapshot.snapshot_hash.removeprefix("sha256:") / "manifest.json"
    return reader, reader.open(manifest), snapshot


def test_snapshot_hash_manifest_and_market_vintage_replay_are_stable(tmp_path: Path) -> None:
    reader, handle, built, _manifest = persist_snapshot(tmp_path)

    replayed = reader.read_snapshot(handle)
    vintage_one = reader.market_vintage(handle, decision_time=ts("2024-02-20", 10))
    vintage_two = reader.market_vintage(handle, decision_time=ts("2024-02-20", 10))

    assert replayed.snapshot_hash == built.snapshot_hash == handle.snapshot_hash
    assert replayed.trading_calendar is not None
    assert replayed.trading_calendar.artifact_hash == handle.calendar_hash
    assert isinstance(vintage_one, MarketVintage)
    assert vintage_one.market_state_hash == vintage_two.market_state_hash
    assert vintage_one.artifact_hash == vintage_two.artifact_hash
    assert {row.symbol for row in vintage_one.rows} == {"000001.SZ", "600000.SH", "000002.SZ"}


def test_tampered_manifest_or_raw_file_fail_closed(tmp_path: Path) -> None:
    reader, _handle, _built, manifest = persist_snapshot(tmp_path)
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["coverage"]["close"] = 0.0
    manifest.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    with pytest.raises(SnapshotIntegrityError) as manifest_error:
        reader.open(manifest)
    assert manifest_error.value.code is FailureCode.INVALID_HASH

    reader, _handle, _built, manifest = persist_snapshot(tmp_path / "raw")
    raw_path = manifest.with_name("raw_response.json")
    raw_payload = json.loads(raw_path.read_text(encoding="utf-8"))
    raw_payload["rows"][0]["values"]["close"] = 9999.0
    raw_path.write_text(json.dumps(raw_payload, sort_keys=True), encoding="utf-8")
    with pytest.raises(SnapshotIntegrityError) as raw_error:
        reader.open(manifest)
    assert raw_error.value.code is FailureCode.INVALID_HASH


def test_path_traversal_is_rejected(tmp_path: Path) -> None:
    reader, _handle, _built, _manifest = persist_snapshot(tmp_path)
    with pytest.raises(SnapshotIntegrityError) as caught:
        reader.open("../escape/manifest.json")
    assert caught.value.code is FailureCode.UNAUTHORIZED_CAPABILITY


def test_available_at_later_than_decision_time_is_not_visible(tmp_path: Path) -> None:
    reader, handle, _built, _manifest = persist_snapshot(tmp_path)

    feb19_vintage = reader.market_vintage(handle, decision_time=ts("2024-02-19", 16))
    feb20_vintage = reader.market_vintage(handle, decision_time=ts("2024-02-20", 10))

    assert {row.calendar_session for row in feb19_vintage.rows} == {"2024-02-08"}
    assert "2024-02-19" in {row.calendar_session for row in feb20_vintage.rows}


def test_historical_universe_snapshot_does_not_use_future_members(tmp_path: Path) -> None:
    reader, handle, _snapshot = manual_snapshot_with_future_universe(tmp_path)

    early = reader.historical_universe_snapshot(handle, decision_time=ts("2024-02-19", 16))
    late = reader.historical_universe_snapshot(handle, decision_time=ts("2024-02-21", 16))

    assert isinstance(early, HistoricalUniverseSnapshot)
    assert "2024-02-21" not in early.members_by_date
    assert "688001.SH" not in early.latest_members
    assert "688001.SH" in late.latest_members


def test_revision_does_not_rewrite_old_content_addressed_vintage(tmp_path: Path) -> None:
    early_reader, early_handle, _early_snapshot, _ = persist_snapshot(
        tmp_path / "early",
        req=request(dataset_id="early-revision", decision_time=ts("2024-02-19", 10), end_time=ts("2024-02-08")),
    )
    later_reader, later_handle, _later_snapshot, _ = persist_snapshot(
        tmp_path / "later",
        bars=FixtureBars(feb8_revision_close=77.0, future_note="REDACTION_SENTINEL_456"),
        req=request(dataset_id="later-revision", decision_time=ts("2024-02-20", 10)),
    )

    early_vintage = early_reader.market_vintage(early_handle, decision_time=ts("2024-02-19", 10))
    later_vintage = later_reader.market_vintage(later_handle, decision_time=ts("2024-02-20", 10))

    early_close = [row.values["close"] for row in early_vintage.rows if row.symbol == "000001.SZ"][0]
    later_close = [
        row.values["close"]
        for row in later_vintage.rows
        if row.symbol == "000001.SZ" and row.calendar_session == "2024-02-08"
    ][0]
    assert early_close == 10.5
    assert later_close == 77.0
    assert early_handle.snapshot_hash != later_handle.snapshot_hash


def test_network_disabled_returns_typed_unavailable_without_sensitive_values(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _cal, calendar_root, calendar_manifest = persist_calendar(tmp_path)
    reader = FrozenAShareSnapshotReader(snapshot_root=tmp_path / "snapshots", calendar_root=calendar_root)
    monkeypatch.delenv("ENABLE_ASHARE_NETWORK", raising=False)
    monkeypatch.setenv("VQ_TEST_REDACTION_SENTINEL", "REDACTION_SENTINEL_123")

    availability = reader.real_data_availability(request=request(), calendar_manifest_path=calendar_manifest)
    rendered = json.dumps(availability.to_dict(), sort_keys=True)

    assert isinstance(availability, RealDataAvailability)
    assert availability.status == "UNAVAILABLE"
    assert availability.failure_code == FailureCode.ADAPTER_UNAVAILABLE.value
    assert "REDACTION_SENTINEL_123" not in rendered


def test_fetch_and_freeze_offline_preflight_never_uses_fallback_provider(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _cal, calendar_root, calendar_manifest = persist_calendar(tmp_path)
    reader = FrozenAShareSnapshotReader(snapshot_root=tmp_path / "snapshots", calendar_root=calendar_root)
    monkeypatch.delenv("ENABLE_ASHARE_NETWORK", raising=False)

    result = reader.fetch_and_freeze(request=request(), calendar_manifest_path=calendar_manifest)

    assert isinstance(result, RealDataAvailability)
    assert result.status == "UNAVAILABLE"
    assert result.provider == "vibe-market-data"

