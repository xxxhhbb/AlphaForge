"""Deterministic constrained portfolio construction; no risk-approval authority."""

from __future__ import annotations

import math
from collections.abc import Callable
from typing import Mapping, Sequence

from ..contracts import Component, FailureCode, PortfolioPlan, ResearchState
from ..contracts.base import fail
from ..data import canonical_content_hash
from .models import (
    AssetRiskInput,
    PortfolioConstructionRequest,
    PortfolioConstructionResult,
    PortfolioDiagnostics,
)


Vector = list[float]
Projector = Callable[[Vector], Vector]


def _dot(left: Sequence[float], right: Sequence[float]) -> float:
    return sum(a * b for a, b in zip(left, right))


def _sign(value: float) -> float:
    if value > 0.0:
        return 1.0
    if value < 0.0:
        return -1.0
    return 0.0


def _project_l1_ball(values: Vector, radius: float) -> Vector:
    if radius < 0.0:
        fail(FailureCode.PORTFOLIO_INFEASIBLE, "negative L1 radius")
    if sum(abs(value) for value in values) <= radius:
        return list(values)
    if radius == 0.0:
        return [0.0 for _ in values]
    sorted_abs = sorted((abs(value) for value in values), reverse=True)
    cumulative = 0.0
    rho = 0
    for index, value in enumerate(sorted_abs, start=1):
        cumulative += value
        if value - (cumulative - radius) / index > 0.0:
            rho = index
    theta = (sum(sorted_abs[:rho]) - radius) / rho
    return [_sign(value) * max(abs(value) - theta, 0.0) for value in values]


def _project_centered_l1(values: Vector, center: Vector, radius: float) -> Vector:
    projected = _project_l1_ball(
        [value - base for value, base in zip(values, center)], radius
    )
    return [base + delta for base, delta in zip(center, projected)]


def _project_hyperplane(values: Vector, vector: Vector, target: float = 0.0) -> Vector:
    norm = _dot(vector, vector)
    if norm == 0.0:
        if target != 0.0:
            fail(FailureCode.PORTFOLIO_INFEASIBLE, "empty exposure vector")
        return list(values)
    adjustment = (_dot(values, vector) - target) / norm
    return [value - adjustment * loading for value, loading in zip(values, vector)]


def _project_slab(values: Vector, vector: Vector, limit: float) -> Vector:
    exposure = _dot(values, vector)
    if abs(exposure) <= limit:
        return list(values)
    boundary = limit if exposure > 0.0 else -limit
    return _project_hyperplane(values, vector, boundary)


def estimate_cost_and_risk(
    *,
    weights: Mapping[str, float],
    current_weights: Mapping[str, float],
    assets: Sequence[AssetRiskInput],
    capital: float,
    impact_coefficient: float,
) -> tuple[float, float]:
    """Return currency cost/risk; for fixed weights both are monotone in capital."""

    if capital <= 0.0:
        fail(FailureCode.INVALID_VALUE, "capital must be positive")
    cost = 0.0
    variance = 0.0
    for asset in assets:
        weight = float(weights[asset.symbol])
        delta = weight - float(current_weights[asset.symbol])
        trade_dollars = capital * abs(delta)
        cost += trade_dollars * asset.spread_bps / 10_000.0
        cost += impact_coefficient * trade_dollars * trade_dollars / asset.adv_dollars
        variance += (weight * asset.volatility) ** 2
    return cost, capital * math.sqrt(variance)


