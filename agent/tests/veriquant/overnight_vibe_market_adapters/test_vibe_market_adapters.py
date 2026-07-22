from __future__ import annotations

from datetime import datetime, timedelta, timezone

from veriquant.research_tool_contracts import (
    ResearchToolFailureCode,
    ResearchToolRequest,
    ResearchToolResult,
    ResearchToolVisibility,
    canonical_json,
)
from veriquant.vibe_market_adapters import (
    FundFlowResearchAdapter,
    FundamentalsResearchAdapter,
    InMemoryMarketResearchProvider,
    MarketScreenerResearchAdapter,
)


HASH_A = "sha256:" + "a" * 64
T0 = datetime(2026, 7, 19, 9, 30, tzinfo=timezone.utc)


def request(tool_name: str = "fundamentals_summary", allowed_data=("fundamentals", "price_volume", "market_structure", "events")):
    return ResearchToolRequest(
        campaign_id="campaign_001",
        campaign_hash=HASH_A,
        desk_id="fundamental",
        decision_time=T0,
        allowed_data=allowed_data,
        tool_name=tool_name,
        request_id="req_001",
        offline=True,
    )


def row(record_id: str, payload: dict, **overrides):
    values = {
        "record_id": record_id,
        "event_time": T0 - timedelta(days=5),
        "published_at": T0 - timedelta(days=4),
        "available_at": T0 - timedelta(days=3),
        "retrieved_at": T0 - timedelta(days=2),
        "payload": payload,
    }
    values.update(overrides)
    return values


def provider(records, source_class="fundamental_record"):
    return InMemoryMarketResearchProvider(
        provider_id="eastmoney",
        response={
            "provider": "eastmoney",
            "source_class": source_class,
            "manifest": {"dataset": "fixture", "freeze_id": "freeze_001"},
            "records": records,
        },
    )


def test_late_record_is_rejected_as_unavailable_as_of_decision_time():
    late = row(
        "late",
        {"symbol": "000001.SZ", "roe": 0.12},
        available_at=T0 + timedelta(minutes=1),
        retrieved_at=T0 + timedelta(minutes=2),
    )
    result = FundamentalsResearchAdapter(provider([late])).run(request())
    assert isinstance(result, ResearchToolResult)
    assert result.failure_code == ResearchToolFailureCode.DATA_NOT_AVAILABLE_AS_OF
    assert result.artifacts == ()


def test_row_order_changes_do_not_change_canonical_result_hash():
    rows = [
        row("b", {"symbol": "000002.SZ", "roe": 0.09}),
        row("a", {"symbol": "000001.SZ", "roe": 0.12}),
    ]
    first = FundamentalsResearchAdapter(provider(rows)).run(request())
    second = FundamentalsResearchAdapter(provider(list(reversed(rows)))).run(request())
    assert first.failure_code is None
    assert second.failure_code is None
    assert first.artifact_hash == second.artifact_hash


def test_missing_publication_or_availability_semantics_fails_closed():
    missing_available_at = row("missing_time", {"symbol": "000001.SZ", "roe": 0.12})
    del missing_available_at["available_at"]
    result = FundamentalsResearchAdapter(provider([missing_available_at])).run(request())
    assert result.failure_code == ResearchToolFailureCode.MISSING_AVAILABLE_AT


def test_market_screener_cannot_expand_campaign_frozen_universe():
    records = [
        row("inside", {"code": "000001.SZ", "name": "Ping An", "change_pct": 1.2}),
        row("outside", {"code": "600519.SH", "name": "Kweichow", "change_pct": 2.4}),
    ]
    adapter = MarketScreenerResearchAdapter(
        provider(records, source_class="market_screener"),
        frozen_universe=("000001.SZ",),
    )
    result = adapter.run(request("market_screener_summary", allowed_data=("price_volume",)))
    assert result.failure_code is None
    assert result.payload["row_count"] == 1
    assert result.payload["excluded_universe_count"] == 1
    assert result.payload["rows"][0]["code"] == "000001.SZ"


def test_secret_header_or_provider_surface_is_rejected_before_desk_output():
    bad = row("secret", {"symbol": "000001.SZ", "headers": {"Authorization": "Bearer hiddenvalue"}})
    result = FundamentalsResearchAdapter(provider([bad])).run(request())
    assert result.failure_code == ResearchToolFailureCode.SECRET_LEAKAGE
    assert result.artifacts == ()


def test_fake_provider_runs_offline_without_network_or_key():
    fake = provider([row("ok", {"symbol": "000001.SZ", "roe": 0.12})])
    result = FundamentalsResearchAdapter(fake).run(request(), {"fields": ["roe"]})
    assert result.failure_code is None
    assert len(fake.calls) == 1
    assert result.payload["status"] == "ok"


def test_output_contains_no_privileged_authority_fields():
    result = FundamentalsResearchAdapter(provider([row("ok", {"symbol": "000001.SZ", "roe": 0.12})])).run(request())
    rendered = canonical_json(result).lower()
    for forbidden in (
        "official_metrics",
        "promotion",
        "risk_approval",
        "target_weights",
        "orders",
        "ledger",
        "broker",
    ):
        assert forbidden not in rendered


def test_minute_fund_flow_is_auxiliary_only():
    records = [row("minute", {"symbol": "000001.SZ", "timestamp": "2026-07-19 09:31", "main": 100.0})]
    result = FundFlowResearchAdapter(provider(records, source_class="fund_flow")).run(
        request("fund_flow_summary", allowed_data=("market_structure",)),
        {"period": "min"},
    )
    assert result.failure_code is None
    assert result.visibility == ResearchToolVisibility.AUXILIARY_RESEARCH_EVIDENCE


def test_provider_unavailable_does_not_break_offline_startup():
    class FailingProvider:
        provider_id = "eastmoney"

        def fetch_market_research(self, request, query):
            raise RuntimeError("network disabled")

    result = FundamentalsResearchAdapter(FailingProvider()).run(request())
    assert result.failure_code == ResearchToolFailureCode.PROVIDER_UNAVAILABLE
    assert result.payload["status"] == "unavailable"
