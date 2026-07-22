from __future__ import annotations

from datetime import datetime, timezone

import pytest

from veriquant.contracts import Component, ContractValidationError, EvidenceClass
from veriquant.research_closure_contracts import (
    APPROVED_PROXY_VENDOR_LABEL,
    CandidateAttemptReceipt,
    CandidateAttemptStatus,
    CandidateResearchOutcome,
    CampaignResearchOutcome,
    MetricDirection,
    OfficialMetricRecord,
    OpaquePromotionReceiptRef,
    ResearchClosureFailureCode,
    ResearchOutcomeStatus,
    ResearchRunStatus,
    ResearchabilityStatus,
    ResearchabilityVerdict,
    USER_FACING_VOCABULARY,
    UserFacingVocabulary,
    VerifiedDatasetReceipt,
)


HASH_A = "sha256:" + "a" * 64
HASH_B = "sha256:" + "b" * 64
HASH_C = "sha256:" + "c" * 64
HASH_D = "sha256:" + "d" * 64
HASH_E = "sha256:" + "e" * 64
T0 = datetime(2024, 1, 2, 15, 0, tzinfo=timezone.utc)
T1 = datetime(2024, 3, 29, 15, 0, tzinfo=timezone.utc)
TD = datetime(2024, 4, 1, 1, 25, tzinfo=timezone.utc)


def test_missing_data_scope_blocks_researchability() -> None:
    with pytest.raises(ContractValidationError):
        ResearchabilityVerdict(
            verdict_id="verdict.missing",
            status=ResearchabilityStatus.RESEARCHABLE,
            question_hash=HASH_A,
            dataset_ready=True,
            calendar_id=None,
        )


def test_verified_dataset_rejects_official_tushare_mislabel() -> None:
    with pytest.raises(ContractValidationError):
        _dataset(provider_source_label="OFFICIAL_TUSHARE", official_tushare_source=True)


def test_official_metric_requires_evidence_identity() -> None:
    with pytest.raises(ContractValidationError):
        OfficialMetricRecord(
            metric_id="metric.rankic",
            candidate_id="candidate.mom50",
            metric_name="rank_ic",
            value=0.03,
            direction=MetricDirection.HIGHER_IS_BETTER,
            evidence_hash="not-a-hash",
            split="VALIDATION",
            policy_version="policy.v1",
        )


def test_auxiliary_metric_cannot_masquerade_as_official() -> None:
    with pytest.raises(ContractValidationError):
        OfficialMetricRecord(
            metric_id="metric.aux",
            candidate_id="candidate.mom50",
            metric_name="rank_ic",
            value=0.03,
            direction=MetricDirection.HIGHER_IS_BETTER,
            evidence_hash=HASH_A,
            split="VALIDATION",
            policy_version="policy.v1",
            evidence_class=EvidenceClass.AUXILIARY_RESEARCH_EVIDENCE,
        )


def test_llm_cannot_produce_official_metric_payload() -> None:
    payload = _metric().to_dict()
    payload["produced_by"] = Component.LLM_RESEARCHER.value
    with pytest.raises(ContractValidationError):
        OfficialMetricRecord.from_payload(payload, component=Component.LLM_RESEARCHER)


def test_candidate_without_official_evidence_cannot_validate() -> None:
    with pytest.raises(ContractValidationError):
        CandidateResearchOutcome(
            candidate_id="candidate.mom50",
            proposer_component=Component.LLM_RESEARCHER,
            attempt=_attempt(),
            outcome=ResearchOutcomeStatus.VALIDATED_RESEARCH,
            official_metrics=(),
        )


