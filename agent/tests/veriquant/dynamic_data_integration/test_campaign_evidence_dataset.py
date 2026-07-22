from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from veriquant.bitemporal_features import BitemporalStore
from veriquant.data import MarketSnapshot, SnapshotRow, canonical_content_hash
from veriquant.dynamic_data_integration import (
    CampaignEvidenceDatasetBuilder,
    DynamicCampaignDataFreezer,
)
from veriquant.dynamic_data_planning import (
    DataPlanningRequest,
    DynamicDataRequirementCompiler,
)
from veriquant.research_validation_pipeline_v2 import FrozenValidationData
from veriquant.research_validation_pipeline_v2.models import FrozenRowProvenance
from veriquant.integration.campaign_evidence_dataset import (
    CampaignEvidenceDatasetRepository,
)
from veriquant.integration.campaign_data_preparation import (
    CampaignDataPreparationService,
    CampaignDataPreparationStatus,
    _load_spine_cache,
    _save_spine_cache,
    _snapshot_required_fields,
)
from veriquant.approved_proxy_scale_adapter_v2 import ProxyScaleAdapterStatus
from veriquant.real_ashare_scale import (
    FrozenAshareScaleInput,
    FrozenDataVintage,
    HistoricalUniverseSnapshot,
    InputOrigin,
    ScaleInputRow,
    stable_content_hash,
)
from veriquant.research_real_data_v2 import PRICE_CORE_FEATURE_FIELDS
from veriquant.research_os_v3.product_service import ResearchOSV3ProductService
from veriquant.data_capability import DataRequirementPlanner, DatasetCapabilityAssessor


CN = timezone(timedelta(hours=8))
SYMBOLS = ("000001.SZ", "600000.SH")
SESSIONS = ("20240102", "20240103", "20240104", "20240105")


class FundamentalTransport:
    offline_fixture = True

    def query(self, api_name, *, params, fields):
        symbol = str(params.get("ts_code") or SYMBOLS[0])
        market = {
            "daily": {
                "ts_code": symbol, "trade_date": "20240102", "open": 10,
                "high": 11, "low": 9, "close": 10.5, "vol": 1000,
                "amount": 10500,
            },
            "adj_factor": {
                "ts_code": symbol, "trade_date": "20240102", "adj_factor": 1,
            },
            "daily_basic": {
                "ts_code": symbol, "trade_date": "20240102", "turnover_rate": 1,
                "pe_ttm": 10, "pb": 2, "total_mv": 100000, "circ_mv": 80000,
            },
            "moneyflow": {
                "ts_code": symbol, "trade_date": "20240102",
                "buy_lg_amount": 100, "sell_lg_amount": 50,
                "buy_elg_amount": 20, "sell_elg_amount": 10,
                "net_mf_vol": 3, "net_mf_amount": 60,
            },
            "index_daily": {
                "ts_code": "000001.SH", "trade_date": "20240102",
                "close": 3000, "pct_chg": 1, "vol": 100, "amount": 1000,
            },
            "shibor": {
                "date": "20240102", "on": 1.5, "1w": 1.6, "1m": 1.7,
                "3m": 1.8, "6m": 1.9, "1y": 2.0,
            },
        }
        if api_name in market:
            row = market[api_name]
            return [{field: row.get(field) for field in fields}]
        if api_name == "trade_cal":
            return [
                {
                    "exchange": params["exchange"], "cal_date": session, "is_open": 1,
                    "pretrade_date": SESSIONS[max(0, index - 1)],
                }
                for index, session in enumerate(SESSIONS)
            ]
        if api_name == "stock_basic":
            row = {
                "ts_code": symbol, "industry": "银行", "list_date": "19910403",
                "delist_date": None, "list_status": "L",
            }
            return [{field: row.get(field) for field in fields}]
        if api_name == "fina_indicator":
            rows = (
                {
                    "ts_code": symbol, "ann_date": "20240102", "end_date": "20231231",
                    "roe": 10, "roa": 1, "grossprofit_margin": 20,
                    "netprofit_margin": 10, "debt_to_assets": 50,
                    "current_ratio": 1.2, "quick_ratio": 1.1, "assets_turn": 0.5,
                    "or_yoy": 5, "netprofit_yoy": 6, "ocf_to_or": 0.2, "bps": 3,
                    "update_flag": "0",
                },
                {
                    "ts_code": symbol, "ann_date": "20240104",
                    "end_date": "20231231", "roe": 12, "roa": 1,
                    "grossprofit_margin": 20, "netprofit_margin": 10,
                    "debt_to_assets": 50, "current_ratio": 1.2,
                    "quick_ratio": 1.1, "assets_turn": 0.5, "or_yoy": 5,
                    "netprofit_yoy": 6, "ocf_to_or": 0.2, "bps": 3,
                    "update_flag": "1",
                },
            )
            return [{field: row.get(field) for field in fields} for row in rows]
        if api_name in {"income", "balancesheet", "cashflow", "forecast", "express"}:
            return []
        raise AssertionError(api_name)


