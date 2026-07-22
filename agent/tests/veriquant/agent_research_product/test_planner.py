from __future__ import annotations

from pathlib import Path

from veriquant.agent_research_product.planner import (
    OUTPUT_KIND_BY_EXPERIMENT_TYPE,
    ResearchTaskPlanner,
)
from veriquant.research500_harness import load_catalog


ROOT = Path(__file__).resolve().parents[4]
CATALOG = ROOT / ".autobuild" / "veriquant" / "factor_question_500" / "catalog.json"


def test_catalog_plans_preserve_lead_and_output_contract_for_all_500() -> None:
    catalog = load_catalog(CATALOG)
    planner = ResearchTaskPlanner(catalog_path=CATALOG)

    for item in catalog.questions:
        plan = planner.compile(item.question)
        assert plan.lead_specialist == item.lead_specialist
        assert item.lead_specialist in plan.selected_specialists
        assert plan.output_kind == OUTPUT_KIND_BY_EXPERIMENT_TYPE[item.experiment_type]
        assert plan.required_data == tuple(sorted(item.required_data))
        assert plan.source == "RESEARCH500_CATALOG"


def test_same_catalog_question_has_stable_plan_hash() -> None:
    question = load_catalog(CATALOG).questions[0].question
    first = ResearchTaskPlanner(catalog_path=CATALOG).compile(question)
    second = ResearchTaskPlanner(catalog_path=CATALOG).compile(question)
    assert first.to_dict() == second.to_dict()
    assert first.artifact_hash == second.artifact_hash


def test_arbitrary_event_and_diagnostic_questions_choose_typed_outputs() -> None:
    planner = ResearchTaskPlanner()
    event = planner.compile("财报公告后是否存在可交易的延迟反应？")
    diagnostic = planner.compile("为什么最近动量因子失效？")

    assert event.output_kind == "EventTable"
    assert "expectations_events" in event.selected_specialists
    assert diagnostic.output_kind == "DiagnosticTable"
    assert diagnostic.mode == "DIAGNOSE_FACTOR_PORTFOLIO"


def test_task_planner_does_not_limit_every_question_to_three_specialists() -> None:
    plan = ResearchTaskPlanner().compile("研究新闻、公告、宏观状态和资金流共同作用的事件 Alpha")
    assert 1 <= len(plan.selected_specialists) <= 6
    assert plan.lead_specialist in plan.selected_specialists
    assert {"alternative_data", "expectations_events", "macro_regime", "market_structure"} <= set(
        plan.selected_specialists
    )


def test_complex_portfolio_gap_adds_macro_specialist_and_per_group_output() -> None:
    plan = ResearchTaskPlanner().compile(
        "在大 A 市场中，寻找预测未来20日收益、与现有50日动量低相关，"
        "并且在高低 Shibor 环境中稳定的 Alpha。请考虑估值、盈利质量、量价与流动性。"
    )

    assert plan.mode == "PORTFOLIO_GAP_SEARCH"
    assert set(plan.selected_specialists) == {
        "fundamental", "macro_regime", "market_structure", "meta_alpha"
    }
    assert plan.output_kind_for("meta_alpha") == "RegressionTable"
    assert plan.output_kind_for("macro_regime") == "RegimeComparisonTable"
    assert "macro_regime" in plan.required_data
