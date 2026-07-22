from __future__ import annotations

from dataclasses import replace

import pytest

from veriquant.contracts import ContractValidationError, FailureCode
from veriquant.real_ashare_data.reader import FrozenSnapshotHandle
from veriquant.real_factor_smoke.api import RealAShareSmokeController, SmokeRunStatus, default_factor_definitions
from veriquant.real_research.api import (
    AShareResearchBridge,
    build_default_bridge_request,
    build_offline_real_research_fixture,
)
from veriquant.integration.v1_provider import (
    FrozenAShareBundleProvider,
    FrozenBundleRegistration,
    FrozenBundleStatus,
)


def _bundle():
    fixture = build_offline_real_research_fixture()
    request = build_default_bridge_request(
        snapshot_id=fixture.snapshot_id,
        decision_time=fixture.decision_time,
        split_manifest=fixture.split_manifest,
        forward_days=2,
        execution_lag_days=1,
        feature_fields=fixture.feature_fields,
        run_id="integration-v1-provider",
    )
    return fixture, AShareResearchBridge(fixture.reader).build_bundle(request)


class Reader:
    def __init__(self, handle):
        self.handle = handle
        self.reads = 0

    def open(self, manifest_path):
        assert manifest_path == "registered/manifest.json"
        return self.handle

    def read_snapshot(self, handle):
        assert handle == self.handle
        self.reads += 1
        return object()


class Materializer:
    def __init__(self, bundle):
        self.bundle = bundle

    def build_bundle(self, *, snapshot_id, handle, reader):
        assert snapshot_id == self.bundle.bridge_run_manifest.snapshot_id
        assert handle.snapshot_hash == self.bundle.bridge_run_manifest.snapshot_hash
        return self.bundle


def _provider(bundle):
    snapshot_hash = bundle.bridge_run_manifest.snapshot_hash
    handle = FrozenSnapshotHandle(
        dataset_id="fixture-ashare-real",
        snapshot_hash=snapshot_hash,
        snapshot_dir="registered",
        manifest_path="registered/manifest.json",
        raw_path="registered/raw_response.json",
        manifest_hash="sha256:" + "1" * 64,
        raw_content_hash="sha256:" + "2" * 64,
        universe_hash="sha256:" + "3" * 64,
        calendar_hash="sha256:" + "4" * 64,
        calendar_manifest_path="registered/calendar.json",
    )
    reader = Reader(handle)
    provider = FrozenAShareBundleProvider(
        reader=reader,
        materializer=Materializer(bundle),
        registrations={
            bundle.bridge_run_manifest.snapshot_id: FrozenBundleRegistration(
                snapshot_id=bundle.bridge_run_manifest.snapshot_id,
                manifest_path=handle.manifest_path,
            )
        },
    )
    return provider, reader


def test_verified_agent_a_handle_materializes_agent_b_bundle_and_real_factor_smoke() -> None:
    _fixture, bundle = _bundle()
    provider, reader = _provider(bundle)

    loaded = provider.load_bundle(snapshot_id=bundle.bridge_run_manifest.snapshot_id)
    manifest = RealAShareSmokeController(provider=provider).run(
        snapshot_id=bundle.bridge_run_manifest.snapshot_id,
        seed=7,
        factors=(default_factor_definitions()[1],),
    )

    assert loaded is not None
    assert reader.reads == 2
    assert provider.availability(snapshot_id=bundle.bridge_run_manifest.snapshot_id).status is FrozenBundleStatus.AVAILABLE
    assert manifest.status is SmokeRunStatus.COMPLETED
    assert manifest.factors[0].train_evidence_hash
    assert manifest.factors[0].validation_evidence_hash


def test_missing_tushare_snapshot_is_typed_unavailable_and_never_falls_back() -> None:
    _fixture, bundle = _bundle()
    provider, _reader = _provider(bundle)

    assert provider.load_bundle(snapshot_id="missing-tushare") is None
    availability = provider.availability(snapshot_id="missing-tushare")
    assert availability.status is FrozenBundleStatus.UNAVAILABLE
    assert availability.failure_code == FailureCode.ADAPTER_UNAVAILABLE.value
    assert availability.source == "tushare"


def test_non_tushare_registration_and_hash_mismatch_fail_closed() -> None:
    with pytest.raises(ContractValidationError) as source_error:
        FrozenBundleRegistration(
            snapshot_id="bad-source",
            manifest_path="registered/manifest.json",
            source="tencent",
        )
    assert source_error.value.code is FailureCode.UNAUTHORIZED_CAPABILITY

    _fixture, bundle = _bundle()
    provider, _reader = _provider(
        replace(
            bundle,
            bridge_run_manifest=replace(
                bundle.bridge_run_manifest,
                snapshot_hash="sha256:" + "f" * 64,
            ),
        )
    )
    assert provider.load_bundle(snapshot_id=bundle.bridge_run_manifest.snapshot_id) is None
    assert provider.availability(snapshot_id=bundle.bridge_run_manifest.snapshot_id).status is FrozenBundleStatus.FAILED

