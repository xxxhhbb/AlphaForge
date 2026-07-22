from __future__ import annotations

from datetime import datetime, timezone

import pytest

from veriquant.contracts import Component, ContractValidationError, EvidenceClass
from veriquant.formal_report import FormalReportComposer, ResearchGroupSection, render_markdown
from veriquant.research_closure_contracts import (
    APPROVED_PROXY_VENDOR_LABEL,
    CandidateAttemptReceipt,
    CandidateAttemptStatus,
    CandidateResearchOutcome,
    CampaignResearchOutcome,
    MetricDirection,
    OfficialMetricRecord,
    ResearchOutcomeStatus,
    ResearchRunStatus,
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


def test_no_evidence_report_cannot_claim_validation() -> None:
    outcome = _campaign_outcome(ResearchOutcomeStatus.NO_OFFICIAL_EVIDENCE, metrics=())
    report = _compose(outcome, dataset=None)
    markdown = render_markdown(report)
    assert "无法" in markdown or "尚未" in markdown
    assert "VALIDATED_RESEARCH" not in markdown
    assert "CHALLENGER" not in markdown


def test_revise_report_contains_metric_provenance_and_next_steps() -> None:
    report = _compose(_campaign_outcome(ResearchOutcomeStatus.REVISE, metrics=(_metric("rank_ic", 0.02),)))
    markdown = render_markdown(report)
    assert "APPROVED_PROXY_DATA_VENDOR" in markdown
    assert "rank_ic" in markdown
    assert HASH_A in markdown
    assert "修订" in markdown


def test_metric_must_have_official_identity() -> None:
    with pytest.raises(ContractValidationError):
        OfficialMetricRecord(
            metric_id="metric.bad",
            candidate_id="candidate.mom50",
            metric_name="rank_ic",
            value=0.02,
            direction=MetricDirection.HIGHER_IS_BETTER,
            evidence_hash=HASH_A,
            split="VALIDATION",
            policy_version="policy.v1",
            evidence_class=EvidenceClass.AUXILIARY_RESEARCH_EVIDENCE,
        )


def test_public_report_rejects_forbidden_words_and_secrets() -> None:
    with pytest.raises(ContractValidationError):
        ResearchGroupSection(
            group_id="group.bad",
            group_label="Market Desk",
            hypothesis="测试",
            candidate_expression="pct_change(close, 50)",
            status="完成",
        )
    report = _compose(_campaign_outcome(ResearchOutcomeStatus.REVISE, metrics=(_metric("rank_ic", 0.02),)))
    markdown = render_markdown(report)
    assert "Desk" not in markdown
    assert "Router" not in markdown
    assert "sealed" not in markdown.lower()
    assert "api_key" not in markdown.lower()


def test_same_input_has_stable_report_hash_and_markdown() -> None:
    left = _compose(_campaign_outcome(ResearchOutcomeStatus.REVISE, metrics=(_metric("rank_ic", 0.02),)))
    right = _compose(_campaign_outcome(ResearchOutcomeStatus.REVISE, metrics=(_metric("rank_ic", 0.02),)))
    assert left.artifact_hash == right.artifact_hash
    assert render_markdown(left) == render_markdown(right)


def _compose(outcome: CampaignResearchOutcome, dataset: VerifiedDatasetReceipt | None = None):
    return FormalReportComposer().compose(
        report_id="report.momentum50",
        question="挖一下最近50天大动量的因子",
        mode_label="新 Alpha 探索",
        depth_label="标准研究",
        interaction_label="批处理式",
        campaign_outcome=outcome,
        dataset_receipt=_dataset() if dataset is None and outcome.research_outcome is not ResearchOutcomeStatus.NO_OFFICIAL_EVIDENCE else dataset,
        research_groups=(
            ResearchGroupSection(
                group_id="market_structure",
                group_label="市场结构研究小组",
                hypothesis="50日强势股票可能延续短期风险偏好。",
                candidate_expression="rank_cross_section(pct_change(close, 50))",
                status="已提交候选",
            ),
        ),
        candidate_expressions={"candidate.mom50": "rank_cross_section(pct_change(close, 50))"},
    )


def _campaign_outcome(
    status: ResearchOutcomeStatus,
    *,
    metrics: tuple[OfficialMetricRecord, ...],
) -> CampaignResearchOutcome:
    candidate = CandidateResearchOutcome(
        candidate_id="candidate.mom50",
        proposer_component=Component.LLM_RESEARCHER,
        attempt=CandidateAttemptReceipt(
            attempt_id="attempt.mom50.001",
            candidate_id="candidate.mom50",
            family_id="family.momentum50",
            status=CandidateAttemptStatus.OFFICIAL_EVIDENCE_READY
            if metrics
            else CandidateAttemptStatus.CONSUMED_FAILED,
            draft_hash=HASH_A,
            trial_identity_hash=HASH_B,
            budget_consumed=1,
            terminal_event_hash=HASH_C,
        ),
        outcome=status,
        official_metrics=metrics,
        reason="成本后表现不足，建议修订" if status is ResearchOutcomeStatus.REVISE else "",
    )
    return CampaignResearchOutcome(
        campaign_id="campaign.momentum50",
        run_id="run.momentum50",
        run_status=ResearchRunStatus.COMPLETED,
        research_outcome=status,
        dataset_receipt_hash=_dataset().artifact_hash if metrics else None,
        selected_research_groups=("市场结构研究小组",),
        candidate_outcomes=(candidate,),
        summary="系统运行完成，研究结论按机器证据生成。" if metrics else "系统运行完成，但无官方机器证据。",
    )


def _metric(name: str, value: float) -> OfficialMetricRecord:
    return OfficialMetricRecord(
        metric_id=f"metric.{name}",
        candidate_id="candidate.mom50",
        metric_name=name,
        value=value,
        direction=MetricDirection.HIGHER_IS_BETTER,
        evidence_hash=HASH_A,
        split="VALIDATION",
        policy_version="evidence.policy.v1",
    )


def _dataset() -> VerifiedDatasetReceipt:
    return VerifiedDatasetReceipt(
        receipt_id="dataset.approved_proxy.small",
        provider_source_label=APPROVED_PROXY_VENDOR_LABEL,
        official_tushare_source=False,
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
