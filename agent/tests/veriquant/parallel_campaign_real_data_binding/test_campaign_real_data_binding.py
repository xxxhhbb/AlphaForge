from __future__ import annotations

import pytest

from veriquant.campaign_data_binding import CampaignDatasetBundle
from veriquant.campaign_real_data_binding import (
    APPROVED_PROXY_SNAPSHOT_ID,
    ApprovedProxyRealDataBinder,
    RealDataBindingRequest,
    RealDataBindingStatus,
)
from veriquant.contracts import ContractValidationError
from veriquant.research_closure_contracts import APPROVED_PROXY_VENDOR_LABEL, ResearchabilityStatus


HASH_A = "sha256:" + "a" * 64
HASH_B = "sha256:" + "b" * 64
HASH_BAD = "sha256:" + "0" * 64


def test_approved_proxy_snapshot_produces_verified_receipt() -> None:
    result = ApprovedProxyRealDataBinder().bind(_request())
    assert result.status is RealDataBindingStatus.BOUND
    assert result.receipt is not None
    assert result.receipt.provider_source_label == APPROVED_PROXY_VENDOR_LABEL
    assert result.receipt.official_tushare_source is False
    assert result.receipt.row_count == 116
    assert result.receipt.symbol_count == 2
    assert result.receipt.session_count == 58
    assert result.receipt.coverage_by_field["close"] == 1.0
    assert result.researchability.status is ResearchabilityStatus.RESEARCHABLE


def test_metadata_only_bundle_cannot_be_used_as_real_receipt() -> None:
    assert not isinstance(object(), CampaignDatasetBundle)
    with pytest.raises(ContractValidationError):
        RealDataBindingRequest(
            request_id="bind.metadata",
            campaign_hash=HASH_A,
            question_hash=HASH_B,
            snapshot_id="dataset_bundle.metadata_only",
        )


def test_official_tushare_label_is_rejected() -> None:
    with pytest.raises(ContractValidationError):
        RealDataBindingRequest(
            request_id="bind.badlabel",
            campaign_hash=HASH_A,
            question_hash=HASH_B,
            provider_source_label="OFFICIAL_TUSHARE",
        )


def test_missing_required_field_blocks_researchability() -> None:
    result = ApprovedProxyRealDataBinder().bind(_request(required_fields=("close", "market_cap")))
    assert result.status is RealDataBindingStatus.BLOCKED
    assert result.receipt is None
    assert result.researchability.status is ResearchabilityStatus.BLOCKED
    assert "market_cap" in result.message


def test_snapshot_hash_mismatch_fails_closed() -> None:
    with pytest.raises(ContractValidationError):
        ApprovedProxyRealDataBinder().bind(_request(expected_snapshot_hash=HASH_BAD))


def test_path_traversal_snapshot_id_rejected() -> None:
    with pytest.raises(ContractValidationError):
        RealDataBindingRequest(
            request_id="bind.traversal",
            campaign_hash=HASH_A,
            question_hash=HASH_B,
            snapshot_id="../secret",
        )


def test_same_input_has_stable_receipt_hash() -> None:
    left = ApprovedProxyRealDataBinder().bind(_request()).receipt
    right = ApprovedProxyRealDataBinder().bind(_request()).receipt
    assert left is not None and right is not None
    assert left.artifact_hash == right.artifact_hash


def _request(**kwargs):
    payload = {
        "request_id": "bind.approved_proxy",
        "campaign_hash": HASH_A,
        "question_hash": HASH_B,
        "snapshot_id": APPROVED_PROXY_SNAPSHOT_ID,
    }
    payload.update(kwargs)
    return RealDataBindingRequest(**payload)