class DeterministicPortfolioConstructor:
    """Projected-gradient optimizer with hard, never-relaxed constraints."""

    service_id = "deterministic-portfolio-constructor-v1"

    @staticmethod
    def _violations(
        weights: Vector,
        *,
        symbols: tuple[str, ...],
        assets: tuple[AssetRiskInput, ...],
        current: Vector,
        request: PortfolioConstructionRequest,
        multiplier: float = 1.0,
    ) -> tuple[str, ...]:
        constraints = request.constraints
        tolerance = constraints.tolerance * multiplier
        violations: list[str] = []
        if constraints.market_neutral and abs(sum(weights)) > tolerance:
            violations.append("MARKET_NEUTRALITY")
        if sum(abs(value) for value in weights) > constraints.max_gross_leverage + tolerance:
            violations.append("GROSS_LEVERAGE")
        if max((abs(value) for value in weights), default=0.0) > constraints.max_single_name + tolerance:
            violations.append("SINGLE_NAME")
        beta = sum(weight * asset.beta for weight, asset in zip(weights, assets))
        if abs(beta) > constraints.max_beta_exposure + tolerance:
            violations.append("BETA_EXPOSURE")
        industry_exposures: dict[str, float] = {}
        for weight, asset in zip(weights, assets):
            industry_exposures[asset.industry] = industry_exposures.get(asset.industry, 0.0) + weight
        if max((abs(value) for value in industry_exposures.values()), default=0.0) > (
            constraints.max_industry_exposure + tolerance
        ):
            violations.append("INDUSTRY_EXPOSURE")
        turnover = sum(abs(weight - base) for weight, base in zip(weights, current))
        if turnover > constraints.max_turnover + tolerance:
            violations.append("TURNOVER")
        participation = max(
            (
                constraints.capital * abs(weight - base) / asset.adv_dollars
                for weight, base, asset in zip(weights, current, assets)
            ),
            default=0.0,
        )
        if participation > constraints.max_adv_participation + tolerance:
            violations.append("ADV_PARTICIPATION")
        return tuple(violations)

    def _projector(
        self,
        *,
        symbols: tuple[str, ...],
        assets: tuple[AssetRiskInput, ...],
        current: Vector,
        request: PortfolioConstructionRequest,
    ) -> Callable[[Vector], Vector]:
        constraints = request.constraints
        count = len(symbols)
        lower: Vector = []
        upper: Vector = []
        for base, asset in zip(current, assets):
            trade_cap = constraints.max_adv_participation * asset.adv_dollars / constraints.capital
            low = max(-constraints.max_single_name, base - trade_cap)
            high = min(constraints.max_single_name, base + trade_cap)
            if low > high + constraints.tolerance:
                fail(
                    FailureCode.PORTFOLIO_INFEASIBLE,
                    "single-name and ADV constraints have an empty intersection",
                    symbol=asset.symbol,
                )
            lower.append(low)
            upper.append(high)

        projectors: list[Projector] = [
            lambda values: [
                min(max(value, low), high)
                for value, low, high in zip(values, lower, upper)
            ],
            lambda values: _project_l1_ball(values, constraints.max_gross_leverage),
            lambda values: _project_centered_l1(values, current, constraints.max_turnover),
            lambda values: _project_slab(
                values, [asset.beta for asset in assets], constraints.max_beta_exposure
            ),
        ]
        if constraints.market_neutral:
            projectors.append(lambda values: _project_hyperplane(values, [1.0] * count))
        industries = sorted({asset.industry for asset in assets})
        for industry in industries:
            vector = [1.0 if asset.industry == industry else 0.0 for asset in assets]
            projectors.append(
                lambda values, vector=vector: _project_slab(
                    values, vector, constraints.max_industry_exposure
                )
            )

        def project(seed: Vector) -> Vector:
            values = list(seed)
            corrections = [[0.0] * count for _ in projectors]
            for _ in range(2500):
                previous = list(values)
                for index, projector in enumerate(projectors):
                    shifted = [
                        value + correction
                        for value, correction in zip(values, corrections[index])
                    ]
                    projected = projector(shifted)
                    corrections[index] = [
                        before - after for before, after in zip(shifted, projected)
                    ]
                    values = projected
                movement = max(
                    (abs(value - before) for value, before in zip(values, previous)),
                    default=0.0,
                )
                if movement <= constraints.tolerance and not self._violations(
                    values,
                    symbols=symbols,
                    assets=assets,
                    current=current,
                    request=request,
                    multiplier=10.0,
                ):
                    return values
            violations = self._violations(
                values,
                symbols=symbols,
                assets=assets,
                current=current,
                request=request,
                multiplier=10.0,
            )
            if violations:
                fail(
                    FailureCode.PORTFOLIO_INFEASIBLE,
                    "hard portfolio constraints are infeasible",
                    violations=violations,
                )
            return values

        return project

    def construct(self, request: PortfolioConstructionRequest) -> PortfolioConstructionResult:
        if not isinstance(request, PortfolioConstructionRequest):
            fail(FailureCode.INVALID_VALUE, "typed PortfolioConstructionRequest is required")
        symbols = request.forecast.universe
        by_symbol = {asset.symbol: asset for asset in request.assets}
        assets = tuple(by_symbol[symbol] for symbol in symbols)
        current = [request.current_weights[symbol] for symbol in symbols]
        alpha = [request.forecast.expected_returns[symbol] for symbol in symbols]
        project = self._projector(
            symbols=symbols,
            assets=assets,
            current=current,
            request=request,
        )
        weights = project(list(current))
        policy = request.objective_policy
        constraints = request.constraints
        for _ in range(policy.iterations):
            gradient: Vector = []
            for index, asset in enumerate(assets):
                delta = weights[index] - current[index]
                risk_gradient = 2.0 * policy.risk_aversion * asset.volatility**2 * weights[index]
                turnover_gradient = policy.turnover_penalty * _sign(delta)
                spread_gradient = asset.spread_bps / 10_000.0 * _sign(delta)
                impact_gradient = (
                    2.0
                    * policy.impact_coefficient
                    * constraints.capital
                    / asset.adv_dollars
                    * delta
                )
                gradient.append(
                    alpha[index]
                    - risk_gradient
                    - turnover_gradient
                    - policy.cost_penalty * (spread_gradient + impact_gradient)
                )
            proposal = [
                value + policy.step_size * direction
                for value, direction in zip(weights, gradient)
            ]
            weights = project(proposal)

        violations = self._violations(
            weights,
            symbols=symbols,
            assets=assets,
            current=current,
            request=request,
            multiplier=20.0,
        )
        if violations:
            fail(
                FailureCode.PORTFOLIO_INFEASIBLE,
                "optimizer did not produce a hard-constraint-feasible plan",
                violations=violations,
            )
        target = {symbol: weights[index] for index, symbol in enumerate(symbols)}
        expected_alpha = _dot(weights, alpha)
        risk_variance = sum((weight * asset.volatility) ** 2 for weight, asset in zip(weights, assets))
        turnover = sum(abs(weight - base) for weight, base in zip(weights, current))
        estimated_cost, estimated_risk = estimate_cost_and_risk(
            weights=target,
            current_weights=request.current_weights,
            assets=assets,
            capital=constraints.capital,
            impact_coefficient=policy.impact_coefficient,
        )
        risk_penalty = policy.risk_aversion * risk_variance
        turnover_penalty = policy.turnover_penalty * turnover
        cost_penalty = policy.cost_penalty * estimated_cost / constraints.capital
        objective_value = expected_alpha - risk_penalty - turnover_penalty - cost_penalty
        material = canonical_content_hash(
            {
                "service_id": self.service_id,
                "request_hash": request.artifact_hash,
                "target_weights": target,
            }
        )
        constraints_hash = canonical_content_hash(
            {
                "constraints": request.constraints,
                "objective_policy": request.objective_policy,
                "assets": assets,
            }
        )
        plan = PortfolioPlan(
            plan_id="portfolio-" + material.removeprefix("sha256:")[:24],
            forecast_hash=request.forecast.artifact_hash,
            universe=symbols,
            decision_time=request.forecast.decision_time,
            target_weights=target,
            constraints_hash=constraints_hash,
            objective_terms={
                "expected_alpha": expected_alpha,
                "risk_penalty": risk_penalty,
                "turnover_penalty": turnover_penalty,
                "cost_penalty": cost_penalty,
                "objective_value": objective_value,
            },
            produced_by=Component.PORTFOLIO_SERVICE,
            state=ResearchState.CHALLENGER,
        )
        industry: dict[str, float] = {}
        for weight, asset in zip(weights, assets):
            industry[asset.industry] = industry.get(asset.industry, 0.0) + weight
        participation = max(
            (
                constraints.capital * abs(weight - base) / asset.adv_dollars
                for weight, base, asset in zip(weights, current, assets)
            ),
            default=0.0,
        )
        stress_loss = constraints.capital * sum(
            abs(weight) * asset.stress_loss_rate for weight, asset in zip(weights, assets)
        )
        diagnostics = PortfolioDiagnostics(
            plan_hash=plan.artifact_hash,
            expected_alpha=expected_alpha,
            risk_variance=risk_variance,
            estimated_risk=estimated_risk,
            turnover=turnover,
            estimated_cost=estimated_cost,
            gross_leverage=sum(abs(value) for value in weights),
            net_exposure=sum(weights),
            beta_exposure=sum(weight * asset.beta for weight, asset in zip(weights, assets)),
            max_industry_exposure=max((abs(value) for value in industry.values()), default=0.0),
            max_single_name=max((abs(value) for value in weights), default=0.0),
            max_adv_participation=participation,
            stress_loss=stress_loss,
            objective_value=objective_value,
        )
        return PortfolioConstructionResult(plan=plan, diagnostics=diagnostics)
