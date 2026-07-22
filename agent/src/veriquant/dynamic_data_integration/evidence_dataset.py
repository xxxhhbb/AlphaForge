"""Materialize a campaign-scoped PIT dataset for the official Evidence boundary.

The market spine (rows, historical universe and labels) is supplied by the
existing approved-proxy scale adapter.  This module only joins additional
content-addressed, bitemporal source revisions onto that spine.  It never
fetches data, executes a formula, or computes official metrics.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable

from ..bitemporal_features import (
    BitemporalError,
    BitemporalStore,
    NormalizedRecord,
    SourceKind,
    canonical_record_hash,
)
from ..data import MarketSnapshot, SnapshotRow, canonical_content_hash
from ..research_validation_pipeline_v2 import FrozenValidationData
from .projection import (
    load_verified_bitemporal_store,
    load_verified_freeze_manifest,
)
from .service import SOURCE_LABEL


SCHEMA_VERSION = "veriquant.campaign-evidence-dataset/v1"

# Market-wide series are frozen under their provider entity rather than copied
# once per equity.  The projection layer deliberately owns this mapping so the
# normalizer can preserve the source's original identity while every equity
# row receives the same point-in-time market observation.
_GLOBAL_SOURCE_ENTITIES = {
    SourceKind.INDEX_DAILY: "000001.SH",
    SourceKind.SHIBOR: "MARKET:CNY",
}


@dataclass(frozen=True, slots=True)
class CampaignEvidenceDatasetManifest:
    schema_version: str
    base_data_hash: str
    dynamic_freeze_hash: str
    bitemporal_store_hash: str
    projected_records_hash: str
    snapshot_hash: str
    labels_hash: str
    qualification_hash: str
    source_label: str
    fields: tuple[str, ...]
    added_fields: tuple[str, ...]
    required_fields: tuple[str, ...]
    optional_fields: tuple[str, ...]
    missing_required_fields: tuple[str, ...]
    row_count: int
    symbol_count: int
    session_count: int
    projected_record_count: int

    @property
    def artifact_hash(self) -> str:
        return canonical_content_hash(self)


@dataclass(frozen=True, kw_only=True)
class CampaignFrozenValidationData(FrozenValidationData):
    """Evidence input plus row-projected source clocks for capability proof."""

    bitemporal_records: tuple[NormalizedRecord, ...] = ()

    def __post_init__(self) -> None:
        super().__post_init__()
        if not isinstance(self.bitemporal_records, tuple) or any(
            not isinstance(item, NormalizedRecord) for item in self.bitemporal_records
        ):
            raise BitemporalError("bitemporal_records must contain NormalizedRecord values")


@dataclass(frozen=True, slots=True)
class CampaignEvidenceDatasetBundle:
    frozen_data: FrozenValidationData
    projected_records: tuple[NormalizedRecord, ...]
    manifest: CampaignEvidenceDatasetManifest

    def __post_init__(self) -> None:
        if self.frozen_data.snapshot.artifact_hash != self.manifest.snapshot_hash:
            raise BitemporalError("dataset manifest snapshot hash mismatch")
        if self.frozen_data.labels_hash != self.manifest.labels_hash:
            raise BitemporalError("dataset manifest labels hash mismatch")
        if canonical_record_hash(self.projected_records) != self.manifest.projected_records_hash:
            raise BitemporalError("dataset manifest projected-record hash mismatch")
        if self.frozen_data.qualification_hash != self.manifest.qualification_hash:
            raise BitemporalError("dataset manifest qualification hash mismatch")

    @property
    def artifact_hash(self) -> str:
        return canonical_content_hash({
            "schema_version": SCHEMA_VERSION,
            "frozen_data_hash": self.frozen_data.artifact_hash,
            "manifest_hash": self.manifest.artifact_hash,
            "projected_records_hash": self.manifest.projected_records_hash,
        })


class CampaignEvidenceDatasetBuilder:
    """Join verified source vintages onto an already-qualified market spine."""

    def build(
        self,
        *,
        base: FrozenValidationData,
        freeze_root: str | Path,
        required_fields: Iterable[str] = (),
        optional_fields: Iterable[str] = (),
    ) -> CampaignEvidenceDatasetBundle:
        if base.source_label != SOURCE_LABEL:
            raise BitemporalError("base data is not from the approved proxy source")
        required = _stable_fields(required_fields, "required_fields")
        optional = _stable_fields(optional_fields, "optional_fields")
        if set(required) & set(optional):
            raise BitemporalError("required and optional fields overlap")

        root = Path(freeze_root).resolve()
        freeze_manifest = load_verified_freeze_manifest(root)
        store = load_verified_bitemporal_store(root, freeze_manifest)
        store_records = store.records
        available = {
            field
            for record in store_records
            for field in record.values
        }
        requested = tuple(sorted(set(required) | set(optional)))
        additions = tuple(
            field
            for field in (requested or tuple(sorted(available)))
            if field in available and field not in base.snapshot.fields
        )
        missing_required = tuple(
            field
            for field in required
            if field not in base.snapshot.fields and field not in available
        )

        fields = tuple(base.snapshot.fields) + additions
        requested_dynamic_fields = frozenset(additions)
        # A field can be supplied by more than one registered source.  Keep all
        # such sources so collision checks remain fail-closed, but do not query
        # unrelated source buckets for every one of hundreds of thousands of
        # market rows.
        fields_by_source: dict[SourceKind, set[str]] = {}
        for record in store_records:
            fields_by_source.setdefault(record.source, set()).update(record.values)
        projection_sources = tuple(
            source
            for source in SourceKind
            if not requested_dynamic_fields.isdisjoint(fields_by_source.get(source, ()))
        )
        rows: list[SnapshotRow] = []
        projected: list[NormalizedRecord] = []
        non_null = {field: 0 for field in fields}
        provenance_by_row = {
            (item.symbol, item.event_time): item
            for item in base.row_provenance
        }
        for base_row in base.snapshot.rows:
            decision_time = _row_decision_time(base_row.event_time, base.snapshot.decision_time)
            values = {field: base_row.values.get(field) for field in base.snapshot.fields}
            projected.extend(
                _base_projected_records(
                    base_row=base_row,
                    decision_time=decision_time,
                    provenance=provenance_by_row.get((base_row.symbol, base_row.event_time)),
                )
            )
            dynamic_values, dynamic_records = _dynamic_projection(
                store=store,
                symbol=base_row.symbol,
                event_time=base_row.event_time,
                decision_time=decision_time,
                fields=additions,
                sources=projection_sources,
            )
            values.update(dynamic_values)
            projected.extend(dynamic_records)
            ordered = {field: values.get(field) for field in fields}
            for field, value in ordered.items():
                if value is not None:
                    non_null[field] += 1
            rows.append(
                SnapshotRow(
                    event_time=base_row.event_time,
                    symbol=base_row.symbol,
                    values=ordered,
                )
            )

        projected_tuple = tuple(
            sorted(
                projected,
                key=lambda item: (
                    item.source.value,
                    item.entity_id,
                    item.event_time,
                    item.available_at,
                    item.revision_id,
                ),
            )
        )
        row_count = len(rows)
        snapshot = MarketSnapshot(
            dataset_id=(
                base.snapshot.dataset_id
                + ".evidence."
                + str(freeze_manifest["artifact_hash"])[7:19]
            ),
            decision_time=base.snapshot.decision_time,
            start_time=base.snapshot.start_time,
            fields=fields,
            rows=tuple(rows),
            historical_universe=base.snapshot.historical_universe,
            coverage_by_field={
                field: (non_null[field] / row_count if row_count else 0.0)
                for field in fields
            },
        )
        projected_hash = canonical_record_hash(projected_tuple)
        qualification_hash = canonical_content_hash({
            "schema_version": SCHEMA_VERSION,
            "base_qualification_hash": base.qualification_hash,
            "base_data_hash": base.artifact_hash,
            "dynamic_freeze_hash": freeze_manifest["artifact_hash"],
            "bitemporal_store_hash": store.artifact_hash,
            "projected_records_hash": projected_hash,
            "fields": fields,
        })
        frozen = CampaignFrozenValidationData(
            snapshot=snapshot,
            labels=base.labels,
            qualification_hash=qualification_hash,
            source_label=SOURCE_LABEL,
            row_provenance=base.row_provenance,
            bitemporal_records=projected_tuple,
        )
        manifest = CampaignEvidenceDatasetManifest(
            schema_version=SCHEMA_VERSION,
            base_data_hash=base.artifact_hash,
            dynamic_freeze_hash=str(freeze_manifest["artifact_hash"]),
            bitemporal_store_hash=store.artifact_hash,
            projected_records_hash=projected_hash,
            snapshot_hash=snapshot.artifact_hash,
            labels_hash=frozen.labels_hash,
            qualification_hash=qualification_hash,
            source_label=SOURCE_LABEL,
            fields=fields,
            added_fields=additions,
            required_fields=required,
            optional_fields=optional,
            missing_required_fields=missing_required,
            row_count=row_count,
            symbol_count=len({row.symbol for row in rows}),
            session_count=len(snapshot.historical_universe),
            projected_record_count=len(projected_tuple),
        )
        return CampaignEvidenceDatasetBundle(
            frozen_data=frozen,
            projected_records=projected_tuple,
            manifest=manifest,
        )


def _stable_fields(values: Iterable[str], name: str) -> tuple[str, ...]:
    output = tuple(sorted(set(values)))
    if any(not isinstance(item, str) or not item.strip() for item in output):
        raise BitemporalError(f"{name} contains an invalid field")
    return output


def _row_decision_time(event_time: datetime, final_decision_time: datetime) -> datetime:
    # Approved A-share market rows are stamped at the 15:00 exchange close and
    # become research-decision eligible at 17:00 in the same absolute clock.
    # Canonical serialization normalizes timestamps to UTC, so rebuilding a
    # literal ``17:00`` in ``event_time.tzinfo`` would turn 17:00 Asia/Shanghai
    # into 17:00 UTC and push the decision into the next local day.
    decision = event_time + timedelta(hours=2)
    if decision > final_decision_time:
        raise BitemporalError("row decision time exceeds frozen dataset decision time")
    return decision


def _base_projected_records(*, base_row, decision_time, provenance) -> tuple[NormalizedRecord, ...]:
    if provenance is None:
        return ()
    try:
        source = SourceKind(provenance.source_kind)
    except ValueError as exc:
        raise BitemporalError("base provenance uses an unsupported source kind") from exc
    return (
        NormalizedRecord(
            source=source,
            entity_id=base_row.symbol,
            event_time=base_row.event_time,
            published_at=provenance.published_at,
            available_at=provenance.available_at,
            decision_time=decision_time,
            revision_id=provenance.revision_id,
            values=dict(base_row.values),
            raw_hash=provenance.source_record_hash,
        ),
    )


def _dynamic_projection(
    *,
    store: BitemporalStore,
    symbol: str,
    event_time: datetime,
    decision_time: datetime,
    fields: tuple[str, ...],
    sources: tuple[SourceKind, ...] | None = None,
) -> tuple[dict[str, object], tuple[NormalizedRecord, ...]]:
    if not fields:
        return {}, ()
    requested = set(fields)
    values: dict[str, object] = {field: None for field in fields}
    projected: list[NormalizedRecord] = []
    for source in SourceKind if sources is None else sources:
        lookup_entity = _GLOBAL_SOURCE_ENTITIES.get(source, symbol)
        record = store.latest_as_of(
            source=source,
            entity_id=lookup_entity,
            event_time=event_time,
            decision_time=decision_time,
            bind_decision_time=False,
        )
        if record is None:
            continue
        selected = {
            field: value
            for field, value in record.values.items()
            if field in requested
        }
        if not selected:
            continue
        for field, value in selected.items():
            previous = values.get(field)
            if previous is not None and value is not None and previous != value:
                raise BitemporalError(f"ambiguous projected feature collision: {field}")
            values[field] = value
        projected.append(
            NormalizedRecord(
                source=source,
                entity_id=symbol,
                event_time=event_time,
                published_at=record.published_at,
                available_at=record.available_at,
                decision_time=decision_time,
                revision_id=(
                    record.revision_id
                    + "|asof:"
                    + event_time.date().isoformat()
                ),
                values=selected,
                raw_hash=record.raw_hash,
            )
        )
    return values, tuple(projected)


__all__ = [
    "CampaignEvidenceDatasetBuilder",
    "CampaignEvidenceDatasetBundle",
    "CampaignEvidenceDatasetManifest",
    "CampaignFrozenValidationData",
    "SCHEMA_VERSION",
]
