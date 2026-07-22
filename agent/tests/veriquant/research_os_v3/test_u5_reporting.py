from __future__ import annotations

import json
import shutil
from dataclasses import replace
from pathlib import Path

import pytest

from veriquant.contracts import ContractValidationError, FailureCode
from veriquant.research_campaign import ResearchCampaign
from veriquant.research_run_controller import (
    ResearchRunIdentityStore,
    ResearchRunLifecycle,
    ResearchRunManifest,
)
from veriquant.research_os_v3 import (
    MultiSpecialistRunResult,
    MultiSpecialistRunStatus,
    ResearchOSV3ReportBackend,
    ResearchReportOutcome,
    FrozenResearchContext,
)


REPO_ROOT = Path(__file__).resolve().parents[4]
SOURCE = REPO_ROOT / ".autobuild/veriquant/research_os_v3_u4"


def _load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _fixture(tmp_path: Path):
    root = tmp_path / "run_root"
    shutil.copytree(SOURCE, root)
    campaign = ResearchCampaign.from_payload(_load(root / "u4_campaign.json"))
    context = FrozenResearchContext.from_payload(_load(root / "u4_context.json"))
    result = MultiSpecialistRunResult.from_payload(_load(root / "u4_smoke_result.json")["result"])
    # The archived U4 payload predates clarification/resolution fields.  Loading
    # it through the current v1 contract correctly materializes those defaults,
    # which changes the Campaign hash.  Migrate the temporary fixture copy as a
    # single canonical identity instead of weakening report identity checks.
    manifest_path = root / "research_runs" / result.run_id / "manifest.json"
    manifest = ResearchRunManifest.from_payload(_load(manifest_path))
    manifest = replace(
        manifest,
        campaign_hash=campaign.artifact_hash,
        report_hash=None,
    )
    manifest_path.write_text(
        json.dumps(manifest.to_dict(), ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    result = replace(
        result,
        campaign_hash=campaign.artifact_hash,
        manifest_hash=manifest.artifact_hash,
    )
    return root, campaign, context, result


def test_real_u4_report_is_deterministic_public_and_idempotent(tmp_path):
    root, campaign, context, result = _fixture(tmp_path)
    backend = ResearchOSV3ReportBackend(run_root=root, publication_root=tmp_path / "published")
    first, first_receipt = backend.compose_and_publish(campaign=campaign, context=context, result=result)
    replay, replay_receipt = backend.compose_and_publish(campaign=campaign, context=context, result=result)

    assert first.outcome is ResearchReportOutcome.DRAFT_RESEARCH_RESULT
    assert first.candidate_count == 20 and first.accepted_preflight_count == 6
    assert first.machine_evidence_status == "NOT_RUN"
    assert first.official_evidence_hash is None and first.promotion_hash is None
    assert replay.artifact_hash == first.artifact_hash
    assert replay_receipt.artifact_hash == first_receipt.artifact_hash
    markdown = (tmp_path / "published" / result.run_id / "report.md").read_text(encoding="utf-8")
    assert "正式指标：无" in markdown
    assert "搜索覆盖" in markdown
    assert "机器结果总览" in markdown
    assert "机器裁决使用最后一个可见验证折" in markdown
    assert "下一轮怎么改" in markdown
    assert all(token not in markdown for token in ("Desk", "Router", "Swarm", "OFFICIAL_TUSHARE"))


def test_report_updates_the_single_canonical_manifest(tmp_path):
    root, campaign, context, result = _fixture(tmp_path)
    report, receipt = ResearchOSV3ReportBackend(
        run_root=root, publication_root=tmp_path / "published"
    ).compose_and_publish(campaign=campaign, context=context, result=result)
    manifest = _load(root / "research_runs" / result.run_id / "manifest.json")
    assert manifest["report_hash"] == report.artifact_hash
    assert receipt.canonical_manifest_hash != result.manifest_hash


def test_tampered_specialist_outcome_fails_closed(tmp_path):
    root, campaign, context, result = _fixture(tmp_path)
    path = root / "research_runs" / result.run_id / "specialists" / "market_structure" / "outcome.json"
    envelope = _load(path)
    envelope["payload"]["message"] = "tampered"
    path.write_text(json.dumps(envelope), encoding="utf-8")
    with pytest.raises(ContractValidationError) as exc:
        ResearchOSV3ReportBackend(
            run_root=root, publication_root=tmp_path / "published"
        ).compose_and_publish(campaign=campaign, context=context, result=result)
    assert exc.value.code in {FailureCode.INVALID_HASH, FailureCode.INVALID_STATE}


def test_mismatched_campaign_identity_fails_closed(tmp_path):
    root, campaign, context, result = _fixture(tmp_path)
    bad = MultiSpecialistRunResult.from_payload({**result.to_dict(), "campaign_hash": "sha256:" + "9" * 64})
    with pytest.raises(ContractValidationError) as exc:
        ResearchOSV3ReportBackend(
            run_root=root, publication_root=tmp_path / "published"
        ).compose_and_publish(campaign=campaign, context=context, result=bad)
    assert exc.value.code is FailureCode.INVALID_HASH


@pytest.mark.parametrize(
    ("run_status", "lifecycle", "expected_outcome", "expected_text"),
    (
        (
            MultiSpecialistRunStatus.FAILED,
            ResearchRunLifecycle.FAILED,
            ResearchReportOutcome.SYSTEM_FAILURE_REPORT,
            "故障报告",
        ),
        (
            MultiSpecialistRunStatus.CANCELLED,
            ResearchRunLifecycle.CANCELLED,
            ResearchReportOutcome.CANCELLED_RESEARCH_REPORT,
            "已按请求取消",
        ),
    ),
)
def test_failed_and_cancelled_runs_publish_truthful_reports(
    tmp_path, run_status, lifecycle, expected_outcome, expected_text
):
    root, campaign, context, result = _fixture(tmp_path)
    manifest_path = root / "research_runs" / result.run_id / "manifest.json"
    manifest = ResearchRunManifest.from_payload(_load(manifest_path))
    manifest = replace(manifest, current_stage=lifecycle, report_hash=None, official_evidence_hash=None)
    manifest_path.write_text(json.dumps(manifest.to_dict(), ensure_ascii=False), encoding="utf-8")
    terminal_result = replace(result, manifest_hash=manifest.artifact_hash, status=run_status)

    report, receipt = ResearchOSV3ReportBackend(
        run_root=root, publication_root=tmp_path / "published"
    ).compose_and_publish(campaign=campaign, context=context, result=terminal_result)

    assert report.outcome is expected_outcome
    assert expected_text in report.executive_answer
    assert report.official_evidence_hash is None
    assert receipt.report_hash == report.artifact_hash


def test_cancel_report_is_archived_when_resumed_run_finishes(tmp_path):
    root, campaign, context, result = _fixture(tmp_path)
    manifest_path = root / "research_runs" / result.run_id / "manifest.json"
    manifest = ResearchRunManifest.from_payload(_load(manifest_path))
    cancelled = replace(
        manifest,
        current_stage=ResearchRunLifecycle.CANCELLED,
        typed_failures=tuple(sorted(set((*manifest.typed_failures, "CANCELLED")))),
        report_hash=None,
        official_evidence_hash=None,
    )
    manifest_path.write_text(json.dumps(cancelled.to_dict(), ensure_ascii=False), encoding="utf-8")
    backend = ResearchOSV3ReportBackend(run_root=root, publication_root=tmp_path / "published")
    cancel_report, _ = backend.compose_and_publish(
        campaign=campaign,
        context=context,
        result=replace(result, manifest_hash=cancelled.artifact_hash, status=MultiSpecialistRunStatus.CANCELLED),
    )

    store = ResearchRunIdentityStore(root)
    published_cancelled = store.load_manifest(result.run_id)
    assert published_cancelled is not None
    planning = store.ensure_manifest(
        replace(
            published_cancelled,
            current_stage=ResearchRunLifecycle.PLANNING,
            typed_failures=tuple(item for item in published_cancelled.typed_failures if item != "CANCELLED"),
            report_hash=None,
        ),
        expected_artifact_hash=published_cancelled.artifact_hash,
        allow_cancelled_resume=True,
    )
    completed = store.ensure_manifest(
        replace(planning, current_stage=ResearchRunLifecycle.COMPLETED),
        expected_artifact_hash=planning.artifact_hash,
    )
    completed_report, _ = backend.compose_and_publish(
        campaign=campaign,
        context=context,
        result=replace(result, manifest_hash=completed.artifact_hash, status=MultiSpecialistRunStatus.COMPLETED),
    )

    history = tmp_path / "published" / result.run_id / "history" / cancel_report.artifact_hash.split(":", 1)[1]
    assert completed_report.artifact_hash != cancel_report.artifact_hash
    assert (history / "machine_report.json").exists()
    assert (history / "report.md").exists()
    assert (history / "receipt.json").exists()
