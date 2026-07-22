from __future__ import annotations

import json
from datetime import datetime
from zoneinfo import ZoneInfo

from veriquant.data import MarketSnapshot, SnapshotRow, canonical_content_hash
from veriquant.dynamic_acquisition import ResourceBudget
from veriquant.dynamic_data_integration import (
    DynamicCampaignDataFreezer,
    SOURCE_LABEL,
    enrich_frozen_validation_data,
)
from veriquant.dynamic_data_planning import (
    DataPlanningRequest,
    DynamicDataRequirementCompiler,
)
from veriquant.research_validation_pipeline_v2 import FrozenValidationData


class FakeTransport:
    offline_fixture = True

    def __init__(self) -> None:
        self.calls = []

    def query(self, api_name, *, params, fields):
        self.calls.append((api_name, dict(params), fields))
        base = {
            "daily": {
                "ts_code": "000001.SZ",
                "trade_date": "20240102",
                "open": 10,
                "high": 11,
                "low": 9,
                "close": 10.5,
                "vol": 1000,
                "amount": 10500,
            },
            "adj_factor": {
                "ts_code": "000001.SZ",
                "trade_date": "20240102",
                "adj_factor": 1.0,
            },
            "daily_basic": {
                "ts_code": "000001.SZ",
                "trade_date": "20240102",
                "turnover_rate": 1.2,
                "pe_ttm": 10,
                "pb": 2,
                "total_mv": 100000,
                "circ_mv": 80000,
            },
            "trade_cal": {
                "exchange": params.get("exchange", "SSE"),
                "cal_date": "20240102",
                "is_open": 1,
                "pretrade_date": "20231229",
            },
            "stock_basic": {
                "ts_code": "000001.SZ",
                "industry": "银行",
                "list_date": "19910403",
                "delist_date": None,
                "list_status": "L",
            },
        }[api_name]
        return [{field: base[field] for field in fields}]


class NetworkTransport(FakeTransport):
    offline_fixture = False


def _plan():
    return DynamicDataRequirementCompiler().compile(
        DataPlanningRequest(
            question="research a 50 day momentum alpha",
            research_mode="NEW_ALPHA_EXPLORATION",
            factor_lookback_sessions=50,
        )
    )


def test_freeze_persists_rows_normalizes_features_and_is_reproducible(tmp_path):
    freezer = DynamicCampaignDataFreezer(tmp_path / "freeze")
    transport = FakeTransport()
    first = freezer.freeze(
        plan=_plan(),
        transport=transport,
        start_date="20240101",
        end_date="20240131",
        symbols=("000001.SZ",),
    )
    second = freezer.freeze(
        plan=_plan(),
        transport=FakeTransport(),
        start_date="20240101",
        end_date="20240131",
        symbols=("000001.SZ",),
    )
    assert first.status == "SUCCESS"
    assert first.source_label == SOURCE_LABEL
    assert first.manifest_hash == second.manifest_hash
    assert first.normalized_record_count == 3
    manifest = json.loads(
        (tmp_path / "freeze" / "freeze_manifest.json").read_text(
            encoding="utf-8"
        )
    )
    assert manifest["official_tushare"] is False
    assert manifest["source_label"] == SOURCE_LABEL
    assert manifest["requested_symbol_count"] == 1
    assert manifest["reference_partitions"]["trade_cal"] == ["SSE", "SZSE"]
    assert manifest["reference_partitions"]["stock_basic"] == ["L", "D", "P"]
    assert "token" not in json.dumps(manifest).lower()


def test_network_is_disabled_by_default(tmp_path):
    result = DynamicCampaignDataFreezer(tmp_path / "freeze").freeze(
        plan=_plan(),
        transport=NetworkTransport(),
        start_date="20240101",
        end_date="20240131",
        symbols=("000001.SZ",),
        budget=ResourceBudget(max_api_calls=20),
    )
    assert result.status == "NETWORK_DISABLED"
    assert result.normalized_record_count == 0


