from __future__ import annotations

import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from src.veriquant.contracts import ContractValidationError, FailureCode
from src.veriquant.data.ashare import (
    AShareDataRequest,
    AShareFailure,
    FrozenAShareSnapshot,
    FrozenSnapshotStore,
    TushareHistoricalUniverseProvider,
    VibeAshareLoader,
    VibeAshareSnapshotAdapter,
)


SH = ZoneInfo("Asia/Shanghai")


def at(day: int, hour: int = 15, minute: int = 0) -> datetime:
    return datetime(2024, 1, day, hour, minute, tzinfo=SH)


def request(*, decision_time: datetime = at(5, 10)) -> AShareDataRequest:
    return AShareDataRequest(
        dataset_id="csi300-pit",
        codes=("000001.SZ", "600000.SH"),
        index_code="000300.SH",
        start_time=at(1),
        end_time=at(2) if decision_time < at(4) else at(4),
        frequency="1D",
        adjustment="raw",
        source="tushare",
        decision_time=decision_time,
        fields=("open", "high", "low", "close", "volume"),
        data_version="fixture/v1",
        cache_policy="reuse-identical",
    )


class FixtureProvider:
    provider_name = "fixture-tushare"
    offline_fixture = True

    def __init__(self, *, perturb_future: bool = False, secret_error: bool = False) -> None:
        self.perturb_future = perturb_future
        self.secret_error = secret_error
        self.calls = 0

    def fetch(self, ignored_request: AShareDataRequest):
        self.calls += 1
        if self.secret_error:
            raise RuntimeError("do-not-leak-secret-token")
        future_close = 999999.0 if self.perturb_future else 12.0
        return {
            "000001.SZ": [
                {"trade_date": "2024-01-01", "open": 10, "high": 11, "low": 9, "close": 10.5, "volume": 100},
                {"trade_date": "2024-01-02", "open": 10.5, "high": 12, "low": 10, "close": 11, "volume": 110},
                {"trade_date": "2024-01-04", "available_at": at(8, 9, 25), "open": 11, "high": 13, "low": 10, "close": future_close, "volume": 120},
            ],
            "600000.SH": [
                {"trade_date": "2024-01-01", "open": 8, "high": 9, "low": 7, "close": 8.5, "volume": 200},
                {"trade_date": "2024-01-02", "open": 8.5, "high": 10, "low": 8, "close": 9, "volume": 220},
            ],
        }


class UniverseClient:
    def __init__(self, rows=None) -> None:
        self.rows = rows if rows is not None else [
            {"trade_date": "20231229", "con_code": "000001.SZ"},
            {"trade_date": "20231229", "con_code": "600000.SH"},
            {"trade_date": "20240102", "con_code": "000001.SZ"},
            {"trade_date": "20240102", "con_code": "600000.SH"},
        ]

    def index_weight(self, **kwargs):
        return self.rows


def adapter(provider: FixtureProvider, store=None) -> VibeAshareSnapshotAdapter:
    return VibeAshareSnapshotAdapter(
        loader=VibeAshareLoader(provider=provider, clock=lambda: at(5, 10)),
        universe_provider=TushareHistoricalUniverseProvider(client=UniverseClient()),
        store=store,
    )


def test_same_request_and_future_perturbation_keep_frozen_hash() -> None:
    first = adapter(FixtureProvider()).build(request())
    second = adapter(FixtureProvider(perturb_future=True)).build(request())
    assert isinstance(first, FrozenAShareSnapshot)
    assert isinstance(second, FrozenAShareSnapshot)
    assert first.snapshot_hash == second.snapshot_hash
    assert first.rows == second.rows
    assert all(row.available_at <= request().decision_time for row in first.rows)


