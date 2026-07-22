from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone

import pytest

from src.veriquant.contracts import ContractValidationError
from src.veriquant.provider_provenance import (
    ProviderEligibility,
    ProviderProvenanceManifest,
    ProviderProvenancePolicy,
    ProviderSourceClass,
    evaluate_provider_provenance,
)


HASH = "sha256:" + "1" * 64
ATTESTATION = "sha256:" + "2" * 64


def manifest(**changes):
    value = ProviderProvenanceManifest(
        provider_id="proxy-sdk",
        source_class=ProviderSourceClass.TUSHARE_COMPATIBLE_PROXY,
        endpoint_host="ts.gyzcloud.top",
        endpoint_path="/api",
        sdk_name="tushare",
        sdk_version="1.4.29",
        acquired_at=datetime(2026, 7, 18, tzinfo=timezone.utc),
        api_names=("daily", "index_weight", "trade_cal"),
        snapshot_hash=HASH,
        capture_hash="sha256:" + "3" * 64,
        calendar_hash="sha256:" + "4" * 64,
        universe_hash="sha256:" + "5" * 64,
    )
    return replace(value, **changes)


def test_compatible_proxy_is_auxiliary_by_default() -> None:
    decision = evaluate_provider_provenance(manifest())
    assert decision.eligibility is ProviderEligibility.AUXILIARY_ONLY
    assert decision.official_evidence_eligible is False
    assert decision.promotion_eligible is False
    assert decision.official_v1_tushare_smoke is False


def test_proxy_needs_both_host_and_attestation_approval() -> None:
    approved = ProviderProvenancePolicy(
        approved_proxy_hosts=("ts.gyzcloud.top",),
        approved_proxy_attestation_hashes=(ATTESTATION,),
    )
    missing = evaluate_provider_provenance(manifest(), approved)
    passed = evaluate_provider_provenance(
        manifest(upstream_attestation_hash=ATTESTATION), approved
    )
    assert missing.eligibility is ProviderEligibility.AUXILIARY_ONLY
    assert passed.eligibility is ProviderEligibility.ELIGIBLE_FOR_OFFICIAL_EVIDENCE


def test_official_class_cannot_use_proxy_host() -> None:
    decision = evaluate_provider_provenance(
        manifest(source_class=ProviderSourceClass.OFFICIAL_TUSHARE)
    )
    assert decision.eligibility is ProviderEligibility.AUXILIARY_ONLY


def test_unknown_source_is_rejected() -> None:
    decision = evaluate_provider_provenance(
        manifest(source_class=ProviderSourceClass.UNKNOWN)
    )
    assert decision.eligibility is ProviderEligibility.REJECTED


@pytest.mark.parametrize(
    "path",
    ("/mcp/token=secret", "/api?token=secret", "/api#secret"),
)
def test_secret_bearing_endpoint_path_is_rejected_without_echo(path: str) -> None:
    with pytest.raises(ContractValidationError) as captured:
        manifest(endpoint_path=path)
    assert "secret" not in str(captured.value)


def test_unknown_api_capability_is_rejected() -> None:
    with pytest.raises(ContractValidationError):
        manifest(api_names=("daily", "factor.py"))


def test_identity_changes_with_snapshot_or_capture() -> None:
    first = manifest()
    changed_snapshot = replace(first, snapshot_hash="sha256:" + "6" * 64)
    changed_capture = replace(first, capture_hash="sha256:" + "7" * 64)
    assert len({first.artifact_hash, changed_snapshot.artifact_hash, changed_capture.artifact_hash}) == 3


def test_same_input_has_same_manifest_and_decision_hash() -> None:
    first = evaluate_provider_provenance(manifest())
    second = evaluate_provider_provenance(manifest())
    assert first.artifact_hash == second.artifact_hash


def test_serialized_policy_and_decision_have_no_secret_fields() -> None:
    payload = str(evaluate_provider_provenance(manifest()).to_dict()).lower()
    assert "token" not in payload
    assert "authorization" not in payload
    assert "api_key" not in payload
