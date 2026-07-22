"""Read-only import and data-lineage freeze for the completed representative Harness."""

from __future__ import annotations

import json
import mmap
from pathlib import Path
from typing import Any, Mapping

from .storage import atomic_json, canonical_hash, content_hash, load_hashed


BASELINE_SCHEMA = "alphaforge.research-quality-baseline/v1"


def import_representative_baseline(*, source_root: str | Path, output_root: str | Path) -> dict[str, Any]:
    source = Path(source_root).resolve()
    output = Path(output_root).resolve()
    plan = load_hashed(source / "representative_plan.json")
    progress = load_hashed(source / "representative_progress.json")
    preflight = load_hashed(source / "representative_preflight.json")
    final = load_hashed(source / "final_verdict.json")
    if progress.get("plan_hash") != plan.get("artifact_hash") or preflight.get("plan_hash") != plan.get("artifact_hash"):
        raise ValueError("representative plan/progress/preflight identity mismatch")
    if final.get("representative_real_execution_count") != 25 or progress.get("passed_count") != 25:
        raise ValueError("completed 25-case representative baseline is required")
    if final.get("static_question_count") != 500 or final.get("static_question_status") != "COVERED_NOT_RUN":
        raise ValueError("static 500 catalog identity is incomplete")
    receipt_index = _receipt_index(source / "representative_runs")
    imported = []
    for row in plan.get("rows", ()):
        receipt_hash = row.get("receipt_hash")
        located = receipt_index.get(receipt_hash)
        if located is None:
            raise ValueError(f"missing representative receipt: {row.get('question_id')}")
        receipt_path, receipt = located
        if receipt.get("question_hash") != row.get("question_hash"):
            raise ValueError("representative receipt question mismatch")
        imported.append({
            "question_id": row["question_id"],
            "question_hash": row["question_hash"],
            "family_id": row["family_id"],
            "lead_specialist": row["lead_specialist"],
            "output_kind": row["required_output_kind"],
            "source_hash": row["source_hash"],
            "review_hash": row["review_hash"],
            "runner_receipt_hash": row["runner_receipt_hash"],
            "legacy_evidence_authority": receipt.get("evidence_authority"),
            "legacy_evidence_hash": row.get("official_evidence_hash"),
            "report_hash": row["report_hash"],
            "acceptance_receipt_hash": receipt_hash,
            "acceptance_receipt_path": str(receipt_path),
        })
    result = {
        "schema_version": BASELINE_SCHEMA,
        "artifact_type": "baseline-import",
        "source_root": str(source),
        "import_mode": "READ_ONLY_CONTENT_ADDRESSED_REFERENCES",
        "static_catalog_count": 500,
        "representative_count": len(imported),
        "representative_family_count": len({item["family_id"] for item in imported}),
        "identity_hashes": {
            "representative_plan": plan["artifact_hash"],
            "representative_progress": progress["artifact_hash"],
            "representative_preflight": preflight["artifact_hash"],
            "representative_final_verdict": final["artifact_hash"],
        },
        "representatives": imported,
    }
    return atomic_json(output / "baseline_import.json", result)


