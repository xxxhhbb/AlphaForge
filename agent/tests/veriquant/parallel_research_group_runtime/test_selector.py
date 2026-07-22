from __future__ import annotations

import pytest

from veriquant.contracts import ContractValidationError
from veriquant.research_campaign import MethodModel, ResearchMode
from veriquant.research_group_runtime import DeterministicResearchGroupSelector, GroupSelectionContext


H = "sha256:" + "b" * 64


def context(**overrides):
    values = dict(
        campaign_hash=H,
        mode=ResearchMode.NEW_ALPHA_EXPLORATION,
        method_models=(MethodModel.HYPOTHESIS_DRIVEN, MethodModel.ADVERSARIAL_FALSIFICATION),
        question_tags=("momentum", "cost"),
        available_fields=("close", "volume", "adv", "registered_factor_summary", "macro"),
        context_keys=("question", "visible_evidence_summary", "data_manifest"),
    )
    values.update(overrides)
    return GroupSelectionContext(**values)


def test_momentum_selects_bounded_complementary_groups() -> None:
    result = DeterministicResearchGroupSelector().select(context())
    ids = tuple(item.group_id for item in result.assignments)
    assert 1 <= len(ids) <= 6
    assert ids[0] == "market_structure"
    assert "meta_alpha" in ids
    assert set(ids).isdisjoint(result.excluded_group_ids)


def test_only_selected_groups_receive_context() -> None:
    result = DeterministicResearchGroupSelector().select(context(max_groups=2))
    assert tuple(item.group_id for item in result.isolated_contexts) == tuple(item.group_id for item in result.assignments)
    assert len(result.isolated_contexts) == 2


def test_sealed_metrics_cannot_enter_selection_context() -> None:
    with pytest.raises(ContractValidationError):
        context(context_keys=("question", "sealed_metrics"))


def test_selection_is_stable() -> None:
    selector = DeterministicResearchGroupSelector()
    assert selector.select(context()).artifact_hash == selector.select(context()).artifact_hash


def test_question_category_is_not_forced_to_all_six_groups() -> None:
    result = DeterministicResearchGroupSelector().select(context(max_groups=3))
    assert 1 <= len(result.assignments) <= 3
    assert len(result.excluded_group_ids) >= 3


def test_comprehensive_context_can_select_all_six_groups() -> None:
    result = DeterministicResearchGroupSelector().select(context(
        max_groups=6,
        question_tags=(
            "momentum",
            "fundamental",
            "earnings",
            "event",
            "alternative",
            "macro",
            "portfolio",
        ),
        available_fields=(
            "close",
            "volume",
            "roe",
            "market_cap",
            "forecast_profit_change_min",
            "document",
            "available_at",
            "shibor_overnight",
        ),
    ))
    assert {item.group_id for item in result.assignments} == {
        "fundamental",
        "expectations_events",
        "market_structure",
        "alternative_data",
        "macro_regime",
        "meta_alpha",
    }
    assert result.excluded_group_ids == ()


def test_candidate_budget_is_one_to_three() -> None:
    with pytest.raises(ContractValidationError):
        context(max_candidates_per_group=4)


def test_groups_missing_executable_minimum_fields_are_excluded() -> None:
    result = DeterministicResearchGroupSelector().select(context(
        available_fields=("open", "high", "low", "close", "volume", "amount", "market_cap"),
        max_groups=3,
    ))
    ids = tuple(item.group_id for item in result.assignments)
    assert ids == ("market_structure", "meta_alpha")
    assert "macro_regime" in result.excluded_group_ids
    assert "fundamental" in result.excluded_group_ids
