"""Candidate tournament over visible official evidence only."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import ClassVar

from ..contracts import CanonicalContract, FailureCode
from ..contracts.base import fail, require_hash, require_identifier
from ..research_os import ResearchGroupSubmission, ValidationDisposition, ValidationResultRef


SCHEMA_VERSION = "alphaforge.candidate-tournament/v1"


class TournamentStatus(str, Enum):
    COMPLETED_WITH_WINNER = "COMPLETED_WITH_WINNER"
    COMPLETED_NO_WINNER = "COMPLETED_NO_WINNER"
    INCOMPLETE_VALIDATION = "INCOMPLETE_VALIDATION"


@dataclass(frozen=True)
class CandidateMetricBundle(CanonicalContract):
    evidence_pack_hash: str
    net_icir_score: float
    stability_score: float
    cost_capacity_score: float
    novelty_score: float
    incrementality_score: float
    family_budget_score: float
    visible_partition: str = "TRAIN_VALIDATION"
    schema_version: ClassVar[str] = SCHEMA_VERSION

    def __post_init__(self) -> None:
        require_hash(self.evidence_pack_hash, "evidence_pack_hash")
        for name in (
            "net_icir_score", "stability_score", "cost_capacity_score",
            "novelty_score", "incrementality_score", "family_budget_score",
        ):
            value = getattr(self, name)
            if not isinstance(value, (int, float)) or not 0.0 <= float(value) <= 1.0:
                fail(FailureCode.INVALID_VALUE, f"{name} must be normalized to [0,1]", field=name)
        if self.visible_partition != "TRAIN_VALIDATION":
            fail(FailureCode.UNAUTHORIZED_CAPABILITY, "tournament accepts visible train/validation evidence only")

    @property
    def composite_score(self) -> float:
        return round(
            0.25 * self.net_icir_score
            + 0.20 * self.stability_score
            + 0.20 * self.cost_capacity_score
            + 0.15 * self.novelty_score
            + 0.15 * self.incrementality_score
            + 0.05 * self.family_budget_score,
            8,
        )


@dataclass(frozen=True)
class CandidateTournamentEntry(CanonicalContract):
    group_id: str
    candidate_draft_hash: str
    validation: ValidationResultRef
    metrics: CandidateMetricBundle | None
    schema_version: ClassVar[str] = SCHEMA_VERSION

    def __post_init__(self) -> None:
        require_identifier(self.group_id, "group_id")
        require_hash(self.candidate_draft_hash, "candidate_draft_hash")
        if self.validation.disposition in {ValidationDisposition.VALIDATED_RESEARCH, ValidationDisposition.CHALLENGER}:
            if self.metrics is None:
                fail(FailureCode.MISSING_REQUIRED_FIELD, "validated candidate requires comparison metrics")
            if self.validation.evidence_pack_hash != self.metrics.evidence_pack_hash:
                fail(FailureCode.INVALID_HASH, "metric evidence does not match validation evidence")
        elif self.metrics is not None:
            fail(FailureCode.INVALID_STATE, "non-validated candidate cannot enter scored comparison")
        if self.validation.disposition is ValidationDisposition.NOT_EVALUATED and not self.validation.failure_codes:
            fail(FailureCode.MISSING_REQUIRED_FIELD, "non-evaluated candidate requires terminal failure accounting")


@dataclass(frozen=True)
class CombinationProposal(CanonicalContract):
    candidate_draft_hashes: tuple[str, ...]
    status: str = "REQUIRES_INDEPENDENT_REVALIDATION"
    schema_version: ClassVar[str] = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if not 2 <= len(self.candidate_draft_hashes) <= 3:
            fail(FailureCode.INVALID_VALUE, "combination requires two or three candidates")
        for value in self.candidate_draft_hashes:
            require_hash(value, "candidate_draft_hashes")
        if self.status != "REQUIRES_INDEPENDENT_REVALIDATION":
            fail(FailureCode.UNAUTHORIZED_CAPABILITY, "tournament cannot validate a combination")


@dataclass(frozen=True)
class CandidateTournamentResult(CanonicalContract):
    status: TournamentStatus
    ranked_candidate_hashes: tuple[str, ...]
    winner_candidate_hash: str | None
    combination_proposal: CombinationProposal | None
    unvalidated_candidate_hashes: tuple[str, ...]
    compared_group_ids: tuple[str, ...]
    schema_version: ClassVar[str] = SCHEMA_VERSION


class CandidateTournament:
    """Require full candidate accounting, then rank only eligible evidence."""

    def compare(
        self,
        *,
        submissions: tuple[ResearchGroupSubmission, ...],
        entries: tuple[CandidateTournamentEntry, ...],
    ) -> CandidateTournamentResult:
        if not submissions:
            fail(FailureCode.MISSING_REQUIRED_FIELD, "research group submissions are required")
        expected = {
            (submission.group_id, candidate_hash)
            for submission in submissions
            for candidate_hash in submission.candidate_draft_hashes
        }
        received = {(entry.group_id, entry.candidate_draft_hash) for entry in entries}
        if len(received) != len(entries):
            fail(FailureCode.INVALID_VALUE, "duplicate tournament entry")
        unexpected = received - expected
        if unexpected:
            fail(FailureCode.INVALID_VALUE, "entry does not belong to a group submission", entries=tuple(sorted(unexpected)))
        missing = expected - received
        incomplete = missing
        compared_groups = tuple(sorted({submission.group_id for submission in submissions}))
        if incomplete:
            return CandidateTournamentResult(
                status=TournamentStatus.INCOMPLETE_VALIDATION,
                ranked_candidate_hashes=(),
                winner_candidate_hash=None,
                combination_proposal=None,
                unvalidated_candidate_hashes=tuple(sorted(value for _, value in incomplete)),
                compared_group_ids=compared_groups,
            )
        eligible = [
            entry for entry in entries
            if entry.validation.disposition in {ValidationDisposition.VALIDATED_RESEARCH, ValidationDisposition.CHALLENGER}
        ]
        eligible.sort(key=lambda item: (-item.metrics.composite_score, item.candidate_draft_hash))  # type: ignore[union-attr]
        ranked = tuple(item.candidate_draft_hash for item in eligible)
        if not ranked:
            return CandidateTournamentResult(
                status=TournamentStatus.COMPLETED_NO_WINNER,
                ranked_candidate_hashes=(), winner_candidate_hash=None,
                combination_proposal=None, unvalidated_candidate_hashes=(), compared_group_ids=compared_groups,
            )
        combination = CombinationProposal(ranked[:2]) if len(ranked) >= 2 else None
        return CandidateTournamentResult(
            status=TournamentStatus.COMPLETED_WITH_WINNER,
            ranked_candidate_hashes=ranked,
            winner_candidate_hash=ranked[0],
            combination_proposal=combination,
            unvalidated_candidate_hashes=(),
            compared_group_ids=compared_groups,
        )
