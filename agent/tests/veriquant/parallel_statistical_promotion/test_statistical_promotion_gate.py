from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from src.veriquant.statistical_promotion.contracts import (
    CandidateEvidenceView,
    CandidateVersionRef,
    FactorFamilyManifest,
    MultipleTestingMethod,
    MultipleTestingPolicy,
    PromotionState,
    RobustnessPolicy,
    StatisticalPromotionError,
    StatisticalPromotionFailureCode,
    content_hash,
)
from src.veriquant.statistical_promotion.multiplicity import apply_multiple_testing
from src.veriquant.statistical_promotion.reader import (
    SealedAggregateHandle,
    SealedDecisionResponse,
)
from src.veriquant.statistical_promotion.service import StatisticalPromotionService


DECISION_TIME = datetime(2026, 7, 18, 2, 0, tzinfo=timezone.utc)


def h(label: str) -> str:
    return content_hash({"fixture": label})


def manifest_for(*keys: tuple[str, str]) -> FactorFamilyManifest:
    return FactorFamilyManifest(
        family_id="family.alpha",
        manifest_version="v1",
        frozen_at=DECISION_TIME,
        candidate_versions=tuple(
            CandidateVersionRef(candidate_id=candidate, candidate_version=version)
            for candidate, version in keys
        ),
    )


