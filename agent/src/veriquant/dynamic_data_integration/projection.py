"""Project a verified dynamic freeze into the existing research snapshot."""

from __future__ import annotations

from datetime import datetime, time
import json
from pathlib import Path
from typing import Any, TYPE_CHECKING

from ..bitemporal_features import (
    BitemporalStore,
    normalize_rows,
)
from ..data import MarketSnapshot, SnapshotRow, canonical_content_hash
from ..dynamic_acquisition import ContentAddressedRowStore, stable_content_hash
from .service import SOURCE_LABEL, _FEATURE_APIS

if TYPE_CHECKING:
    from ..research_validation_pipeline_v2 import FrozenValidationData


def enrich_frozen_validation_data(
    base: "FrozenValidationData",
    *,
    freeze_root: str | Path,
) -> "FrozenValidationData":
    """Add PIT-safe normalized fields without altering base rows or labels."""

    root = Path(freeze_root).resolve()
    manifest = load_verified_freeze_manifest(root)
    store = load_verified_bitemporal_store(root, manifest)
    discovered = tuple(
        sorted({
            field
            for record in store.records
            for field in record.values
            if field not in base.snapshot.fields
        })
    )
    if not discovered:
        raise ValueError("dynamic freeze contains no new normalized fields")
    fields = tuple(base.snapshot.fields) + discovered
    rows = []
    non_null = {field: 0 for field in fields}
    for base_row in base.snapshot.rows:
        decision_time = datetime.combine(
            base_row.event_time.date(),
            time(17, 0),
            tzinfo=base_row.event_time.tzinfo,
        )
        visible = store.as_of(
            event_time=base_row.event_time,
            decision_time=decision_time,
            entities=(base_row.symbol, "MARKET:CNY"),
        )
        feature_values = {field: None for field in discovered}
        for record in visible:
            for field, value in record.values.items():
                if field in feature_values:
                    feature_values[field] = value
        values = {
            **{field: base_row.values.get(field) for field in base.snapshot.fields},
            **feature_values,
        }
        for field, value in values.items():
            if value is not None:
                non_null[field] += 1
        rows.append(
            SnapshotRow(
                event_time=base_row.event_time,
                symbol=base_row.symbol,
                values=values,
            )
        )
    row_count = len(rows)
    snapshot = MarketSnapshot(
        dataset_id=(
            base.snapshot.dataset_id
            + ".dynamic."
            + str(manifest["artifact_hash"])[7:19]
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
    # Delayed import prevents the legacy research_os/research_campaign import
    # cycle from contaminating otherwise standalone dynamic-data tooling.
    from ..research_validation_pipeline_v2 import FrozenValidationData

    return FrozenValidationData(
        snapshot=snapshot,
        labels=base.labels,
        qualification_hash=canonical_content_hash({
            "base_qualification_hash": base.qualification_hash,
            "dynamic_freeze_hash": manifest["artifact_hash"],
            "bitemporal_store_hash": store.artifact_hash,
            "projection_schema": "veriquant.dynamic-data-projection/v1",
        }),
        source_label=SOURCE_LABEL,
    )


def load_verified_bitemporal_store(
    root: Path,
    manifest: dict[str, Any],
) -> BitemporalStore:
    calendar_rows = _rows(root, "trade_cal")
    sessions = tuple(
        sorted({
            datetime.strptime(str(row["cal_date"]), "%Y%m%d").date()
            for row in calendar_rows
            if str(row.get("is_open")) in {"1", "1.0", "True", "true"}
        })
    )
    if stable_content_hash(calendar_rows) != manifest.get("calendar_hash"):
        raise ValueError("frozen calendar hash mismatch")
    store = BitemporalStore()
    statuses = manifest.get("api_statuses", {})
    for api_name in sorted(_FEATURE_APIS):
        if statuses.get(api_name) not in {"SUCCESS", "EMPTY"}:
            continue
        batch = normalize_rows(
            api_name,
            _rows(root, api_name),
            trading_sessions=sessions or None,
        )
        store.append(batch.records)
    if store.artifact_hash != manifest.get("normalized_store_hash"):
        raise ValueError("normalized bitemporal store hash mismatch")
    return store


def _rows(root: Path, api_name: str) -> tuple[dict[str, Any], ...]:
    path = root / "apis" / api_name / "partitions"
    if not path.exists():
        return ()
    return ContentAddressedRowStore(path).load_all()


def load_verified_freeze_manifest(root: str | Path) -> dict[str, Any]:
    path = Path(root).resolve() / "freeze_manifest.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    claimed = payload.pop("artifact_hash", None)
    if claimed != stable_content_hash(payload):
        raise ValueError("dynamic freeze manifest hash mismatch")
    payload["artifact_hash"] = claimed
    if payload.get("source_label") != SOURCE_LABEL:
        raise ValueError("dynamic freeze source is not approved")
    if payload.get("official_tushare") is not False:
        raise ValueError("dynamic freeze must not claim official Tushare")
    if payload.get("status") != "SUCCESS":
        raise ValueError("dynamic freeze is not complete")
    return payload


__all__ = [
    "enrich_frozen_validation_data",
    "load_verified_bitemporal_store",
    "load_verified_freeze_manifest",
]
