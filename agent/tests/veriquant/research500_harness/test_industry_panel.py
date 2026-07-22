from __future__ import annotations

from datetime import date, datetime, timezone
import json
from types import SimpleNamespace

import pytest

from veriquant.approved_proxy_scale_adapter_v2 import AllowlistedProxyScaleClient
from veriquant.data import MarketSnapshot, SnapshotRow
from veriquant.research500_harness.industry_panel import (
    IndustryInterval,
    _carry_registered_baselines,
    enrich_snapshot_with_industry,
    normalize_industry_intervals,
)
from veriquant.research_toolbox.contracts import canonical_hash


UTC = timezone.utc


def _snapshot(days=(1, 2, 3)) -> MarketSnapshot:
    rows = tuple(
        SnapshotRow(
            event_time=datetime(2024, 1, day, 16, tzinfo=UTC),
            symbol="000001.SZ",
            values={"close": float(day)},
        )
        for day in days
    )
    return MarketSnapshot(
        dataset_id="industry.test",
        decision_time=datetime(2024, 1, max(days), 16, tzinfo=UTC),
        start_time=datetime(2024, 1, min(days), 16, tzinfo=UTC),
        fields=("close",),
        rows=rows,
        historical_universe={f"2024-01-{day:02d}": ("000001.SZ",) for day in days},
        coverage_by_field={"close": 1.0},
    )


def test_next_session_availability_delays_membership() -> None:
    interval = IndustryInterval("000001.SZ", date(2024, 1, 1), None, "801000", "银行")
    panel, diagnostics = enrich_snapshot_with_industry(
        _snapshot(), (interval,), minimum_coverage=0.5
    )
    assert [row.values["industry"] for row in panel.rows] == [None, "银行", "银行"]
    assert diagnostics["industry_coverage"] == pytest.approx(2 / 3)


def test_future_membership_does_not_change_past_projection() -> None:
    base = (IndustryInterval("000001.SZ", date(2023, 1, 1), None, "801000", "银行"),)
    first, _ = enrich_snapshot_with_industry(_snapshot(), base)
    future = base + (IndustryInterval("000001.SZ", date(2025, 1, 1), None, "802000", "科技"),)
    second, _ = enrich_snapshot_with_industry(_snapshot(), future)
    assert [row.values for row in first.rows] == [row.values for row in second.rows]


@pytest.mark.parametrize("value", ["", "bad", "20240230"])
def test_invalid_in_date_fails_closed(value: str) -> None:
    with pytest.raises(ValueError):
        normalize_industry_intervals([{
            "ts_code": "000001.SZ", "l1_code": "801000", "l1_name": "银行", "in_date": value
        }])


def test_conflicting_overlap_fails_closed() -> None:
    with pytest.raises(ValueError, match="overlapping"):
        normalize_industry_intervals([
            {"ts_code": "000001.SZ", "l1_code": "A", "l1_name": "银行", "in_date": "20200101", "out_date": "20220101"},
            {"ts_code": "000001.SZ", "l1_code": "B", "l1_name": "科技", "in_date": "20210101"},
        ])


def test_later_effective_date_closes_unbounded_prior_interval() -> None:
    values = normalize_industry_intervals([
        {"ts_code": "000001.SZ", "l1_code": "A", "l1_name": "银行", "in_date": "20200101"},
        {"ts_code": "000001.SZ", "l1_code": "B", "l1_name": "科技", "in_date": "20260701"},
    ])
    assert values[0].member_until == date(2026, 7, 1)
    assert values[0].end_inferred is True
    assert values[1].member_until is None


def test_snapshot_hash_is_canonical() -> None:
    interval = IndustryInterval("000001.SZ", date(2023, 1, 1), None, "801000", "银行")
    first, _ = enrich_snapshot_with_industry(_snapshot(), (interval,))
    second, _ = enrich_snapshot_with_industry(_snapshot(), (interval,))
    assert first.artifact_hash == second.artifact_hash


def test_registered_baseline_receipt_is_content_addressed_on_new_snapshot(tmp_path) -> None:
    base = tmp_path / "base"
    output = tmp_path / "output"
    base.mkdir()
    raw = {
        "schema_version": "alphaforge.research500-registered-panel/v1",
        "status": "PASSED",
        "data_hash": "sha256:" + "a" * 64,
        "snapshot_hash": "sha256:" + "b" * 64,
        "qualification_hash": "sha256:" + "c" * 64,
        "factors": [{"factor_id": "f", "field_name": "registered_f", "score_count": 2}],
    }
    raw["artifact_hash"] = canonical_hash(raw)
    (base / "registered_baselines_receipt.json").write_text(json.dumps(raw), encoding="utf-8")
    derived = SimpleNamespace(
        artifact_hash="sha256:" + "d" * 64,
        qualification_hash="sha256:" + "e" * 64,
        snapshot=SimpleNamespace(fields=("close", "registered_f", "industry"), artifact_hash="sha256:" + "f" * 64),
    )
    receipt_hash = _carry_registered_baselines(
        base_root=base,
        output_root=output,
        base_snapshot_hash="sha256:" + "b" * 64,
        derived=derived,
    )
    carried = json.loads((output / "registered_baselines_receipt.json").read_text(encoding="utf-8"))
    assert carried["artifact_hash"] == receipt_hash
    assert carried["snapshot_hash"] == derived.snapshot.artifact_hash
    assert carried["source_receipt_hash"] == raw["artifact_hash"]
