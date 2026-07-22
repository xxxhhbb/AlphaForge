from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import datetime, timezone

import pytest

from src.veriquant.contracts import (
    AlphaForecast,
    Component,
    ContractValidationError,
    FailureCode,
    ResearchState,
    RiskDecision,
    RiskVerdict,
)
from src.veriquant.downstream import (
    AssetRiskInput,
    DeterministicPortfolioConstructor,
    IndependentRiskGate,
    PortfolioConstructionRequest,
    PortfolioConstraints,
    PortfolioObjectivePolicy,
    RiskGateRequest,
    RiskPolicy,
    estimate_cost_and_risk,
)


NOW = datetime(2025, 1, 31, 16, 0, tzinfo=timezone.utc)
IDENTITY_HASH = "sha256:" + "c" * 64
HASH = "sha256:" + "a" * 64
EVIDENCE_HASH = "sha256:" + "b" * 64


def forecast() -> AlphaForecast:
    return AlphaForecast(
        forecast_id="alpha-forecast-001",
        candidate_hash=HASH,
        evidence_hash=EVIDENCE_HASH,
        universe=("AAA", "BBB", "CCC", "DDD"),
        horizon="5d",
        decision_time=NOW,
        expected_returns={"AAA": 0.05, "BBB": -0.04, "CCC": 0.03, "DDD": -0.02},
        confidence=0.75,
        decay_half_life_days=20.0,
        factor_correlation=0.20,
        evidence_identity_hash=IDENTITY_HASH,
    )


def assets() -> tuple[AssetRiskInput, ...]:
    return (
        AssetRiskInput(
            symbol="AAA", industry="TECH", beta=1.10, volatility=0.20,
            adv_dollars=20_000_000.0, spread_bps=2.0, stress_loss_rate=0.12,
        ),
        AssetRiskInput(
            symbol="BBB", industry="TECH", beta=0.90, volatility=0.18,
            adv_dollars=18_000_000.0, spread_bps=2.5, stress_loss_rate=0.10,
        ),
        AssetRiskInput(
            symbol="CCC", industry="FINANCE", beta=1.20, volatility=0.22,
            adv_dollars=16_000_000.0, spread_bps=3.0, stress_loss_rate=0.14,
        ),
        AssetRiskInput(
            symbol="DDD", industry="FINANCE", beta=0.80, volatility=0.16,
            adv_dollars=15_000_000.0, spread_bps=2.0, stress_loss_rate=0.09,
        ),
    )


def construction_request(
    *,
    constraints: PortfolioConstraints | None = None,
    current_weights: dict[str, float] | None = None,
) -> PortfolioConstructionRequest:
    return PortfolioConstructionRequest(
        request_id="portfolio-request-001",
        forecast=forecast(),
        assets=assets(),
        current_weights=current_weights
        or {"AAA": 0.0, "BBB": 0.0, "CCC": 0.0, "DDD": 0.0},
        constraints=constraints
        or PortfolioConstraints(
            capital=1_000_000.0,
            market_neutral=True,
            max_gross_leverage=0.60,
            max_single_name=0.20,
            max_industry_exposure=0.10,
            max_beta_exposure=0.05,
            max_turnover=0.60,
            max_adv_participation=0.05,
        ),
        objective_policy=PortfolioObjectivePolicy(
            risk_aversion=2.0,
            turnover_penalty=0.0001,
            cost_penalty=1.0,
            impact_coefficient=0.10,
            step_size=0.20,
            iterations=120,
        ),
    )


def test_portfolio_hard_exposure_leverage_turnover_and_liquidity_constraints_bind() -> None:
    result = DeterministicPortfolioConstructor().construct(construction_request())
    diagnostics = result.diagnostics

    assert abs(diagnostics.net_exposure) <= 2e-7
    assert diagnostics.gross_leverage <= 0.60 + 2e-7
    assert diagnostics.max_single_name <= 0.20 + 2e-7
    assert diagnostics.max_industry_exposure <= 0.10 + 2e-7
    assert abs(diagnostics.beta_exposure) <= 0.05 + 2e-7
    assert diagnostics.turnover <= 0.60 + 2e-7
    assert diagnostics.max_adv_participation <= 0.05 + 2e-7
    assert set(result.plan.objective_terms) == {
        "expected_alpha",
        "risk_penalty",
        "turnover_penalty",
        "cost_penalty",
        "objective_value",
    }


