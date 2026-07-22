"""Frozen identities and deterministic evidence-firewall value objects."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from types import MappingProxyType
from typing import Mapping

from ..contracts import CanonicalContract, Component, EvidenceClass, FailureCode
from ..contracts.base import (
    fail,
    require_aware_datetime,
    require_finite_number,
    require_hash,
    require_identifier,
    require_text,
)


@dataclass(frozen=True, kw_only=True)
class ExperimentIdentity(CanonicalContract):
    experiment_id: str
    dataset_hash: str
    universe_hash: str
    factor_hash: str
    split_hash: str
    label_hash: str
    cost_hash: str
    code_hash: str
    availability_hash: str
    seed: int
    operator_manifest_hash: str | None = None
    formula_policy_hash: str | None = None
    frozen_provider_hash: str | None = None
    review_policy_hash: str | None = None
    source_draft_hash: str | None = None
    produced_by: Component = Component.EVIDENCE_SERVICE

    def __post_init__(self) -> None:
        require_identifier(self.experiment_id, "experiment_id")
        for name in (
            "dataset_hash",
            "universe_hash",
            "factor_hash",
            "split_hash",
            "label_hash",
            "cost_hash",
            "code_hash",
            "availability_hash",
        ):
            require_hash(getattr(self, name), name)
        if isinstance(self.seed, bool) or not isinstance(self.seed, int) or self.seed < 0:
            fail(FailureCode.INVALID_VALUE, "seed must be a non-negative integer")
        formula_bindings = (
            self.operator_manifest_hash,
            self.formula_policy_hash,
            self.frozen_provider_hash,
            self.review_policy_hash,
            self.source_draft_hash,
        )
        if any(value is not None for value in formula_bindings):
            if not all(value is not None for value in formula_bindings):
                fail(FailureCode.MISSING_REQUIRED_FIELD, "certified formula identity bindings must be all-or-none")
            for name in (
                "operator_manifest_hash",
                "formula_policy_hash",
                "frozen_provider_hash",
                "review_policy_hash",
                "source_draft_hash",
            ):
                require_hash(getattr(self, name), name)
        if self.produced_by is not Component.EVIDENCE_SERVICE:
            fail(FailureCode.INVALID_ARTIFACT_PRODUCER, "identity belongs to Evidence Service")

    @property
    def identity_hash(self) -> str:
        return self.artifact_hash


@dataclass(frozen=True, kw_only=True)
class CostModel(CanonicalContract):
    spread_bps: float
    linear_impact_bps: float

    def __post_init__(self) -> None:
        require_finite_number(self.spread_bps, "spread_bps")
        require_finite_number(self.linear_impact_bps, "linear_impact_bps")
        if self.spread_bps < 0 or self.linear_impact_bps < 0:
            fail(FailureCode.INVALID_VALUE, "cost rates cannot be negative")


@dataclass(frozen=True, kw_only=True)
class WalkForwardSpec(CanonicalContract):
    train_periods: int
    validation_periods: int
    sealed_periods: int
    step_periods: int
    quantile_count: int = 2
    annualization: int = 252

    def __post_init__(self) -> None:
        for name in ("train_periods", "validation_periods", "sealed_periods", "step_periods"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                fail(FailureCode.INVALID_VALUE, f"{name} must be a positive integer")
        if not 2 <= self.quantile_count <= 10:
            fail(FailureCode.INVALID_VALUE, "quantile_count must be between 2 and 10")
        if self.annualization < 1:
            fail(FailureCode.INVALID_VALUE, "annualization must be positive")


@dataclass(frozen=True, kw_only=True)
class EvaluationRow:
    event_time: datetime
    symbol: str
    score: float
    label: float
    market_regime: str
    volume: float

    def __post_init__(self) -> None:
        require_aware_datetime(self.event_time, "event_time")
        require_identifier(self.symbol, "symbol")
        require_text(self.market_regime, "market_regime")
        for name in ("score", "label", "volume"):
            value = getattr(self, name)
            require_finite_number(value, name)
        if self.volume <= 0:
            fail(FailureCode.INVALID_VALUE, "volume must be positive")


@dataclass(frozen=True, kw_only=True)
class RegisteredFactor(CanonicalContract):
    factor_id: str
    factor_hash: str
    scores: Mapping[str, float]

    def __post_init__(self) -> None:
        require_identifier(self.factor_id, "factor_id")
        require_hash(self.factor_hash, "factor_hash")
        frozen: dict[str, float] = {}
        for key, value in self.scores.items():
            require_text(key, "score_key")
            require_finite_number(value, "registered_score")
            frozen[key] = float(value)
        object.__setattr__(self, "scores", MappingProxyType(frozen))


@dataclass(frozen=True, kw_only=True)
class MetricSet(CanonicalContract):
    pearson_ic: float
    rank_ic: float
    ic_mean: float
    ic_std: float
    icir: float
    quantile_spread: float
    turnover: float
    gross_return: float
    net_return: float
    spread_cost: float
    linear_impact_cost: float
    max_drawdown: float
    subperiod_positive_fraction: float
    subperiod_min_ic: float
    regime_positive_fraction: float
    regime_min_ic: float
    max_registered_abs_correlation: float
    incremental_ic_mean: float
    observation_count: int
    period_count: int

    def __post_init__(self) -> None:
        for name, value in self.__dict__.items():
            if name in {"observation_count", "period_count"}:
                if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                    fail(FailureCode.INVALID_VALUE, f"{name} must be non-negative")
            else:
                require_finite_number(value, name)

    def flat(self) -> Mapping[str, float]:
        return MappingProxyType(
            {
                name: float(value)
                for name, value in self.__dict__.items()
                if name not in {"observation_count", "period_count"}
            }
            | {
                "observation_count": float(self.observation_count),
                "period_count": float(self.period_count),
            }
        )


@dataclass(frozen=True, kw_only=True)
class OfficialEvidence(CanonicalContract):
    identity_hash: str
    partition: str
    metrics: MetricSet
    input_hashes: tuple[str, ...]
    produced_by: Component = Component.EVIDENCE_SERVICE
    evidence_class: EvidenceClass = EvidenceClass.OFFICIAL_EVIDENCE

    def __post_init__(self) -> None:
        require_hash(self.identity_hash, "identity_hash")
        if self.partition not in {"TRAIN", "VALIDATION", "SEALED"}:
            fail(FailureCode.INVALID_VALUE, "unknown evidence partition")
        if not isinstance(self.metrics, MetricSet):
            fail(FailureCode.INVALID_VALUE, "metrics must be MetricSet")
        if not self.input_hashes:
            fail(FailureCode.INVALID_VALUE, "official evidence requires frozen input hashes")
        for value in self.input_hashes:
            require_hash(value, "input_hashes")
        if self.produced_by is not Component.EVIDENCE_SERVICE:
            fail(FailureCode.INVALID_ARTIFACT_PRODUCER, "official evidence belongs to Evidence Service")
        if self.evidence_class is not EvidenceClass.OFFICIAL_EVIDENCE:
            fail(FailureCode.AUXILIARY_MASQUERADE, "official evidence class is immutable")


VISIBLE_SUMMARY_METRICS = frozenset(
    {
        "pearson_ic",
        "rank_ic",
        "ic_mean",
        "ic_std",
        "icir",
        "quantile_spread",
        "turnover",
        "gross_return",
        "net_return",
        "spread_cost",
        "linear_impact_cost",
        "max_drawdown",
        "subperiod_positive_fraction",
        "subperiod_min_ic",
        "regime_positive_fraction",
        "regime_min_ic",
        "max_registered_abs_correlation",
        "incremental_ic_mean",
        "observation_count",
        "period_count",
    }
)


@dataclass(frozen=True, kw_only=True)
class ResearchSummary(CanonicalContract):
    identity_hash: str
    train_metrics: Mapping[str, float]
    validation_metrics: Mapping[str, float]

    def __post_init__(self) -> None:
        require_hash(self.identity_hash, "identity_hash")
        for field_name in ("train_metrics", "validation_metrics"):
            values = getattr(self, field_name)
            if set(values) - VISIBLE_SUMMARY_METRICS:
                fail(FailureCode.UNAUTHORIZED_CAPABILITY, "summary contains non-allowlisted fields")
            frozen: dict[str, float] = {}
            for key, value in values.items():
                require_finite_number(value, key)
                frozen[key] = float(value)
            object.__setattr__(self, field_name, MappingProxyType(frozen))


class CourtOutcome(str, Enum):
    REJECT = "REJECT"
    REVISE = "REVISE"
    VALIDATED_RESEARCH = "VALIDATED_RESEARCH"
    PROMOTE_CHALLENGER = "PROMOTE_CHALLENGER"


@dataclass(frozen=True, kw_only=True)
class MechanicalDecision(CanonicalContract):
    decision_id: str
    identity_hash: str
    evidence_hash: str
    proposer_id: str
    approver_id: str
    outcome: CourtOutcome
    reason_codes: tuple[str, ...]
    decided_at: datetime
    produced_by: Component = Component.PROMOTION_SERVICE

    def __post_init__(self) -> None:
        require_identifier(self.decision_id, "decision_id")
        require_hash(self.identity_hash, "identity_hash")
        require_hash(self.evidence_hash, "evidence_hash")
        require_identifier(self.proposer_id, "proposer_id")
        require_identifier(self.approver_id, "approver_id")
        if self.proposer_id == self.approver_id:
            fail(FailureCode.SELF_APPROVAL, "the proposer cannot approve itself")
        if not isinstance(self.outcome, CourtOutcome):
            fail(FailureCode.INVALID_VALUE, "unknown court outcome")
        if not self.reason_codes:
            fail(FailureCode.INVALID_VALUE, "court decision requires reason codes")
        require_aware_datetime(self.decided_at, "decided_at")
        if self.produced_by is not Component.PROMOTION_SERVICE:
            fail(FailureCode.INVALID_ARTIFACT_PRODUCER, "decision belongs to Promotion Service")
