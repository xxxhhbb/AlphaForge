from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone

from src.veriquant.specialist_research import (
    PreflightStatus,
    SpecialistCandidateDraft,
    SpecialistFactorPreflight,
    SpecialistResearchRequest,
)


def test_specialist_preflight_prefers_certified_qlib_program() -> None:
    request = SpecialistResearchRequest(
        run_id="qlib-specialist-preflight",
        campaign_hash="sha256:" + "a" * 64,
        specialist_id="market_structure",
        question="研究 50 日价格延续",
        universe="cn-ashare-pit",
        label_horizon_sessions=5,
        decision_time=datetime(2026, 7, 21, tzinfo=timezone.utc),
        available_fields=("close", "volume"),
        requested_lookback_sessions=50,
        seed=7,
    )
    candidate = SpecialistCandidateDraft(
        candidate_id="qlib_momentum_50",
        family_id="momentum",
        hypothesis="中期价格延续包含横截面信息。",
        economic_mechanism="投资者反应不足可能形成价格延续。",
        expected_direction="POSITIVE",
        forecast_horizon_sessions=5,
        lookback_sessions=50,
        required_fields=("close",),
        expression="($close/Ref($close,50))-1",
        availability_assumptions=("close available_at <= decision_time",),
        neutralization=("industry",),
        falsification_tests=("成本后收益为负",),
        cost_risks=("换手和冲击成本",),
        exposure_risks=("行业和市值暴露",),
        novelty_explanation="作为受控 Qlib Expression 动量基线。",
    )

    result = SpecialistFactorPreflight().check(request, candidate)

    assert result.status is PreflightStatus.ACCEPTED
    assert result.formula_program_kind == "CERTIFIED_QLIB_EXPRESSION"
    assert result.formula_program_hash == result.effective_program_hash
    assert result.factor_dag_hash is None
    assert result.formula_manifest_hash is not None
    assert result.normalized_expression == "(($close/Ref($close,50))-1)"


def test_specialist_preflight_keeps_legacy_factor_dag_compatibility() -> None:
    request = SpecialistResearchRequest(
        run_id="legacy-specialist-preflight",
        campaign_hash="sha256:" + "b" * 64,
        specialist_id="market_structure",
        question="研究 50 日价格延续",
        universe="cn-ashare-pit",
        label_horizon_sessions=5,
        decision_time=datetime(2026, 7, 21, tzinfo=timezone.utc),
        available_fields=("close",),
        requested_lookback_sessions=50,
        seed=7,
    )
    candidate = SpecialistCandidateDraft(
        candidate_id="legacy_momentum_50",
        family_id="momentum",
        hypothesis="中期价格延续包含横截面信息。",
        economic_mechanism="投资者反应不足可能形成价格延续。",
        expected_direction="POSITIVE",
        forecast_horizon_sessions=5,
        lookback_sessions=50,
        required_fields=("close",),
        expression="rank_cross_section(pct_change(close, 50))",
        availability_assumptions=("close available_at <= decision_time",),
        neutralization=("industry",),
        falsification_tests=("成本后收益为负",),
        cost_risks=("换手和冲击成本",),
        exposure_risks=("行业和市值暴露",),
        novelty_explanation="兼容旧 FactorDAG 已提交候选。",
    )

    result = SpecialistFactorPreflight().check(request, candidate)

    assert result.status is PreflightStatus.ACCEPTED
    assert result.formula_program_kind == "LEGACY_FACTOR_DAG"
    assert result.effective_program_hash is not None
    assert result.formula_program_hash is None


def test_product_preflight_rejects_legacy_factor_dag_fallback() -> None:
    request = SpecialistResearchRequest(
        run_id="product-qlib-only",
        campaign_hash="sha256:" + "d" * 64,
        specialist_id="market_structure",
        question="研究 50 日反转",
        universe="cn-ashare-pit",
        label_horizon_sessions=5,
        decision_time=datetime(2026, 7, 21, tzinfo=timezone.utc),
        available_fields=("close",),
        requested_lookback_sessions=50,
        seed=7,
    )
    legacy = SpecialistCandidateDraft(
        candidate_id="legacy_reversal_50",
        family_id="reversal",
        hypothesis="中期过度反应可能发生反转。",
        economic_mechanism="投资者过度外推后价格均值回归。",
        expected_direction="POSITIVE",
        forecast_horizon_sessions=5,
        lookback_sessions=50,
        required_fields=("close",),
        expression="multiply(-1, pct_change(close, 50))",
        availability_assumptions=("close available_at <= decision_time",),
        neutralization=("industry",),
        falsification_tests=("成本后收益为负",),
        cost_risks=("换手和冲击成本",),
        exposure_risks=("行业和市值暴露",),
        novelty_explanation="验证产品路径不会回退旧语言。",
    )

    rejected = SpecialistFactorPreflight(allow_legacy_factor_dag=False).check(request, legacy)
    assert rejected.status is not PreflightStatus.ACCEPTED
    assert rejected.formula_program_kind == "LEGACY_FACTOR_DAG"
    assert rejected.effective_program_hash is None

    qlib = replace(
        legacy,
        candidate_id="qlib_reversal_50",
        expression="1-($close/Ref($close,50))",
    )
    accepted = SpecialistFactorPreflight(allow_legacy_factor_dag=False).check(request, qlib)
    assert accepted.status is PreflightStatus.ACCEPTED
    assert accepted.formula_program_kind == "CERTIFIED_QLIB_EXPRESSION"
    assert accepted.factor_dag_hash is None


def test_known_difference_qlib_operator_revises_before_official_execution() -> None:
    request = SpecialistResearchRequest(
        run_id="qlib-known-difference",
        campaign_hash="sha256:" + "c" * 64,
        specialist_id="market_structure",
        question="研究条件动量",
        universe="cn-ashare-pit",
        label_horizon_sessions=5,
        decision_time=datetime(2026, 7, 21, tzinfo=timezone.utc),
        available_fields=("close",),
        requested_lookback_sessions=50,
        seed=7,
    )
    candidate = SpecialistCandidateDraft(
        candidate_id="conditional_momentum",
        family_id="momentum",
        hypothesis="均线状态可能条件化价格延续。",
        economic_mechanism="状态依赖的投资者反应不足。",
        expected_direction="POSITIVE",
        forecast_horizon_sessions=5,
        lookback_sessions=50,
        required_fields=("close",),
        expression="If(Mean($close,20)>Mean($close,50),1,0)",
        availability_assumptions=("close available_at <= decision_time",),
        neutralization=("industry",),
        falsification_tests=("状态内效果不稳定",),
        cost_risks=("换手和冲击成本",),
        exposure_risks=("行业暴露",),
        novelty_explanation="条件状态候选。",
    )

    result = SpecialistFactorPreflight().check(request, candidate)

    assert result.status is PreflightStatus.REVISE
    assert result.failure_code == "QLIB_SEMANTICS_PARITY_REQUIRED"
    assert result.effective_program_hash is None
