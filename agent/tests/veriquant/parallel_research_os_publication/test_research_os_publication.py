from __future__ import annotations

from dataclasses import replace

import pytest

from veriquant.contracts import ContractValidationError
from veriquant.formal_report_v2 import (
    CandidateReportV2,
    DataCoverageV2,
    FormalResearchReportV2,
    ResearchGroupReportV2,
    ResearchabilitySectionV2,
)
from veriquant.research_closure_contracts import ResearchOutcomeStatus
from veriquant.research_os import AnswerLevel
from veriquant.research_os_publication import (
    PublicationArtifactKind,
    PublicationArtifactRef,
    ResearchOSPublicationRepository,
    assert_publication_payload_safe,
    require_safe_run_id,
)


H0 = "sha256:" + "0" * 64
H1 = "sha256:" + "1" * 64
H2 = "sha256:" + "2" * 64
H3 = "sha256:" + "3" * 64


def report(run_id: str = "campaign_report_seed7") -> FormalResearchReportV2:
    return FormalResearchReportV2(
        report_id="report_alpha_1",
        campaign_id="campaign_alpha_1",
        run_id=run_id,
        question="挖一下最近50天大动量的因子",
        executive_answer="本次形成一个可复核候选，但成本后表现需要继续修订。",
        research_mode_label="新 Alpha 探索",
        research_depth_label="标准研究",
        collaboration_label="标准协作",
        researchability=ResearchabilitySectionV2(
            strongest_answer_level=AnswerLevel.OFFICIAL_EVALUATED,
            answerable_levels=(AnswerLevel.CONCEPTUAL, AnswerLevel.AUXILIARY_RESEARCH, AnswerLevel.OFFICIAL_EVALUATED),
            official_evaluation_ready=True,
        ),
        data_coverage=DataCoverageV2(
            provider_label="APPROVED_PROXY_DATA_VENDOR",
            available_sessions=180,
            required_sessions=120,
            available_symbols=30,
            required_symbols=20,
            lookback_sessions=50,
            label_horizon_sessions=5,
            required_fields=("close", "volume", "market_cap"),
            optional_fields=("industry",),
            missing_required_fields=(),
            field_availability_hash=H1,
        ),
        selected_research_groups=(
            ResearchGroupReportV2(
                group_id="market_structure",
                group_label="市场结构研究小组",
                hypothesis="高成交确认的50日价格强势可能保留短期延续性。",
                candidate_name="成交确认动量",
                candidate_expression="rank_cross_section(pct_change(close, 50))",
                status="已提交草案",
                source_hashes=(H2,),
            ),
        ),
        candidates=(
            CandidateReportV2(
                candidate_id="candidate_momentum_50",
                group_label="市场结构研究小组",
                hypothesis="高成交确认的50日价格强势可能保留短期延续性。",
                expression="rank_cross_section(pct_change(close, 50))",
                translation_status="已转译",
                pit_status="已通过",
                evidence_status="已有机器证据",
                critic_status="需要修订",
                outcome=ResearchOutcomeStatus.REVISE,
                evidence_refs=(H3,),
                attempt_hash=H2,
                failure_reasons=("成本后优势不足。",),
                next_actions=("降低换手后重新验证。",),
            ),
        ),
        comparison_summary="本轮没有候选达到进入下一阶段的标准。",
        limitations=("样本较小，结果只作为受控研究输出。",),
        next_actions=("扩大样本并重新运行机器证据。",),
        artifact_hashes=(H0, H1, H2, H3),
    )


def test_publish_writes_artifacts_and_is_idempotent(tmp_path) -> None:
    repo = ResearchOSPublicationRepository(tmp_path)
    first = repo.publish(report=report(), acceptance_report_hash=H0, run_evidence_hash=H1)
    second = repo.publish(report=report(), acceptance_report_hash=H0, run_evidence_hash=H1)

    assert first.artifact_hash == second.artifact_hash
    assert (tmp_path / report().run_id / "machine_report.json").exists()
    assert (tmp_path / report().run_id / "report.md").read_text(encoding="utf-8").startswith("# AlphaForge")
    assert (tmp_path / report().run_id / "artifact_manifest.json").exists()
    assert (tmp_path / report().run_id / "publication_receipt.json").exists()
    assert (tmp_path / "latest.json").exists()
    assert repo.read_receipt(report().run_id).artifact_hash == first.artifact_hash
    assert repo.read_manifest(report().run_id).report_hash == report().artifact_hash
    assert repo.read_machine_report(report().run_id)["run_id"] == report().run_id
    assert "机器证据" in repo.read_markdown(report().run_id)
    assert repo.read_latest().receipt_hash == first.artifact_hash
    assert tuple(item.run_id for item in repo.list_receipts()) == (report().run_id,)


def test_conflicting_same_run_publication_fails_closed(tmp_path) -> None:
    repo = ResearchOSPublicationRepository(tmp_path)
    repo.publish(report=report(), acceptance_report_hash=H0, run_evidence_hash=H1)
    (tmp_path / report().run_id / "report.md").write_text("tampered", encoding="utf-8")

    with pytest.raises(ContractValidationError):
        repo.publish(report=report(), acceptance_report_hash=H0, run_evidence_hash=H1)


def test_path_traversal_and_live_trading_paths_are_rejected(tmp_path) -> None:
    repo = ResearchOSPublicationRepository(tmp_path)
    with pytest.raises(ContractValidationError):
        require_safe_run_id("campaign/escape")
    with pytest.raises(ContractValidationError):
        repo.read_receipt("campaign/escape")
    with pytest.raises(ContractValidationError):
        PublicationArtifactRef(
            kind=PublicationArtifactKind.MACHINE_JSON,
            relative_path="broker.json",
            content_hash=H0,
            content_type="application/json",
            byte_size=2,
        )


def test_publication_safety_scanner_rejects_sealed_secrets_and_engineering_terms() -> None:
    with pytest.raises(ContractValidationError):
        assert_publication_payload_safe({"headline": "Router should show sealed metrics"})
    with pytest.raises(ContractValidationError):
        assert_publication_payload_safe("Authorization: bearer abc")
    with pytest.raises(ContractValidationError):
        assert_publication_payload_safe("OFFICIAL_TUSHARE")


def test_latest_pointer_can_move_without_mutating_prior_run(tmp_path) -> None:
    repo = ResearchOSPublicationRepository(tmp_path)
    first = repo.publish(report=report("campaign_a"), acceptance_report_hash=H0, run_evidence_hash=H1)
    second_report = replace(report("campaign_b"), executive_answer="第二次研究发布完成。")
    second = repo.publish(report=second_report, acceptance_report_hash=H0, run_evidence_hash=H1)

    assert repo.read_latest().run_id == "campaign_b"
    assert repo.read_receipt("campaign_a").artifact_hash == first.artifact_hash
    assert repo.read_receipt("campaign_b").artifact_hash == second.artifact_hash
    assert [item.run_id for item in repo.list_receipts()] == ["campaign_a", "campaign_b"]

