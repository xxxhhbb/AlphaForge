from __future__ import annotations

from dataclasses import replace

import pytest

from veriquant.contracts import ContractValidationError, FailureCode
from veriquant.factor_review import (
    FactorReviewAgent,
    FactorReviewPolicy,
    FactorReviewRequest,
    FactorReviewVerdict,
    RegisteredDraftReference,
)
from veriquant.specialist_research import CandidatePreflightResult, PreflightStatus, SpecialistCandidateDraft


HASH_A = "sha256:" + "a" * 64
HASH_B = "sha256:" + "b" * 64


def _candidate(**changes) -> SpecialistCandidateDraft:
    values = dict(
        candidate_id="momentum_50d",
        family_id="price_momentum",
        hypothesis="Past medium-term returns contain persistent cross-sectional information.",
        economic_mechanism="Gradual information diffusion can create return continuation.",
        expected_direction="higher score predicts higher forward return",
        forecast_horizon_sessions=5,
        lookback_sessions=50,
        required_fields=("close",),
        expression="Rank(Ref($close, 50) / $close - 1)",
        availability_assumptions=("close is available after the frozen session close",),
        neutralization=("industry",),
        falsification_tests=("lag the signal by one additional session",),
        cost_risks=("turnover may erase gross performance",),
        exposure_risks=("may load on market beta and recent winners",),
        novelty_explanation="A transparent medium-term baseline for independent evaluation.",
    )
    values.update(changes)
    return SpecialistCandidateDraft(**values)


def _accepted(candidate: SpecialistCandidateDraft) -> CandidatePreflightResult:
    return CandidatePreflightResult(
        candidate_id=candidate.candidate_id,
        draft_hash=candidate.artifact_hash,
        status=PreflightStatus.ACCEPTED,
        normalized_expression=candidate.expression,
        formula_program_hash=HASH_A,
        formula_program_kind="CERTIFIED_QLIB_EXPRESSION",
        formula_manifest_hash=HASH_B,
        message="certified for review",
    )


def _request(candidate=None, preflight=None, **changes) -> FactorReviewRequest:
    selected = candidate or _candidate()
    values = dict(
        proposer_id="research-specialist.market_structure",
        reviewer_id="critic.factor-review",
        candidate=selected,
        preflight=preflight or _accepted(selected),
        available_fields=("close", "volume", "market_cap", "industry"),
        policy=FactorReviewPolicy(),
    )
    values.update(changes)
    return FactorReviewRequest(**values)


def test_accepts_consistent_independent_draft_for_certification() -> None:
    first = FactorReviewAgent().review(_request())
    second = FactorReviewAgent().review(_request())

    assert first.verdict is FactorReviewVerdict.ACCEPT_FOR_CERTIFICATION
    assert first.findings == () and first.revision_advice is None
    assert first.artifact_hash == second.artifact_hash
    assert first.policy_hash == FactorReviewPolicy().artifact_hash
    assert first.authority == "DRAFT_REVIEW_ONLY"


def test_same_proposer_cannot_review_own_draft() -> None:
    with pytest.raises(ContractValidationError) as error:
        _request(reviewer_id="research-specialist.market_structure")
    assert error.value.code is FailureCode.SELF_APPROVAL


def test_missing_field_and_expression_mismatch_get_one_bounded_revision() -> None:
    candidate = _candidate(required_fields=("close", "analyst_revision"))
    preflight = CandidatePreflightResult(
        candidate_id=candidate.candidate_id,
        draft_hash=candidate.artifact_hash,
        status=PreflightStatus.CAPABILITY_REQUEST,
        failure_code="MISSING_DATA_FIELDS",
        message="missing analyst_revision",
    )
    decision = FactorReviewAgent().review(_request(candidate, preflight))

    assert decision.verdict is FactorReviewVerdict.REVISE
    assert decision.revision_advice is not None
    assert decision.revision_advice.revision_number == 1
    assert len(decision.revision_advice.instructions) == len(decision.revision_advice.reason_codes)


def test_prohibited_future_proxy_is_rejected_not_rewritten() -> None:
    candidate = _candidate(
        required_fields=("future_return",),
        expression="Rank($future_return)",
    )
    decision = FactorReviewAgent().review(_request(
        candidate,
        _accepted(candidate),
        available_fields=("future_return",),
    ))
    assert decision.verdict is FactorReviewVerdict.REJECT_DRAFT
    assert decision.revision_advice is None
    assert "UNTRADEABLE_PROXY" in {item.code.value for item in decision.findings}


def test_duplicate_registered_draft_is_rejected() -> None:
    candidate = _candidate()
    previous = RegisteredDraftReference(
        candidate_id="registered_momentum",
        candidate_hash=HASH_B,
        family_id="price_momentum",
        normalized_expression=" rank( ref($close,50) / $close - 1 ) ",
    )
    decision = FactorReviewAgent().review(_request(candidate, registered_drafts=(previous,)))
    assert decision.verdict is FactorReviewVerdict.REJECT_DRAFT
    assert decision.findings[0].related_candidate_id == "registered_momentum"


def test_failed_certification_after_one_revision_is_terminal_reject() -> None:
    original = _candidate()
    revised = replace(
        original,
        candidate_id="momentum_50d_r1",
        parent_candidate_id=original.candidate_id,
        revision_number=1,
        expression="UnknownSafeOperator($close, 50)",
    )
    preflight = CandidatePreflightResult(
        candidate_id=revised.candidate_id,
        draft_hash=revised.artifact_hash,
        status=PreflightStatus.REVISE,
        failure_code="UNKNOWN_OPERATOR",
        message="unknown certified operator",
    )
    decision = FactorReviewAgent().review(_request(
        revised,
        preflight,
        previous_review_hash=HASH_A,
    ))
    assert decision.verdict is FactorReviewVerdict.REJECT_DRAFT
    assert decision.previous_review_hash == HASH_A
    assert decision.revision_number == 1


def test_review_payload_has_no_financial_truth_or_sealed_surface() -> None:
    payload = FactorReviewAgent().review(_request()).to_dict()
    rendered = str(payload).lower()
    assert all(name not in payload for name in (
        "official_metrics", "promotion", "risk_approval", "target_weights", "orders"
    ))
    assert all(token not in rendered for token in ("sealed_metric", "holdout", "test_rank"))

