from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
from types import SimpleNamespace

import pytest

from veriquant.contracts import ContractValidationError
from veriquant.data import MarketSnapshot, SnapshotRow, canonical_content_hash
from veriquant.data_capability import DataGapCode, DataRequirementPlanner, DatasetCapabilityAssessor
from veriquant.dynamic_data_planning import DataPlanningRequest, DynamicDataRequirementCompiler
from veriquant.integration.research_os_v2 import (
    FROZEN_DATA_SCHEMA_VERSION,
    FrozenValidationDataRepository,
)
from veriquant.integration.capture_research_os_v2_data import build_validation_data
from veriquant.real_ashare_scale import InputOrigin, ScaleInputRow
from veriquant.research_validation_pipeline_v2 import FrozenValidationData
from veriquant.research_validation_pipeline_v2.models import FrozenRowProvenance


UTC = timezone.utc


def _sha(value: object) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _frozen(*, available_offset_hours: int = 1, provenance: bool = True) -> FrozenValidationData:
    event_time = datetime(2026, 1, 5, 15, tzinfo=UTC)
    decision_time = event_time + timedelta(hours=2)
    fields = ("close", "volume")
    snapshot = MarketSnapshot(
        dataset_id="repair24.approved-proxy",
        decision_time=decision_time,
        start_time=event_time,
        fields=fields,
        rows=(SnapshotRow(event_time=event_time, symbol="000001.SZ", values={"close": 10.0, "volume": 1_000.0}),),
        historical_universe={event_time.isoformat(): ("000001.SZ",)},
        coverage_by_field={"close": 1.0, "volume": 1.0},
    )
    available_at = event_time + timedelta(hours=available_offset_hours)
    row_provenance = ()
    if provenance:
        row_provenance = (FrozenRowProvenance(
            symbol="000001.SZ",
            event_time=event_time,
            published_at=available_at,
            available_at=available_at,
            revision_id="proxy-r1",
            source_kind="daily",
            source_label="APPROVED_PROXY_DATA_VENDOR",
            provider_class="APPROVED_PROXY_DATA_VENDOR",
            source_class="TUSHARE_COMPATIBLE_PROXY",
            input_origin="FROZEN_REAL",
            captured_at=event_time + timedelta(hours=4),
            source_record_hash=canonical_content_hash({"source-row": 1}),
            adapter_manifest_hash=canonical_content_hash({"adapter": 1}),
            calendar_hash=canonical_content_hash({"calendar": 1}),
            universe_hash=canonical_content_hash({"universe": 1}),
            availability_policy_hash=canonical_content_hash({"availability": 1}),
            publication_time_policy="AVAILABLE_AT_CONSERVATIVE_PROXY",
        ),)
    return FrozenValidationData(
        snapshot=snapshot,
        labels={f"{event_time.isoformat()}|000001.SZ": 0.01},
        qualification_hash=canonical_content_hash({"qualification": 1}),
        source_label="APPROVED_PROXY_DATA_VENDOR",
        row_provenance=row_provenance,
    )


def _requirement():
    plan = DynamicDataRequirementCompiler().compile(DataPlanningRequest(
        question="验证一日价格信号",
        research_mode="NEW_ALPHA_EXPLORATION",
        factor_lookback_sessions=1,
        label_horizon_sessions=1,
        train_sessions=1,
        validation_sessions=1,
        sealed_sessions=1,
        buffer_sessions=1,
    ))
    return DataRequirementPlanner().build(plan)


