from __future__ import annotations

import sys
from datetime import datetime, time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))

from veriquant.contracts import ContractValidationError, FailureCode
from veriquant.factors import parse_compile_factor_dag
from veriquant.data import FieldRole
from veriquant.real_research.api import (
    AShareResearchBridge,
    QlibBridgeAdapter,
    QlibBridgeUnavailable,
    ResearchSplitManifest,
    build_default_bridge_request,
    build_offline_real_research_fixture,
    visible_rows_by_partition,
)


def _open_at(calendar, index: int) -> datetime:
    return datetime.combine(calendar[index].date(), time(9, 25), tzinfo=calendar[index].tzinfo)


def _bundle(*, fixture=None, decision_time=None, split=None, forward_days=2, execution_lag_days=1):
    fixture = fixture or build_offline_real_research_fixture()
    request = build_default_bridge_request(
        snapshot_id=fixture.snapshot_id,
        decision_time=decision_time or fixture.decision_time,
        split_manifest=split or fixture.split_manifest,
        forward_days=forward_days,
        execution_lag_days=execution_lag_days,
        feature_fields=fixture.feature_fields,
    )
    return AShareResearchBridge(fixture.reader).build_bundle(request)


def _latest_close(fixture, *, symbol: str, event_time: datetime, decision_time: datetime) -> float:
    rows = [
        row
        for row in fixture.snapshot.rows
        if row.symbol == symbol
        and row.event_time == event_time
        and row.available_at <= decision_time
        and row.values.get("close") is not None
    ]
    row = max(rows, key=lambda item: (item.available_at, item.published_at))
    return float(row.values["close"])


def test_reproducible_bundle_hash_for_same_frozen_input() -> None:
    first = _bundle()
    second = _bundle(fixture=build_offline_real_research_fixture())

    assert first.bridge_run_manifest.output_hash == second.bridge_run_manifest.output_hash
    assert first.artifact_hash == second.artifact_hash
    assert first.data_quality_manifest.rows_used > 0
    assert not any("future_return" in row.values for row in first.feature_rows)


def test_future_rows_and_late_availability_are_rejected() -> None:
    fixture = build_offline_real_research_fixture()
    early_split = ResearchSplitManifest(
        split_id="early-late-field-split",
        train_start=fixture.calendar[2],
        train_end=fixture.calendar[3],
        validation_start=fixture.calendar[4],
        validation_end=fixture.calendar[4],
        sealed_start=fixture.calendar[5],
        sealed_end=fixture.calendar[5],
    )
    bundle = _bundle(
        fixture=fixture,
        decision_time=_open_at(fixture.calendar, 9),
        split=early_split,
    )

    reasons = bundle.data_quality_manifest.rejection_reasons
    assert reasons["FUTURE_EVENT"] > 0
    assert reasons["ROW_UNAVAILABLE"] > 0
    assert reasons["MISSING_FEATURE_amount"] == 1
    assert all(row.available_at <= bundle.decision_time for row in bundle.feature_rows)


def test_historical_universe_blocks_future_member_leakage() -> None:
    fixture = build_offline_real_research_fixture()
    bundle = _bundle(fixture=fixture)
    future_member = "000010.SZ"

    assert bundle.data_quality_manifest.rejection_reasons["UNIVERSE_NOT_MEMBER"] > 0
    assert not any(
        row.symbol == future_member and row.event_time < fixture.calendar[9]
        for row in bundle.feature_rows
    )
    reasons = bundle.data_quality_manifest.rejection_reasons
    assert reasons["SUSPENDED"] == 1
    assert reasons["PRICE_LIMIT"] == 1
    assert reasons["MISSING_LIMIT_UP"] == 1
    assert reasons["DELISTED"] == 1
    assert reasons["LISTING_AGE_INSUFFICIENT"] > 0


def test_label_off_by_one_and_execution_lag_are_explicit() -> None:
    fixture = build_offline_real_research_fixture()
    lag_one = _bundle(fixture=fixture, execution_lag_days=1)
    lag_zero = _bundle(fixture=fixture, execution_lag_days=0)
    symbol = "000001.SZ"
    event_time = fixture.calendar[2]

    row = next(row for row in lag_one.label_rows if row.symbol == symbol and row.event_time == event_time)
    expected_entry = fixture.calendar[3]
    expected_exit = fixture.calendar[5]
    expected = (
        _latest_close(fixture, symbol=symbol, event_time=expected_exit, decision_time=fixture.decision_time)
        / _latest_close(fixture, symbol=symbol, event_time=expected_entry, decision_time=fixture.decision_time)
        - 1.0
    )
    assert row.entry_time == expected_entry
    assert row.exit_time == expected_exit
    assert row.value == pytest.approx(expected)

    zero_row = next(row for row in lag_zero.label_rows if row.symbol == symbol and row.event_time == event_time)
    assert zero_row.entry_time == fixture.calendar[2]
    assert zero_row.exit_time == fixture.calendar[4]
    assert zero_row.value != pytest.approx(row.value)


def test_snapshot_and_revision_changes_change_output_hash() -> None:
    base_fixture = build_offline_real_research_fixture(revision="r1", revision_close_delta=0.7)
    revised_fixture = build_offline_real_research_fixture(revision="r2", revision_close_delta=1.3)
    base = _bundle(fixture=base_fixture)
    revised = _bundle(fixture=revised_fixture)

    assert base_fixture.snapshot.snapshot_hash != revised_fixture.snapshot.snapshot_hash
    assert base.bridge_run_manifest.output_hash != revised.bridge_run_manifest.output_hash


def test_unknown_field_fails_closed() -> None:
    fixture = build_offline_real_research_fixture()
    request = build_default_bridge_request(
        snapshot_id=fixture.snapshot_id,
        decision_time=fixture.decision_time,
        split_manifest=fixture.split_manifest,
        feature_fields=("close", "future_return_5d"),
    )

    with pytest.raises(ContractValidationError) as exc:
        AShareResearchBridge(fixture.reader).build_bundle(request)
    assert exc.value.code is FailureCode.UNKNOWN_FIELD


def test_qlib_unavailable_is_typed_and_visible_rows_exclude_sealed(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("ENABLE_QLIB_ADAPTER", raising=False)
    fixture = build_offline_real_research_fixture()
    bundle = _bundle(fixture=fixture)
    candidate = parse_compile_factor_dag(
        {
            "schema_version": "veriquant.factor-dag/v1",
            "dag_id": "close-score",
            "nodes": [{"id": "close", "op": "field", "field": "close"}],
            "output_node": "close",
        },
        field_roles={"close": FieldRole.FEATURE},
    )
    adapter = QlibBridgeAdapter()
    materialized = adapter.build_visible_request(
        bundle=bundle,
        provider_uri=tmp_path / "missing-provider",
        score_field="close",
    )

    counts = visible_rows_by_partition(materialized.visible_rows)
    assert counts["train"] > 0
    assert counts["validation"] > 0
    assert all(row.partition in {"train", "validation"} for row in materialized.visible_rows)

    result = adapter.run_optional_visible(
        bundle=bundle,
        provider_uri=tmp_path / "missing-provider",
        candidate=candidate,
        score_field="close",
    )
    assert isinstance(result, QlibBridgeUnavailable)
    assert result.status == "UNAVAILABLE"
    assert result.code is FailureCode.ADAPTER_UNAVAILABLE
