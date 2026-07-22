from __future__ import annotations

import json
from datetime import datetime, timezone

from veriquant.contracts import Component
from veriquant.formal_report import FormalReportComposer, ResearchGroupSection
from veriquant.formal_report_publication import FormalWorkbenchPublisher
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


def test_publish_formal_report_to_workbench(tmp_path) -> None:
    report = _report()
    publication = FormalWorkbenchPublisher().publish(
        root=tmp_path,
        report=report,
        updated_at="2024-04-01T09:25:00+08:00",
    )
    assert publication.workbench_status == "INCOMPLETE"
    overview = json.loads((tmp_path / "runs" / report.run_id / "overview.json").read_text(encoding="utf-8"))
    assert "正式研究报告" in overview["markdown"]
    assert "Desk" not in overview["markdown"]
    assert "Router" not in overview["markdown"]
    assert "sealed" not in overview["markdown"].lower()
    assert "api_key" not in overview["markdown"].lower()


def test_publish_is_idempotent_for_same_report(tmp_path) -> None:
    report = _report()
    left = FormalWorkbenchPublisher().publish(root=tmp_path, report=report, updated_at="2024-04-01T09:25:00+08:00")
    right = FormalWorkbenchPublisher().publish(root=tmp_path, report=report, updated_at="2024-04-01T09:25:00+08:00")
    assert left.detail_hash == right.detail_hash


def _report():
    candidate = CandidateResearchOutcome(
        candidate_id="candidate.mom50",
        proposer_component=Component.LLM_RESEARCHER,
        attempt=CandidateAttemptReceipt(
            attempt_id="attempt.mom50.001",
            candidate_id="candidate.mom50",
            family_id="family.momentum50",
            status=CandidateAttemptStatus.OFFICIAL_EVIDENCE_READY,
            draft_hash=HASH_A,
            trial_identity_hash=HASH_B,
            budget_consumed=1,
            terminal_event_hash=HASH_C,
        ),
        outcome=ResearchOutcomeStatus.REVISE,
        official_metrics=(
            OfficialMetricRecord(
                metric_id="metric.rank_ic",
                candidate_id="candidate.mom50",
                metric_name="rank_ic",
                value=0.02,
                direction=MetricDirection.HIGHER_IS_BETTER,
                evidence_hash=HASH_A,
                split="VALIDATION",
                policy_version="evidence.policy.v1",
            ),
        ),
        reason="成本后表现不足，建议修订",
    )
    outcome = CampaignResearchOutcome(
        campaign_id="campaign.mom50",
        run_id="run.mom50",
        run_status=ResearchRunStatus.COMPLETED,
        research_outcome=ResearchOutcomeStatus.REVISE,
        dataset_receipt_hash=_dataset().artifact_hash,
        selected_research_groups=("市场结构研究小组",),
        candidate_outcomes=(candidate,),
    )
    return FormalReportComposer().compose(
        report_id="report.mom50",
        question="挖一下最近50天大动量的因子",
        mode_label="新 Alpha 探索",
        depth_label="标准研究",
        interaction_label="批处理式",
        campaign_outcome=outcome,
        dataset_receipt=_dataset(),
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