def test_challenger_requires_independent_opaque_promotion_receipt() -> None:
    with pytest.raises(ContractValidationError):
        CandidateResearchOutcome(
            candidate_id="candidate.mom50",
            proposer_component=Component.LLM_RESEARCHER,
            attempt=_attempt(),
            outcome=ResearchOutcomeStatus.CHALLENGER,
            official_metrics=(_metric(),),
            promotion_receipt=None,
        )
    candidate = CandidateResearchOutcome(
        candidate_id="candidate.mom50",
        proposer_component=Component.LLM_RESEARCHER,
        attempt=_attempt(),
        outcome=ResearchOutcomeStatus.CHALLENGER,
        official_metrics=(_metric(),),
        promotion_receipt=OpaquePromotionReceiptRef(
            receipt_id="promotion.opaque",
            receipt_hash=HASH_B,
            policy_version="promotion.v1",
            approver_component=Component.PROMOTION_SERVICE,
        ),
        reason="机器评审允许进入挑战池",
    )
    assert candidate.outcome is ResearchOutcomeStatus.CHALLENGER


def test_campaign_run_status_is_separate_from_research_outcome() -> None:
    candidate = CandidateResearchOutcome(
        candidate_id="candidate.mom50",
        proposer_component=Component.LLM_RESEARCHER,
        attempt=_attempt(status=CandidateAttemptStatus.EVALUATION_REJECTED),
        outcome=ResearchOutcomeStatus.REVISE,
        official_metrics=(_metric(value=-0.01),),
        reason="成本后表现不足，建议修订",
    )
    outcome = CampaignResearchOutcome(
        campaign_id="campaign.momentum50",
        run_id="run.momentum50",
        run_status=ResearchRunStatus.COMPLETED,
        research_outcome=ResearchOutcomeStatus.REVISE,
        dataset_receipt_hash=_dataset().artifact_hash,
        selected_research_groups=("基本面研究小组", "市场结构研究小组"),
        candidate_outcomes=(candidate,),
        summary="系统运行完成，研究结论为需要修订",
    )
    assert outcome.run_status is ResearchRunStatus.COMPLETED
    assert outcome.research_outcome is ResearchOutcomeStatus.REVISE


def test_same_object_has_stable_hash() -> None:
    left = _dataset()
    right = _dataset()
    assert left.canonical_json() == right.canonical_json()
    assert left.artifact_hash == right.artifact_hash


def test_user_labels_do_not_expose_engineering_terms() -> None:
    rendered = USER_FACING_VOCABULARY.canonical_json()
    assert "Desk" not in rendered
    assert "Router" not in rendered
    assert "研究小组" in rendered
    with pytest.raises(ContractValidationError):
        UserFacingVocabulary(specialist_group_label="Desk")


def _dataset(
    *,
    provider_source_label: str = APPROVED_PROXY_VENDOR_LABEL,
    official_tushare_source: bool = False,
) -> VerifiedDatasetReceipt:
    return VerifiedDatasetReceipt(
        receipt_id="dataset.approved_proxy.small",
        provider_source_label=provider_source_label,
        official_tushare_source=official_tushare_source,
        snapshot_hash=HASH_A,
        manifest_hash=HASH_B,
        calendar_hash=HASH_C,
        universe_hash=HASH_D,
        content_hash=HASH_E,
        calendar_id="cn_ashare_sse_szse",
        universe=("000001.SZ", "600000.SH"),
        fields=("close", "volume"),
        row_count=116,
        session_count=58,
        symbol_count=2,
        decision_time=TD,
        start_time=T0,
        end_time=T1,
        coverage_by_field={"close": 1.0, "volume": 1.0},
        research_visible_summary_hash=HASH_A,
    )


def _attempt(
    *,
    status: CandidateAttemptStatus = CandidateAttemptStatus.OFFICIAL_EVIDENCE_READY,
) -> CandidateAttemptReceipt:
    return CandidateAttemptReceipt(
        attempt_id="attempt.mom50.001",
        candidate_id="candidate.mom50",
        family_id="family.momentum",
        status=status,
        draft_hash=HASH_A,
        trial_identity_hash=HASH_B,
        budget_consumed=1,
        terminal_event_hash=HASH_C,
        failure=None,
    )


def _metric(*, value: float = 0.03) -> OfficialMetricRecord:
    return OfficialMetricRecord(
        metric_id="metric.rankic",
        candidate_id="candidate.mom50",
        metric_name="rank_ic",
        value=value,
        direction=MetricDirection.HIGHER_IS_BETTER,
        evidence_hash=HASH_A,
        split="VALIDATION",
        policy_version="evidence.policy.v1",
    )