def test_full_market_uses_one_date_partitioned_plan_without_symbol_fanout(tmp_path):
    transport = FakeTransport()
    result = DynamicCampaignDataFreezer(tmp_path / "full-market").freeze(
        plan=_plan(),
        transport=transport,
        start_date="20240101",
        end_date="20240102",
        symbols=("000001.SZ", "600000.SH"),
        acquisition_mode="FULL_MARKET",
        budget=ResourceBudget(max_api_calls=20, max_parallel_requests=2),
    )

    assert result.status == "SUCCESS"
    exact_market_calls = [
        params
        for api, params, _fields in transport.calls
        if api in {"daily", "daily_basic"}
    ]
    assert len(exact_market_calls) == 4
    assert all("ts_code" not in params for params in exact_market_calls)
    assert {
        (params["start_date"], params["end_date"])
        for params in exact_market_calls
    } == {("20240101", "20240101"), ("20240102", "20240102")}
    adj_calls = [params for api, params, _fields in transport.calls if api == "adj_factor"]
    assert adj_calls == [
        {
            "start_date": "20240101",
            "end_date": "20240102",
            "ts_code": "000001.SZ,600000.SH",
        }
    ]
    manifest = json.loads(
        (tmp_path / "full-market" / "freeze_manifest.json").read_text(
            encoding="utf-8"
        )
    )
    assert manifest["acquisition_mode"] == "FULL_MARKET"
    assert manifest["reference_partitions"]["daily"] == ["FULL_MARKET_DATE"]
    assert manifest["reference_partitions"]["adj_factor"] == [
        "FULL_MARKET_SYMBOL_BATCH"
    ]


def test_call_budget_is_global_across_apis_and_reported_exactly(tmp_path):
    transport = FakeTransport()
    result = DynamicCampaignDataFreezer(tmp_path / "budgeted").freeze(
        plan=_plan(),
        transport=transport,
        start_date="20240101",
        end_date="20240102",
        symbols=("000001.SZ",),
        budget=ResourceBudget(max_api_calls=3, max_parallel_requests=2),
    )
    manifest = json.loads(
        (tmp_path / "budgeted" / "freeze_manifest.json").read_text(encoding="utf-8")
    )
    assert len(transport.calls) == 3
    assert result.api_calls_used == 3
    assert manifest["total_api_calls_used"] == 3
    assert result.status == "BUDGET_EXHAUSTED"


def test_dynamic_freeze_projects_new_fields_by_historical_decision_time(tmp_path):
    root = tmp_path / "freeze"
    DynamicCampaignDataFreezer(root).freeze(
        plan=_plan(),
        transport=FakeTransport(),
        start_date="20240101",
        end_date="20240131",
        symbols=("000001.SZ",),
    )
    zone = ZoneInfo("Asia/Shanghai")
    event_time = datetime(2024, 1, 2, 15, tzinfo=zone)
    snapshot = MarketSnapshot(
        dataset_id="base",
        decision_time=datetime(2024, 1, 2, 17, tzinfo=zone),
        start_time=event_time,
        fields=("close", "volume"),
        rows=(
            SnapshotRow(
                event_time=event_time,
                symbol="000001.SZ",
                values={"close": 10.5, "volume": 1000.0},
            ),
        ),
        historical_universe={event_time.isoformat(): ("000001.SZ",)},
        coverage_by_field={"close": 1.0, "volume": 1.0},
    )
    enriched = enrich_frozen_validation_data(
        FrozenValidationData(
            snapshot=snapshot,
            labels={},
            qualification_hash=canonical_content_hash({"base": True}),
            source_label=SOURCE_LABEL,
        ),
        freeze_root=root,
    )
    assert enriched.snapshot.rows[0].values["market_cap"] == 100000.0
    assert enriched.snapshot.rows[0].values["turnover_rate"] == 1.2
    assert enriched.source_label == SOURCE_LABEL
