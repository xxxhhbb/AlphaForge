from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))

from veriquant.contracts import ContractValidationError
from veriquant.data import canonical_content_hash
from veriquant.real_factor_smoke.api import (
    InternalFactorRunner,
    QlibComparisonStatus,
    RealAShareSmokeController,
    RealSmokeManifest,
    SmokeFactorDefinition,
    SmokeFactorStatus,
    SmokeRunStatus,
    default_factor_definitions,
)
from veriquant.real_research.api import (
    AShareResearchBridge,
    ResearchDatasetBundle,
    build_default_bridge_request,
    build_offline_real_research_fixture,
)
from veriquant.real_research.models import bundle_payload_hash


class FakeFrozenSnapshotProvider:
    def __init__(self, bundle: ResearchDatasetBundle | None) -> None:
        self.bundle = bundle

    def load_bundle(self, *, snapshot_id: str) -> ResearchDatasetBundle | None:
        return self.bundle


def _bundle() -> ResearchDatasetBundle:
    fixture = build_offline_real_research_fixture()
    request = build_default_bridge_request(
        snapshot_id=fixture.snapshot_id,
        decision_time=fixture.decision_time,
        split_manifest=fixture.split_manifest,
        forward_days=2,
        execution_lag_days=1,
        feature_fields=fixture.feature_fields,
        run_id="real-factor-smoke-test",
    )
    return AShareResearchBridge(fixture.reader).build_bundle(request)


def _run_controller(bundle: ResearchDatasetBundle, *, seed: int = 7, factors=None, runner=None) -> RealSmokeManifest:
    controller = RealAShareSmokeController(
        provider=FakeFrozenSnapshotProvider(bundle),
        runner=runner,
    )
    return controller.run(snapshot_id="fixture-ashare-real", seed=seed, factors=factors)


def _factor_result(manifest: RealSmokeManifest, factor_id: str):
    return next(item for item in manifest.factors if item.factor_id == factor_id)


def _mutate_sealed_only(bundle: ResearchDatasetBundle) -> ResearchDatasetBundle:
    rows = []
    changed = False
    for row in bundle.feature_rows:
        if row.partition == "sealed" and not changed:
            values = dict(row.values)
            values["close"] = values["close"] + 123.456
            values["volume"] = values["volume"] * 3.0
            values["amount"] = values["amount"] * 3.0
            rows.append(replace(row, values=values))
            changed = True
        else:
            rows.append(row)
    assert changed, "fixture must contain a sealed feature row"
    feature_rows = tuple(rows)
    output_hash = bundle_payload_hash(
        feature_rows=feature_rows,
        label_rows=bundle.label_rows,
        rejections=bundle.rejections,
        label_specification=bundle.label_specification,
        split_manifest=bundle.split_manifest,
        universe_manifest=bundle.universe_manifest,
        data_quality_manifest=bundle.data_quality_manifest,
    )
    return ResearchDatasetBundle(
        bundle_id=bundle.bundle_id,
        dataset_card=replace(
            bundle.dataset_card,
            snapshot_hash=output_hash,
            record_count=len(feature_rows) + len(bundle.label_rows),
        ),
        decision_time=bundle.decision_time,
        feature_fields=bundle.feature_fields,
        feature_rows=feature_rows,
        label_rows=bundle.label_rows,
        rejections=bundle.rejections,
        label_specification=bundle.label_specification,
        split_manifest=bundle.split_manifest,
        universe_manifest=bundle.universe_manifest,
        data_quality_manifest=bundle.data_quality_manifest,
        bridge_run_manifest=replace(
            bundle.bridge_run_manifest,
            input_hash=canonical_content_hash({"base": bundle.bridge_run_manifest.input_hash, "mutation": "sealed-only"}),
            output_hash=output_hash,
        ),
    )


def test_same_snapshot_config_seed_reproduces_manifest_hash() -> None:
    bundle = _bundle()
    factors = tuple(item for item in default_factor_definitions() if item.factor_id in {"reversal_5d", "volume_change_5d"})

    first = _run_controller(bundle, seed=7, factors=factors)
    second = _run_controller(bundle, seed=7, factors=factors)

    assert first.artifact_hash == second.artifact_hash
    assert first.status in {SmokeRunStatus.COMPLETED, SmokeRunStatus.PARTIAL}
    assert any(item.status is SmokeFactorStatus.EVALUATED for item in first.factors)
    assert all(item.train_evidence_hash for item in first.factors if item.status is SmokeFactorStatus.EVALUATED)


