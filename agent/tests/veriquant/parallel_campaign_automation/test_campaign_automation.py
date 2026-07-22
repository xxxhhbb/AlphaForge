from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.veriquant.campaign_automation import (
    AutomationReport,
    AutomationRequest,
    AutomationRuntimeMode,
    AutomationStatus,
    CampaignAutomationService,
)
from src.veriquant.campaign_automation.models import DataVendorClass
from src.veriquant.campaign_run_store import CampaignRunStoreStatus, FileCampaignRunStore
from src.veriquant.contracts import ContractValidationError, FailureCode
from src.veriquant.research_campaign import ResearchMode


def _request(tmp_path, **overrides) -> AutomationRequest:
    payload = {
        "question": "挖一下最近50天大动量的因子",
        "run_id": "campaign.auto.momentum50",
        "output_root": str(tmp_path),
        "decision_time": datetime(2026, 7, 19, 9, 30, tzinfo=timezone.utc),
        "seed": 7,
    }
    payload.update(overrides)
    return AutomationRequest(**payload)


def test_campaign_automation_runs_full_deterministic_backend_flow(tmp_path) -> None:
    result = CampaignAutomationService().run(_request(tmp_path))

    assert result.status is AutomationStatus.COMPLETED
    assert result.campaign_hash is not None
    assert result.route_hash is not None
    assert result.protocol_plan_hash is not None
    assert result.runtime_manifest_hash is not None
    assert result.report_hash is not None
    assert result.json_report_path is not None
    assert result.markdown_report_path is not None

    payload = json.loads(open(result.json_report_path, encoding="utf-8").read())
    report = payload["report"]
    campaign = payload["campaign"]
    runtime = payload["runtime_manifest"]

    assert campaign["mode"] == ResearchMode.NEW_ALPHA_EXPLORATION.value
    assert campaign["subject"]["horizon"] == "50d"
    assert payload["data_manifest"]["horizon"] == "50d"
    assert payload["data_binding"]["status"] == "BOUND"
    assert payload["data_binding"]["bundle"]["research_visible_summary_hash"].startswith("sha256:")
    assert 1 <= len(report["selected_desks"]) <= 3
    assert set(report["completed_desks"]) == set(report["selected_desks"])
    assert report["official_evidence_claimed"] is False
    assert report["promotion_claimed"] is False
    assert runtime["status"] == "COMPLETED"
    assert len(runtime["branch_results"]) == len(report["selected_desks"])

    store = FileCampaignRunStore(Path(result.json_report_path).parent / "run_store")
    replayed = store.replay(result.run_id)
    assert replayed.status is CampaignRunStoreStatus.COMPLETED
    assert replayed.runtime_manifest_hash == result.runtime_manifest_hash
    assert replayed.committed_stage_keys


def test_approved_proxy_vendor_is_labeled_without_official_tushare_claim(tmp_path) -> None:
    result = CampaignAutomationService().run(
        _request(
            tmp_path,
            data_vendor_class=DataVendorClass.APPROVED_PROXY_DATA_VENDOR,
            run_id="campaign.auto.proxy",
        )
    )

    payload = json.loads(open(result.json_report_path, encoding="utf-8").read())
    data_manifest = payload["data_manifest"]
    data_binding = payload["data_binding"]
    assert data_manifest["vendor_class"] == "APPROVED_PROXY_DATA_VENDOR"
    assert data_manifest["source_label"] == "APPROVED_PROXY_DATA_VENDOR"
    assert data_manifest["official_tushare_source"] is False
    assert data_binding["bundle"]["provider_source_label"] == "APPROVED_PROXY_DATA_VENDOR"
    assert data_binding["bundle"]["official_tushare_source"] is False


def test_real_llm_mode_without_provider_fails_closed(tmp_path) -> None:
    result = CampaignAutomationService().run(
        _request(
            tmp_path,
            run_id="campaign.auto.real.provider.off",
            runtime_mode=AutomationRuntimeMode.REAL_LLM,
            provider_available=False,
        )
    )

    assert result.status is AutomationStatus.FAILED
    assert result.failure_code is FailureCode.ADAPTER_UNAVAILABLE
    assert result.report_hash is None


def test_real_llm_mode_cannot_be_labeled_real_with_deterministic_executor(tmp_path) -> None:
    result = CampaignAutomationService().run(
        _request(
            tmp_path,
            run_id="campaign.auto.real.provider.flag.only",
            runtime_mode=AutomationRuntimeMode.REAL_LLM,
            provider_available=True,
        )
    )

    assert result.status is AutomationStatus.FAILED
    assert result.failure_code is FailureCode.ADAPTER_UNAVAILABLE
    assert result.report_hash is None


def test_report_rejects_downstream_authority_claims() -> None:
    with pytest.raises(ContractValidationError) as exc:
        AutomationReport(
            report_id="report.bad",
            run_id="run.bad",
            campaign_hash="sha256:" + "a" * 64,
            route_hash="sha256:" + "b" * 64,
            protocol_plan_hash="sha256:" + "c" * 64,
            runtime_manifest_hash="sha256:" + "d" * 64,
            data_manifest_hash="sha256:" + "e" * 64,
            mode=ResearchMode.NEW_ALPHA_EXPLORATION,
            runtime_mode=AutomationRuntimeMode.DETERMINISTIC_OFFLINE,
            status=AutomationStatus.COMPLETED,
            selected_desks=("market_structure",),
            completed_desks=("market_structure",),
            blocked_desks=(),
            not_selected_desks=("fundamental",),
            stage_count=5,
            official_evidence_claimed=False,
            promotion_claimed=True,
            generated_at=datetime(2026, 7, 19, 9, 30, tzinfo=timezone.utc),
            summary="bad report",
        )
    assert exc.value.code is FailureCode.UNAUTHORIZED_CAPABILITY


def test_run_id_path_traversal_is_rejected(tmp_path) -> None:
    with pytest.raises(ContractValidationError):
        _request(tmp_path, run_id="campaign/escape")
