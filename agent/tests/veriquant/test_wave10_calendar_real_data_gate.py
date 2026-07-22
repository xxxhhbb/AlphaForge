from __future__ import annotations

import argparse
import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

import src.veriquant.qlib_setup as setup
import src.veriquant.data.ashare.adapter as ashare_adapter
from src.veriquant.adapters.qlib_subprocess import SubprocessLocalQlibVisibleRunner
from src.veriquant.contracts import ContractValidationError, FailureCode
from src.veriquant.data.ashare import (
    AShareDataRequest,
    AShareFailure,
    AvailabilityPolicy,
    FrozenAShareSnapshot,
    FrozenSnapshotStore,
    FrozenTradingCalendar,
    FrozenTradingCalendarStore,
    HistoricalUniverseResult,
    RepositoryMarketDataProvider,
    VibeAshareLoader,
    VibeAshareSnapshotAdapter,
)
from src.veriquant.data.ashare.calendar import calendar_session_hash
from src.veriquant.data.ashare.staging import QlibCsvStager, VisibleStagingSplit


SH = ZoneInfo("Asia/Shanghai")


def ts(value: str, hour: int = 15, minute: int = 0) -> datetime:
    year, month, day = (int(part) for part in value.split("-"))
    return datetime(year, month, day, hour, minute, tzinfo=SH)


def calendar() -> FrozenTradingCalendar:
    sessions = ("2024-02-08", "2024-02-19", "2024-02-20")
    return FrozenTradingCalendar(
        calendar_id="cn-ashare-sse-szse",
        venue_codes=("SSE", "SZSE"),
        source="fixture-exchange-calendars",
        source_calendar="XSHG",
        source_version="test",
        policy_version="sse-szse-shared-session-calendar/v1",
        session_start="2024-02-08",
        session_end="2024-02-20",
        session_dates=sessions,
        session_content_hash=calendar_session_hash(sessions),
    )


def request() -> AShareDataRequest:
    return AShareDataRequest(
        dataset_id="calendar-pit",
        codes=("000001.SZ",),
        index_code="000300.SH",
        start_time=ts("2024-02-08"),
        end_time=ts("2024-02-19"),
        frequency="1D",
        adjustment="raw",
        source="tushare",
        decision_time=ts("2024-02-20", 10),
        fields=("open", "close", "volume"),
        data_version="fixture/v1",
        cache_policy="reuse-identical",
    )


class Bars:
    provider_name = "offline-bars"
    offline_fixture = True

    def __init__(self) -> None:
        self.calls = 0

    def fetch(self, ignored_request):
        self.calls += 1
        return {
            "000001.SZ": [
                {"trade_date": "2024-02-08", "open": 10, "close": 11, "volume": 100},
                {"trade_date": "2024-02-19", "open": 11, "close": 12, "volume": 120},
            ]
        }


class Universe:
    def fetch(self, *, request, decision_dates):
        return HistoricalUniverseResult(
            status="AVAILABLE",
            index_code=request.index_code,
            snapshots={item.date().isoformat(): ("000001.SZ",) for item in decision_dates},
            source="offline-universe",
        )


def snapshot(*, output_store=None) -> FrozenAShareSnapshot:
    cal = calendar()
    loader = VibeAshareLoader(
        provider=Bars(),
        trading_calendar=cal,
        clock=lambda: ts("2024-02-20", 10),
    )
    result = VibeAshareSnapshotAdapter(
        loader=loader,
        universe_provider=Universe(),
        store=output_store,
        trading_calendar=cal,
    ).build(request())
    assert isinstance(result, FrozenAShareSnapshot)
    return result


def test_frozen_calendar_delays_signal_across_holiday_gap() -> None:
    available = AvailabilityPolicy().daily_available_at(
        ts("2024-02-08"),
        trading_calendar=calendar(),
    )
    assert available == ts("2024-02-19", 9, 25)


