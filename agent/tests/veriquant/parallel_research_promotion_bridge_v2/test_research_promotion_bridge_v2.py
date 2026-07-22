from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from veriquant.integration.statistical import InMemoryOfficialEvidenceReader
from veriquant.research_promotion_bridge_v2 import (
    PromotionBridgeRequest,
    PromotionBridgeTerminalState,
    ResearchPromotionBridgeV2,
)
from veriquant.statistical_promotion.contracts import (
    CandidateEvidenceView,
    CandidateVersionRef,
    FactorFamilyManifest,
    PromotionState,
    StatisticalPromotionError,
    StatisticalPromotionFailureCode,
    content_hash,
)


NOW = datetime(2026, 7, 19, 6, 0, tzinfo=timezone.utc)


def h(value: str) -> str:
    return content_hash({"fixture": value})


def family(*candidate_ids: str) -> FactorFamilyManifest:
    return FactorFamilyManifest(
        family_id="family.momentum50",
        manifest_version="v1",
        frozen_at=NOW,
        candidate_versions=tuple(
            CandidateVersionRef(candidate_id=item, candidate_version="v1")
            for item in candidate_ids
        ),
    )


def view(
    manifest: FactorFamilyManifest,
    candidate_id: str,
    *,
    p_value: float = 0.01,
    state: PromotionState = PromotionState.VALIDATED_RESEARCH,
    metric_overrides: dict[str, float] | None = None,
    rank_ic_only: bool = False,
) -> CandidateEvidenceView:
    visible = {
        "TRAIN": {"rank_ic": 0.06, "icir": 1.2, "observation_count": 120.0},
        "VALIDATION": {"rank_ic": 0.05, "icir": 1.0, "observation_count": 90.0},
    }
    robustness = {} if rank_ic_only else {
        "subperiod_positive_fraction": 0.75,
        "subperiod_min_ic": 0.02,
        "regime_positive_fraction": 0.75,
        "regime_min_ic": 0.02,
        "max_abs_industry_exposure": 0.10,
        "max_abs_market_cap_exposure": 0.10,
        "turnover": 0.35,
        "gross_return_before_cost": 0.08,
        "net_return_after_cost": 0.03,
        "cost_sensitivity_loss": 0.01,
        "capacity_utilization": 0.10,
        "coverage": 0.96,
        "parameter_sensitivity_max_delta": 0.06,
    }
    robustness.update(metric_overrides or {})
    return CandidateEvidenceView(
        candidate_id=candidate_id,
        candidate_version="v1",
        family_id=manifest.family_id,
        family_manifest_hash=manifest.artifact_hash,
        identity_hash=h(f"identity:{candidate_id}"),
        evidence_hash=h(f"evidence:{candidate_id}"),
        input_hash=h(f"input:{candidate_id}"),
        proposer_id="research-group.market-structure",
        state=state,
        visible_metrics=visible,
        robustness_metrics=robustness,
        p_value=p_value,
    )


def request(item: CandidateEvidenceView, *, key: str = "promotion.momentum50.c1") -> PromotionBridgeRequest:
    return PromotionBridgeRequest(
        candidate_id=item.candidate_id,
        candidate_version=item.candidate_version,
        family_id=item.family_id,
        manifest_version="v1",
        idempotency_key=key,
        proposer_id=item.proposer_id,
        approver_id="independent-promotion-authority",
        decision_time=NOW,
        expected_evidence_hash=item.evidence_hash,
        expected_identity_hash=item.identity_hash,
        expected_family_manifest_hash=item.family_manifest_hash,
    )


def bridge_for(
    manifest: FactorFamilyManifest,
    views: tuple[CandidateEvidenceView, ...],
    *,
    sealed_passed=True,
) -> ResearchPromotionBridgeV2:
    reader = InMemoryOfficialEvidenceReader(
        manifest=manifest,
        views=views,
        sealed_passed=sealed_passed,
        reader_id="exclusive-sealed-reader-v2",
    )
    return ResearchPromotionBridgeV2(sealed_reader=reader)


def test_only_validated_research_can_enter_bridge() -> None:
    manifest = family("c1")
    item = view(manifest, "c1")
    with pytest.raises(StatisticalPromotionError) as exc:
        replace(request(item), current_state=PromotionState.EXPLORATORY)
    assert exc.value.code is StatisticalPromotionFailureCode.INVALID_STATE_TRANSITION


def test_proposer_cannot_approve_itself() -> None:
    manifest = family("c1")
    item = view(manifest, "c1")
    with pytest.raises(StatisticalPromotionError) as exc:
        replace(request(item), approver_id=item.proposer_id)
    assert exc.value.code is StatisticalPromotionFailureCode.SELF_APPROVAL


