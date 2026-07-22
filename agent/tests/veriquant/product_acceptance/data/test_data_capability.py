from __future__ import annotations

from datetime import datetime, timedelta, timezone
from dataclasses import dataclass

import pytest

from veriquant.bitemporal_features import NormalizedRecord, SourceKind, canonical_hash
from veriquant.data import MarketSnapshot, SnapshotRow, canonical_content_hash
from veriquant.data_capability import (
    APPROVED_SOURCE_LABEL,
    CapabilityStatus,
    DataGapCode,
    DataRequirementPlanner,
    DatasetCapabilityAssessor,
)
from veriquant.dynamic_data_planning import (
    DataPlanningRequest,
    DynamicDataRequirementCompiler,
    UniverseKind,
    UniverseSpec,
)
from veriquant.research_validation_pipeline_v2.models import FrozenRowProvenance


@dataclass(frozen=True)
class FrozenFixture:
    snapshot: MarketSnapshot
    labels: dict[str, float]
    qualification_hash: str
    source_label: str
    row_provenance: tuple[FrozenRowProvenance, ...]


def _dynamic_plan(*, lookback=3, symbols=("AAA", "BBB", "CCC", "DDD")):
    return DynamicDataRequirementCompiler().compile(DataPlanningRequest(
        question=f"研究 {lookback} 日动量预测未来 1 日收益",
        research_mode="NEW_ALPHA_EXPLORATION",
        factor_lookback_sessions=lookback,
        label_horizon_sessions=1,
        train_sessions=4,
        validation_sessions=2,
        sealed_sessions=1,
        buffer_sessions=1,
        universe=UniverseSpec(kind=UniverseKind.EXPLICIT_SYMBOLS, symbols=symbols),
    ))


def _frozen(requirement, *, missing_close=False, late_clock=False, duplicate_revision=False):
    start = datetime(2026, 1, 1, 15, tzinfo=timezone.utc)
    decision_time = start + timedelta(days=requirement.minimum_sessions + 1)
    symbols = tuple(f"S{index:03d}" for index in range(requirement.minimum_cross_section))
    rows = []
    labels = {}
    universe = {}
    records = []
    provenance = []
    for day in range(requirement.minimum_sessions):
        event_time = start + timedelta(days=day)
        universe[event_time.isoformat()] = symbols
        for index, symbol in enumerate(symbols):
            values = {
                "close": None if missing_close else 10.0 + day * 0.1 + index * 0.01,
                "volume": 1000.0 + index,
                "adj_factor": 1.0,
                "market_cap": 1_000_000.0 + index,
                "turnover_rate": 1.0,
                "list_date": "20200101",
                "delist_date": None,
            }
            rows.append(SnapshotRow(event_time=event_time, symbol=symbol, values=values))
            labels[f"{event_time.isoformat()}|{symbol}"] = (index + 1) * 0.0001
            available_at = (
                start + timedelta(days=requirement.minimum_sessions + 2)
                if late_clock
                else event_time + timedelta(hours=2)
            )
            records.append(NormalizedRecord(
                source=SourceKind.DAILY,
                entity_id=symbol,
                event_time=event_time,
                published_at=event_time + timedelta(hours=1),
                available_at=available_at,
                revision_id=f"r-{day}-{index}",
                values={"close": values["close"]},
                raw_hash=canonical_hash({"day": day, "symbol": symbol}),
            ))
            if not late_clock:
                provenance.append(FrozenRowProvenance(
                    symbol=symbol,
                    event_time=event_time,
                    published_at=event_time + timedelta(hours=1),
                    available_at=available_at,
                    revision_id=f"r-{day}-{index}",
                    source_kind=SourceKind.DAILY.value,
                    source_label=APPROVED_SOURCE_LABEL,
                    provider_class="approved_proxy_adapter",
                    source_class="tushare_compatible_proxy",
                    input_origin="test_fixture",
                    captured_at=decision_time,
                    source_record_hash=canonical_hash({"day": day, "symbol": symbol}),
                    adapter_manifest_hash=canonical_hash({"adapter": "fixture-v1"}),
                    calendar_hash=canonical_hash({"calendar": "fixture-v1"}),
                    universe_hash=canonical_hash({"universe": symbols}),
                    availability_policy_hash=canonical_hash({"availability": "fixture-v1"}),
                    publication_time_policy="PROVIDER_DECLARED_PUBLICATION_TIME",
                ))
    if duplicate_revision:
        records.append(records[0])
    snapshot = MarketSnapshot(
        dataset_id="approved-proxy.capability-fixture",
        decision_time=decision_time,
        start_time=start,
        fields=("close", "volume", "adj_factor", "market_cap", "turnover_rate", "list_date", "delist_date"),
        rows=tuple(rows),
        historical_universe=universe,
        coverage_by_field={field: 1.0 for field in ("volume", "adj_factor", "market_cap", "turnover_rate", "list_date", "delist_date")}
        | {"close": 0.0 if missing_close else 1.0},
    )
    return FrozenFixture(
        snapshot=snapshot,
        labels=labels,
        qualification_hash=canonical_content_hash({"fixture": True}),
        source_label=APPROVED_SOURCE_LABEL,
        row_provenance=tuple(provenance),
    ), tuple(records)


