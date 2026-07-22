"""Atomic plan-scoped persistence for campaign Evidence datasets."""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any, Mapping

from ..bitemporal_features import EvidenceEligibility, NormalizedRecord, SourceKind
from ..data import canonical_content_hash
from ..dynamic_data_integration import (
    CampaignEvidenceDatasetBundle,
    CampaignEvidenceDatasetManifest,
    CampaignFrozenValidationData,
)
from .research_os_v2 import FrozenValidationDataRepository


_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class CampaignEvidenceDatasetRepository:
    """Persist one immutable bundle per Campaign plan id."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def save(self, plan_id: str, bundle: CampaignEvidenceDatasetBundle) -> str:
        plan_root = self._plan_root(plan_id)
        plan_root.mkdir(parents=True, exist_ok=True)
        FrozenValidationDataRepository(plan_root).save(bundle.frozen_data)
        records_payload = {
            "schema_version": "veriquant.projected-records/v1",
            "records": [_record_payload(item) for item in bundle.projected_records],
        }
        records_payload["artifact_hash"] = canonical_content_hash(records_payload)
        _atomic_json(plan_root / "projected_records.json", records_payload)
        manifest_payload = asdict(bundle.manifest)
        manifest_payload["bundle_hash"] = bundle.artifact_hash
        manifest_payload["artifact_hash"] = bundle.manifest.artifact_hash
        _atomic_json(plan_root / "evidence_dataset_manifest.json", manifest_payload)
        return bundle.artifact_hash

    def load(self, plan_id: str) -> CampaignEvidenceDatasetBundle | None:
        plan_root = self._plan_root(plan_id)
        manifest_path = plan_root / "evidence_dataset_manifest.json"
        if not manifest_path.exists():
            return None
        manifest_raw = _read_mapping(manifest_path)
        claimed_manifest_hash = str(manifest_raw.pop("artifact_hash", ""))
        claimed_bundle_hash = str(manifest_raw.pop("bundle_hash", ""))
        manifest = CampaignEvidenceDatasetManifest(
            schema_version=str(manifest_raw["schema_version"]),
            base_data_hash=str(manifest_raw["base_data_hash"]),
            dynamic_freeze_hash=str(manifest_raw["dynamic_freeze_hash"]),
            bitemporal_store_hash=str(manifest_raw["bitemporal_store_hash"]),
            projected_records_hash=str(manifest_raw["projected_records_hash"]),
            snapshot_hash=str(manifest_raw["snapshot_hash"]),
            labels_hash=str(manifest_raw["labels_hash"]),
            qualification_hash=str(manifest_raw["qualification_hash"]),
            source_label=str(manifest_raw["source_label"]),
            fields=tuple(manifest_raw["fields"]),
            added_fields=tuple(manifest_raw["added_fields"]),
            required_fields=tuple(manifest_raw["required_fields"]),
            optional_fields=tuple(manifest_raw["optional_fields"]),
            missing_required_fields=tuple(manifest_raw["missing_required_fields"]),
            row_count=int(manifest_raw["row_count"]),
            symbol_count=int(manifest_raw["symbol_count"]),
            session_count=int(manifest_raw["session_count"]),
            projected_record_count=int(manifest_raw["projected_record_count"]),
        )
        if manifest.artifact_hash != claimed_manifest_hash:
            raise ValueError("campaign Evidence dataset manifest hash mismatch")
        records_raw = _read_mapping(plan_root / "projected_records.json")
        claimed_records_hash = str(records_raw.pop("artifact_hash", ""))
        if canonical_content_hash(records_raw) != claimed_records_hash:
            raise ValueError("campaign projected-record file hash mismatch")
        records = tuple(_record_from_payload(item) for item in records_raw["records"])
        base_frozen = FrozenValidationDataRepository(plan_root).load(require_strict_provenance=True)
        if base_frozen is None:
            raise ValueError("campaign frozen validation data is missing")
        frozen = CampaignFrozenValidationData(
            snapshot=base_frozen.snapshot,
            labels=base_frozen.labels,
            qualification_hash=base_frozen.qualification_hash,
            source_label=base_frozen.source_label,
            row_provenance=base_frozen.row_provenance,
            bitemporal_records=records,
        )
        bundle = CampaignEvidenceDatasetBundle(
            frozen_data=frozen,
            projected_records=records,
            manifest=manifest,
        )
        if bundle.artifact_hash != claimed_bundle_hash:
            raise ValueError("campaign Evidence dataset bundle hash mismatch")
        return bundle

    def _plan_root(self, plan_id: str) -> Path:
        if not isinstance(plan_id, str) or not _SAFE_ID.fullmatch(plan_id):
            raise ValueError("invalid campaign plan id")
        target = (self.root / plan_id).resolve()
        if target == self.root or self.root not in target.parents:
            raise ValueError("campaign plan path escapes repository")
        return target


def _record_payload(item: NormalizedRecord) -> dict[str, Any]:
    return {
        "source": item.source.value,
        "entity_id": item.entity_id,
        "event_time": item.event_time.isoformat(),
        "published_at": item.published_at.isoformat(),
        "available_at": item.available_at.isoformat(),
        "decision_time": item.decision_time.isoformat() if item.decision_time else None,
        "revision_id": item.revision_id,
        "values": dict(item.values),
        "raw_hash": item.raw_hash,
        "eligibility": item.eligibility.value,
    }


def _record_from_payload(payload: Mapping[str, Any]) -> NormalizedRecord:
    decision = payload.get("decision_time")
    return NormalizedRecord(
        source=SourceKind(str(payload["source"])),
        entity_id=str(payload["entity_id"]),
        event_time=datetime.fromisoformat(str(payload["event_time"])),
        published_at=datetime.fromisoformat(str(payload["published_at"])),
        available_at=datetime.fromisoformat(str(payload["available_at"])),
        decision_time=datetime.fromisoformat(str(decision)) if decision else None,
        revision_id=str(payload["revision_id"]),
        values=dict(payload["values"]),
        raw_hash=str(payload["raw_hash"]),
        eligibility=EvidenceEligibility(str(payload["eligibility"])),
    )


def _read_mapping(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid campaign dataset artifact: {path.name}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"campaign dataset artifact is not an object: {path.name}")
    return value


def _atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", newline="\n", delete=False, dir=path.parent
    )
    temporary = Path(handle.name)
    try:
        json.dump(
            payload,
            handle,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
        handle.close()
        os.replace(temporary, path)
    finally:
        if not handle.closed:
            handle.close()
        if temporary.exists():
            temporary.unlink()


__all__ = ["CampaignEvidenceDatasetRepository"]
