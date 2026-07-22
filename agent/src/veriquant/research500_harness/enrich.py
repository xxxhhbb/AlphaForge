"""Deterministic enrichment of one frozen product panel with PIT source vintages."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import tempfile
from typing import Any, Iterable, Mapping

from ..dynamic_data_integration import CampaignEvidenceDatasetBuilder
from ..integration.campaign_evidence_dataset import CampaignEvidenceDatasetRepository
from ..integration.research_os_v2 import FrozenValidationDataRepository
from ..research_toolbox.contracts import canonical_hash


SCHEMA_VERSION = "alphaforge.research500-enrichment/v1"


def enrich_frozen_panel(
    *,
    base_root: str | Path,
    freeze_root: str | Path,
    repository_root: str | Path,
    plan_id: str,
    required_fields: Iterable[str],
    optional_fields: Iterable[str] = (),
) -> Mapping[str, Any]:
    """Build, persist and replay-verify a content-addressed enriched panel."""

    base = FrozenValidationDataRepository(Path(base_root).resolve()).load(
        require_strict_provenance=True
    )
    if base is None:
        raise ValueError("base frozen panel is unavailable")
    required = tuple(sorted(set(required_fields)))
    optional = tuple(sorted(set(optional_fields) - set(required)))
    if not required:
        raise ValueError("at least one required enrichment field is required")
    builder = CampaignEvidenceDatasetBuilder()
    bundle = builder.build(
        base=base,
        freeze_root=Path(freeze_root).resolve(),
        required_fields=required,
        optional_fields=optional,
    )
    if bundle.manifest.missing_required_fields:
        raise ValueError("enriched panel is missing required fields")
    zero_coverage = tuple(
        field
        for field in required
        if float(bundle.frozen_data.snapshot.coverage_by_field.get(field, 0.0)) <= 0.0
    )
    if zero_coverage:
        raise ValueError("enriched panel has zero coverage for required fields")

    repository = CampaignEvidenceDatasetRepository(Path(repository_root).resolve())
    bundle_hash = repository.save(plan_id, bundle)
    replay = repository.load(plan_id)
    if replay is None or replay.artifact_hash != bundle_hash:
        raise ValueError("enriched panel replay identity mismatch")
    plan_root = repository._plan_root(plan_id)  # noqa: SLF001 - same persistence unit
    value = {
        "schema_version": SCHEMA_VERSION,
        "status": "PASSED",
        "plan_id": plan_id,
        "base_data_hash": bundle.manifest.base_data_hash,
        "dynamic_freeze_hash": bundle.manifest.dynamic_freeze_hash,
        "bundle_hash": bundle_hash,
        "snapshot_hash": bundle.manifest.snapshot_hash,
        "required_fields": list(required),
        "optional_fields": list(optional),
        "coverage_by_required_field": {
            field: float(bundle.frozen_data.snapshot.coverage_by_field[field])
            for field in required
        },
        "row_count": bundle.manifest.row_count,
        "symbol_count": bundle.manifest.symbol_count,
        "session_count": bundle.manifest.session_count,
        "projected_record_count": bundle.manifest.projected_record_count,
        "replay_verified": True,
    }
    value["artifact_hash"] = canonical_hash(value)
    _atomic_json(plan_root / "enrichment_receipt.json", value)
    return value


def _fields(value: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in value.split(",") if item.strip())


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Enrich one frozen AlphaForge panel")
    parser.add_argument("--base-root", required=True)
    parser.add_argument("--freeze-root", required=True)
    parser.add_argument("--repository-root", required=True)
    parser.add_argument("--plan-id", required=True)
    parser.add_argument("--required-fields", required=True)
    parser.add_argument("--optional-fields", default="")
    args = parser.parse_args(argv)
    try:
        value = enrich_frozen_panel(
            base_root=args.base_root,
            freeze_root=args.freeze_root,
            repository_root=args.repository_root,
            plan_id=args.plan_id,
            required_fields=_fields(args.required_fields),
            optional_fields=_fields(args.optional_fields),
        )
        print(json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True))
        return 0
    except Exception:
        print(json.dumps({"schema_version": SCHEMA_VERSION, "status": "BLOCKED", "failure_code": "ENRICHMENT_FAILED"}))
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = ["SCHEMA_VERSION", "enrich_frozen_panel"]
