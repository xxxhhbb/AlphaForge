from __future__ import annotations

import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from src.veriquant.contracts import ContractValidationError, FailureCode
from src.veriquant.data.ashare import (
    AShareDataRequest,
    FrozenAShareSnapshot,
    HistoricalUniverseResult,
    VibeAshareLoader,
    VibeAshareSnapshotAdapter,
)
from src.veriquant.data.ashare.staging import QlibCsvStager, VisibleStagingSplit


SH = ZoneInfo("Asia/Shanghai")


def at(day: int, hour: int = 15) -> datetime:
    return datetime(2024, 2, day, hour, 0, tzinfo=SH)


class Bars:
    provider_name = "offline-bars"
    offline_fixture = True

    def fetch(self, request):
        return {
            "000001.SZ": [
                {"trade_date": "2024-02-01", "open": 10, "high": 11, "low": 9, "close": 10.5, "volume": 100},
                {"trade_date": "2024-02-02", "open": 10.5, "high": 12, "low": 10, "close": 11.0, "volume": 110},
                {"trade_date": "2024-02-05", "open": 11, "high": 13, "low": 10, "close": 12.0, "volume": 120},
                {"trade_date": "2024-02-06", "open": 12, "high": 13, "low": 11, "close": 12.5, "volume": 130},
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


def snapshot() -> FrozenAShareSnapshot:
    request = AShareDataRequest(
        dataset_id="stager-fixture",
        codes=("000001.SZ",),
        index_code="000300.SH",
        start_time=at(1),
        end_time=at(6),
        frequency="1D",
        adjustment="raw",
        source="tushare",
        decision_time=at(7, 10),
        fields=("open", "high", "low", "close", "volume"),
        data_version="fixture/v1",
        cache_policy="freeze",
    )
    result = VibeAshareSnapshotAdapter(
        loader=VibeAshareLoader(provider=Bars(), clock=lambda: at(7, 10)),
        universe_provider=Universe(),
    ).build(request)
    assert isinstance(result, FrozenAShareSnapshot)
    return result


def split() -> VisibleStagingSplit:
    return VisibleStagingSplit(
        train_start=at(1),
        train_end=at(2),
        validation_start=at(5),
        validation_end=at(6),
    )


def test_staging_is_deterministic_content_addressed_and_reusable(tmp_path) -> None:
    stager = QlibCsvStager(tmp_path)
    first = stager.stage(snapshot=snapshot(), split=split())
    second = stager.stage(snapshot=snapshot(), split=split())
    assert first.staging_hash == second.staging_hash
    assert first.manifest_hash == second.manifest_hash
    assert first.reused is False
    assert second.reused is True
    assert first.train_rows == 2
    assert first.validation_rows == 2


def test_manifest_freezes_source_universe_adjustment_policy_mapping_and_visible_splits(tmp_path) -> None:
    staged = QlibCsvStager(tmp_path).stage(snapshot=snapshot(), split=split())
    manifest = json.loads((tmp_path / staged.staging_hash.removeprefix("sha256:") / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["source_snapshot_hash"] == staged.source_snapshot_hash
    assert manifest["adjustment"] == "raw"
    assert manifest["partitions"] == ["train", "validation"]
    assert manifest["historical_universe_hash"].startswith("sha256:")
    assert manifest["availability_policy"]["policy_version"] == "ashare-availability/v1"
    assert manifest["field_mapping"]["close"] == "close"
    assert set(manifest["file_hashes"]) == set(staged.file_hashes)


def test_staging_contains_no_sealed_partition_or_authority_payload(tmp_path) -> None:
    staged = QlibCsvStager(tmp_path).stage(snapshot=snapshot(), split=split())
    directory = tmp_path / staged.staging_hash.removeprefix("sha256:")
    manifest_text = (directory / "manifest.json").read_text(encoding="utf-8").lower()
    assert "sealed" not in manifest_text
    assert '"test"' not in manifest_text
    assert "official_metrics" not in manifest_text
    assert "promotion" not in manifest_text
    for csv_path in (directory / "csv").glob("*.csv"):
        text = csv_path.read_text(encoding="utf-8").lower()
        assert ",train," in text
        assert ",validation," in text
        assert ",test," not in text


def test_unknown_or_forbidden_mapping_and_incomplete_split_fail_closed(tmp_path) -> None:
    stager = QlibCsvStager(tmp_path)
    with pytest.raises(ContractValidationError) as unknown:
        stager.stage(snapshot=snapshot(), split=split(), field_mapping={"future_return": "label"})
    assert unknown.value.code is FailureCode.UNKNOWN_FIELD
    with pytest.raises(ContractValidationError) as forbidden:
        stager.stage(snapshot=snapshot(), split=split(), field_mapping={"close": "sealed_score"})
    assert forbidden.value.code is FailureCode.UNAUTHORIZED_CAPABILITY
    no_validation = VisibleStagingSplit(
        train_start=at(1),
        train_end=at(2),
        validation_start=at(20),
        validation_end=at(21),
    )
    with pytest.raises(ContractValidationError) as incomplete:
        stager.stage(snapshot=snapshot(), split=no_validation)
    assert incomplete.value.code is FailureCode.INCOMPLETE_RESULT
