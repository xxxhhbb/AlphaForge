from __future__ import annotations

from datetime import date

import pytest

from src.veriquant.dynamic_data_planning import (
    CapabilityAwareResearchRouter,
    DataCapabilitySnapshot,
    DataPlanningRequest,
    DataReadiness,
    DynamicDataRequirementCompiler,
    ResearchTheme,
    UniverseKind,
    UniverseSpec,
)
from src.veriquant.research_campaign import ResearchMode


def _compile(question: str, mode: ResearchMode, **kwargs: object):
    return DynamicDataRequirementCompiler().compile(
        DataPlanningRequest(question=question, research_mode=mode, **kwargs)
    )


def test_history_is_derived_from_explicit_protocol_parts_and_not_fixed_260() -> None:
    plan = _compile(
        "研究 90 日动量预测未来 20 日收益",
        ResearchMode.VALIDATE_IDEA,
        factor_lookback_sessions=90,
        label_horizon_sessions=20,
        train_sessions=200,
        validation_sessions=80,
        sealed_sessions=50,
        buffer_sessions=17,
    )

    assert plan.history.minimum_total_sessions == 90 + 200 + 80 + 50 + 20 + 17
    assert plan.time_range.derived_minimum_sessions == 457
    assert plan.history.minimum_total_sessions != 260


@pytest.mark.parametrize("symbol_count", [1, 2, 300, 5000])
def test_explicit_universe_has_no_product_symbol_count_limit(symbol_count: int) -> None:
    symbols = tuple(f"{index:06d}.SZ" for index in range(symbol_count))
    plan = _compile(
        "测试价格动量",
        ResearchMode.NEW_ALPHA_EXPLORATION,
        universe=UniverseSpec(kind=UniverseKind.EXPLICIT_SYMBOLS, symbols=symbols),
    )

    assert len(plan.universe.symbols) == symbol_count


def test_compiler_maps_all_requested_research_families_to_datasets_apis_and_fields() -> None:
    plan = _compile(
        "结合动量、盈利基本面、公告事件、资金流、宏观状态，寻找对已有组合的增量",
        ResearchMode.PORTFOLIO_GAP_SEARCH,
        start_date=date(2012, 1, 1),
        end_date=date(2026, 6, 30),
    )
    dataset_ids = {item.dataset_id for item in plan.requirements}

    assert set(ResearchTheme) == set(plan.themes)
    assert {
        "market_daily",
        "historical_universe",
        "market_valuation",
        "fundamentals",
        "corporate_events",
        "money_flow",
        "macro_regime",
        "registered_research",
    } <= dataset_ids
    assert {"daily", "fina_indicator", "forecast", "moneyflow", "shibor"} <= set(
        plan.requested_api_names
    )
    assert {"close", "ann_date", "registered_factor_summary"} <= set(plan.requested_fields)
    assert plan.time_range.start_date == date(2012, 1, 1)


def test_same_semantic_input_has_stable_hash_and_ordering() -> None:
    left = _compile("盈利公告后的资金流是否预测收益", ResearchMode.VALIDATE_IDEA)
    right = _compile("盈利公告后的资金流是否预测收益", ResearchMode.VALIDATE_IDEA)

    assert left.canonical_json() == right.canonical_json()
    assert left.plan_hash == right.plan_hash
    assert left.requested_api_names == tuple(sorted(left.requested_api_names))


@pytest.mark.parametrize(
    ("question", "included", "excluded"),
    [
        ("研究 ROE 质量因子", {"fina_indicator"}, {"income", "balancesheet", "cashflow"}),
        ("研究经营现金流质量", {"cashflow", "fina_indicator"}, {"balancesheet"}),
        ("研究业绩预告事件", {"forecast"}, {"express"}),
        ("研究 SHIBOR 利率状态", {"shibor"}, {"index_daily"}),
    ],
)
def test_compiler_requests_minimum_relevant_apis_without_narrowing_semantics(
    question: str,
    included: set[str],
    excluded: set[str],
) -> None:
    plan = _compile(question, ResearchMode.VALIDATE_IDEA)
    assert included <= set(plan.requested_api_names)
    assert not (excluded & set(plan.requested_api_names))
    assert plan.history.minimum_total_sessions > 0


def test_router_selects_one_to_three_relevant_groups_from_real_capability() -> None:
    plan = _compile(
        "盈利公告后的资金流能否预测未来收益",
        ResearchMode.VALIDATE_IDEA,
        train_sessions=10,
        validation_sessions=5,
        sealed_sessions=5,
        buffer_sessions=2,
    )
    capability = DataCapabilitySnapshot(
        available_datasets=(
            "market_daily",
            "historical_universe",
            "market_valuation",
            "fundamentals",
            "corporate_events",
            "money_flow",
        ),
        available_fields=plan.requested_fields,
        available_sessions=500,
        provider_label="APPROVED_PROXY_DATA_VENDOR",
        frozen_snapshot=True,
    )
    result = CapabilityAwareResearchRouter().route(plan, capability)

    assert 1 <= len(result.selected_group_ids) <= 6
    assert set(result.selected_group_ids) <= {
        "fundamental",
        "expectations_events",
        "flow_liquidity",
        "market_structure",
    }
    assert all(item.reason for item in result.group_decisions)
    assert any(not item.selected and item.reason.startswith("excluded:") for item in result.group_decisions)


def test_no_data_returns_conceptual_degradation_and_never_fabricates() -> None:
    plan = _compile("宏观利率状态下的动量", ResearchMode.NEW_ALPHA_EXPLORATION)
    result = CapabilityAwareResearchRouter().route(plan, DataCapabilitySnapshot())

    assert result.readiness is DataReadiness.UNAVAILABLE
    assert result.selected_group_ids == ()
    assert result.researchable_parts == ()
    assert result.missing_requirements
    assert any("conceptual" in item.lower() for item in result.degradation_suggestions)
    assert any("do not fabricate" in item.lower() for item in result.degradation_suggestions)


def test_partial_data_reports_researchable_parts_and_precise_missing_reasons() -> None:
    plan = _compile("基本面盈利质量研究", ResearchMode.VALIDATE_IDEA)
    capability = DataCapabilitySnapshot(
        available_datasets=("market_daily", "historical_universe"),
        available_fields=("ts_code", "trade_date", "close", "vol", "adj_factor", "list_date", "delist_date"),
        available_sessions=30,
        provider_label="FROZEN_RESEARCH_MATERIAL",
        frozen_snapshot=True,
    )
    result = CapabilityAwareResearchRouter().route(plan, capability)

    assert result.readiness is DataReadiness.PARTIAL
    assert result.researchable_parts
    fundamental = next(item for item in result.group_decisions if item.group_id == "fundamental")
    assert not fundamental.selected
    assert "fundamentals" in fundamental.missing_datasets
    assert any(item.startswith("history_sessions:") for item in result.missing_requirements)
    assert any("never silently shorten" in item for item in result.degradation_suggestions)


def test_authority_boundary_and_secret_free_capability_are_enforced() -> None:
    plan = _compile("组合增量", ResearchMode.PORTFOLIO_GAP_SEARCH)

    assert any("credentials" in item for item in plan.authority_restrictions)
    assert any("official evidence" in item for item in plan.authority_restrictions)
    assert any("promotion" in item for item in plan.authority_restrictions)
    with pytest.raises(ValueError):
        DataCapabilitySnapshot(provider_label="token=do-not-leak")
    with pytest.raises(ValueError):
        DataCapabilitySnapshot(provider_label="OFFICIAL_TUSHARE")
