from __future__ import annotations

from dataclasses import replace

import pytest

from veriquant.contracts import ContractValidationError
from veriquant.research_evidence_bridge import VisibleComparisonMetrics
from veriquant.research_os_v3.evidence_adapter import (
    CandidateEvidenceTerminal,
    EvidenceAdapterStatus,
    EvidenceDispositionRef,
    U4EvidenceAdapterResult,
)
from veriquant.research_os_v3.selection import DeterministicVisibleCandidateSelector


H1 = "sha256:" + "1" * 64
H2 = "sha256:" + "2" * 64
H3 = "sha256:" + "3" * 64
H4 = "sha256:" + "4" * 64
H5 = "sha256:" + "5" * 64
H6 = "sha256:" + "6" * 64
H7 = "sha256:" + "7" * 64
H8 = "sha256:" + "8" * 64


def _metric_values() -> dict[str, float]:
    raw = {
        "icir": 0.5,
        "net_return": 0.09,
        "gross_return": 0.10,
        "turnover": 0.10,
        "max_drawdown": -0.03,
        "subperiod_positive_fraction": 0.80,
        "regime_positive_fraction": 0.80,
        "max_registered_abs_correlation": 0.20,
        "incremental_ic_mean": 0.04,
    }
    return {f"{partition}_{key}": value for partition in ("train", "validation") for key, value in raw.items()}


def _result(*, outcome: str = "VALIDATED_RESEARCH", metrics: dict[str, float] | None = None):
    visible = VisibleComparisonMetrics(
        candidate_id="candidate.one",
        identity_hash=H1,
        metrics=_metric_values() if metrics is None else metrics,
        metric_source_hash=H2,
    )
    disposition = EvidenceDispositionRef(
        candidate_id="candidate.one",
        identity_hash=H1,
        decision_hash=H3,
        evidence_hash=H4,
        proposer_id="research-specialist.fundamental",
        approver_id="evidence.court.visible",
        outcome=outcome,
        reason_codes=("VISIBLE_POLICY_PASS",),
    )
    terminal = CandidateEvidenceTerminal(
        specialist_id="fundamental",
        candidate_id="candidate.one",
        candidate_hash=H5,
        translation_hash=H6,
        identity_hash=H1,
        trial_id="trial.one",
        status=EvidenceAdapterStatus.OFFICIAL_VISIBLE_EVALUATED,
        budget_consumed=True,
        evidence_hashes=(H4,),
        summary_hash=H2,
        visible_metrics=visible,
        disposition=disposition,
        failure_codes=(),
    )
    return U4EvidenceAdapterResult(
        run_id="run-one",
        source_run_hash=H7,
        frozen_data_hash=H8,
        terminals=(terminal,),
        official_evidence_hash=H4,
    )


def test_visible_multi_metric_candidate_is_selected_for_review_not_promoted():
    selector = DeterministicVisibleCandidateSelector()
    result = selector.select(
        evidence=_result(),
        family_ids={H5: "family.momentum"},
        family_budget_limit=8,
    )

    assert result.promotion_review_candidate_hashes == (H5,)
    assert result.records[0].verdict.value == "SELECTED_FOR_PROMOTION_REVIEW"
    assert result.promotion_verdict is None


def test_visible_court_rejection_cannot_become_selection():
    result = DeterministicVisibleCandidateSelector().select(
        evidence=_result(outcome="REJECT"),
        family_ids={H5: "family.momentum"},
        family_budget_limit=8,
    )

    assert result.promotion_review_candidate_hashes == ()
    assert "CRITIC_NOT_PASSED" in result.records[0].reason_codes


def test_rank_ic_only_payload_fails_closed():
    rank_only = {
        "train_rank_ic": 0.2,
        "validation_rank_ic": 0.2,
    }
    with pytest.raises(ContractValidationError):
        DeterministicVisibleCandidateSelector().select(
            evidence=_result(metrics=rank_only),
            family_ids={H5: "family.momentum"},
            family_budget_limit=8,
        )


def test_selection_hash_is_stable_and_family_identity_is_required():
    selector = DeterministicVisibleCandidateSelector()
    first = selector.select(evidence=_result(), family_ids={H5: "family.momentum"}, family_budget_limit=8)
    second = selector.select(evidence=_result(), family_ids={H5: "family.momentum"}, family_budget_limit=8)
    assert first.artifact_hash == second.artifact_hash

    with pytest.raises(ContractValidationError):
        selector.select(evidence=_result(), family_ids={}, family_budget_limit=8)


def test_identical_factor_dag_from_two_specialists_is_shortlisted_once():
    base = _result()
    first = base.terminals[0]
    second_metrics = replace(first.visible_metrics, candidate_id="candidate.two")
    second_disposition = replace(
        first.disposition,
        candidate_id="candidate.two",
        proposer_id="research-specialist.meta_alpha",
    )
    second = replace(
        first,
        specialist_id="meta_alpha",
        candidate_id="candidate.two",
        candidate_hash=H8,
        visible_metrics=second_metrics,
        disposition=second_disposition,
        trial_id="trial.two",
    )
    evidence = replace(base, terminals=(first, second))

    result = DeterministicVisibleCandidateSelector().select(
        evidence=evidence,
        family_ids={H5: "family.momentum", H8: "family.meta"},
        family_budget_limit=8,
        factor_dag_hashes={H5: H3, H8: H3},
    )

    assert result.promotion_review_candidate_hashes == (H5,)
    duplicate = next(item for item in result.records if item.candidate_hash == H8)
    assert duplicate.verdict.value == "REVISE"
    assert duplicate.reason_codes == ("DUPLICATE_FACTOR_DAG",)