def test_infeasible_portfolio_fails_closed_without_relaxation() -> None:
    impossible = construction_request(
        current_weights={"AAA": 0.30, "BBB": 0.0, "CCC": 0.0, "DDD": 0.0},
        constraints=PortfolioConstraints(
            capital=1_000_000.0,
            market_neutral=True,
            max_gross_leverage=1.0,
            max_single_name=0.50,
            max_industry_exposure=1.0,
            max_beta_exposure=1.0,
            max_turnover=0.0,
            max_adv_participation=1.0,
        ),
    )
    with pytest.raises(ContractValidationError) as caught:
        DeterministicPortfolioConstructor().construct(impossible)
    assert caught.value.code is FailureCode.PORTFOLIO_INFEASIBLE


def risk_request(result, policy: RiskPolicy) -> RiskGateRequest:
    return RiskGateRequest(
        request_id="risk-request-001",
        plan=result.plan,
        assets=assets(),
        current_weights={"AAA": 0.0, "BBB": 0.0, "CCC": 0.0, "DDD": 0.0},
        capital=1_000_000.0,
        policy=policy,
        decision_time=NOW,
    )


def test_independent_risk_gate_emits_approve_scale_down_and_reject() -> None:
    result = DeterministicPortfolioConstructor().construct(construction_request())
    gate = IndependentRiskGate()
    approved = gate.assess(
        risk_request(
            result,
            RiskPolicy(
                max_beta_exposure=0.10,
                max_concentration=0.25,
                max_industry_exposure=0.15,
                max_adv_participation=0.06,
                max_gross_leverage=0.70,
                max_stress_loss_rate=0.10,
            ),
        )
    )
    assert approved.verdict is RiskVerdict.APPROVE
    assert approved.approved is True

    scaled = gate.assess(
        risk_request(
            result,
            RiskPolicy(
                max_beta_exposure=0.02,
                max_concentration=0.08,
                max_industry_exposure=0.04,
                max_adv_participation=0.02,
                max_gross_leverage=0.20,
                max_stress_loss_rate=0.03,
                minimum_scale_factor=0.10,
            ),
        )
    )
    assert scaled.verdict is RiskVerdict.SCALE_DOWN
    assert scaled.approved is False
    assert 0.0 < scaled.scale_factor < 1.0

    rejected = gate.assess(
        risk_request(
            result,
            RiskPolicy(
                max_beta_exposure=0.0,
                max_concentration=0.0,
                max_industry_exposure=0.0,
                max_adv_participation=0.0,
                max_gross_leverage=0.0,
                max_stress_loss_rate=0.0,
                minimum_scale_factor=0.90,
            ),
        )
    )
    assert rejected.verdict is RiskVerdict.REJECT
    assert rejected.approved is False
    assert rejected.scale_factor == 0.0


def test_risk_reject_cannot_be_overridden_by_portfolio_or_agent() -> None:
    result = DeterministicPortfolioConstructor().construct(construction_request())
    rejected = IndependentRiskGate().assess(
        risk_request(
            result,
            RiskPolicy(
                max_beta_exposure=0.0,
                max_concentration=0.0,
                max_industry_exposure=0.0,
                max_adv_participation=0.0,
                max_gross_leverage=0.0,
                max_stress_loss_rate=0.0,
                minimum_scale_factor=0.90,
            ),
        )
    )
    with pytest.raises(FrozenInstanceError):
        rejected.verdict = RiskVerdict.APPROVE  # type: ignore[misc]

    forged = rejected.to_dict()
    forged.update(
        {
            "verdict": "APPROVE",
            "approved": True,
            "breaches": [],
            "scale_factor": 1.0,
            "state": "CHALLENGER",
        }
    )
    with pytest.raises(ContractValidationError) as portfolio_attempt:
        RiskDecision.from_payload(forged, component=Component.PORTFOLIO_SERVICE)
    assert portfolio_attempt.value.code is FailureCode.INVALID_ARTIFACT_PRODUCER
    with pytest.raises(ContractValidationError):
        RiskDecision.from_payload(forged, component=Component.LLM_RESEARCHER)


def test_cost_and_risk_do_not_decrease_when_size_increases() -> None:
    result = DeterministicPortfolioConstructor().construct(construction_request())
    current = {"AAA": 0.0, "BBB": 0.0, "CCC": 0.0, "DDD": 0.0}
    small_cost, small_risk = estimate_cost_and_risk(
        weights=result.plan.target_weights,
        current_weights=current,
        assets=assets(),
        capital=1_000_000.0,
        impact_coefficient=0.10,
    )
    large_cost, large_risk = estimate_cost_and_risk(
        weights=result.plan.target_weights,
        current_weights=current,
        assets=assets(),
        capital=2_000_000.0,
        impact_coefficient=0.10,
    )
    assert large_cost >= small_cost
    assert large_risk >= small_risk
    assert large_cost > small_cost
    assert large_risk > small_risk
