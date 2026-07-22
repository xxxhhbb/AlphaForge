from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from veriquant.research_tool_contracts import (
    DeskToolPolicy,
    FrozenResearchArtifact,
    ResearchToolCapability,
    ResearchToolContractError,
    ResearchToolFailureCode,
    ResearchToolRequest,
    ResearchToolResult,
    ResearchToolVisibility,
    SourceProvenance,
    content_hash,
)


HASH_A = "sha256:" + "a" * 64
HASH_B = "sha256:" + "b" * 64
HASH_C = "sha256:" + "c" * 64
HASH_D = "sha256:" + "d" * 64
T0 = datetime(2026, 7, 19, 9, 30, tzinfo=timezone.utc)


def request(**overrides):
    values = {
        "campaign_id": "campaign_001",
        "campaign_hash": HASH_A,
        "desk_id": "fundamental",
        "decision_time": T0,
        "allowed_data": ("fundamentals", "price_volume"),
        "tool_name": "fundamentals_summary",
        "request_id": "req_001",
        "offline": True,
    }
    values.update(overrides)
    return ResearchToolRequest(**values)


def provenance(**overrides):
    values = {
        "event_time": T0 - timedelta(days=4),
        "published_at": T0 - timedelta(days=3),
        "available_at": T0 - timedelta(days=2),
        "retrieved_at": T0 - timedelta(days=1),
        "provider": "frozen_vendor",
        "source_class": "fundamental_record",
        "provider_artifact_id": "artifact_001",
        "source_hash": HASH_B,
        "content_hash": HASH_C,
        "manifest_hash": HASH_D,
    }
    values.update(overrides)
    return SourceProvenance(**values)


def artifact(**overrides):
    payload = {"summary": "coverage ok", "fields": ["roe", "close"]}
    values = {
        "artifact_id": "artifact_001",
        "artifact_type": "market_summary",
        "provenance": provenance(),
        "payload": payload,
        "payload_hash": content_hash(payload),
    }
    values.update(overrides)
    return FrozenResearchArtifact(**values)


def test_same_input_has_same_hash_and_generated_at_is_ignored():
    first = ResearchToolResult(
        request=request(),
        result_id="result_001",
        visibility=ResearchToolVisibility.DRAFT_SUPPORT,
        capability=ResearchToolCapability.READ_FROZEN_DATA,
        payload={"summary": "same"},
        artifacts=(artifact(generated_at=T0),),
        generated_at=T0,
    )
    second = ResearchToolResult(
        request=request(),
        result_id="result_001",
        visibility=ResearchToolVisibility.DRAFT_SUPPORT,
        capability=ResearchToolCapability.READ_FROZEN_DATA,
        payload={"summary": "same"},
        artifacts=(artifact(generated_at=T0 + timedelta(hours=1)),),
        generated_at=T0 + timedelta(hours=2),
    )
    assert first.artifact_hash == second.artifact_hash


def test_future_or_late_availability_is_rejected():
    late = artifact(provenance=provenance(available_at=T0 + timedelta(minutes=1), retrieved_at=T0 + timedelta(minutes=2)))
    with pytest.raises(ResearchToolContractError) as exc:
        ResearchToolResult(
            request=request(),
            result_id="result_late",
            visibility=ResearchToolVisibility.DRAFT_SUPPORT,
            capability=ResearchToolCapability.READ_FROZEN_DATA,
            payload={"summary": "late"},
            artifacts=(late,),
        )
    assert exc.value.code == ResearchToolFailureCode.DATA_NOT_AVAILABLE_AS_OF


def test_missing_request_fields_fail_closed():
    payload = {
        "schema_version": "alphaforge.research-tool-contracts/v1",
        "campaign_hash": HASH_A,
        "allowed_data": ["fundamentals"],
        "tool_name": "fundamentals_summary",
        "request_id": "req_001",
        "offline": True,
    }
    with pytest.raises(ResearchToolContractError) as exc:
        ResearchToolRequest.from_payload(payload)
    assert exc.value.code == ResearchToolFailureCode.MISSING_REQUIRED_FIELD
    assert set(exc.value.details["fields"]) >= {"campaign_id", "desk_id", "decision_time"}