def robust_metrics(**overrides: float) -> dict[str, float]:
    metrics = {
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
    metrics.update(overrides)
    return metrics


def make_view(
    manifest: FactorFamilyManifest,
    candidate_id: str,
    version: str,
    *,
    p_value: float,
    metric_overrides: dict[str, float] | None = None,
    drop_metric: str | None = None,
    visible_overrides: dict[str, float] | None = None,
) -> CandidateEvidenceView:
    visible = {
        "TRAIN": {
            "rank_ic": 0.05,
            "icir": 1.20,
            "observation_count": 120.0,
        },
        "VALIDATION": {
            "rank_ic": 0.04,
            "icir": 1.05,
            "observation_count": 90.0,
        },
    }
    if visible_overrides:
        visible["VALIDATION"].update(visible_overrides)
    metrics = robust_metrics(**(metric_overrides or {}))
    if drop_metric is not None:
        metrics.pop(drop_metric, None)
    return CandidateEvidenceView(
        candidate_id=candidate_id,
        candidate_version=version,
        family_id=manifest.family_id,
        family_manifest_hash=manifest.artifact_hash,
        identity_hash=h(f"identity:{candidate_id}:{version}"),
        evidence_hash=h(f"evidence:{candidate_id}:{version}"),
        input_hash=h(f"official-input:{candidate_id}:{version}"),
        proposer_id="desk.fundamental",
        state=PromotionState.VALIDATED_RESEARCH,
        visible_metrics=visible,
        robustness_metrics=metrics,
        p_value=p_value,
    )


class FakeEvidenceReader:
    def __init__(
        self,
        manifest: FactorFamilyManifest,
        views: tuple[CandidateEvidenceView, ...],
        *,
        sealed_passed: bool = True,
        tamper_policy: bool = False,
    ) -> None:
        self._manifest = manifest
        self._views = {view.key: view for view in views}
        self._sealed_passed = sealed_passed
        self._tamper_policy = tamper_policy
        self._handles: dict[str, SealedAggregateHandle] = {}
        self._consumed: dict[str, str] = {}
        self.sealed_reads = 0
        self.last_handle: SealedAggregateHandle | None = None
        self.secret_marker = "sealed_metric_0.777_rank_3"

    @property
    def reader_id(self) -> str:
        return "fake-evidence-reader"

    def read_visible_evidence(self, *, candidate_id: str, candidate_version: str) -> CandidateEvidenceView:
        return self._views[f"{candidate_id}@{candidate_version}"]

    def read_family_manifest(self, *, family_id: str, manifest_version: str) -> FactorFamilyManifest:
        assert family_id == self._manifest.family_id
        assert manifest_version == self._manifest.manifest_version
        return self._manifest

    def read_family_visible_evidence(self, *, manifest: FactorFamilyManifest) -> tuple[CandidateEvidenceView, ...]:
        return tuple(self._views[item.key] for item in manifest.candidate_versions if item.key in self._views)

    def read_sealed_handle(
        self,
        *,
        candidate: CandidateEvidenceView,
        input_hash: str,
        policy_hash: str,
    ) -> SealedAggregateHandle:
        actual_policy_hash = h("tampered-policy") if self._tamper_policy else policy_hash
        handle_id = "sealed-handle-" + content_hash(
            {
                "candidate": candidate.key,
                "input_hash": input_hash,
                "policy_hash": actual_policy_hash,
            }
        ).removeprefix("sha256:")[:20]
        handle = SealedAggregateHandle(
            handle_id=handle_id,
            handle_hash=content_hash({"handle_id": handle_id}),
            candidate_id=candidate.candidate_id,
            candidate_version=candidate.candidate_version,
            input_hash=input_hash,
            policy_hash=actual_policy_hash,
        )
        self._handles[handle.handle_hash] = handle
        self.last_handle = handle
        return handle

    def read_authorized_sealed_decision(
        self,
        *,
        handle: SealedAggregateHandle,
        request_hash: str,
    ) -> SealedDecisionResponse:
        if handle.handle_hash not in self._handles:
            raise StatisticalPromotionError(
                StatisticalPromotionFailureCode.FORGED_SEALED_HANDLE,
                "unknown sealed handle",
            )
        previous = self._consumed.get(handle.handle_hash)
        if previous is not None and previous != request_hash:
            raise StatisticalPromotionError(
                StatisticalPromotionFailureCode.SEALED_REPLAY,
                "sealed handle already consumed",
            )
        self._consumed[handle.handle_hash] = request_hash
        self.sealed_reads += 1
        return SealedDecisionResponse(
            request_hash=request_hash,
            passed=self._sealed_passed,
            failure_code=None if self._sealed_passed else StatisticalPromotionFailureCode.SEALED_DECISION_REJECTED,
        )


def service_for(
    reader: FakeEvidenceReader,
    *,
    method: MultipleTestingMethod = MultipleTestingMethod.FAMILY_BH_FDR,
    alpha: float = 0.10,
    robustness_policy: RobustnessPolicy | None = None,
) -> StatisticalPromotionService:
    return StatisticalPromotionService(
        reader=reader,
        robustness_policy=robustness_policy or RobustnessPolicy(min_observations=50),
        multiple_testing_policy=MultipleTestingPolicy(method=method, alpha=alpha),
    )


def evaluate(
    service: StatisticalPromotionService,
    candidate_id: str,
    *,
    idempotency_key: str,
    sealed_handle: SealedAggregateHandle | None = None,
):
    return service.evaluate_candidate(
        candidate_id=candidate_id,
        candidate_version="v1",
        family_id="family.alpha",
        manifest_version="v1",
        idempotency_key=idempotency_key,
        proposer_id="desk.fundamental",
        approver_id="statistical-promotion-gate",
        decision_time=DECISION_TIME,
        sealed_handle=sealed_handle,
    )


def test_family_bh_boundary_promotes_only_boundary_passing_candidate() -> None:
    manifest = manifest_for(("c1", "v1"), ("c2", "v1"), ("c3", "v1"))
    views = (
        make_view(manifest, "c1", "v1", p_value=0.01),
        make_view(manifest, "c2", "v1", p_value=0.04),
        make_view(manifest, "c3", "v1", p_value=0.20),
    )
    reader = FakeEvidenceReader(manifest, views)
    service = service_for(reader)

    accepted = evaluate(service, "c2", idempotency_key="idem.c2")
    rejected = evaluate(service, "c3", idempotency_key="idem.c3")

    assert accepted.decision.to_state is PromotionState.CHALLENGER
    assert accepted.decision.produced_by == "STATISTICAL_PROMOTION_GATE"
    assert rejected.decision.to_state is PromotionState.REJECTED
    assert rejected.decision.failure_code is StatisticalPromotionFailureCode.MULTIPLE_TESTING_FAILED
    assert reader.sealed_reads == 1


def test_bonferroni_and_holm_corrections_are_deterministic() -> None:
    p_values = {"c1@v1": 0.01, "c2@v1": 0.03, "c3@v1": 0.04}

    bonferroni = apply_multiple_testing(
        p_values,
        MultipleTestingPolicy(method=MultipleTestingMethod.BONFERRONI, alpha=0.05),
    )
    holm = apply_multiple_testing(
        p_values,
        MultipleTestingPolicy(method=MultipleTestingMethod.HOLM, alpha=0.05),
    )

    assert bonferroni.passed == {"c1@v1": True, "c2@v1": False, "c3@v1": False}
    assert holm.passed == {"c1@v1": True, "c2@v1": False, "c3@v1": False}
    assert bonferroni.artifact_hash == apply_multiple_testing(
        p_values,
        MultipleTestingPolicy(method=MultipleTestingMethod.BONFERRONI, alpha=0.05),
    ).artifact_hash


def test_duplicate_candidate_manifest_fails_closed() -> None:
    with pytest.raises(StatisticalPromotionError) as exc:
        manifest_for(("c1", "v1"), ("c1", "v1"))

    assert exc.value.code is StatisticalPromotionFailureCode.DUPLICATE_CANDIDATE


def test_missing_required_evidence_fails_closed() -> None:
    manifest = manifest_for(("c1", "v1"))
    view = make_view(manifest, "c1", "v1", p_value=0.01, drop_metric="coverage")
    reader = FakeEvidenceReader(manifest, (view,))
    service = service_for(reader)

    with pytest.raises(StatisticalPromotionError) as exc:
        evaluate(service, "c1", idempotency_key="missing.coverage")

    assert exc.value.code is StatisticalPromotionFailureCode.MISSING_EVIDENCE
    assert reader.sealed_reads == 0


def test_forged_handle_is_rejected_before_sealed_read() -> None:
    manifest = manifest_for(("c1", "v1"))
    view = make_view(manifest, "c1", "v1", p_value=0.01)
    reader = FakeEvidenceReader(manifest, (view,))
    service = service_for(reader)
    forged = SealedAggregateHandle(
        handle_id="forged-handle",
        handle_hash=h("forged-handle"),
        candidate_id="other-candidate",
        candidate_version="v1",
        input_hash=view.input_hash,
        policy_hash=service.gate.combined_policy_hash,
    )

    with pytest.raises(StatisticalPromotionError) as exc:
        evaluate(service, "c1", idempotency_key="forged.handle", sealed_handle=forged)

    assert exc.value.code is StatisticalPromotionFailureCode.FORGED_SEALED_HANDLE
    assert reader.sealed_reads == 0


def test_replay_handle_is_rejected_but_exact_idempotent_repeat_is_cached() -> None:
    manifest = manifest_for(("c1", "v1"))
    view = make_view(manifest, "c1", "v1", p_value=0.01)
    reader = FakeEvidenceReader(manifest, (view,))
    service = service_for(reader)

    first = evaluate(service, "c1", idempotency_key="idem.replay")
    repeat = evaluate(service, "c1", idempotency_key="idem.replay")

    assert repeat.decision.decision_hash == first.decision.decision_hash
    assert reader.sealed_reads == 1
    assert reader.last_handle is not None
    with pytest.raises(StatisticalPromotionError) as exc:
        evaluate(
            service,
            "c1",
            idempotency_key="idem.replay.second",
            sealed_handle=reader.last_handle,
        )

    assert exc.value.code is StatisticalPromotionFailureCode.SEALED_REPLAY


def test_threshold_or_policy_tampering_fails_closed() -> None:
    manifest = manifest_for(("c1", "v1"))
    view = make_view(manifest, "c1", "v1", p_value=0.01)
    reader = FakeEvidenceReader(manifest, (view,), tamper_policy=True)
    service = service_for(reader)

    with pytest.raises(StatisticalPromotionError) as exc:
        evaluate(service, "c1", idempotency_key="tampered.policy")

    assert exc.value.code is StatisticalPromotionFailureCode.POLICY_TAMPERED


def test_cost_after_impact_can_reject_gross_positive_candidate() -> None:
    manifest = manifest_for(("c1", "v1"))
    view = make_view(
        manifest,
        "c1",
        "v1",
        p_value=0.01,
        metric_overrides={
            "gross_return_before_cost": 0.05,
            "net_return_after_cost": -0.01,
        },
    )
    reader = FakeEvidenceReader(manifest, (view,))
    service = service_for(reader)

    result = evaluate(service, "c1", idempotency_key="cost.reject")

    assert result.decision.to_state is PromotionState.REJECTED
    assert result.decision.failure_code is StatisticalPromotionFailureCode.COST_FAILED
    assert reader.sealed_reads == 0


def test_capacity_failure_rejects_before_sealed_promotion() -> None:
    manifest = manifest_for(("c1", "v1"))
    view = make_view(
        manifest,
        "c1",
        "v1",
        p_value=0.01,
        metric_overrides={"capacity_utilization": 0.45},
    )
    reader = FakeEvidenceReader(manifest, (view,))
    service = service_for(reader)

    result = evaluate(service, "c1", idempotency_key="capacity.reject")

    assert result.decision.to_state is PromotionState.REJECTED
    assert result.decision.failure_code is StatisticalPromotionFailureCode.CAPACITY_FAILED
    assert reader.sealed_reads == 0


def test_sealed_rejection_is_redacted_and_does_not_leak_values() -> None:
    manifest = manifest_for(("c1", "v1"))
    view = make_view(manifest, "c1", "v1", p_value=0.01)
    reader = FakeEvidenceReader(manifest, (view,), sealed_passed=False)
    service = service_for(reader)

    result = evaluate(service, "c1", idempotency_key="sealed.reject")
    serialized = json.dumps(
        {
            "decision": result.decision.to_dict(),
            "audit": result.audit.to_dict(),
        },
        sort_keys=True,
    )

    assert result.decision.to_state is PromotionState.REJECTED
    assert result.decision.failure_code is StatisticalPromotionFailureCode.SEALED_DECISION_REJECTED
    assert reader.secret_marker not in serialized
    assert reader.last_handle is not None
    assert reader.last_handle.handle_id not in serialized
    assert "0.777" not in serialized


def test_same_input_reproduces_decision_and_audit_hashes() -> None:
    manifest = manifest_for(("c1", "v1"))
    view = make_view(manifest, "c1", "v1", p_value=0.01)

    reader_a = FakeEvidenceReader(manifest, (view,))
    reader_b = FakeEvidenceReader(manifest, (view,))
    result_a = evaluate(service_for(reader_a), "c1", idempotency_key="repro.same")
    result_b = evaluate(service_for(reader_b), "c1", idempotency_key="repro.same")

    assert result_a.decision.decision_hash == result_b.decision.decision_hash
    assert result_a.audit.artifact_hash == result_b.audit.artifact_hash

