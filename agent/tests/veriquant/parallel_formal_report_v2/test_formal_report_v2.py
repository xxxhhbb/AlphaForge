from __future__ import annotations

from datetime import datetime, timezone

import pytest

from veriquant.contracts import Component, ContractValidationError, EvidenceClass
from veriquant.data_planning import DataAvailabilitySnapshot
from veriquant.formal_report_v2 import (
    FormalReportV2Composer,
    ResearchGroupReportV2,
    render_markdown_v2,
)
from veriquant.research_closure_contracts import (
    CandidateAttemptReceipt,
    CandidateAttemptStatus,
    CandidateResearchOutcome,
    CampaignResearchOutcome,
    MetricDirection,
    OfficialMetricRecord,
    ResearchOutcomeStatus,
    ResearchRunStatus,
)
from veriquant.research_planner import DeterministicResearchQuestionPlanner, QuestionAssessmentRequest


HASH_A = "sha256:" + "a" * 64
HASH_B = "sha256:" + "b" * 64
HASH_C = "sha256:" + "c" * 64


def test_partial_report_is_useful_when_official_data_is_missing() -> None:
    assessment = _assessment(
        "北向资金是否增强50日动量？",
        fields=("close", "volume"),
        sessions=80,
        symbols=10,
    )
    report = FormalReportV2Composer().compose(
        report_id="report.northbound.partial",
        campaign_id="campaign.northbound",
        run_id="run.northbound",
        question_assessment=assessment.assessment,
        data_planning=assessment.data_planning,
        selected_groups=(_group(),),
    )
    markdown = render_markdown_v2(report)

    assert "官方机器证据是否可启动：否" in markdown
    assert "northbound_flow" in markdown
    assert "暂无官方机器证据" in markdown
    assert "报告不补写指标" in markdown
    assert "专业研究小组" in markdown
    assert "Desk" not in markdown
    assert "Router" not in markdown


def test_official_report_uses_only_official_metric_refs() -> None:
    assessment = _assessment(
        "挖一下最近50天大动量的因子",
        fields=("close", "volume", "market_cap", "industry"),
        sessions=240,
        symbols=80,
    )
    metric = _metric("rank_ic", 0.031)
    outcome = _campaign_outcome(ResearchOutcomeStatus.REVISE, metrics=(metric,))
    report = FormalReportV2Composer().compose(
        report_id="report.momentum50.official",
        campaign_id="campaign.momentum50",
        run_id="run.momentum50",
        question_assessment=assessment.assessment,
        data_planning=assessment.data_planning,
        selected_groups=(_group(),),
        candidate_outcomes=outcome.candidate_outcomes,
        campaign_outcome=outcome,
        candidate_expressions={"candidate.mom50": "rank_cross_section(pct_change(close, 50))"},
    )
    markdown = render_markdown_v2(report)

    assert "rank_ic" in markdown
    assert HASH_A in markdown
    assert "需要修订" in markdown
    assert report.candidates[0].official_metrics[0].evidence_class is EvidenceClass.OFFICIAL_EVIDENCE


def test_auxiliary_metric_cannot_enter_formal_report_as_official() -> None:
    with pytest.raises(ContractValidationError):
        CandidateResearchOutcome(
            candidate_id="candidate.bad",
            proposer_component=Component.LLM_RESEARCHER,
            attempt=_attempt(),
            outcome=ResearchOutcomeStatus.REVISE,
            official_metrics=(
                OfficialMetricRecord(
                    metric_id="metric.aux",
                    candidate_id="candidate.bad",
                    metric_name="rank_ic",
                    value=0.1,
                    direction=MetricDirection.HIGHER_IS_BETTER,
                    evidence_hash=HASH_A,
                    split="VALIDATION",
                    policy_version="policy.v1",
                    evidence_class=EvidenceClass.AUXILIARY_RESEARCH_EVIDENCE,
                ),
            ),
        )


def test_public_markdown_redacts_sensitive_and_engineering_terms() -> None:
    assessment = _assessment(
        "挖一下最近50天大动量的因子",
        fields=("close", "volume", "market_cap", "industry"),
        sessions=240,
        symbols=80,
    )
    with pytest.raises(ContractValidationError):
        FormalReportV2Composer().compose(
            report_id="report.bad",
            campaign_id="campaign.bad",
            run_id="run.bad",
            question_assessment=assessment.assessment,
            data_planning=assessment.data_planning,
            selected_groups=(
                ResearchGroupReportV2(
                    group_id="market_structure",
                    group_label="Market Desk",
                    hypothesis="测试",
                    candidate_name="bad",
                    candidate_expression="pct_change(close, 50)",
                    status="完成",
                ),
            ),
        )


def test_same_inputs_produce_same_hash_and_markdown() -> None:
    assessment = _assessment(
        "挖一下最近50天大动量的因子",
        fields=("close", "volume", "market_cap", "industry"),
        sessions=240,
        symbols=80,
    )
    kwargs = {
        "report_id": "report.stable",
        "campaign_id": "campaign.stable",
        "run_id": "run.stable",
        "question_assessment": assessment.assessment,
        "data_planning": assessment.data_planning,
        "selected_groups": (_group(),),
    }
    left = FormalReportV2Composer().compose(**kwargs)
    right = FormalReportV2Composer().compose(**kwargs)

    assert left.artifact_hash == right.artifact_hash
    assert render_markdown_v2(left) == render_markdown_v2(right)


def _assessment(question: str, *, fields: tuple[str, ...], sessions: int, symbols: int):
    return DeterministicResearchQuestionPlanner().assess(
        QuestionAssessmentRequest(
            question=question,
            availability=DataAvailabilitySnapshot(
                availability_id="availability.report",
                available_fields=fields,
                available_sessions=sessions,
                available_symbols=symbols,
                provider_label="APPROVED_PROXY_DATA_VENDOR",
            ),
        )
    )


def _group() -> ResearchGroupReportV2:
    return ResearchGroupReportV2(
        group_id="market_structure",
        group_label="市场结构研究小组",
        hypothesis="强势股票可能在短期内延续风险偏好。",
        candidate_name="50日大动量候选",
        candidate_expression="rank_cross_section(pct_change(close, 50))",
        status="已提交草案",
        source_hashes=(HASH_A,),
    )


def _attempt() -> CandidateAttemptReceipt:
    return CandidateAttemptReceipt(
        attempt_id="attempt.mom50.001",
        candidate_id="candidate.mom50",
        family_id="family.momentum50",
        status=CandidateAttemptStatus.OFFICIAL_EVIDENCE_READY,
        draft_hash=HASH_A,
        trial_identity_hash=HASH_B,
        budget_consumed=1,
        terminal_event_hash=HASH_C,
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


def _campaign_outcome(
    status: ResearchOutcomeStatus,
    *,
    metrics: tuple[OfficialMetricRecord, ...],
) -> CampaignResearchOutcome:
    candidate = CandidateResearchOutcome(
        candidate_id="candidate.mom50",
        proposer_component=Component.LLM_RESEARCHER,
        attempt=_attempt(),
        outcome=status,
        official_metrics=metrics,
        reason="成本后表现不足，建议修订。",
        next_actions=("降低换手后重新验证。",),
    )
    return CampaignResearchOutcome(
        campaign_id="campaign.momentum50",
        run_id="run.momentum50",
        run_status=ResearchRunStatus.COMPLETED,
        research_outcome=status,
        dataset_receipt_hash=HASH_B,
        selected_research_groups=("市场结构研究小组",),
        candidate_outcomes=(candidate,),
        summary="机器证据链完成。",
    )
