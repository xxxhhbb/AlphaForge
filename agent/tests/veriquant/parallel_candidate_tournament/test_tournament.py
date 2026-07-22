from __future__ import annotations

from veriquant.candidate_tournament import CandidateMetricBundle, CandidateTournament, CandidateTournamentEntry, TournamentStatus
from veriquant.research_os import AnswerLevel, ResearchGroupSubmission, ValidationDisposition, ValidationResultRef


def h(char: str) -> str:
    return "sha256:" + char * 64


def submission(group: str, candidates: tuple[str, ...]) -> ResearchGroupSubmission:
    return ResearchGroupSubmission(campaign_hash=h("a"), group_id=group, hypothesis_hash=h("b"), candidate_draft_hashes=candidates, answer_level=AnswerLevel.CONCEPTUAL)


def entry(group: str, candidate: str, disposition: ValidationDisposition, score: float = 0.5) -> CandidateTournamentEntry:
    evidence = h(candidate[-1]) if disposition is not ValidationDisposition.NOT_EVALUATED else None
    validation = ValidationResultRef(request_hash=h("d"), evidence_pack_hash=evidence, disposition=disposition, failure_codes=("UNSUPPORTED",) if disposition is ValidationDisposition.NOT_EVALUATED else ())
    metrics = None
    if disposition in {ValidationDisposition.VALIDATED_RESEARCH, ValidationDisposition.CHALLENGER}:
        metrics = CandidateMetricBundle(evidence_pack_hash=evidence, net_icir_score=score, stability_score=score, cost_capacity_score=score, novelty_score=score, incrementality_score=score, family_budget_score=score)
    return CandidateTournamentEntry(group_id=group, candidate_draft_hash=candidate, validation=validation, metrics=metrics)


def test_every_group_candidate_must_have_terminal_validation() -> None:
    submissions = (submission("market_structure", (h("1"),)), submission("meta_alpha", (h("2"),)))
    result = CandidateTournament().compare(submissions=submissions, entries=(entry("market_structure", h("1"), ValidationDisposition.REJECT),))
    assert result.status is TournamentStatus.INCOMPLETE_VALIDATION
    assert result.winner_candidate_hash is None


def test_rank_uses_full_composite_not_rankic_shortcut() -> None:
    submissions = (submission("market_structure", (h("1"),)), submission("meta_alpha", (h("2"),)))
    result = CandidateTournament().compare(
        submissions=submissions,
        entries=(entry("market_structure", h("1"), ValidationDisposition.VALIDATED_RESEARCH, 0.4), entry("meta_alpha", h("2"), ValidationDisposition.VALIDATED_RESEARCH, 0.9)),
    )
    assert result.winner_candidate_hash == h("2")
    assert result.combination_proposal.status == "REQUIRES_INDEPENDENT_REVALIDATION"


def test_no_candidate_passing_is_valid_completed_result() -> None:
    submissions = (submission("market_structure", (h("1"),)), submission("meta_alpha", (h("2"),)))
    result = CandidateTournament().compare(submissions=submissions, entries=(entry("market_structure", h("1"), ValidationDisposition.REJECT), entry("meta_alpha", h("2"), ValidationDisposition.REVISE)))
    assert result.status is TournamentStatus.COMPLETED_NO_WINNER


def test_unsupported_candidate_is_terminal_accounting_not_missing_validation() -> None:
    submissions = (submission("market_structure", (h("1"), h("2"))),)
    result = CandidateTournament().compare(submissions=submissions, entries=(entry("market_structure", h("1"), ValidationDisposition.REJECT), entry("market_structure", h("2"), ValidationDisposition.NOT_EVALUATED)))
    assert result.status is TournamentStatus.COMPLETED_NO_WINNER
    assert result.unvalidated_candidate_hashes == ()


def test_sealed_partition_is_rejected_by_contract() -> None:
    import pytest
    from veriquant.contracts import ContractValidationError

    with pytest.raises(ContractValidationError):
        CandidateMetricBundle(evidence_pack_hash=h("e"), net_icir_score=.5, stability_score=.5, cost_capacity_score=.5, novelty_score=.5, incrementality_score=.5, family_budget_score=.5, visible_partition="SEALED_TEST")


def test_same_inputs_produce_same_report_hash() -> None:
    submissions = (submission("market_structure", (h("1"),)),)
    entries = (entry("market_structure", h("1"), ValidationDisposition.VALIDATED_RESEARCH, .7),)
    tournament = CandidateTournament()
    assert tournament.compare(submissions=submissions, entries=entries).artifact_hash == tournament.compare(submissions=submissions, entries=entries).artifact_hash
