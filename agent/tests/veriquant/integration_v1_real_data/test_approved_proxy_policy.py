from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone

import pytest

from veriquant.contracts import ContractValidationError, FailureCode
from veriquant.integration.v1_approved_proxy_policy import (
    APPROVED_PROXY_POLICY_HASH,
    ApprovedProviderClass,
    OperatorProxyApproval,
    load_operator_proxy_approval,
    qualify_approved_proxy_inputs,
)
from veriquant.integration.v1_real_data_chain import RealAuxiliaryChainResult
from veriquant.integration import v1_approved_proxy_cli
from veriquant.provider_provenance import ProviderEligibility, ProviderSourceClass


HASH = "sha256:" + "1" * 64


def _auxiliary_result() -> RealAuxiliaryChainResult:
    return RealAuxiliaryChainResult(
        status="COMPLETED_AUXILIARY",
        source_class="TUSHARE_COMPATIBLE_PROXY",
        authority_class="AUXILIARY_RESEARCH_EVIDENCE",
        provider_eligibility="AUXILIARY_ONLY",
        snapshot_fetched_at=datetime(2026, 7, 18, 8, 33, tzinfo=timezone.utc),
        decision_time=datetime(2024, 4, 1, 2, 0, tzinfo=timezone.utc),
        snapshot_hash=HASH,
        snapshot_manifest_hash=HASH,
        calendar_hash=HASH,
        universe_hash=HASH,
        bar_capture_hash=HASH,
        market_state_capture_hash=HASH,
        market_state_vintage_hash=HASH,
        market_state_manifest_hash=HASH,
        provenance_manifest_hash=HASH,
        provenance_decision_hash=HASH,
        research_manifest_hash=HASH,
        factor_dag_hashes=(HASH,),
        factor_output_hashes=(HASH,),
        market_state_row_count=1,
        factor_count=1,
        network_used=False,
        capture_replay_verified=True,
        official_evidence_run=False,
        promotion_run=False,
        challenger_created=False,
        real_shadow_replay_run=False,
        gate_blocker="operator policy not yet applied",
    )


def test_pinned_operator_policy_is_stable_and_never_claims_official_tushare() -> None:
    first = load_operator_proxy_approval()
    second = load_operator_proxy_approval()

    assert first.artifact_hash == second.artifact_hash == APPROVED_PROXY_POLICY_HASH
    assert first.provider_class is ApprovedProviderClass.APPROVED_PROXY_DATA_VENDOR
    assert first.operator_accepts_non_official_source_risk is True
    assert first.official_tushare_claim_allowed is False
    assert first.promotion_authority_granted is False
    assert first.risk_authority_granted is False
    assert first.order_authority_granted is False
    assert first.ledger_authority_granted is False


def test_default_contract_rejects_official_tushare_masquerade() -> None:
    approval = load_operator_proxy_approval()

    with pytest.raises(ContractValidationError) as error:
        replace(approval, official_tushare_claim_allowed=True)

    assert error.value.code is FailureCode.UNAUTHORIZED_CAPABILITY


def test_contract_rejects_changed_host_and_financial_authority() -> None:
    approval = load_operator_proxy_approval()

    with pytest.raises(ContractValidationError):
        replace(approval, endpoint_host="example.invalid")
    with pytest.raises(ContractValidationError):
        replace(approval, promotion_authority_granted=True)


def test_unpinned_operator_payload_cannot_qualify_inputs() -> None:
    approval = load_operator_proxy_approval()
    unpinned = replace(approval, policy_version="1.0.1")

    with pytest.raises(ContractValidationError) as error:
        qualify_approved_proxy_inputs(result=_auxiliary_result(), approval=unpinned)

    assert error.value.code is FailureCode.INVALID_HASH


def test_qualification_remains_separate_from_evidence_and_promotion(monkeypatch) -> None:
    approval = load_operator_proxy_approval()
    auxiliary = _auxiliary_result()

    class ManifestStub:
        artifact_hash = HASH
        source_class = ProviderSourceClass.TUSHARE_COMPATIBLE_PROXY

    # The reconstruction hash is covered by the real frozen integration test;
    # this unit test isolates the authority semantics of the approval wrapper.
    monkeypatch.setattr(
        "veriquant.integration.v1_approved_proxy_policy._provider_manifest_from_auxiliary_result",
        lambda _result: ManifestStub(),
    )
    monkeypatch.setattr(
        "veriquant.integration.v1_approved_proxy_policy.evaluate_provider_provenance",
        lambda _manifest, policy: type(
            "DecisionStub",
            (),
            {
                "eligibility": ProviderEligibility.ELIGIBLE_FOR_OFFICIAL_EVIDENCE,
                "artifact_hash": HASH,
            },
        )(),
    )

    qualification = qualify_approved_proxy_inputs(result=auxiliary, approval=approval)

    assert qualification.provider_class is ApprovedProviderClass.APPROVED_PROXY_DATA_VENDOR
    assert qualification.provider_eligibility is ProviderEligibility.ELIGIBLE_FOR_OFFICIAL_EVIDENCE
    assert qualification.official_tushare_source is False
    assert qualification.upstream_attestation_present is False
    assert qualification.eligible_for_official_evidence_inputs is True
    assert qualification.official_evidence_run is False
    assert qualification.promotion_run is False
    assert qualification.challenger_created is False


def test_cli_requires_explicit_pinned_policy_hash(tmp_path, monkeypatch) -> None:
    qualification_path = tmp_path / "qualification.json"
    monkeypatch.setattr(
        v1_approved_proxy_cli,
        "_paths",
        lambda: {"auxiliary": tmp_path / "missing.json", "qualification": qualification_path},
    )

    assert v1_approved_proxy_cli.main([]) == 0
    disabled = __import__("json").loads(qualification_path.read_text(encoding="utf-8"))
    assert disabled["status"] == "APPROVAL_NOT_APPLIED"

    assert (
        v1_approved_proxy_cli.main(
            ["--apply-operator-approval", "--policy-hash", "sha256:" + "0" * 64]
        )
        == 2
    )
    failed = __import__("json").loads(qualification_path.read_text(encoding="utf-8"))
    assert failed["status"] == "FAILED"
    assert failed["official_tushare_source"] is False