def _plan():
    return DynamicDataRequirementCompiler().compile(
        DataPlanningRequest(
            question="研究 ROE 质量因子与资金流、利率和指数市场状态",
            research_mode="NEW_ALPHA_EXPLORATION",
            factor_lookback_sessions=2,
        )
    )


def _base() -> FrozenValidationData:
    rows = []
    provenance = []
    labels = {}
    historical = {}
    captured = datetime(2024, 1, 8, 17, tzinfo=CN)
    manifest_hash = canonical_content_hash({"base": "manifest"})
    calendar_hash = canonical_content_hash(SESSIONS)
    universe_hash = canonical_content_hash(SYMBOLS)
    policy_hash = canonical_content_hash({"daily": "16:00"})
    for session_index, session in enumerate(SESSIONS):
        event_time = datetime.strptime(session, "%Y%m%d").replace(
            hour=15, tzinfo=CN
        )
        historical[event_time.isoformat()] = SYMBOLS
        for symbol_index, symbol in enumerate(SYMBOLS):
            values = {
                "close": 10.0 + session_index + symbol_index,
                "volume": 1000.0 + session_index,
            }
            row = SnapshotRow(event_time=event_time, symbol=symbol, values=values)
            rows.append(row)
            labels[f"{event_time.isoformat()}|{symbol}"] = 0.01 * (symbol_index + 1)
            provenance.append(
                FrozenRowProvenance(
                    symbol=symbol,
                    event_time=event_time,
                    published_at=event_time + timedelta(minutes=30),
                    available_at=event_time + timedelta(hours=1),
                    revision_id=f"daily-{session}-{symbol}",
                    source_kind="daily",
                    source_label="APPROVED_PROXY_DATA_VENDOR",
                    provider_class="APPROVED_PROXY_DATA_VENDOR",
                    source_class="TUSHARE_COMPATIBLE_PROXY",
                    input_origin="TEST_FIXTURE",
                    captured_at=captured,
                    source_record_hash=canonical_content_hash(values),
                    adapter_manifest_hash=manifest_hash,
                    calendar_hash=calendar_hash,
                    universe_hash=universe_hash,
                    availability_policy_hash=policy_hash,
                    publication_time_policy="EXPLICIT_PROVIDER_CLOCKS",
                )
            )
    snapshot = MarketSnapshot(
        dataset_id="scale-spine",
        decision_time=datetime(2024, 1, 5, 17, tzinfo=CN),
        start_time=datetime(2024, 1, 2, 15, tzinfo=CN),
        fields=("close", "volume"),
        rows=tuple(rows),
        historical_universe=historical,
        coverage_by_field={"close": 1.0, "volume": 1.0},
    )
    return FrozenValidationData(
        snapshot=snapshot,
        labels=labels,
        qualification_hash=canonical_content_hash({"base": True}),
        source_label="APPROVED_PROXY_DATA_VENDOR",
        row_provenance=tuple(provenance),
    )


