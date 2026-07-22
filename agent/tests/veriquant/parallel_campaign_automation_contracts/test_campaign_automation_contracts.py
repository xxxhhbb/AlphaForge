from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.veriquant.campaign_automation_contracts import (
    ArtifactRef,
    AutomationFailureCode,
    AutomationProviderMode,
    AutomationRunStatus,
    AutomationStageStatus,
    CampaignAutomationRequest,
    CampaignDataBindingRef,
    CampaignPublicationManifest,
    CampaignStageCommit,
    DataVendorKind,
)
from src.veriquant.contracts import Component, ContractValidationError
from src.veriquant.research_campaign import CampaignState, ResearchDepth


HASH_A = "sha256:" + "a" * 64
HASH_B = "sha256:" + "b" * 64
HASH_C = "sha256:" + "c" * 64
HASH_D = "sha256:" + "d" * 64
HASH_E = "sha256:" + "e" * 64


def _dt() -> datetime:
    return datetime(2026, 7, 19, 9, 30, tzinfo=timezone.utc)


def _request(**overrides) -> CampaignAutomationRequest:
    payload = {
        "run_id": "campaign.auto.001",
        "campaign_hash": HASH_A,
        "route_hash": HASH_B,
        "protocol_plan_hash": HASH_C,
        "campaign_state": CampaignState.CONFIRMED,
        "research_depth": ResearchDepth.STANDARD_RESEARCH,
        "provider_mode": AutomationProviderMode.DETERMINISTIC,
        "requested_at": _dt(),
    }
    payload.update(overrides)
    return CampaignAutomationRequest(**payload)


def _artifact_ref(**overrides) -> ArtifactRef:
    payload = {
        "ref_id": "report.machine",
        "artifact_type": "machine_report",
        "content_hash": HASH_D,
        "locator": "runs/campaign.auto.001/report.json",
        "produced_by": Component.ATTRIBUTION_SERVICE,
    }
    payload.update(overrides)
    return ArtifactRef(**payload)


def test_automation_request_requires_confirmed_campaign() -> None:
    with pytest.raises(ContractValidationError) as exc:
        _request(campaign_state=CampaignState.DRAFT)

    assert exc.value.details["automation_failure_code"] == AutomationFailureCode.CAMPAIGN_NOT_CONFIRMED.value


def test_real_llm_requires_explicit_server_side_authorization() -> None:
    with pytest.raises(ContractValidationError) as exc:
        _request(provider_mode=AutomationProviderMode.REAL_LLM, allow_real_provider=False)

    assert exc.value.details["automation_failure_code"] == AutomationFailureCode.PROVIDER_UNAVAILABLE.value
    assert _request(provider_mode=AutomationProviderMode.REAL_LLM, allow_real_provider=True).provider_mode is AutomationProviderMode.REAL_LLM


def test_same_request_has_stable_canonical_hash() -> None:
    left = _request(request_tags=("real_smoke", "campaign"))
    right = CampaignAutomationRequest.from_payload(left.to_dict(), component=Component.OPERATOR)

    assert left.artifact_hash == right.artifact_hash


def test_data_binding_marks_approved_proxy_without_official_tushare_label() -> None:
    binding = CampaignDataBindingRef(
        binding_id="binding.approved.proxy.001",
        campaign_hash=HASH_A,
        dataset_id="approved.proxy.csi300",
        universe_id="csi300_pit",
        horizon="5d",
        decision_time=_dt(),
        vendor_kind=DataVendorKind.APPROVED_PROXY_DATA_VENDOR,
        dataset_manifest_hash=HASH_B,
        snapshot_hash=HASH_C,
        calendar_hash=HASH_D,
        availability_policy_hash=HASH_E,
        source_label="APPROVED_PROXY_DATA_VENDOR:ts.gyzcloud.top",
    )

    assert binding.vendor_kind is DataVendorKind.APPROVED_PROXY_DATA_VENDOR
    with pytest.raises(ContractValidationError):
        CampaignDataBindingRef(
            binding_id="binding.bad",
            campaign_hash=HASH_A,
            dataset_id="approved.proxy.csi300",
            universe_id="csi300_pit",
            horizon="5d",
            decision_time=_dt(),
            vendor_kind=DataVendorKind.APPROVED_PROXY_DATA_VENDOR,
            dataset_manifest_hash=HASH_B,
            snapshot_hash=HASH_C,
            calendar_hash=HASH_D,
            availability_policy_hash=HASH_E,
            source_label="OFFICIAL_TUSHARE",
        )


def test_artifact_locator_rejects_path_traversal_and_absolute_paths() -> None:
    for locator in ("../secret.json", "runs/../secret.json", "D:/secret.json", "/tmp/secret.json", "http://example.test/a"):
        with pytest.raises(ContractValidationError):
            _artifact_ref(locator=locator)


def test_llm_stage_commit_cannot_claim_control_reference() -> None:
    with pytest.raises(ContractValidationError) as exc:
        CampaignStageCommit(
            run_id="campaign.auto.001",
            route_task_id="task.fundamental",
            desk_id="fundamental",
            stage_id="official_evaluate",
            stage_index=2,
            status=AutomationStageStatus.CONTROL_REFERENCE,
            produced_by=Component.RESEARCH_DESK,
            input_hash=HASH_A,
            output_hash=HASH_B,
        )

    assert exc.value.details["automation_failure_code"] == AutomationFailureCode.AUTHORITY_VIOLATION.value


def test_failed_stage_requires_typed_failure_code() -> None:
    with pytest.raises(ContractValidationError):
        CampaignStageCommit(
            run_id="campaign.auto.001",
            route_task_id="task.fundamental",
            desk_id="fundamental",
            stage_id="hypothesis",
            stage_index=0,
            status=AutomationStageStatus.BLOCKED,
            produced_by=Component.OPERATOR,
            input_hash=HASH_A,
            output_hash=None,
        )

    commit = CampaignStageCommit(
        run_id="campaign.auto.001",
        route_task_id="task.fundamental",
        desk_id="fundamental",
        stage_id="hypothesis",
        stage_index=0,
        status=AutomationStageStatus.BLOCKED,
        produced_by=Component.OPERATOR,
        input_hash=HASH_A,
        output_hash=None,
        failure_code=AutomationFailureCode.STAGE_FAILED_CLOSED,
    )
    assert commit.stage_key == "task.fundamental:hypothesis"


def test_quick_exploration_publication_cannot_include_official_evidence_or_promotion() -> None:
    with pytest.raises(ContractValidationError) as exc:
        CampaignPublicationManifest(
            run_id="campaign.auto.001",
            automation_snapshot_hash=HASH_A,
            research_depth=ResearchDepth.QUICK_EXPLORATION,
            status=AutomationRunStatus.COMPLETED,
            machine_report_hash=HASH_B,
            markdown_report_hash=HASH_C,
            published_refs=(_artifact_ref(),),
            redaction_policy_hash=HASH_D,
            official_evidence_hashes=(HASH_E,),
        )

    assert exc.value.details["automation_failure_code"] == AutomationFailureCode.QUICK_DEPTH_AUTHORITY_VIOLATION.value