def test_repository_roundtrip_replays_every_clock_revision_and_source_field(tmp_path):
    original = _frozen()
    repository = FrozenValidationDataRepository(tmp_path)
    saved_hash = repository.save(original)
    replayed = repository.load(require_strict_provenance=True)

    assert replayed is not None
    assert replayed.artifact_hash == original.artifact_hash
    assert replayed.row_provenance == original.row_provenance
    assert replayed.strict_provenance_available is True
    payload = json.loads(repository.path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == FROZEN_DATA_SCHEMA_VERSION
    assert payload["artifact_hash"] == saved_hash
    assert set(payload["row_provenance"][0]) >= {
        "event_time", "published_at", "available_at", "revision_id",
        "source_record_hash", "adapter_manifest_hash", "source_class", "provider_class",
    }


def test_repository_hash_is_stable_for_identical_frozen_input(tmp_path):
    left = FrozenValidationDataRepository(tmp_path / "left").save(_frozen())
    right = FrozenValidationDataRepository(tmp_path / "right").save(_frozen())
    assert left == right


@pytest.mark.parametrize("missing", ["published_at", "available_at", "revision_id"])
def test_repository_rejects_missing_clock_or_revision_even_with_recomputed_hash(tmp_path, missing):
    repository = FrozenValidationDataRepository(tmp_path)
    repository.save(_frozen())
    payload = json.loads(repository.path.read_text(encoding="utf-8"))
    payload["row_provenance"][0].pop(missing)
    material = {key: value for key, value in payload.items() if key != "artifact_hash"}
    payload["artifact_hash"] = _sha(material)
    repository.path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")

    with pytest.raises(ValueError, match="provenance is incomplete"):
        repository.load()


def test_future_available_at_is_rejected_by_frozen_contract():
    with pytest.raises(ContractValidationError, match="unavailable at snapshot decision_time"):
        _frozen(available_offset_hours=3)


def test_legacy_snapshot_loads_but_remains_strictly_unavailable(tmp_path):
    frozen = _frozen(provenance=False)
    repository = FrozenValidationDataRepository(tmp_path)
    payload = {
        "schema_version": "alphaforge.research-os-v2-integration/v2",
        "qualification_hash": frozen.qualification_hash,
        "source_label": frozen.source_label,
        "snapshot": {
            "dataset_id": frozen.snapshot.dataset_id,
            "decision_time": frozen.snapshot.decision_time.isoformat(),
            "start_time": frozen.snapshot.start_time.isoformat(),
            "fields": list(frozen.snapshot.fields),
            "rows": [{
                "event_time": frozen.snapshot.rows[0].event_time.isoformat(),
                "symbol": frozen.snapshot.rows[0].symbol,
                "values": dict(frozen.snapshot.rows[0].values),
            }],
            "historical_universe": dict(frozen.snapshot.historical_universe),
            "coverage_by_field": dict(frozen.snapshot.coverage_by_field),
        },
        "labels": dict(frozen.labels),
    }
    payload["artifact_hash"] = _sha(payload)
    repository.path.parent.mkdir(parents=True, exist_ok=True)
    repository.path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")

    replayed = repository.load()
    assert replayed is not None
    assert replayed.strict_provenance_available is False
    with pytest.raises(ValueError, match="strict frozen validation data unavailable"):
        repository.load(require_strict_provenance=True)

    report = DatasetCapabilityAssessor().assess(
        requirement=_requirement(), frozen_data=replayed, bitemporal_records=(),
    )
    assert DataGapCode.AVAILABLE_AT_UNVERIFIED in report.gap_codes
    assert DataGapCode.REVISION_SEMANTICS_UNVERIFIED in report.gap_codes
    assert DataGapCode.SOURCE_PROVENANCE_UNVERIFIED in report.gap_codes
    assert report.source_provenance_verified is False


def test_embedded_provenance_is_used_when_product_passes_empty_record_tuple():
    report = DatasetCapabilityAssessor().assess(
        requirement=_requirement(), frozen_data=_frozen(), bitemporal_records=(),
    )
    assert report.available_at_verified is True
    assert report.revision_semantics_verified is True
    assert report.source_provenance_verified is True


def test_new_capture_preserves_original_scale_row_lineage_without_legacy_backfill():
    event_time = datetime(2026, 1, 5, 15, tzinfo=UTC)
    available_at = event_time + timedelta(hours=1)
    source_row = ScaleInputRow(
        symbol="000001.SZ",
        session="2026-01-05",
        event_time=event_time,
        available_at=available_at,
        revision_id="proxy-r1",
        features={"close": 10.0, "volume": 1_000.0},
        labels={"future_return_5d": 0.01},
    )
    frozen = SimpleNamespace(
        rows=(source_row,),
        feature_fields=("close", "volume"),
        historical_universe=(SimpleNamespace(session="2026-01-05", symbols=("000001.SZ",)),),
        vintages=(SimpleNamespace(session="2026-01-05", decision_time=event_time + timedelta(hours=2)),),
        dataset_id="repair24.capture",
        provider_class="APPROVED_PROXY_DATA_VENDOR",
        source_class="TUSHARE_COMPATIBLE_PROXY",
        input_origin=InputOrigin.FROZEN_REAL,
        capture_time=event_time + timedelta(hours=4),
        adapter_manifest_hash=canonical_content_hash({"adapter": 1}),
        calendar_hash=canonical_content_hash({"calendar": 1}),
        universe_hash=canonical_content_hash({"universe": 1}),
        availability_policy_hash=canonical_content_hash({"availability": 1}),
    )

    projected = build_validation_data(
        frozen,
        qualification_hash=canonical_content_hash({"qualification": 1}),
    )

    lineage = projected.row_provenance[0]
    assert lineage.event_time == source_row.event_time
    assert lineage.available_at == source_row.available_at
    assert lineage.published_at == source_row.available_at
    assert lineage.revision_id == source_row.revision_id
    assert lineage.source_record_hash == source_row.artifact_hash
    assert lineage.publication_time_policy == "AVAILABLE_AT_CONSERVATIVE_PROXY"