def _scale_spine() -> FrozenAshareScaleInput:
    rows = []
    universes = []
    vintages = []
    for session_index, session_raw in enumerate(SESSIONS):
        session = datetime.strptime(session_raw, "%Y%m%d").date().isoformat()
        universes.append(HistoricalUniverseSnapshot(session=session, symbols=SYMBOLS))
        row_hashes = []
        for symbol_index, symbol in enumerate(SYMBOLS):
            row = ScaleInputRow(
                symbol=symbol,
                session=session,
                event_time=datetime.fromisoformat(session + "T15:00:00+08:00"),
                available_at=datetime.fromisoformat(session + "T16:00:00+08:00"),
                revision_id=f"scale-{session}-{symbol}",
                features={
                    "open": 10.0, "high": 11.0, "low": 9.0,
                    "close": 10.0 + session_index + symbol_index,
                    "volume": 1000.0, "amount": 10000.0,
                    "adj_factor": 1.0, "market_cap": 1_000_000.0,
                    "status": "ACTIVE",
                },
                labels={"future_return_5d": 0.01 * (symbol_index + 1)},
            )
            rows.append(row)
            row_hashes.append(row.artifact_hash)
        vintages.append(FrozenDataVintage(
            vintage_id=f"vintage-{session_raw}",
            session=session,
            available_at=datetime.fromisoformat(session + "T16:00:00+08:00"),
            decision_time=datetime.fromisoformat(session + "T17:00:00+08:00"),
            revision_id=f"vintage-revision-{session_raw}",
            content_hash=stable_content_hash(tuple(row_hashes)),
        ))
    row_tuple = tuple(rows)
    universe_tuple = tuple(universes)
    sessions = tuple(item.session for item in universes)
    return FrozenAshareScaleInput(
        dataset_id="campaign-scale-spine",
        provider_class="APPROVED_PROXY_DATA_VENDOR",
        source_class="TUSHARE_COMPATIBLE_PROXY",
        input_origin=InputOrigin.TEST_FIXTURE,
        capture_time=datetime(2024, 1, 8, 17, tzinfo=CN),
        capture_network_used=False,
        calendar_id="test-calendar",
        calendar_sessions=sessions,
        calendar_hash=stable_content_hash(sessions),
        historical_universe=universe_tuple,
        universe_hash=stable_content_hash(universe_tuple),
        rows=row_tuple,
        data_hash=stable_content_hash(row_tuple),
        feature_fields=PRICE_CORE_FEATURE_FIELDS,
        label_fields=("future_return_5d",),
        availability_policy_hash=stable_content_hash({"decision": "17:00"}),
        vintages=tuple(vintages),
        adapter_manifest_hash=stable_content_hash({"adapter": "fixture"}),
    )


class FakeScaleAdapter:
    def __init__(self):
        self.calls = 0

    def capture(self, request):
        self.calls += 1
        spine = _scale_spine()
        return type("CaptureResult", (), {
            "status": ProxyScaleAdapterStatus.READY,
            "frozen_input": spine,
            "request_hash": request.artifact_hash,
            "api_manifest_hash": spine.adapter_manifest_hash,
            "failure_codes": (),
        })()


def test_materializes_financial_revisions_by_each_historical_decision(tmp_path):
    root = tmp_path / "freeze"
    DynamicCampaignDataFreezer(root).freeze(
        plan=_plan(),
        transport=FundamentalTransport(),
        start_date="20240101",
        end_date="20240105",
        symbols=SYMBOLS,
    )
    result = CampaignEvidenceDatasetBuilder().build(
        base=_base(),
        freeze_root=root,
        required_fields=("roe",),
        optional_fields=("net_moneyflow_amount", "shibor_overnight"),
    )
    by_key = {
        (row.event_time.date().isoformat(), row.symbol): row
        for row in result.frozen_data.snapshot.rows
    }
    assert by_key[("2024-01-02", SYMBOLS[0])].values["roe"] is None
    assert by_key[("2024-01-03", SYMBOLS[0])].values["roe"] == 10.0
    assert by_key[("2024-01-04", SYMBOLS[0])].values["roe"] == 10.0
    assert by_key[("2024-01-05", SYMBOLS[0])].values["roe"] == 12.0
    assert "future_return_5d" not in result.frozen_data.snapshot.fields
    assert result.frozen_data.labels == _base().labels
    assert result.frozen_data.row_provenance == _base().row_provenance
    assert result.manifest.missing_required_fields == ()


