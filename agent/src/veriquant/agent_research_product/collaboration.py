"""Deterministic, Evidence-mediated specialist result aggregation.

The aggregator receives only content-addressed machine Evidence summaries.  It
does not expose one specialist's prompt/context to another and owns neither
Evidence calculation nor promotion.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..agent_research_sandbox.process_runner import canonical_hash
from .contracts import ProductExperimentResult, ProductExperimentStatus, ProductRunResult


@dataclass(frozen=True, slots=True)
class EvidenceContribution:
    specialist_id: str
    experiment_id: str
    evidence_hash: str
    court_outcome: str
    metrics: tuple[tuple[str, str], ...]
    authority: str = "OFFICIAL_EVIDENCE"

    def to_dict(self) -> dict[str, Any]:
        return {
            "specialist_id": self.specialist_id, "experiment_id": self.experiment_id,
            "evidence_hash": self.evidence_hash, "court_outcome": self.court_outcome,
            "metrics": [{"name": name, "display_value": value} for name, value in self.metrics],
            "authority": self.authority,
        }


@dataclass(frozen=True, slots=True)
class EvidenceCollaborationResult:
    run_id: str
    contributions: tuple[EvidenceContribution, ...]
    conflict_codes: tuple[str, ...]
    aggregate_outcome: str
    authority: str = "DETERMINISTIC_RESEARCH_AGGREGATION"

    def to_dict(self, *, include_hash: bool = True) -> dict[str, Any]:
        value = {
            "schema_version": "alphaforge.evidence-collaboration/v1",
            "run_id": self.run_id,
            "contributions": [item.to_dict() for item in self.contributions],
            "conflict_codes": list(self.conflict_codes),
            "aggregate_outcome": self.aggregate_outcome,
            "authority": self.authority,
        }
        if include_hash:
            value["artifact_hash"] = canonical_hash(value)
        return value

    @property
    def artifact_hash(self) -> str:
        return str(self.to_dict()["artifact_hash"])


class EvidenceCollaborationAggregator:
    def aggregate(self, result: ProductRunResult) -> EvidenceCollaborationResult:
        official = tuple(
            item for item in result.experiments
            if item.status is ProductExperimentStatus.OFFICIAL_EVIDENCE
            and item.official_evidence_hash and item.court_outcome
        )
        specialists = {item.specialist_id for item in official}
        if len(official) < 2 or len(specialists) < 2:
            raise ValueError("collaboration requires official Evidence from distinct specialists")
        contributions = tuple(sorted(
            (self._contribution(item) for item in official),
            key=lambda item: (item.specialist_id, item.experiment_id),
        ))
        outcomes = {item.court_outcome for item in contributions}
        conflicts = []
        if len(outcomes) > 1:
            conflicts.append("SPECIALIST_VERDICT_CONFLICT")
        for name in sorted({name for item in contributions for name, _ in item.metrics}):
            values = []
            for item in contributions:
                for metric_name, display in item.metrics:
                    if metric_name == name:
                        try:
                            values.append(float(display))
                        except ValueError:
                            pass
            if any(value > 0 for value in values) and any(value < 0 for value in values):
                conflicts.append(f"METRIC_DIRECTION_CONFLICT:{name}")
        # Conservative deterministic synthesis; this is not Promotion.
        if "REJECT" in outcomes:
            aggregate = "REJECT"
        elif outcomes == {"VALIDATED_RESEARCH"}:
            aggregate = "VALIDATED_RESEARCH"
        else:
            aggregate = "REVISE"
        return EvidenceCollaborationResult(
            run_id=result.run_id, contributions=contributions,
            conflict_codes=tuple(sorted(conflicts)), aggregate_outcome=aggregate,
        )

    def summarize_experiments(
        self, *, run_id: str, experiments: tuple[ProductExperimentResult, ...]
    ) -> EvidenceCollaborationResult:
        """Summarize every available Evidence contribution for the product path.

        Unlike the strict multi-specialist acceptance method above, this method
        is defined for zero, one, or many candidates and preserves auxiliary
        authority.  It does not promote or calculate financial metrics.
        """

        contributions = []
        for item in experiments:
            evidence_hash = item.official_evidence_hash or item.research_evidence_hash
            outcome = item.court_outcome or item.research_verdict
            metrics = item.official_metrics or item.research_metrics
            if not evidence_hash or not outcome:
                continue
            contributions.append(EvidenceContribution(
                specialist_id=item.specialist_id,
                experiment_id=item.experiment_id,
                evidence_hash=evidence_hash,
                court_outcome=outcome,
                metrics=tuple(sorted(metrics)),
                authority=(
                    "OFFICIAL_EVIDENCE"
                    if item.official_evidence_hash is not None
                    else "AUXILIARY_ONLY"
                ),
            ))
        ordered = tuple(sorted(
            contributions, key=lambda item: (item.specialist_id, item.experiment_id)
        ))
        conflicts = self._conflicts(ordered)
        outcomes = {item.court_outcome for item in ordered}
        positive = outcomes & {"VALIDATED_RESEARCH", "SUPPORTED", "CONDITIONALLY_SUPPORTED"}
        negative = outcomes & {"REJECT", "REJECTED"}
        if positive and negative:
            aggregate = "REVISE"
        elif positive:
            aggregate = (
                "VALIDATED_RESEARCH"
                if outcomes <= {"VALIDATED_RESEARCH", "SUPPORTED"}
                else "REVISE"
            )
        elif negative and outcomes <= {"REJECT", "REJECTED"}:
            aggregate = "REJECT"
        elif ordered:
            aggregate = "REVISE"
        else:
            aggregate = "INSUFFICIENT_EVIDENCE"
        return EvidenceCollaborationResult(
            run_id=run_id,
            contributions=ordered,
            conflict_codes=conflicts,
            aggregate_outcome=aggregate,
        )

    @staticmethod
    def _conflicts(contributions: tuple[EvidenceContribution, ...]) -> tuple[str, ...]:
        outcomes = {item.court_outcome for item in contributions}
        conflicts: list[str] = []
        if outcomes & {"VALIDATED_RESEARCH", "SUPPORTED", "CONDITIONALLY_SUPPORTED"} and outcomes & {"REJECT", "REJECTED"}:
            conflicts.append("SPECIALIST_VERDICT_CONFLICT")
        authorities = {item.authority for item in contributions}
        if len(authorities) > 1:
            conflicts.append("EVIDENCE_AUTHORITY_MIXED")
        for name in sorted({name for item in contributions for name, _ in item.metrics}):
            values = []
            for item in contributions:
                for metric_name, display in item.metrics:
                    if metric_name == name:
                        try:
                            values.append(float(display))
                        except ValueError:
                            pass
            if any(value > 0 for value in values) and any(value < 0 for value in values):
                conflicts.append(f"METRIC_DIRECTION_CONFLICT:{name}")
        return tuple(sorted(conflicts))

    @staticmethod
    def _contribution(item: ProductExperimentResult) -> EvidenceContribution:
        assert item.official_evidence_hash is not None and item.court_outcome is not None
        return EvidenceContribution(
            specialist_id=item.specialist_id, experiment_id=item.experiment_id,
            evidence_hash=item.official_evidence_hash, court_outcome=item.court_outcome,
            metrics=tuple(sorted(item.official_metrics)),
        )


__all__ = ["EvidenceCollaborationAggregator", "EvidenceCollaborationResult", "EvidenceContribution"]