def test_manifest_missing_fields_rejected() -> None:
    with pytest.raises(ContractValidationError):
        RealSmokeManifest.from_payload(
            {
                "contract_type": "RealSmokeManifest",
                "schema_version": "veriquant.real-factor-smoke/v1",
                "manifest_id": "missing-fields",
            }
        )


def test_future_perturbation_does_not_change_past_signal() -> None:
    base = _bundle()
    mutated = _mutate_sealed_only(base)
    factor = tuple(item for item in default_factor_definitions() if item.factor_id == "reversal_5d")

    base_result = _factor_result(_run_controller(base, factors=factor), "reversal_5d")
    mutated_result = _factor_result(_run_controller(mutated, factors=factor), "reversal_5d")

    assert base_result.status is SmokeFactorStatus.EVALUATED
    assert mutated_result.status is SmokeFactorStatus.EVALUATED
    assert base_result.factor_output_hash == mutated_result.factor_output_hash


def test_non_factordag_factor_is_rejected_fail_closed() -> None:
    bundle = _bundle()
    bad = SmokeFactorDefinition(
        factor_id="bad-python-factor",
        payload={
            "schema_version": "not-a-factor-dag",
            "dag_id": "bad-python-factor",
            "python": "import os; os.system('echo forbidden')",
        },
    )

    result = _factor_result(_run_controller(bundle, factors=(bad,)), "bad-python-factor")

    assert result.status is SmokeFactorStatus.FAILED
    assert result.train_evidence_hash is None
    assert result.promotion_decision_hash is None
    assert "NOT_AVAILABLE" == result.official_verdict


def test_qlib_auxiliary_does_not_change_official_verdict() -> None:
    bundle = _bundle()
    factor = tuple(item for item in default_factor_definitions() if item.factor_id == "reversal_5d")
    runner = InternalFactorRunner(enable_fake_qlib_comparison=True)

    result = _factor_result(_run_controller(bundle, factors=factor, runner=runner), "reversal_5d")

    assert result.status is SmokeFactorStatus.EVALUATED
    assert result.qlib_comparison.status is QlibComparisonStatus.AUXILIARY_RESEARCH_EVIDENCE
    assert result.qlib_comparison.evidence_class == "AUXILIARY_RESEARCH_EVIDENCE"
    assert result.qlib_comparison.official_verdict_before == result.official_verdict
    assert result.qlib_comparison.official_verdict_after == result.official_verdict


def test_factor_failure_does_not_fake_success_and_others_continue() -> None:
    bundle = _bundle()
    bad = SmokeFactorDefinition(
        factor_id="bad-unknown-op",
        payload={
            "schema_version": "veriquant.factor-dag/v1",
            "dag_id": "bad-unknown-op",
            "nodes": [
                {"id": "close", "op": "field", "field": "close"},
                {"id": "x", "op": "python_callable", "inputs": ["close"]},
            ],
            "output_node": "x",
        },
    )
    good = next(item for item in default_factor_definitions() if item.factor_id == "reversal_5d")

    manifest = _run_controller(bundle, factors=(bad, good))
    failed = _factor_result(manifest, "bad-unknown-op")
    succeeded = _factor_result(manifest, "reversal_5d")

    assert manifest.status is SmokeRunStatus.PARTIAL
    assert failed.status is SmokeFactorStatus.FAILED
    assert failed.train_evidence_hash is None
    assert failed.failure_code
    assert succeeded.status is SmokeFactorStatus.EVALUATED
    assert succeeded.train_evidence_hash


def test_unavailable_snapshot_is_explicit_unavailable() -> None:
    controller = RealAShareSmokeController(provider=FakeFrozenSnapshotProvider(None))

    manifest = controller.run(snapshot_id="missing-real-snapshot", seed=7)

    assert manifest.status is SmokeRunStatus.UNAVAILABLE
    assert manifest.factors == ()
    assert "unavailable" in manifest.message.lower()
    assert "success" not in manifest.message.lower()