def test_market_wide_index_and_shibor_are_projected_to_every_equity(tmp_path):
    root = tmp_path / "freeze"
    DynamicCampaignDataFreezer(root).freeze(
        plan=_plan(),
        transport=FundamentalTransport(),
        start_date="20240101",
        end_date="20240105",
        symbols=SYMBOLS,
    )
    result = CampaignEvidenceDatasetBuilder().build(
        base=_base(),
        freeze_root=root,
        optional_fields=("index_pct_change", "shibor_overnight"),
    )
    rows = [
        row
        for row in result.frozen_data.snapshot.rows
        if row.event_time.date().isoformat() == "2024-01-03"
    ]
    assert {row.symbol for row in rows} == set(SYMBOLS)
    assert {row.values["index_pct_change"] for row in rows} == {1.0}
    assert {row.values["shibor_overnight"] for row in rows} == {1.5}


def test_projection_queries_only_sources_capable_of_requested_fields(tmp_path, monkeypatch):
    root = tmp_path / "freeze"
    DynamicCampaignDataFreezer(root).freeze(
        plan=_plan(),
        transport=FundamentalTransport(),
        start_date="20240101",
        end_date="20240105",
        symbols=SYMBOLS,
    )
    calls: list[str] = []
    original = BitemporalStore.latest_as_of

    def counted(self, **kwargs):
        calls.append(str(kwargs["source"].value))
        return original(self, **kwargs)

    monkeypatch.setattr(BitemporalStore, "latest_as_of", counted)
    bundle = CampaignEvidenceDatasetBuilder().build(
        base=_base(),
        freeze_root=root,
        required_fields=("roe",),
        optional_fields=("shibor_overnight",),
    )

    assert bundle.manifest.row_count == len(_base().snapshot.rows)
    assert set(calls) == {"fina_indicator", "shibor"}
    assert len(calls) == bundle.manifest.row_count * 2


def test_replay_is_stable_and_missing_required_field_is_explicit(tmp_path):
    root = tmp_path / "freeze"
    DynamicCampaignDataFreezer(root).freeze(
        plan=_plan(),
        transport=FundamentalTransport(),
        start_date="20240101",
        end_date="20240105",
        symbols=SYMBOLS,
    )
    builder = CampaignEvidenceDatasetBuilder()
    first = builder.build(
        base=_base(), freeze_root=root, required_fields=("roe", "northbound_flow")
    )
    second = builder.build(
        base=_base(), freeze_root=root, required_fields=("roe", "northbound_flow")
    )
    assert first.artifact_hash == second.artifact_hash
    assert first.manifest.missing_required_fields == ("northbound_flow",)
    assert len({row.symbol for row in first.frozen_data.snapshot.rows}) == 2
    assert len(first.frozen_data.snapshot.historical_universe) == 4


def test_projected_fundamental_field_enters_machine_capability_proof(tmp_path):
    root = tmp_path / "freeze"
    plan = _plan()
    DynamicCampaignDataFreezer(root).freeze(
        plan=plan,
        transport=FundamentalTransport(),
        start_date="20240101",
        end_date="20240105",
        symbols=SYMBOLS,
    )
    bundle = CampaignEvidenceDatasetBuilder().build(
        base=_base(), freeze_root=root, required_fields=("roe",)
    )
    requirement = DataRequirementPlanner().build(plan)
    report = DatasetCapabilityAssessor().assess(
        requirement=requirement,
        frozen_data=bundle.frozen_data,
        bitemporal_records=bundle.projected_records,
    )
    proofs = {item.field_name: item for item in report.field_proofs}
    assert "roe" in requirement.required_fields
    assert "ann_date" not in requirement.required_fields
    assert proofs["roe"].usable is True
    assert report.available_at_verified is True
    assert report.revision_semantics_verified is True