def test_late_revision_cannot_rewrite_an_earlier_as_of_snapshot() -> None:
    baseline = adapter(FixtureProvider()).build(request())

    class LateRevisionProvider(FixtureProvider):
        def fetch(self, ignored_request):
            payload = super().fetch(ignored_request)
            payload["000001.SZ"].append(
                {
                    "trade_date": "2024-01-01", "published_at": at(6), "available_at": at(6),
                    "open": 10, "high": 11, "low": 9, "close": 777777.0, "volume": 100,
                }
            )
            return payload

    revised = adapter(LateRevisionProvider()).build(request())
    assert isinstance(baseline, FrozenAShareSnapshot)
    assert isinstance(revised, FrozenAShareSnapshot)
    assert revised.snapshot_hash == baseline.snapshot_hash


def test_daily_close_becomes_available_at_next_session_open() -> None:
    early = adapter(FixtureProvider()).build(request(decision_time=at(3, 9, 24)))
    late = adapter(FixtureProvider()).build(request(decision_time=at(3, 9, 26)))
    assert isinstance(early, FrozenAShareSnapshot)
    assert isinstance(late, FrozenAShareSnapshot)
    assert {row.event_time.date().isoformat() for row in early.rows} == {"2024-01-01"}
    assert {row.event_time.date().isoformat() for row in late.rows} == {"2024-01-01", "2024-01-02"}


def test_network_disabled_fails_before_repository_provider_call(monkeypatch: pytest.MonkeyPatch) -> None:
    class NetworkProvider(FixtureProvider):
        offline_fixture = False

    provider = NetworkProvider()
    monkeypatch.delenv("ENABLE_ASHARE_NETWORK", raising=False)
    result = VibeAshareLoader(provider=provider).fetch(request())
    assert isinstance(result, AShareFailure)
    assert result.code is FailureCode.ADAPTER_UNAVAILABLE
    assert provider.calls == 0


def test_provider_exception_and_failure_artifact_do_not_leak_secret(tmp_path) -> None:
    store = FrozenSnapshotStore(tmp_path)
    result = adapter(FixtureProvider(secret_error=True), store=store).build(request())
    assert isinstance(result, AShareFailure)
    assert "do-not-leak" not in result.message
    rendered = "\n".join(path.read_text(encoding="utf-8") for path in tmp_path.rglob("*.json"))
    assert "do-not-leak-secret-token" not in rendered


def test_missing_historical_membership_is_typed_and_never_falls_back_current() -> None:
    universe = TushareHistoricalUniverseProvider(client=UniverseClient(rows=[])).fetch(
        request=request(),
        decision_dates=(at(1), at(2)),
    )
    assert universe.status == "DEGRADED"
    assert universe.snapshots == {}
    assert universe.failure is not None
    assert universe.failure.code is FailureCode.INCOMPLETE_RESULT


def test_snapshot_store_is_content_addressed_atomic_and_idempotent(tmp_path) -> None:
    store = FrozenSnapshotStore(tmp_path)
    built = adapter(FixtureProvider()).build(request())
    assert isinstance(built, FrozenAShareSnapshot)
    first = store.persist(built)
    second = store.persist(built)
    assert first.manifest_hash == second.manifest_hash
    assert second.reused is True
    manifest = json.loads((tmp_path / "csi300-pit" / built.snapshot_hash.removeprefix("sha256:") / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["snapshot_hash"] == built.snapshot_hash
    assert manifest["raw_content_hash"] == first.raw_content_hash
    assert not list(tmp_path.rglob("*.tmp"))


def test_request_schema_rejects_callback_and_non_tushare_source() -> None:
    payload = request().to_dict()
    payload["loader"] = "arbitrary.module:callable"
    with pytest.raises(ContractValidationError) as caught:
        AShareDataRequest.from_payload(payload)
    assert caught.value.code is FailureCode.UNKNOWN_FIELD
    with pytest.raises(ContractValidationError):
        AShareDataRequest(
            **{**{key: getattr(request(), key) for key in request().__dataclass_fields__}, "source": "auto"}
        )
