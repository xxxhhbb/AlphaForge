from __future__ import annotations

from datetime import datetime, timezone

import pytest

from veriquant.campaign_official_evidence import (
    CampaignOfficialEvidenceBridge,
    CampaignOfficialEvidenceRequest,
    CampaignOfficialEvidenceStatus,
)
from veriquant.contracts import ContractValidationError
from veriquant.research_closure_contracts import (
    APPROVED_PROXY_VENDOR_LABEL,
    ResearchOutcomeStatus,
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


def test_bridge_runs_real_visible_official_evidence(tmp_path) -> None:
    result = CampaignOfficialEvidenceBridge().run(_request(tmp_path))
    assert result.status in {CampaignOfficialEvidenceStatus.COMPLETED, CampaignOfficialEvidenceStatus.PARTIAL}
    assert result.campaign_outcome.research_outcome in {
        ResearchOutcomeStatus.REJECT,
        ResearchOutcomeStatus.REVISE,
        ResearchOutcomeStatus.VALIDATED_RESEARCH,
    }
    assert result.campaign_outcome.research_outcome is not ResearchOutcomeStatus.CHALLENGER
    assert result.campaign_outcome.candidate_outcomes
    assert any(candidate.official_metrics for candidate in result.campaign_outcome.candidate_outcomes)
    assert result.trial_ledger_tip_hash.startswith("sha256:")


def test_smoke_metrics_and_caller_sealed_override_are_rejected(tmp_path) -> None:
    with pytest.raises(ContractValidationError):
        CampaignOfficialEvidenceRequest(
            request_id="official.badsmoke",
            campaign_id="campaign.mom50",
            run_id="run.mom50",
            campaign_hash=HASH_A,
            dataset_receipt=_dataset(),
            output_root=str(tmp_path),
            allow_smoke_metrics=True,
        )
    with pytest.raises(ContractValidationError):
        CampaignOfficialEvidenceRequest(
            request_id="official.badsealed",
            campaign_id="campaign.mom50",
            run_id="run.mom50",
            campaign_hash=HASH_A,
            dataset_receipt=_dataset(),
            output_root=str(tmp_path),
            sealed_passed_override=True,
        )


def test_same_inputs_replay_stably(tmp_path) -> None:
    left = CampaignOfficialEvidenceBridge().run(_request(tmp_path / "left"))
    right = CampaignOfficialEvidenceBridge().run(_request(tmp_path / "right"))
    assert left.campaign_outcome.research_outcome == right.campaign_outcome.research_outcome
    left_metrics = [
        (candidate.candidate_id, metric.metric_name, metric.value)
        for candidate in left.campaign_outcome.candidate_outcomes
        for metric in candidate.official_metrics
    ]
    right_metrics = [
        (candidate.candidate_id, metric.metric_name, metric.value)
        for candidate in right.campaign_outcome.candidate_outcomes
        for metric in candidate.official_metrics
    ]
    assert left_metrics == right_metrics


def _request(output_root) -> CampaignOfficialEvidenceRequest:
    return CampaignOfficialEvidenceRequest(
        request_id="official.mom50",
        campaign_id="campaign.mom50",
        run_id="run.mom50",
        campaign_hash=HASH_A,
        dataset_receipt=_dataset(),
        output_root=str(output_root),
    )


def _dataset() -> VerifiedDatasetReceipt:
    return VerifiedDatasetReceipt(
        receipt_id="dataset.approved_proxy.small",
        provider_source_label=APPROVED_PROXY_VENDOR_LABEL,
        official_tushare_source=False,
        snapshot_hash="sha256:dd9b48413fae1c89c52e54b5f52cf7ef46de88fd3d98fc6bae776a1a30cd527a",
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