def test_calendar_store_is_content_addressed_and_tamper_detected(tmp_path) -> None:
    store = FrozenTradingCalendarStore(tmp_path)
    first = store.persist(calendar())
    second = store.persist(calendar())
    assert first.calendar_hash == second.calendar_hash
    assert second.reused is True
    manifest = tmp_path / "cn-ashare-sse-szse" / calendar().artifact_hash.removeprefix("sha256:") / "manifest.json"
    loaded = store.load_manifest(manifest)
    assert loaded.artifact_hash == calendar().artifact_hash
    sessions = manifest.with_name("sessions.json")
    payload = json.loads(sessions.read_text(encoding="utf-8"))
    payload["session_dates"][-1] = "2024-02-21"
    sessions.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    with pytest.raises(ContractValidationError):
        store.load_manifest(manifest)


def test_real_network_path_requires_calendar_before_provider_call(monkeypatch) -> None:
    class NetworkProvider(Bars):
        offline_fixture = False

    provider = NetworkProvider()
    monkeypatch.setenv("ENABLE_ASHARE_NETWORK", "1")
    result = VibeAshareLoader(provider=provider).fetch(request())
    assert isinstance(result, AShareFailure)
    assert result.code is FailureCode.INCOMPLETE_RESULT
    assert provider.calls == 0


def test_snapshot_store_and_staging_bind_calendar_hash(tmp_path) -> None:
    built = snapshot(output_store=FrozenSnapshotStore(tmp_path / "snapshots"))
    assert built.trading_calendar is not None
    assert any(
        item == f"calendar={built.trading_calendar.calendar_id}:{built.trading_calendar.artifact_hash}"
        for item in built.dataset_card.availability_assumptions
    )
    split = VisibleStagingSplit(
        train_start=ts("2024-02-08"),
        train_end=ts("2024-02-08"),
        validation_start=ts("2024-02-19"),
        validation_end=ts("2024-02-19"),
    )
    staged = QlibCsvStager(tmp_path / "qlib").stage(snapshot=built, split=split)
    manifest = json.loads(
        (tmp_path / "qlib" / staged.staging_hash.removeprefix("sha256:") / "manifest.json").read_text(
            encoding="utf-8"
        )
    )
    assert manifest["trading_calendar_hash"] == built.trading_calendar.artifact_hash


def test_repository_provider_never_falls_back_when_tushare_unavailable(monkeypatch) -> None:
    monkeypatch.setenv("ENABLE_ASHARE_NETWORK", "1")
    monkeypatch.setattr(
        ashare_adapter.importlib.util,
        "find_spec",
        lambda name: None if name == "tushare" else object(),
    )
    result = VibeAshareLoader(
        provider=RepositoryMarketDataProvider(),
        trading_calendar=calendar(),
    ).fetch(request())
    assert isinstance(result, AShareFailure)
    assert result.code is FailureCode.ADAPTER_UNAVAILABLE
    assert "fallback" in result.message


def test_qlib_subprocess_environment_disables_user_site() -> None:
    assert SubprocessLocalQlibVisibleRunner._environment()["PYTHONNOUSERSITE"] == "1"


def test_stage_from_vibe_requires_manifest_before_real_request(monkeypatch) -> None:
    monkeypatch.setenv("ENABLE_ASHARE_NETWORK", "1")
    monkeypatch.setattr(
        setup,
        "VibeAshareSnapshotAdapter",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("provider constructed")),
    )
    result, code = setup.stage_from_vibe(
        argparse.Namespace(codes=["000001.SZ"], source="tushare", calendar_manifest=None)
    )
    assert code == 2
    assert result["failure_code"] == "INCOMPLETE_RESULT"
    assert "calendar-manifest" in result["message"]


def test_freeze_calendar_cli_helper_persists_manifest(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(setup, "freeze_sse_szse_calendar", lambda **kwargs: calendar())
    result, code = setup.freeze_calendar(
        argparse.Namespace(
            output=str(tmp_path),
            start_date=ts("2024-02-08").date(),
            end_date=ts("2024-02-20").date(),
            source_calendar="XSHG",
        )
    )
    assert code == 0
    assert result["status"] == "FROZEN"
    assert result["network_used"] is False
    assert (tmp_path / "cn-ashare-sse-szse" / result["calendar_hash"].removeprefix("sha256:") / "manifest.json").is_file()