def build_data_lineage_report(
    *, frozen_data_path: str | Path, preflight_path: str | Path,
    industry_receipt_path: str | Path, registered_receipt_path: str | Path,
    output_root: str | Path,
) -> dict[str, Any]:
    frozen_path = Path(frozen_data_path).resolve()
    preflight = load_hashed(preflight_path)
    industry = load_hashed(industry_receipt_path)
    registered = load_hashed(registered_receipt_path)
    metadata = _read_snapshot_metadata(frozen_path)
    coverage = metadata.get("coverage_by_field") or {}
    fields = tuple(metadata.get("fields") or preflight.get("frozen_panel_fields") or ())
    rows = []
    for field in sorted(fields):
        measured = coverage.get(field)
        if isinstance(measured, bool) or not isinstance(measured, (int, float)):
            measured = None
        rows.append({
            "canonical_field": field,
            "source_field": field,
            "mapping_version": "FROZEN_PANEL_SEMANTICS_V1",
            "derivation_rule": _derivation(field),
            "nonempty_coverage": measured,
            "entity_coverage": measured,
            "time_coverage": measured,
            "available_at_semantics": "source_available_at <= decision_time",
            "decision_time_semantics": "frozen SSE/SZSE trading-session decision clock",
            "status": "USABLE" if measured is not None and measured > 0 else "BLOCKED_ZERO_OR_UNKNOWN_COVERAGE",
        })
    zero = [item["canonical_field"] for item in rows if item["status"] != "USABLE"]
    result = {
        "schema_version": "alphaforge.data-lineage-report/v1",
        "artifact_type": "data-lineage-report",
        "dataset_root": str(frozen_path.parent),
        "dataset_file": str(frozen_path),
        "dataset_file_hash": content_hash(frozen_path),
        "snapshot_hash": metadata.get("artifact_hash") or industry.get("snapshot_hash"),
        "data_hash": industry.get("data_hash"),
        "data_source": industry.get("source"),
        "official_tushare": False,
        "row_count": industry.get("row_count"),
        "entity_count": industry.get("panel_symbol_count"),
        "industry_coverage": industry.get("industry_coverage"),
        "registered_factor_receipt_hash": registered.get("artifact_hash"),
        "availability_policy": industry.get("availability_policy"),
        "field_count": len(rows),
        "usable_field_count": len(rows) - len(zero),
        "blocked_zero_or_unknown_fields": zero,
        "nan_inf_policy": "NORMALIZE_TO_NULL_BEFORE_CANONICAL_HASH_NO_IMPUTATION",
        "field_lineage": rows,
    }
    return atomic_json(Path(output_root) / "data_lineage_report.json", result)


def _receipt_index(root: Path) -> dict[str, tuple[Path, Mapping[str, Any]]]:
    result = {}
    for path in root.rglob("acceptance_receipt.json"):
        receipt = load_hashed(path)
        key = receipt["artifact_hash"]
        if key in result:
            # Failed attempts may legitimately share neither hash nor path; a
            # duplicate content hash at two paths is ambiguous and fails closed.
            raise ValueError("duplicate acceptance receipt identity")
        result[key] = (path, receipt)
    return result


def _read_snapshot_metadata(path: Path) -> dict[str, Any]:
    """Extract the snapshot object with mmap without loading labels/rows twice."""

    with path.open("rb") as stream, mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_READ) as mapped:
        marker = b'"snapshot"'
        position = mapped.find(marker)
        if position < 0:
            raise ValueError("frozen validation data has no snapshot object")
        colon = mapped.find(b":", position + len(marker))
        start = mapped.find(b"{", colon)
        if start < 0:
            raise ValueError("snapshot object is malformed")
        # Snapshot rows are large.  Extract only scalar/list/dict metadata that
        # precedes the rows array.  The production serializer sorts keys, so
        # coverage_by_field and fields occur before historical_universe/rows.
        rows_marker = mapped.find(b'"rows"', start)
        if rows_marker < 0:
            raise ValueError("snapshot rows marker is absent")
        prefix = mapped[start:rows_marker].decode("utf-8")
        # Close the truncated object after removing the trailing comma.
        candidate = prefix.rstrip().rstrip(",") + "}"
        value = json.loads(candidate)
        if not isinstance(value, dict):
            raise ValueError("snapshot metadata must be an object")
        return value


def _derivation(field: str) -> str:
    if field.startswith("registered_"):
        return "CONTENT_ADDRESSED_REGISTERED_FACTOR_DERIVATION_V1"
    if field in {"industry"}:
        return "DATED_MEMBERSHIP_ASOF_NEXT_TRADING_SESSION_V1"
    if field.startswith("shibor_") or field.startswith("index_"):
        return "MARKET_WIDE_ASOF_BROADCAST_V1"
    return "SOURCE_VALUE_PRESERVED_NO_IMPUTATION_V1"


__all__ = ["build_data_lineage_report", "import_representative_baseline"]
