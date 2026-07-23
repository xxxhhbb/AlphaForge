"""Deterministic coverage checks between answer requirements and Evidence."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Mapping

from ..agent_research_product.contracts import ProductExperimentResult
from .adapters import AnswerAdapterRegistry
from .contracts import CandidateDefinition, QuestionRequirement


class CoverageStatus(str, Enum):
    SATISFIED = "SATISFIED"
    PARTIAL = "PARTIAL"
    MISSING = "MISSING"


@dataclass(frozen=True, slots=True)
class RequirementCoverage:
    requirement_id: str
    status: CoverageStatus
    candidate_ids: tuple[str, ...]
    covered_semantics: tuple[str, ...]
    missing_semantics: tuple[str, ...]
    missing_metrics: tuple[str, ...]
    reason_codes: tuple[str, ...]


class EvidenceCoverageGate:
    """Check semantic answer coverage without calculating financial metrics."""

    def __init__(self, registry: AnswerAdapterRegistry | None = None) -> None:
        self.registry = registry or AnswerAdapterRegistry()

    def assess(
        self,
        requirement: QuestionRequirement,
        candidates: tuple[CandidateDefinition, ...],
        experiments: Mapping[str, ProductExperimentResult],
    ) -> RequirementCoverage:
        available = tuple(
            candidate for candidate in candidates
            if candidate.candidate_id in experiments
            and self._has_evidence(experiments[candidate.candidate_id])
        )
        semantics_by_id = {
            candidate.candidate_id: self._semantics(candidate, experiments[candidate.candidate_id])
            for candidate in available
        }
        required = set(requirement.dimensions)
        matched = tuple(
            candidate for candidate in available
            if required.intersection(semantics_by_id[candidate.candidate_id])
        )
        covered = set().union(*(semantics_by_id[item.candidate_id] for item in matched)) if matched else set()
        metric_names = {
            name for candidate in matched
            for name, _value in (
                experiments[candidate.candidate_id].official_metrics
                or experiments[candidate.candidate_id].research_metrics
            )
        }
        distinct_alpha = {
            item.source_hash for item in matched if item.result_role == "ALPHA_SIGNAL"
        }
        if len(distinct_alpha) >= requirement.minimum_distinct_candidates:
            covered.add("candidate_comparison")
        missing_semantics = (
            set() if requirement.semantic_match == "ANY" and required.intersection(covered)
            else required - covered
        )
        missing_metrics = set(requirement.required_metrics) - metric_names
        comparison_missing = (
            requirement.comparison_required
            and len(distinct_alpha) < requirement.minimum_distinct_candidates
        )
        reasons = []
        if not matched:
            reasons.append("NO_EVIDENCE_MATCHES_REQUIRED_SEMANTICS")
        if missing_semantics:
            reasons.append("REQUIRED_SEMANTICS_MISSING")
        if missing_metrics:
            reasons.append("REQUIRED_METRICS_MISSING")
        if comparison_missing:
            reasons.append("DISTINCT_CANDIDATE_COMPARISON_MISSING")
        if not reasons:
            status = CoverageStatus.SATISFIED
        elif matched:
            status = CoverageStatus.PARTIAL
        else:
            status = CoverageStatus.MISSING
        return RequirementCoverage(
            requirement_id=requirement.requirement_id,
            status=status,
            candidate_ids=tuple(sorted(item.candidate_id for item in matched)),
            covered_semantics=tuple(sorted(covered.intersection(required))),
            missing_semantics=tuple(sorted(missing_semantics)),
            missing_metrics=tuple(sorted(missing_metrics)),
            reason_codes=tuple(sorted(reasons)),
        )

    def _semantics(
        self, candidate: CandidateDefinition, experiment: ProductExperimentResult
    ) -> set[str]:
        values = set(self.registry.get(candidate.output_kind).dimensions)
        metrics = dict(experiment.official_metrics or experiment.research_metrics)
        if candidate.result_role == "ALPHA_SIGNAL" and (
            (candidate.implementation_summary and candidate.input_fields)
            or len(candidate.method_summary) >= 60
        ):
            values.add("implementable_definition")
        if "incremental_ic_mean" in metrics or "max_registered_abs_correlation" in metrics:
            values.add("incrementality")
        if {"net_return", "turnover", "max_drawdown"}.issubset(metrics):
            values.add("trading_quality")
        return values

    @staticmethod
    def _has_evidence(item: ProductExperimentResult) -> bool:
        return bool(
            (item.official_evidence_hash or item.research_evidence_hash)
            and (item.official_metrics or item.research_metrics)
        )


__all__ = ["CoverageStatus", "EvidenceCoverageGate", "RequirementCoverage"]