def test_minimum_sample_is_derived_from_question_protocol_and_universe_not_product_caps():
    small_dynamic = _dynamic_plan(lookback=3, symbols=("A", "B", "C", "D"))
    large_dynamic = _dynamic_plan(lookback=50, symbols=tuple(f"S{i}" for i in range(80)))
    small = DataRequirementPlanner().build(small_dynamic)
    large = DataRequirementPlanner().build(large_dynamic)

    assert small.minimum_sessions == small_dynamic.history.minimum_total_sessions + 1 + 1
    assert large.minimum_sessions - small.minimum_sessions == 47
    assert small.minimum_cross_section == 4
    assert large.minimum_cross_section == 80
    assert large.minimum_rows == large.minimum_sessions * 80
    assert large.purge_sessions == large.label_horizon_sessions
    assert "embargo" in large.split_policy


def test_ready_requires_observed_coverage_clocks_revisions_universe_and_labels():
    requirement = DataRequirementPlanner().build(_dynamic_plan())
    frozen, records = _frozen(requirement)
    report = DatasetCapabilityAssessor().assess(
        requirement=requirement,
        frozen_data=frozen,
        bitemporal_records=records,
    )

    assert report.status is CapabilityStatus.READY
    assert report.gap_codes == ()
    assert report.available_at_verified is True
    assert report.revision_semantics_verified is True
    assert report.historical_universe_verified is True
    assert report.label_coverage == 1.0
    assert all(item.usable and item.observed_rows > 0 for item in report.field_proofs)


def test_field_name_without_non_null_observations_cannot_masquerade_as_available():
    requirement = DataRequirementPlanner().build(_dynamic_plan())
    frozen, records = _frozen(requirement, missing_close=True)
    report = DatasetCapabilityAssessor().assess(
        requirement=requirement,
        frozen_data=frozen,
        bitemporal_records=records,
    )

    close = next(item for item in report.field_proofs if item.field_name == "close")
    assert close.observed_rows == 0
    assert close.usable is False
    assert DataGapCode.FIELD_HAS_NO_OBSERVATIONS in report.gap_codes
    assert report.status is CapabilityStatus.UNAVAILABLE