def test_official_sealed_promotion_risk_order_and_ledger_payloads_are_rejected():
    forbidden_payloads = [
        {"official_metrics": {"rankic": 0.1}},
        {"nested": {"sealed": {"ic": 0.2}}},
        {"promotion_decision": "PROMOTE_CHALLENGER"},
        {"risk_approval": "APPROVE"},
        {"target_weights": {"000001.SZ": 0.1}},
        {"orders": [{"symbol": "000001.SZ"}]},
        {"ledger": {"cash": 1}},
    ]
    for payload in forbidden_payloads:
        with pytest.raises(ResearchToolContractError) as exc:
            ResearchToolResult(
                request=request(),
                result_id="result_forbidden",
                visibility=ResearchToolVisibility.DRAFT_SUPPORT,
                capability=ResearchToolCapability.READ_FROZEN_DATA,
                payload=payload,
            )
        assert exc.value.code == ResearchToolFailureCode.FORBIDDEN_AUTHORITY_FIELD


def test_secret_path_and_url_leakage_is_rejected():
    forbidden_payloads = [
        {"Authorization": "Bearer abcdefghijk"},
        {"note": "token=abcdef1234567890"},
        {"note": "see https://example.invalid/source"},
        {"note": r"C:\\Users\\Administrator\\.env"},
        {"note": "../sealed/private.json"},
    ]
    for payload in forbidden_payloads:
        with pytest.raises(ResearchToolContractError) as exc:
            ResearchToolResult(
                request=request(),
                result_id="result_secret",
                visibility=ResearchToolVisibility.DRAFT_SUPPORT,
                capability=ResearchToolCapability.READ_FROZEN_DATA,
                payload=payload,
            )
        assert exc.value.code in {
            ResearchToolFailureCode.SECRET_LEAKAGE,
            ResearchToolFailureCode.PATH_OR_URL_LEAKAGE,
        }


def test_offline_true_cannot_use_network_provider():
    with pytest.raises(ResearchToolContractError) as exc:
        ResearchToolResult(
            request=request(offline=True),
            result_id="result_network",
            visibility=ResearchToolVisibility.AUXILIARY_RESEARCH_EVIDENCE,
            capability=ResearchToolCapability.NETWORK_PROVIDER,
            payload={"summary": "network"},
        )
    assert exc.value.code == ResearchToolFailureCode.OFFLINE_NETWORK_DISABLED


def test_illegal_capability_is_rejected():
    with pytest.raises(ResearchToolContractError) as exc:
        ResearchToolResult(
            request=request(),
            result_id="result_write",
            visibility=ResearchToolVisibility.DRAFT_SUPPORT,
            capability=ResearchToolCapability.WRITE_FILE,
            payload={"summary": "write"},
        )
    assert exc.value.code == ResearchToolFailureCode.UNAUTHORIZED_CAPABILITY


def test_policy_authorization_matches_campaign_desk_and_allowed_data():
    policy = DeskToolPolicy(
        campaign_id="campaign_001",
        campaign_hash=HASH_A,
        desk_id="fundamental",
        allowed_data=("fundamentals",),
        allowed_tools=("fundamentals_summary",),
        tool_capabilities={"fundamentals_summary": (ResearchToolCapability.READ_FROZEN_DATA,)},
    )
    policy.authorize(request(allowed_data=("fundamentals",)), ResearchToolCapability.READ_FROZEN_DATA)
    with pytest.raises(ResearchToolContractError) as exc:
        policy.authorize(request(allowed_data=("fundamentals", "price_volume")), ResearchToolCapability.READ_FROZEN_DATA)
    assert exc.value.code == ResearchToolFailureCode.ALLOWED_DATA_MISMATCH
