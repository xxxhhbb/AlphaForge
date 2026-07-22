"""Typed deterministic inputs and outputs for the Prompt 06 thin slice."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import Mapping

from ..contracts import (
    AlphaForecast,
    CanonicalContract,
    Component,
    FailureCode,
    PortfolioPlan,
    ResearchState,
)
from ..contracts.base import (
    fail,
    require_aware_datetime,
    require_finite_number,
    require_hash,
    require_identifier,
    require_text,
)
from ..evidence import CourtOutcome, MechanicalDecision, OfficialEvidence


def _freeze_numeric_mapping(
    owner: object, field_name: str, values: Mapping[str, float]
) -> None:
    if not isinstance(values, Mapping):
        fail(FailureCode.INVALID_VALUE, f"{field_name} must be a mapping")
    frozen: dict[str, float] = {}
    for key, value in values.items():
        require_text(key, f"{field_name}.key")
        require_finite_number(value, f"{field_name}.{key}")
        frozen[key] = float(value)
    object.__setattr__(owner, field_name, MappingProxyType(frozen))


def _validate_universe(universe: tuple[str, ...]) -> None:
    if not isinstance(universe, tuple) or not universe:
        fail(FailureCode.INVALID_VALUE, "universe must be a non-empty tuple")
    if len(set(universe)) != len(universe):
        fail(FailureCode.INVALID_VALUE, "universe contains duplicates")
    for symbol in universe:
        require_identifier(symbol, "universe.symbol")


@dataclass(frozen=True, kw_only=True)
class AlphaCalibrationRequest(CanonicalContract):
    candidate_id: str
    candidate_hash: str
    state: ResearchState
    universe: tuple[str, ...]
    factor_scores: Mapping[str, float]
    horizon: str
    decision_time: datetime
    evidence: OfficialEvidence
    decision: MechanicalDecision
    decay_half_life_days: float = 20.0

    def __post_init__(self) -> None:
        require_identifier(self.candidate_id, "candidate_id")
        require_hash(self.candidate_hash, "candidate_hash")
        if self.state is not ResearchState.VALIDATED_RESEARCH:
            fail(
                FailureCode.INVALID_ALPHA_SOURCE,
                "Alpha Service accepts only VALIDATED_RESEARCH candidates",
            )
        _validate_universe(self.universe)
        _freeze_numeric_mapping(self, "factor_scores", self.factor_scores)
        if set(self.factor_scores) != set(self.universe):
            fail(FailureCode.INVALID_VALUE, "factor_scores must exactly match universe")
        require_text(self.horizon, "horizon")
        require_aware_datetime(self.decision_time, "decision_time")
        require_finite_number(self.decay_half_life_days, "decay_half_life_days")
        if self.decay_half_life_days <= 0.0:
            fail(FailureCode.INVALID_VALUE, "decay_half_life_days must be positive")
        if not isinstance(self.evidence, OfficialEvidence):
            fail(FailureCode.INVALID_ALPHA_SOURCE, "official Evidence Service output is required")
        if self.evidence.partition not in {"TRAIN", "VALIDATION"}:
            fail(FailureCode.INVALID_ALPHA_SOURCE, "sealed evidence cannot calibrate research alpha")
        if not isinstance(self.decision, MechanicalDecision):
            fail(FailureCode.INVALID_ALPHA_SOURCE, "mechanical Evidence Court decision is required")
        if self.decision.outcome is not CourtOutcome.VALIDATED_RESEARCH:
            fail(FailureCode.INVALID_ALPHA_SOURCE, "candidate was not VALIDATED_RESEARCH")
        if self.decision.evidence_hash != self.evidence.artifact_hash:
            fail(FailureCode.INVALID_HASH, "decision does not bind supplied official evidence")
        if self.decision.identity_hash != self.evidence.identity_hash:
            fail(FailureCode.INVALID_HASH, "decision/evidence identity mismatch")


@dataclass(frozen=True, kw_only=True)
class AssetRiskInput(CanonicalContract):
    symbol: str
    industry: str
    beta: float
    volatility: float
    adv_dollars: float
    spread_bps: float
    stress_loss_rate: float

    def __post_init__(self) -> None:
        require_identifier(self.symbol, "symbol")
        require_identifier(self.industry, "industry")
        for name in (
            "beta",
            "volatility",
            "adv_dollars",
            "spread_bps",
            "stress_loss_rate",
        ):
            require_finite_number(getattr(self, name), name)
        if self.volatility < 0.0 or self.spread_bps < 0.0 or self.stress_loss_rate < 0.0:
            fail(FailureCode.INVALID_VALUE, "risk and cost inputs cannot be negative")
        if self.adv_dollars <= 0.0:
            fail(FailureCode.INVALID_VALUE, "adv_dollars must be positive")


@dataclass(frozen=True, kw_only=True)
class PortfolioObjectivePolicy(CanonicalContract):
    risk_aversion: float = 4.0
    turnover_penalty: float = 0.001
    cost_penalty: float = 1.0
    impact_coefficient: float = 0.10
    step_size: float = 0.25
    iterations: int = 160

    def __post_init__(self) -> None:
        for name in (
            "risk_aversion",
            "turnover_penalty",
            "cost_penalty",
            "impact_coefficient",
            "step_size",
        ):
            require_finite_number(getattr(self, name), name)
            if getattr(self, name) < 0.0:
                fail(FailureCode.INVALID_VALUE, f"{name} cannot be negative")
        if self.step_size <= 0.0:
            fail(FailureCode.INVALID_VALUE, "step_size must be positive")
        if isinstance(self.iterations, bool) or not isinstance(self.iterations, int):
            fail(FailureCode.INVALID_VALUE, "iterations must be an integer")
        if not 1 <= self.iterations <= 2000:
            fail(FailureCode.INVALID_VALUE, "iterations must be between 1 and 2000")


@dataclass(frozen=True, kw_only=True)
class PortfolioConstraints(CanonicalContract):
    capital: float
    market_neutral: bool = True
    max_gross_leverage: float = 1.0
    max_single_name: float = 0.20
    max_industry_exposure: float = 0.20
    max_beta_exposure: float = 0.10
    max_turnover: float = 1.0
    max_adv_participation: float = 0.05
    tolerance: float = 1e-8

    def __post_init__(self) -> None:
        if type(self.market_neutral) is not bool:
            fail(FailureCode.INVALID_VALUE, "market_neutral must be boolean")
        for name in (
            "capital",
            "max_gross_leverage",
            "max_single_name",
            "max_industry_exposure",
            "max_beta_exposure",
            "max_turnover",
            "max_adv_participation",
            "tolerance",
        ):
            require_finite_number(getattr(self, name), name)
        if self.capital <= 0.0 or self.tolerance <= 0.0:
            fail(FailureCode.INVALID_VALUE, "capital and tolerance must be positive")
        if any(
            getattr(self, name) < 0.0
            for name in (
                "max_gross_leverage",
                "max_single_name",
                "max_industry_exposure",
                "max_beta_exposure",
                "max_turnover",
                "max_adv_participation",
            )
        ):
            fail(FailureCode.INVALID_VALUE, "portfolio limits cannot be negative")


@dataclass(frozen=True, kw_only=True)
class PortfolioConstructionRequest(CanonicalContract):
    request_id: str
    forecast: AlphaForecast
    assets: tuple[AssetRiskInput, ...]
    current_weights: Mapping[str, float]
    constraints: PortfolioConstraints
    objective_policy: PortfolioObjectivePolicy

    def __post_init__(self) -> None:
        require_identifier(self.request_id, "request_id")
        if not isinstance(self.forecast, AlphaForecast):
            fail(FailureCode.INVALID_ALPHA_SOURCE, "portfolio requires AlphaForecast")
        if self.forecast.produced_by is not Component.ALPHA_SERVICE:
            fail(FailureCode.INVALID_ALPHA_SOURCE, "forecast was not produced by Alpha Service")
        if self.forecast.state is not ResearchState.CHALLENGER:
            fail(FailureCode.INVALID_ALPHA_SOURCE, "forecast is not an active challenger")
        if not isinstance(self.assets, tuple) or not self.assets:
            fail(FailureCode.INVALID_VALUE, "assets must be a non-empty tuple")
        if not all(isinstance(asset, AssetRiskInput) for asset in self.assets):
            fail(FailureCode.INVALID_VALUE, "assets contain an invalid risk input")
        if len({asset.symbol for asset in self.assets}) != len(self.assets):
            fail(FailureCode.INVALID_VALUE, "asset symbols must be unique")
        if {asset.symbol for asset in self.assets} != set(self.forecast.universe):
            fail(FailureCode.INVALID_VALUE, "asset risk inputs must exactly match forecast universe")
        _freeze_numeric_mapping(self, "current_weights", self.current_weights)
        if set(self.current_weights) != set(self.forecast.universe):
            fail(FailureCode.INVALID_VALUE, "current_weights must exactly match universe")
        if not isinstance(self.constraints, PortfolioConstraints):
            fail(FailureCode.INVALID_VALUE, "constraints must be PortfolioConstraints")
        if not isinstance(self.objective_policy, PortfolioObjectivePolicy):
            fail(FailureCode.INVALID_VALUE, "objective_policy has the wrong type")


@dataclass(frozen=True, kw_only=True)
class PortfolioDiagnostics(CanonicalContract):
    plan_hash: str
    expected_alpha: float
    risk_variance: float
    estimated_risk: float
    turnover: float
    estimated_cost: float
    gross_leverage: float
    net_exposure: float
    beta_exposure: float
    max_industry_exposure: float
    max_single_name: float
    max_adv_participation: float
    stress_loss: float
    objective_value: float

    def __post_init__(self) -> None:
        require_hash(self.plan_hash, "plan_hash")
        for name, value in self.__dict__.items():
            if name != "plan_hash":
                require_finite_number(value, name)


@dataclass(frozen=True, kw_only=True)
class PortfolioConstructionResult(CanonicalContract):
    plan: PortfolioPlan
    diagnostics: PortfolioDiagnostics

    def __post_init__(self) -> None:
        if not isinstance(self.plan, PortfolioPlan) or not isinstance(
            self.diagnostics, PortfolioDiagnostics
        ):
            fail(FailureCode.INVALID_VALUE, "invalid portfolio construction result")
        if self.diagnostics.plan_hash != self.plan.artifact_hash:
            fail(FailureCode.INVALID_HASH, "diagnostics do not bind the portfolio plan")


@dataclass(frozen=True, kw_only=True)
class RiskPolicy(CanonicalContract):
    max_beta_exposure: float
    max_concentration: float
    max_industry_exposure: float
    max_adv_participation: float
    max_gross_leverage: float
    max_stress_loss_rate: float
    minimum_scale_factor: float = 0.20
    tolerance: float = 1e-8

    def __post_init__(self) -> None:
        for name, value in self.__dict__.items():
            require_finite_number(value, name)
            if value < 0.0:
                fail(FailureCode.INVALID_VALUE, f"{name} cannot be negative")
        if not 0.0 <= self.minimum_scale_factor <= 1.0:
            fail(FailureCode.INVALID_VALUE, "minimum_scale_factor must be between zero and one")
        if self.tolerance <= 0.0:
            fail(FailureCode.INVALID_VALUE, "tolerance must be positive")