def test_unverified_available_at_or_revision_is_a_typed_gap():
    requirement = DataRequirementPlanner().build(_dynamic_plan())
    late_data, late_records = _frozen(requirement, late_clock=True)
    late = DatasetCapabilityAssessor().assess(
        requirement=requirement, frozen_data=late_data, bitemporal_records=late_records,
    )
    duplicate_data, duplicate_records = _frozen(requirement, duplicate_revision=True)
    duplicate = DatasetCapabilityAssessor().assess(
        requirement=requirement, frozen_data=duplicate_data, bitemporal_records=duplicate_records,
    )

    assert DataGapCode.AVAILABLE_AT_UNVERIFIED in late.gap_codes
    assert DataGapCode.REVISION_SEMANTICS_UNVERIFIED in duplicate.gap_codes


def test_missing_labels_cannot_be_hidden_by_feature_coverage():
    requirement = DataRequirementPlanner().build(_dynamic_plan())
    frozen, records = _frozen(requirement)
    without_labels = FrozenFixture(
        snapshot=frozen.snapshot,
        labels={},
        qualification_hash=frozen.qualification_hash,
        source_label=frozen.source_label,
        row_provenance=frozen.row_provenance,
    )
    report = DatasetCapabilityAssessor().assess(
        requirement=requirement, frozen_data=without_labels, bitemporal_records=records,
    )

    assert report.label_rows == 0
    assert DataGapCode.LABELS_MISSING in report.gap_codes
    assert report.status is CapabilityStatus.UNAVAILABLE


def test_receipt_is_stable_offline_and_has_no_evidence_authority():
    requirement = DataRequirementPlanner().build(_dynamic_plan())
    frozen, records = _frozen(requirement)
    assessor = DatasetCapabilityAssessor()
    report = assessor.assess(requirement=requirement, frozen_data=frozen, bitemporal_records=records)
    manifest_hash = canonical_content_hash({"approved_proxy_freeze": True})

    left = assessor.receipt(
        requirement=requirement, frozen_data=frozen, report=report, freeze_manifest_hash=manifest_hash,
    )
    right = assessor.receipt(
        requirement=requirement, frozen_data=frozen, report=report, freeze_manifest_hash=manifest_hash,
    )

    assert left.status is CapabilityStatus.READY
    assert left.artifact_hash == right.artifact_hash
    assert left.official_evidence_authority is False
    assert left.network_used is False
    assert left.source_label == APPROVED_SOURCE_LABEL
    assert "OFFICIAL_TUSHARE" not in left.canonical_json()


def test_missing_manifest_or_local_source_returns_typed_offline_gap():
    dynamic = _dynamic_plan()
    requirement = DataRequirementPlanner().build(dynamic)
    frozen, records = _frozen(requirement, missing_close=True)
    assessor = DatasetCapabilityAssessor()
    report = assessor.assess(requirement=requirement, frozen_data=frozen, bitemporal_records=records)
    receipt = assessor.receipt(
        requirement=requirement, frozen_data=frozen, report=report, freeze_manifest_hash=None,
    )
    expansion = assessor.expansion_request(
        dynamic_plan=dynamic,
        requirement=requirement,
        report=report,
        approved_local_source_available=False,
    )

    assert DataGapCode.FREEZE_MANIFEST_MISSING in receipt.gap_codes
    assert receipt.status is CapabilityStatus.UNAVAILABLE
    assert expansion.status == "BLOCKED_NO_APPROVED_LOCAL_SOURCE"
    assert expansion.network_allowed is False
    assert "close" in expansion.missing_fields


def test_non_approved_source_is_rejected_before_capability_claim():
    requirement = DataRequirementPlanner().build(_dynamic_plan())
    frozen, records = _frozen(requirement)
    forged = FrozenFixture(
        snapshot=frozen.snapshot,
        labels=frozen.labels,
        qualification_hash=frozen.qualification_hash,
        source_label="DETERMINISTIC_OFFLINE_FIXTURE",
        row_provenance=frozen.row_provenance,
    )
    with pytest.raises(ValueError, match="APPROVED_PROXY_DATA_VENDOR"):
        DatasetCapabilityAssessor().assess(
            requirement=requirement, frozen_data=forged, bitemporal_records=records,
        )
