"""Visible-only, multi-metric evolutionary parent scoring."""

from __future__ import annotations

import math
from statistics import fmean
from typing import Mapping, Sequence

from ..contracts import ContractValidationError, FailureCode
from ..contracts.base import require_hash
from ..evidence import ResearchSummary
from .models import ParentScore, VisibleParentEvidence


_REQUIRED_METRICS = frozenset(
    {
        "icir",
        "rank_ic",
        "gross_return",
        "net_return",
        "spread_cost",
        "linear_impact_cost",
        "subperiod_positive_fraction",
        "regime_positive_fraction",
        "max_registered_abs_correlation",
        "incremental_ic_mean",
    }
)


def _required(metrics: Mapping[str, float], partition: str) -> None:
    missing = _REQUIRED_METRICS - set(metrics)
    if missing:
        raise ContractValidationError(
            FailureCode.MISSING_REQUIRED_FIELD,
            "parent evidence is missing required visible metrics",
            details={"partition": partition, "fields": tuple(sorted(missing))},
        )


def _net_efficiency(metrics: Mapping[str, float]) -> float:
    gross = float(metrics["gross_return"])
    net = float(metrics["net_return"])
    if abs(gross) <= 1e-15:
        return 0.0 if net <= 0.0 else 1.0
    return max(-1.0, min(1.0, net / abs(gross)))


def parent_evidence_from_summary(
    *,
    candidate_id: str,
    candidate_hash: str,
    summary: ResearchSummary,
    novelty: float,
) -> VisibleParentEvidence:
    """Derive the five scoring dimensions from a redacted Wave 03 summary."""

    if not isinstance(summary, ResearchSummary):
        raise ContractValidationError(
            FailureCode.EVOLUTION_CONTEXT_REJECTED,
            "parent selection accepts only ResearchSummary",
        )
    require_hash(candidate_hash, "candidate_hash")
    _required(summary.train_metrics, "TRAIN")
    _required(summary.validation_metrics, "VALIDATION")
    partitions = (summary.train_metrics, summary.validation_metrics)
    net_icir = fmean(
        float(metrics["icir"]) * _net_efficiency(metrics) for metrics in partitions
    )
    stability = min(
        min(
            float(metrics["subperiod_positive_fraction"]),
            float(metrics["regime_positive_fraction"]),
        )
        for metrics in partitions
    )
    cost = fmean(
        float(metrics["spread_cost"]) + float(metrics["linear_impact_cost"])
        for metrics in partitions
    )
    incrementality = fmean(
        float(metrics["incremental_ic_mean"])
        * (1.0 - min(1.0, abs(float(metrics["max_registered_abs_correlation"]))))
        for metrics in partitions
    )
    rank_ic = fmean(float(metrics["rank_ic"]) for metrics in partitions)
    return VisibleParentEvidence(
        candidate_id=candidate_id,
        candidate_hash=candidate_hash,
        evidence_hash=summary.artifact_hash,
        net_icir=net_icir,
        stability=stability,
        cost=cost,
        novelty=novelty,
        incrementality=incrementality,
        rank_ic_diagnostic=rank_ic,
    )


class ParentSelector:
    """Mechanical score. RankIC is intentionally absent from the formula."""

    def score(self, evidence: VisibleParentEvidence) -> ParentScore:
        if not isinstance(evidence, VisibleParentEvidence):
            raise ContractValidationError(
                FailureCode.EVOLUTION_CONTEXT_REJECTED,
                "parent score input must be visible typed evidence",
            )
        net_icir_component = 0.5 + 0.5 * math.tanh(evidence.net_icir)
        stability_component = evidence.stability
        cost_component = 1.0 / (1.0 + 100.0 * evidence.cost)
        novelty_component = evidence.novelty
        incrementality_component = 0.5 + 0.5 * math.tanh(
            10.0 * evidence.incrementality
        )
        composite = (
            0.30 * net_icir_component
            + 0.20 * stability_component
            + 0.15 * cost_component
            + 0.15 * novelty_component
            + 0.20 * incrementality_component
        )
        return ParentScore(
            candidate_id=evidence.candidate_id,
            candidate_hash=evidence.candidate_hash,
            evidence_hash=evidence.evidence_hash,
            net_icir=evidence.net_icir,
            stability=evidence.stability,
            cost=evidence.cost,
            novelty=evidence.novelty,
            incrementality=evidence.incrementality,
            composite=composite,
        )

    def select(
        self, evidence: Sequence[VisibleParentEvidence], *, count: int
    ) -> tuple[ParentScore, ...]:
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise ContractValidationError(FailureCode.INVALID_VALUE, "invalid parent count")
        if len(evidence) < count:
            raise ContractValidationError(
                FailureCode.EVOLUTION_CONTEXT_REJECTED,
                "not enough eligible visible parents",
            )
        scores = [self.score(item) for item in evidence]
        scores.sort(key=lambda item: (-item.composite, item.candidate_id))
        return tuple(scores[:count])
