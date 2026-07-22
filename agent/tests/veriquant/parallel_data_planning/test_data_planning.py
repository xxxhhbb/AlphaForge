from __future__ import annotations

from veriquant.data_planning import DataAvailabilitySnapshot, DeterministicDataPlanner, PlannedFieldRole
from veriquant.research_campaign import ResearchDepth, ResearchMode
from veriquant.research_os import AnswerLevel, ResearchIntent


def _intent(
    question: str = "挖一下最近50天大动量的因子",
    *,
    mode: ResearchMode = ResearchMode.NEW_ALPHA_EXPLORATION,
    lookback: int | None = 50,
    horizon: int = 5,
) -> ResearchIntent:
    return ResearchIntent(
        question=question,
        mode=mode,
        depth=ResearchDepth.STANDARD_RESEARCH,
        factor_lookback_sessions=lookback,
        label_horizon_sessions=horizon,
        requested_outputs=("formal_report",),
    )


def _availability(
    *,
    fields: tuple[str, ...] = ("close", "volume", "market_cap", "industry"),
    sessions: int = 300,
    symbols: int = 50,
) -> DataAvailabilitySnapshot:
    return DataAvailabilitySnapshot(
        availability_id="availability.test",
        available_fields=fields,
        available_sessions=sessions,
        available_symbols=symbols,
        provider_label="APPROVED_PROXY_DATA_VENDOR",
    )


def test_protocol_sessions_use_full_research_plan_not_only_factor_window() -> None:
    result = DeterministicDataPlanner().plan(_intent(), _availability())

    assert result.data_plan.factor_lookback_sessions == 50
    assert result.data_plan.label_horizon_sessions == 5
    assert result.data_plan.minimum_total_sessions == 260
    assert result.official_evaluation_ready is True
    assert result.strongest_answer_level is AnswerLevel.OFFICIAL_EVALUATED


def test_small_snapshot_blocks_official_layer_but_keeps_planning_result() -> None:
    result = DeterministicDataPlanner().plan(
        _intent(), _availability(sessions=58, symbols=1)
    )

    assert result.official_evaluation_ready is False
    assert result.strongest_answer_level is AnswerLevel.SOURCED_RESEARCH
    assert any("历史交易日不足" in reason for reason in result.official_blockers)
    assert any("股票数量不足" in reason for reason in result.official_blockers)


def test_fields_are_classified_as_required_optional_and_proxy() -> None:
    intent = _intent("北向资金是否增强50日动量？", mode=ResearchMode.VALIDATE_IDEA)
    result = DeterministicDataPlanner().plan(intent, _availability(fields=("close", "volume", "turnover")))
    by_name = {(item.field_name, item.proxy_for): item for item in result.field_availability}

    assert by_name[("northbound_flow", ())].role is PlannedFieldRole.REQUIRED
    assert by_name[("northbound_flow", ())].available is False
    assert by_name[("turnover", ("northbound_flow",))].role is PlannedFieldRole.PROXY
    assert by_name[("turnover", ("northbound_flow",))].available is True
    assert result.official_evaluation_ready is False
    assert any("northbound_flow" in reason for reason in result.official_blockers)


def test_unknown_required_field_degrades_instead_of_silent_substitution() -> None:
    intent = _intent("盈利上调能否预测未来收益？", mode=ResearchMode.VALIDATE_IDEA)
    result = DeterministicDataPlanner().plan(intent, _availability(fields=("close", "volume", "market_cap")))

    assert result.official_evaluation_ready is False
    assert any("earnings_revision" in reason for reason in result.official_blockers)
    assert all("official" not in item.reason.lower() or item.role is not PlannedFieldRole.PROXY for item in result.field_availability)


def test_same_input_produces_same_data_plan_hash() -> None:
    planner = DeterministicDataPlanner()
    left = planner.plan(_intent(), _availability())
    right = planner.plan(_intent(), _availability())

    assert left.canonical_json() == right.canonical_json()
    assert left.artifact_hash == right.artifact_hash