def test_request_cannot_name_a_different_approver() -> None:
    manifest = family("c1")
    item = view(manifest, "c1")
    bridge = bridge_for(manifest, (item,))
    with pytest.raises(StatisticalPromotionError) as exc:
        bridge.evaluate(replace(request(item), approver_id="unregistered-approver"))
    assert exc.value.code is StatisticalPromotionFailureCode.INVALID_CONTRACT


def test_official_evidence_hash_binding_fails_closed() -> None:
    manifest = family("c1")
    item = view(manifest, "c1")
    bridge = bridge_for(manifest, (item,))
    with pytest.raises(StatisticalPromotionError) as exc:
        bridge.evaluate(replace(request(item), expected_evidence_hash=h("wrong")))
    assert exc.value.code is StatisticalPromotionFailureCode.MISSING_EVIDENCE


def test_rank_ic_only_cannot_promote() -> None:
    manifest = family("c1")
    item = view(manifest, "c1", rank_ic_only=True)
    bridge = bridge_for(manifest, (item,))
    with pytest.raises(StatisticalPromotionError) as exc:
        bridge.evaluate(request(item))
    assert exc.value.code is StatisticalPromotionFailureCode.MISSING_EVIDENCE


def test_multiplicity_reject_is_terminal_and_opaque() -> None:
    manifest = family("c1", "c2", "c3")
    items = (
        view(manifest, "c1", p_value=0.01),
        view(manifest, "c2", p_value=0.20),
        view(manifest, "c3", p_value=0.30),
    )
    bridge = bridge_for(manifest, items)
    receipt = bridge.evaluate(request(items[1], key="promotion.multiplicity.c2"))
    assert receipt.terminal_state is PromotionBridgeTerminalState.REJECTED
    serialized = json.dumps(receipt.to_dict(), sort_keys=True)
    assert "adjusted_p" not in serialized
    assert "sealed_handle" not in serialized
    assert "reason_codes" not in serialized


def test_cost_or_capacity_failure_rejects_without_challenger() -> None:
    manifest = family("c1")
    item = view(
        manifest,
        "c1",
        metric_overrides={"net_return_after_cost": -0.01, "capacity_utilization": 0.5},
    )
    receipt = bridge_for(manifest, (item,)).evaluate(request(item))
    assert receipt.terminal_state is PromotionBridgeTerminalState.REJECTED
    with pytest.raises(StatisticalPromotionError):
        receipt.to_closure_ref()


def test_sealed_rejection_leaks_no_marker_or_handle() -> None:
    manifest = family("c1")
    item = view(manifest, "c1")
    bridge = bridge_for(manifest, (item,), sealed_passed=False)
    receipt = bridge.evaluate(request(item, key="promotion.sealed.reject"))
    serialized = json.dumps(receipt.to_dict(), sort_keys=True)
    assert receipt.terminal_state is PromotionBridgeTerminalState.REJECTED
    assert "sealed_metric_0.777" not in serialized
    assert "sealed_handle" not in serialized.lower()
    assert "raw_sealed" not in serialized.lower()
    assert "private" not in serialized.lower()
    assert not hasattr(receipt, "handle")


def test_optional_challenger_returns_existing_opaque_closure_ref() -> None:
    manifest = family("c1")
    item = view(manifest, "c1")
    receipt = bridge_for(manifest, (item,), sealed_passed=True).evaluate(request(item))
    assert receipt.terminal_state is PromotionBridgeTerminalState.CHALLENGER
    closure_ref = receipt.to_closure_ref()
    assert closure_ref.receipt_hash == receipt.receipt_hash
    assert closure_ref.sealed_detail_disclosed is False


def test_same_identity_replay_is_idempotent_and_does_not_reopen_sealed() -> None:
    manifest = family("c1")
    item = view(manifest, "c1")
    bridge = bridge_for(manifest, (item,), sealed_passed=True)
    req = request(item, key="promotion.replay.same")
    first = bridge.evaluate(req)
    second = bridge.evaluate(req)
    assert first.receipt_hash == second.receipt_hash
    assert first.decision_hash == second.decision_hash


def test_same_idempotency_key_with_changed_identity_fails_closed() -> None:
    manifest = family("c1")
    item = view(manifest, "c1")
    bridge = bridge_for(manifest, (item,), sealed_passed=True)
    req = request(item, key="promotion.replay.conflict")
    bridge.evaluate(req)
    object.__setattr__(item, "input_hash", h("mutated-input"))
    with pytest.raises(StatisticalPromotionError) as exc:
        bridge.evaluate(req)
    assert exc.value.code is StatisticalPromotionFailureCode.IDEMPOTENCY_CONFLICT
