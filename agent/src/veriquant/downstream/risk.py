"""Independent deterministic Risk Gate; portfolio and agents cannot override it."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import Mapping

from ..contracts import (
    CanonicalContract,
    Component,
    FailureCode,
    PortfolioPlan,
    ResearchState,
    RiskDecision,
    RiskVerdict,
)
from ..contracts.base import (
    fail,
    require_aware_datetime,
    require_finite_number,
    require_identifier,
)
from ..data import canonical_content_hash
from .models import AssetRiskInput, RiskPolicy, _freeze_numeric_mapping


@dataclass(frozen=True, kw_only=True)
class RiskGateRequest(CanonicalContract):
    request_id: str
    plan: PortfolioPlan
    assets: tuple[AssetRiskInput, ...]
    current_weights: Mapping[str, float]
    capital: float
    policy: RiskPolicy
    decision_time: datetime

    def __post_init__(self) -> None:
        require_identifier(self.request_id, "request_id")
        if not isinstance(self.plan, PortfolioPlan):
            fail(FailureCode.RISK_INPUT_INVALID, "Risk Gate requires PortfolioPlan")
        if self.plan.produced_by is not Component.PORTFOLIO_SERVICE:
            fail(FailureCode.RISK_INPUT_INVALID, "plan was not produced by Portfolio Service")
        if self.plan.state is not ResearchState.CHALLENGER:
            fail(FailureCode.RISK_INPUT_INVALID, "plan is not an active challenger")
        if not isinstance(self.assets, tuple) or not self.assets:
            fail(FailureCode.RISK_INPUT_INVALID, "assets must be non-empty")
        if not all(isinstance(asset, AssetRiskInput) for asset in self.assets):
            fail(FailureCode.RISK_INPUT_INVALID, "invalid asset risk input")
        if len({asset.symbol for asset in self.assets}) != len(self.assets):
            fail(FailureCode.RISK_INPUT_INVALID, "duplicate asset risk input")
        if {asset.symbol for asset in self.assets} != set(self.plan.universe):
            fail(FailureCode.RISK_INPUT_INVALID, "risk inputs do not match portfolio universe")
        _freeze_numeric_mapping(self, "current_weights", self.current_weights)
        if set(self.current_weights) != set(self.plan.universe):
            fail(FailureCode.RISK_INPUT_INVALID, "current_weights do not match universe")
        require_finite_number(self.capital, "capital")
        if self.capital <= 0.0:
            fail(FailureCode.RISK_INPUT_INVALID, "capital must be positive")
        if not isinstance(self.policy, RiskPolicy):
            fail(FailureCode.RISK_INPUT_INVALID, "policy must be RiskPolicy")
        require_aware_datetime(self.decision_time, "decision_time")
        if self.decision_time != self.plan.decision_time:
            fail(FailureCode.RISK_INPUT_INVALID, "Risk Gate decision_time must bind plan decision_time")


@dataclass(frozen=True, kw_only=True)
class RiskMeasurements:
    beta_exposure: float
    concentration: float
    industry_exposure: float
    adv_participation: float
    gross_leverage: float
    stress_loss_rate: float

    def as_mapping(self) -> Mapping[str, float]:
        return MappingProxyType(
            {
                "beta_exposure": self.beta_exposure,
                "concentration": self.concentration,
                "industry_exposure": self.industry_exposure,
                "adv_participation": self.adv_participation,
                "gross_leverage": self.gross_leverage,
                "stress_loss_rate": self.stress_loss_rate,
            }
        )


class IndependentRiskGate:
    """Recomputes every risk limit from plan weights and independent raw inputs."""

    gate_id = "independent-risk-gate-v1"

    @staticmethod
    def _measure(
        *,
        weights: Mapping[str, float],
        assets: tuple[AssetRiskInput, ...],
        current_weights: Mapping[str, float],
        capital: float,
    ) -> RiskMeasurements:
        industry: dict[str, float] = {}
        beta = 0.0
        gross = 0.0
        concentration = 0.0
        participation = 0.0
        stress = 0.0
        for asset in assets:
            weight = float(weights[asset.symbol])
            gross += abs(weight)
            concentration = max(concentration, abs(weight))
            beta += weight * asset.beta
            industry[asset.industry] = industry.get(asset.industry, 0.0) + weight
            delta = weight - float(current_weights[asset.symbol])
            participation = max(participation, capital * abs(delta) / asset.adv_dollars)
            stress += abs(weight) * asset.stress_loss_rate
        return RiskMeasurements(
            beta_exposure=abs(beta),
            concentration=concentration,
            industry_exposure=max((abs(value) for value in industry.values()), default=0.0),
            adv_participation=participation,
            gross_leverage=gross,
            stress_loss_rate=stress,
        )

    @staticmethod
    def _breaches(measurements: RiskMeasurements, policy: RiskPolicy) -> tuple[str, ...]:
        tolerance = policy.tolerance
        checks = (
            ("BETA_LIMIT", measurements.beta_exposure, policy.max_beta_exposure),
            ("CONCENTRATION_LIMIT", measurements.concentration, policy.max_concentration),
            ("INDUSTRY_LIMIT", measurements.industry_exposure, policy.max_industry_exposure),
            ("LIQUIDITY_LIMIT", measurements.adv_participation, policy.max_adv_participation),
            ("LEVERAGE_LIMIT", measurements.gross_leverage, policy.max_gross_leverage),
            ("STRESS_LOSS_LIMIT", measurements.stress_loss_rate, policy.max_stress_loss_rate),
        )
        return tuple(code for code, actual, limit in checks if actual > limit + tolerance)

    def _largest_safe_scale(self, request: RiskGateRequest) -> float:
        assets = tuple(
            {asset.symbol: asset for asset in request.assets}[symbol]
            for symbol in request.plan.universe
        )
        target = request.plan.target_weights
        best = 0.0
        for step in range(1000, 0, -1):
            scale = step / 1000.0
            scaled = {symbol: float(target[symbol]) * scale for symbol in request.plan.universe}
            measurements = self._measure(
                weights=scaled,
                assets=assets,
                current_weights=request.current_weights,
                capital=request.capital,
            )
            if not self._breaches(measurements, request.policy):
                best = scale
                break
        return best

    def assess(self, request: RiskGateRequest) -> RiskDecision:
        if not isinstance(request, RiskGateRequest):
            fail(FailureCode.RISK_INPUT_INVALID, "typed RiskGateRequest is required")
        assets_by_symbol = {asset.symbol: asset for asset in request.assets}
        assets = tuple(assets_by_symbol[symbol] for symbol in request.plan.universe)
        measurements = self._measure(
            weights=request.plan.target_weights,
            assets=assets,
            current_weights=request.current_weights,
            capital=request.capital,
        )
        breaches = self._breaches(measurements, request.policy)
        if not breaches:
            verdict = RiskVerdict.APPROVE
            approved = True
            scale_factor = 1.0
            state = ResearchState.CHALLENGER
        else:
            scale_factor = self._largest_safe_scale(request)
            if scale_factor >= request.policy.minimum_scale_factor and scale_factor > 0.0:
                verdict = RiskVerdict.SCALE_DOWN
                approved = False
                state = ResearchState.BLOCKED
            else:
                verdict = RiskVerdict.REJECT
                approved = False
                scale_factor = 0.0
                state = ResearchState.BLOCKED
        decision_hash = canonical_content_hash(
            {
                "gate_id": self.gate_id,
                "request_hash": request.artifact_hash,
                "measurements": measurements.as_mapping(),
                "breaches": breaches,
                "verdict": verdict.value,
                "scale_factor": scale_factor,
            }
        )
        return RiskDecision(
            risk_decision_id="risk-" + decision_hash.removeprefix("sha256:")[:24],
            portfolio_plan_hash=request.plan.artifact_hash,
            decision_time=request.decision_time,
            approved=approved,
            policy_hash=request.policy.artifact_hash,
            breaches=breaches,
            produced_by=Component.RISK_GATE,
            state=state,
            verdict=verdict,
            scale_factor=scale_factor,
            checked_metrics=measurements.as_mapping(),
        )
