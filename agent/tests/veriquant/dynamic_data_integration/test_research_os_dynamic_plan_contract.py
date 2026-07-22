from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json

from veriquant.data import MarketSnapshot, SnapshotRow, canonical_content_hash
from veriquant.integration.research_os_v2 import ResearchOSV2Service
from veriquant.research_validation_pipeline_v2 import FrozenValidationData


APPROVED_SOURCE = "APPROVED_PROXY_DATA_VENDOR"


def _frozen_data(*, sessions: int) -> FrozenValidationData:
    start = datetime(2020, 1, 2, 15, tzinfo=timezone.utc)
    fields = ("close", "volume", "amount", "adj_factor", "market_cap")
    symbols = ("000001.SZ", "600000.SH")
    rows: list[SnapshotRow] = []
    universe: dict[str, tuple[str, ...]] = {}
    for offset in range(sessions):
        event_time = start + timedelta(days=offset)
        universe[event_time.isoformat()] = symbols
        for symbol_offset, symbol in enumerate(symbols):
            close = 10.0 + offset * 0.01 + symbol_offset
            rows.append(
                SnapshotRow(
                    event_time=event_time,
                    symbol=symbol,
                    values={
                        "close": close,
                        "volume": 100_000.0 + offset,
                        "amount": close * (100_000.0 + offset),
                        "adj_factor": 1.0,
                        "market_cap": 1_000_000_000.0 + symbol_offset,
                    },
                )
            )
    snapshot = MarketSnapshot(
        dataset_id="approved-proxy.dynamic-plan.contract",
        decision_time=start + timedelta(days=sessions),
        start_time=start,
        fields=fields,
        rows=tuple(rows),
        historical_universe=universe,
        coverage_by_field={field: 1.0 for field in fields},
    )
    return FrozenValidationData(
        snapshot=snapshot,
        labels={},
        qualification_hash=canonical_content_hash({"qualified": True}),
        source_label=APPROVED_SOURCE,
    )


def _service(tmp_path, *, sessions: int | None) -> ResearchOSV2Service:
    data = None if sessions is None else _frozen_data(sessions=sessions)
    return ResearchOSV2Service(
        root=tmp_path / "research-os-v2",
        data_loader=lambda: data,
        dotenv_path=tmp_path / "missing.env",
    )


def _suggest(service: ResearchOSV2Service, question: str) -> dict[str, object]:
    return service.create_suggestion(
        {
            "question": question,
            "depth": "标准研究",
            "collaborationStyle": "标准式",
        }
    )


def test_research_os_exposes_a_stable_secret_free_dynamic_data_plan(tmp_path) -> None:
    service = _service(tmp_path, sessions=400)
    first = _suggest(service, "研究 lookback=90 的 momentum alpha")
    second = _suggest(service, "研究 lookback=90 的 momentum alpha")

    assert first["dataPlan"] == second["dataPlan"]
    plan = first["dataPlan"]
    assert plan["planHash"].startswith("sha256:")
    assert plan["lookbackSessions"] == 90
    assert plan["minimumSessions"] == 90 + 100 + 60 + 30 + 5 + 15
    assert plan["availableSessions"] == 400
    assert "daily" in plan["requiredApis"]
    assert "close" in plan["requiredFields"]
    assert plan["sourceLabel"] == APPROVED_SOURCE

    rendered = json.dumps(first, ensure_ascii=False, sort_keys=True).lower()
    assert "token" not in rendered
    assert "api_key" not in rendered
    assert "authorization" not in rendered
    assert "official_tushare" not in rendered


def test_user_horizon_is_not_capped_or_silently_shrunk(tmp_path) -> None:
    suggestion = _suggest(
        _service(tmp_path, sessions=400),
        "研究 lookback=500 的 momentum alpha",
    )
    plan = suggestion["dataPlan"]

    assert plan["lookbackSessions"] == 500
    assert plan["minimumSessions"] == 500 + 100 + 60 + 30 + 5 + 15
    assert plan["availableSessions"] == 400
    assert plan["acquisitionRequired"] is True
    assert plan["readiness"] == "PARTIAL"
    assert any(
        item.startswith("history_sessions:400/")
        for item in plan["missingRequirements"]
    )


def test_missing_frozen_data_returns_an_acquisition_plan_not_a_fake_scope(
    tmp_path,
) -> None:
    suggestion = _suggest(
        _service(tmp_path, sessions=None),
        "盈利公告后的资金流能否预测未来收益",
    )
    plan = suggestion["dataPlan"]

    assert suggestion["canRun"] is False
    assert plan["acquisitionRequired"] is True
    assert plan["availableSessions"] == 0
    assert plan["sourceLabel"] == "UNAVAILABLE"
    assert {"income", "forecast", "moneyflow"} <= set(plan["requiredApis"])
    assert plan["missingRequirements"]
    assert plan["researchableParts"] == []