def test_tampered_freeze_fails_closed(tmp_path):
    root = tmp_path / "freeze"
    DynamicCampaignDataFreezer(root).freeze(
        plan=_plan(),
        transport=FundamentalTransport(),
        start_date="20240101",
        end_date="20240105",
        symbols=SYMBOLS,
    )
    manifest = root / "freeze_manifest.json"
    manifest.write_text(manifest.read_text(encoding="utf-8") + " ", encoding="utf-8")
    # Whitespace alone is JSON-equivalent, so alter a content-addressed row.
    partition = next((root / "apis" / "fina_indicator" / "partitions").glob("*.json"))
    partition.write_text(partition.read_text(encoding="utf-8").replace('"roe":10', '"roe":99'), encoding="utf-8")
    with pytest.raises(ValueError, match="hash"):
        CampaignEvidenceDatasetBuilder().build(
            base=_base(), freeze_root=root, required_fields=("roe",)
        )


def test_plan_repository_replays_bundle_and_rejects_tampering(tmp_path):
    root = tmp_path / "freeze"
    DynamicCampaignDataFreezer(root).freeze(
        plan=_plan(),
        transport=FundamentalTransport(),
        start_date="20240101",
        end_date="20240105",
        symbols=SYMBOLS,
    )
    bundle = CampaignEvidenceDatasetBuilder().build(
        base=_base(), freeze_root=root, required_fields=("roe",)
    )
    repository = CampaignEvidenceDatasetRepository(tmp_path / "campaign-data")
    repository.save("plan-001", bundle)
    replay = repository.load("plan-001")
    assert replay is not None
    assert replay.artifact_hash == bundle.artifact_hash
    with pytest.raises(ValueError, match="invalid campaign plan id"):
        repository.load("../escape")
    records_path = (
        tmp_path / "campaign-data" / "plan-001" / "projected_records.json"
    )
    records_path.write_text(
        records_path.read_text(encoding="utf-8").replace('"roe":10.0', '"roe":99.0'),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="hash mismatch"):
        repository.load("plan-001")


def test_product_plan_binds_exact_dataset_before_confirmation(tmp_path):
    root = tmp_path / "freeze"
    DynamicCampaignDataFreezer(root).freeze(
        plan=_plan(),
        transport=FundamentalTransport(),
        start_date="20240101",
        end_date="20240105",
        symbols=SYMBOLS,
    )
    bundle = CampaignEvidenceDatasetBuilder().build(
        base=_base(), freeze_root=root, required_fields=("roe",)
    )
    repository = CampaignEvidenceDatasetRepository(tmp_path / "campaign-data")
    service = ResearchOSV3ProductService(
        root=tmp_path / "product",
        data_loader=_base,
        dotenv_path=tmp_path / "missing.env",
        campaign_data_repository=repository,
    )
    try:
        plan = service.create_plan({
            "question": "研究 ROE 质量因子对未来5日收益的预测能力",
            "depth": "标准研究",
            "interactionStyle": "标准式",
        })
        plan_id = plan["campaignId"]
        rebound = service.bind_plan_data(plan_id, bundle)
        confirmed = service.confirm_plan(plan_id)
    finally:
        service.control.close()
    assert rebound["dataScope"]["universe"] == bundle.frozen_data.snapshot.dataset_id
    assert confirmed["dataScope"]["universe"] == bundle.frozen_data.snapshot.dataset_id
    replay = repository.load(plan_id)
    assert replay.artifact_hash == bundle.artifact_hash
    assert "roe" in replay.frozen_data.snapshot.fields
    with pytest.raises(Exception, match="cannot be replaced"):
        service.bind_plan_data(plan_id, bundle)


