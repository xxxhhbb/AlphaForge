from __future__ import annotations

from veriquant.research_planner import IntentResolution, ResearchIntentV2
from veriquant.research_planner.scenario_matrix import PRODUCT_SCENARIOS, scenario_cases
from veriquant.research_planner.semantic import DeterministicResearchIntentCompiler


def test_matrix_has_24_classes_and_144_cases() -> None:
    cases = scenario_cases()
    assert len(PRODUCT_SCENARIOS) == 24
    assert len(cases) == 144
    assert len({item["case_id"] for item in cases}) == 144
    for scenario in PRODUCT_SCENARIOS:
        variants = {item["variant"] for item in cases if item["scenario_id"] == scenario.scenario_id}
        assert variants == {"canonical", "synonym_1", "synonym_2", "synonym_3", "ambiguous", "irrelevant_number"}


def test_all_semantic_variants_use_general_rules_and_fail_closed_when_ambiguous() -> None:
    compiler = DeterministicResearchIntentCompiler()
    by_id = {item.scenario_id: item for item in PRODUCT_SCENARIOS}
    for case in scenario_cases():
        intent = compiler.compile(str(case["question"]))
        assert isinstance(intent, ResearchIntentV2)
        assert intent.execution_allowed is False and intent.confirmed is False
        assert intent.authority == "DRAFT_ONLY"
        if case["variant"] == "ambiguous":
            assert intent.resolution is not IntentResolution.COMPLETE_DRAFT, case["case_id"]
            assert intent.clarification_questions, case["case_id"]
            continue
        expected = by_id[str(case["scenario_id"])]
        assert intent.mode is expected.mode, case["case_id"]
        if expected.expected_lookbacks:
            assert intent.factor_lookbacks == expected.expected_lookbacks, case["case_id"]
        if expected.expected_horizons:
            assert intent.forecast_horizons == expected.expected_horizons, case["case_id"]
        assert set(expected.required_data_any) <= set(intent.required_data), case["case_id"]


def test_unrelated_numbers_never_become_lookback_or_forecast() -> None:
    compiler = DeterministicResearchIntentCompiler()
    for case in scenario_cases():
        if case["variant"] != "irrelevant_number":
            continue
        intent = compiler.compile(str(case["question"]))
        assert 100 not in intent.factor_lookbacks
        assert 100 not in intent.forecast_horizons
        assert 20260720 not in intent.factor_lookbacks
        assert 20260720 not in intent.forecast_horizons


def test_same_semantics_generate_same_canonical_hash() -> None:
    compiler = DeterministicResearchIntentCompiler()
    left = compiler.compile("用过去50日动量预测未来5日收益")
    right = compiler.compile("用过去50日动量预测未来5日收益")
    assert left.canonical_json() == right.canonical_json()
    assert left.artifact_hash == right.artifact_hash