def test_preparation_service_builds_and_binds_query_specific_data(tmp_path):
    repository = CampaignEvidenceDatasetRepository(tmp_path / "campaign-data")
    service = ResearchOSV3ProductService(
        root=tmp_path / "product",
        data_loader=_base,
        dotenv_path=tmp_path / "missing.env",
        campaign_data_repository=repository,
    )
    try:
        plan = service.create_plan({
            "question": "研究 ROE 质量因子对未来5日收益的预测能力",
            "depth": "标准研究",
            "interactionStyle": "标准式",
        })
        scale_adapter = FakeScaleAdapter()
        preparer = CampaignDataPreparationService(
            root=tmp_path / "preparation",
            scale_adapter=scale_adapter,
            dynamic_transport=FundamentalTransport(),
        )
        result = preparer.prepare(
            product_service=service,
            plan_id=plan["campaignId"],
            end_date="20240105",
            capture_time=datetime(2024, 1, 8, 17, tzinfo=CN),
        )
        replay = preparer.prepare(
            product_service=service,
            plan_id=plan["campaignId"],
            end_date="20240105",
            capture_time=datetime(2024, 1, 9, 17, tzinfo=CN),
        )
        confirmed = service.confirm_plan(plan["campaignId"])
    finally:
        service.control.close()
    assert result.status is CampaignDataPreparationStatus.READY
    assert result.dataset_bundle_hash is not None
    assert result.freeze_manifest_hash is not None
    assert replay.dataset_bundle_hash == result.dataset_bundle_hash
    assert scale_adapter.calls == 1
    assert "roe" in result.fields
    assert confirmed["dataScope"]["universe"].startswith("campaign-scale-spine.evidence.")


def test_market_spine_cache_replays_and_tampering_fails_closed(tmp_path):
    path = tmp_path / "market-spine.json"
    spine = _scale_spine()
    identity = canonical_content_hash({"request": "stable"})

    _save_spine_cache(path, identity, spine)
    replay = _load_spine_cache(path, identity)
    assert replay is not None
    assert replay.artifact_hash == spine.artifact_hash

    path.write_text(
        path.read_text(encoding="utf-8").replace('"close":10.0', '"close":99.0', 1),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="hash mismatch"):
        _load_spine_cache(path, identity)


def test_utc_canonical_timestamps_preserve_same_session_decision_clock(tmp_path):
    spine = _scale_spine()
    round_trip = FrozenAshareScaleInput.from_payload(spine.to_dict())
    base = _base()
    utc_rows = tuple(
        SnapshotRow(
            event_time=row.event_time.astimezone(timezone.utc),
            symbol=row.symbol,
            values=row.values,
        )
        for row in base.snapshot.rows
    )
    utc_base = FrozenValidationData(
        snapshot=MarketSnapshot(
            dataset_id=base.snapshot.dataset_id,
            decision_time=base.snapshot.decision_time.astimezone(timezone.utc),
            start_time=base.snapshot.start_time.astimezone(timezone.utc),
            fields=base.snapshot.fields,
            rows=utc_rows,
            historical_universe={
                datetime.fromisoformat(key).astimezone(timezone.utc).isoformat(): value
                for key, value in base.snapshot.historical_universe.items()
            },
            coverage_by_field=base.snapshot.coverage_by_field,
        ),
        labels=base.labels,
        qualification_hash=base.qualification_hash,
        source_label=base.source_label,
    )
    root = tmp_path / "freeze"
    DynamicCampaignDataFreezer(root).freeze(
        plan=_plan(),
        transport=FundamentalTransport(),
        start_date="20240101",
        end_date="20240105",
        symbols=SYMBOLS,
    )

    bundle = CampaignEvidenceDatasetBuilder().build(
        base=utc_base,
        freeze_root=root,
        optional_fields=("roe",),
    )

    assert bundle.manifest.row_count == len(round_trip.rows)
    report = DatasetCapabilityAssessor().assess(
        requirement=DataRequirementPlanner().build(_plan()),
        frozen_data=bundle.frozen_data,
        bitemporal_records=bundle.projected_records,
    )
    assert report.historical_universe_verified is True


def test_requirement_fields_use_the_normalized_panel_vocabulary() -> None:
    assert _snapshot_required_fields((
        "netprofit_yoy",
        "or_yoy",
        "grossprofit_margin",
        "n_cashflow_act",
        "net_mf_amount",
    )) == (
        "gross_margin",
        "net_income_yoy",
        "net_moneyflow_amount",
        "operating_cashflow",
        "revenue_yoy",
    )
