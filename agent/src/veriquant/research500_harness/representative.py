"""Deterministic representative-suite harness for the 25x20 catalog.

The suite is deliberately honest: all 500 questions receive static coverage,
while exactly one question per family is selected for real end-to-end work.
The diagonal selection is a minimum-cardinality set cover because the family
constraint alone requires 25 cases and the first 20 cases cover T01..T20.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import tempfile
from typing import Any, Mapping

from ..research_toolbox.contracts import canonical_hash
from .catalog import CatalogQuestion, Research500Catalog, load_catalog


SCHEMA_VERSION = "alphaforge.research500-representative/v1"
REAL_RECEIPT_SCHEMA = "alphaforge.first-real-e2e-canary-receipt/v1"
_HASH_PREFIX = "sha256:"
_OUTPUT_KIND = {
    "CROSS_SECTIONAL_VALIDATION": "ScoreTable", "QUANTILE_MONOTONICITY": "ScoreTable",
    "NEUTRALIZED_VALIDATION": "ScoreTable", "RESIDUAL_INCREMENTAL": "RegressionTable",
    "NONLINEAR_DIAGNOSTIC": "RegressionTable", "REGISTERED_CORRELATION": "RegressionTable",
    "REGIME_CONDITIONING": "RegimeComparisonTable", "LIQUIDITY_CONDITIONING": "RegimeComparisonTable",
    "UNIVERSE_ROBUSTNESS": "RegimeComparisonTable", "SUBPERIOD_STABILITY": "RegimeComparisonTable",
    "PARAMETER_ROBUSTNESS": "RegimeComparisonTable", "DIAGNOSTIC_MONITORING": "DiagnosticTable",
    "ADVERSARIAL_FALSIFICATION": "ScoreTable", "CAPACITY_STRESS": "ScoreTable",
    "COST_STRESS": "ScoreTable", "OUTLIER_ROBUSTNESS": "ScoreTable",
    "PIT_DELAY_TEST": "ScoreTable", "TURNOVER_OPTIMIZATION": "ScoreTable",
}


def select_representatives(catalog: Research500Catalog) -> tuple[CatalogQuestion, ...]:
    """Select the provably minimal 25-case family suite with full dimension cover."""

    by_pair = {(item.family_id, item.test_id): item for item in catalog.questions}
    families = catalog.families
    dimensions = catalog.dimensions
    selected = tuple(
        by_pair[(family_id, dimensions[index % len(dimensions)])]
        for index, family_id in enumerate(families)
    )
    if len(selected) != 25 or {item.family_id for item in selected} != set(families):
        raise ValueError("representative suite must cover every family exactly once")
    if {item.test_id for item in selected} != set(dimensions):
        raise ValueError("representative suite must cover all research dimensions")
    return selected


class RepresentativeResearchHarness:
    """Create batches and accept only content-addressed real E2E receipts."""

    def initialize(self, *, catalog_path: str | Path, output_dir: str | Path) -> dict[str, Any]:
        catalog = load_catalog(catalog_path)
        selected = select_representatives(catalog)
        rows = []
        for index, item in enumerate(selected):
            rows.append(_plan_row(item, batch_id=f"B{index // 5 + 1:02d}"))
        plan = {
            "schema_version": SCHEMA_VERSION,
            "artifact_type": "representative-plan",
            "catalog_hash": catalog.catalog_hash,
            "selection_policy": "MINIMUM_25_FAMILY_DIAGONAL_FULL_20_DIMENSION_COVER",
            "static_question_count": 500,
            "real_representative_count": 25,
            "batch_count": 5,
            "batch_size": 5,
            "family_coverage": sorted({row["family_id"] for row in rows}),
            "dimension_coverage": sorted({row["test_id"] for row in rows}),
            "rows": rows,
        }
        plan["artifact_hash"] = canonical_hash(plan)
        root = Path(output_dir)
        _atomic_json(root / "representative_plan.json", plan)
        _atomic_json(root / "representative_progress.json", _progress(plan))
        return plan

    def replace_blocked_representative(
        self,
        *,
        catalog_path: str | Path,
        output_dir: str | Path,
        blocked_question_id: str,
        replacement_question_id: str,
        reason_code: str,
    ) -> dict[str, Any]:
        """Replace an exhausted representative without reducing suite coverage."""

        if not reason_code or any(character in reason_code for character in "\\/"):
            raise ValueError("replacement requires a typed non-path reason code")
        root = Path(output_dir)
        plan = json.loads((root / "representative_plan.json").read_text(encoding="utf-8"))
        catalog = load_catalog(catalog_path)
        current = [row for row in plan["rows"] if row["question_id"] == blocked_question_id]
        replacements = [item for item in catalog.questions if item.question_id == replacement_question_id]
        if len(current) != 1 or len(replacements) != 1:
            raise ValueError("replacement identity is not unique")
        old = current[0]
        replacement = replacements[0]
        if old["status"] != "BLOCKED" or int(old["attempt_count"]) < 3:
            raise ValueError("only a blocked representative with three attempts may be replaced")
        if replacement.family_id != old["family_id"]:
            raise ValueError("replacement must remain in the same family")
        if replacement.question_id in {row["question_id"] for row in plan["rows"]}:
            raise ValueError("replacement question is already selected")
        new_row = _plan_row(replacement, batch_id=old["batch_id"])
        index = plan["rows"].index(old)
        plan["rows"][index] = new_row
        if {row["family_id"] for row in plan["rows"]} != set(catalog.families):
            raise ValueError("replacement reduced family coverage")
        if {row["test_id"] for row in plan["rows"]} != set(catalog.dimensions):
            raise ValueError("replacement reduced dimension coverage")
        superseded = list(plan.get("superseded_rows") or ())
        superseded.append({
            "question_id": old["question_id"],
            "question_hash": old["question_hash"],
            "family_id": old["family_id"],
            "test_id": old["test_id"],
            "attempt_count": old["attempt_count"],
            "failure_code": old["failure_code"],
            "receipt_hash": old["receipt_hash"],
            "replacement_question_id": replacement.question_id,
            "reason_code": reason_code,
        })
        plan["superseded_rows"] = sorted(superseded, key=lambda item: item["question_id"])
        plan["selection_policy"] = "ADAPTIVE_MINIMUM_25_FAMILY_FULL_20_DIMENSION_COVER"
        plan["family_coverage"] = sorted({row["family_id"] for row in plan["rows"]})
        plan["dimension_coverage"] = sorted({row["test_id"] for row in plan["rows"]})
        plan.pop("artifact_hash", None)
        plan["artifact_hash"] = canonical_hash(plan)
        _atomic_json(root / "representative_plan.json", plan)
        _atomic_json(root / "representative_progress.json", _progress(plan))
        return plan

    def replace_preflight_blocked_representative(
        self,
        *,
        catalog_path: str | Path,
        output_dir: str | Path,
        blocked_question_id: str,
        replacement_question_id: str,
        preflight_path: str | Path,
    ) -> dict[str, Any]:
        """Replace a mechanically blocked case without wasting real provider trials.

        This path is intentionally narrower than ``replace_blocked_representative``:
        it accepts only a content-addressed preflight artifact, keeps the same
        family, and rejects any replacement that reduces the 25-family/20-
        dimension coverage.  A passed representative can never be replaced.
        """

        root = Path(output_dir)
        plan = json.loads((root / "representative_plan.json").read_text(encoding="utf-8"))
        preflight = json.loads(Path(preflight_path).read_text(encoding="utf-8"))
        claimed = preflight.pop("artifact_hash", None)
        if (
            preflight.get("schema_version") != "alphaforge.research500-representative-preflight/v1"
            or claimed != canonical_hash(preflight)
        ):
            raise ValueError("preflight replacement requires a valid content-addressed artifact")
        if preflight.get("plan_hash") != plan.get("artifact_hash"):
            raise ValueError("preflight replacement belongs to a different representative plan")
        blocked_rows = [row for row in preflight.get("rows", ()) if row.get("question_id") == blocked_question_id]
        if len(blocked_rows) != 1 or blocked_rows[0].get("status") != "BLOCKED" or not blocked_rows[0].get("failure_codes"):
            raise ValueError("preflight does not prove a typed blocker for the representative")

        catalog = load_catalog(catalog_path)
        current = [row for row in plan["rows"] if row["question_id"] == blocked_question_id]
        replacements = [item for item in catalog.questions if item.question_id == replacement_question_id]
        if len(current) != 1 or len(replacements) != 1:
            raise ValueError("replacement identity is not unique")
        old = current[0]
        replacement = replacements[0]
        if old["status"] == "PASSED":
            raise ValueError("a passed representative cannot be replaced")
        if replacement.family_id != old["family_id"]:
            raise ValueError("replacement must remain in the same family")
        if replacement.question_id in {row["question_id"] for row in plan["rows"]}:
            raise ValueError("replacement question is already selected")
        new_row = _plan_row(replacement, batch_id=old["batch_id"])
        index = plan["rows"].index(old)
        candidate_rows = list(plan["rows"])
        candidate_rows[index] = new_row
        if {row["family_id"] for row in candidate_rows} != set(catalog.families):
            raise ValueError("replacement reduced family coverage")
        if {row["test_id"] for row in candidate_rows} != set(catalog.dimensions):
            raise ValueError("replacement reduced dimension coverage")
        plan["rows"] = candidate_rows
        superseded = list(plan.get("superseded_rows") or ())
        superseded.append({
            "question_id": old["question_id"],
            "question_hash": old["question_hash"],
            "family_id": old["family_id"],
            "test_id": old["test_id"],
            "attempt_count": old["attempt_count"],
            "failure_code": "+".join(sorted(set(blocked_rows[0]["failure_codes"]))),
            "receipt_hash": old["receipt_hash"],
            "replacement_question_id": replacement.question_id,
            "reason_code": "CONTENT_ADDRESSED_PREFLIGHT_BLOCKER",
            "preflight_hash": claimed,
        })
        plan["superseded_rows"] = sorted(superseded, key=lambda item: item["question_id"])
        plan["selection_policy"] = "ADAPTIVE_MINIMUM_25_FAMILY_FULL_20_DIMENSION_COVER"
        plan["family_coverage"] = sorted({row["family_id"] for row in plan["rows"]})
        plan["dimension_coverage"] = sorted({row["test_id"] for row in plan["rows"]})
        plan.pop("artifact_hash", None)
        plan["artifact_hash"] = canonical_hash(plan)
        _atomic_json(root / "representative_plan.json", plan)
        _atomic_json(root / "representative_progress.json", _progress(plan))
        return plan

    def record_receipt(
        self,
        *,
        output_dir: str | Path,
        question_id: str,
        receipt_path: str | Path,
    ) -> dict[str, Any]:
        root = Path(output_dir)
        plan = json.loads((root / "representative_plan.json").read_text(encoding="utf-8"))
        receipt = json.loads(Path(receipt_path).read_text(encoding="utf-8"))
        _validate_real_receipt(receipt)
        matches = [row for row in plan["rows"] if row["question_id"] == question_id]
        if len(matches) != 1:
            raise ValueError("question is not a unique representative")
        row = matches[0]
        if receipt.get("question_hash") != row["question_hash"]:
            raise ValueError("representative receipt belongs to a different research question")
        if receipt.get("output_kind") != row.get("required_output_kind"):
            raise ValueError("representative receipt used the wrong standard output contract")
        expected_authority = "OFFICIAL_EVIDENCE" if row["required_output_kind"] == "ScoreTable" else "AUXILIARY_ONLY"
        if receipt.get("evidence_authority") != expected_authority:
            raise ValueError("representative receipt used the wrong Evidence authority class")
        if row["status"] == "PASSED":
            if row["receipt_hash"] != receipt["artifact_hash"]:
                raise ValueError("committed representative receipt cannot be replaced")
            return plan
        row.update(
            {
                "status": "PASSED",
                "attempt_count": int(row["attempt_count"]) + 1,
                "receipt_hash": receipt["artifact_hash"],
                "failure_code": None,
                "source_hash": receipt["source_hash"],
                "review_hash": receipt["review_hash"],
                "runner_receipt_hash": receipt["runner_receipt_hash"],
                "official_evidence_hash": receipt["official_evidence_hash"],
                "report_hash": receipt["report_hash"],
                "report_path": receipt["report_path"],
            }
        )
        row.pop("row_hash", None)
        row["row_hash"] = canonical_hash(row)
        plan.pop("artifact_hash", None)
        plan["artifact_hash"] = canonical_hash(plan)
        _atomic_json(root / "representative_plan.json", plan)
        _atomic_json(root / "representative_progress.json", _progress(plan))
        return plan

    def record_failure(
        self, *, output_dir: str | Path, question_id: str, receipt_path: str | Path,
        attempt_count: int, failure_code: str,
    ) -> dict[str, Any]:
        if not 1 <= attempt_count <= 3 or not failure_code:
            raise ValueError("failure records require 1..3 attempts and a typed code")
        root = Path(output_dir)
        plan = json.loads((root / "representative_plan.json").read_text(encoding="utf-8"))
        receipt = json.loads(Path(receipt_path).read_text(encoding="utf-8"))
        matches = [row for row in plan["rows"] if row["question_id"] == question_id]
        if len(matches) != 1:
            raise ValueError("question is not a unique representative")
        row = matches[0]
        if receipt.get("status") != "BLOCKED" or receipt.get("canary_passed") is not False:
            raise ValueError("failure receipt must be a blocked canary")
        if receipt.get("question_hash") != row["question_hash"] or receipt.get("output_kind") != row["required_output_kind"]:
            raise ValueError("failure receipt identity does not match the representative")
        row.update({
            "status": "BLOCKED", "attempt_count": attempt_count,
            "receipt_hash": receipt.get("artifact_hash"), "failure_code": failure_code,
        })
        row.pop("row_hash", None)
        row["row_hash"] = canonical_hash(row)
        plan.pop("artifact_hash", None)
        plan["artifact_hash"] = canonical_hash(plan)
        _atomic_json(root / "representative_plan.json", plan)
        _atomic_json(root / "representative_progress.json", _progress(plan))
        return plan


def _validate_real_receipt(receipt: Mapping[str, Any]) -> None:
    if receipt.get("schema_version") != REAL_RECEIPT_SCHEMA or receipt.get("status") != "PASSED":
        raise ValueError("representative receipt is not a passed real E2E canary")
    if receipt.get("canary_passed") is not True or receipt.get("failure_code") is not None:
        raise ValueError("representative receipt contains a failure")
    required_hashes = (
        "artifact_hash", "source_hash", "review_hash", "runner_receipt_hash",
        "evidence_result_hash", "report_hash",
    )
    if any(not isinstance(receipt.get(name), str) or not receipt[name].startswith(_HASH_PREFIX) for name in required_hashes):
        raise ValueError("representative receipt is missing a required content hash")
    authority = receipt.get("evidence_authority")
    official_hash = receipt.get("official_evidence_hash")
    if authority == "OFFICIAL_EVIDENCE":
        if not isinstance(official_hash, str) or not official_hash.startswith(_HASH_PREFIX):
            raise ValueError("official ScoreTable receipt is missing official Evidence")
    elif authority == "AUXILIARY_ONLY":
        if official_hash is not None:
            raise ValueError("auxiliary standard tables cannot claim official Evidence")
    else:
        raise ValueError("representative receipt has an unsupported Evidence authority")
    report_path = Path(str(receipt.get("report_path", ""))).resolve()
    if not report_path.is_file() or not report_path.read_text(encoding="utf-8").strip():
        raise ValueError("representative receipt has no customer report")


def _plan_row(item: CatalogQuestion, *, batch_id: str) -> dict[str, Any]:
    row = {
        "question_id": item.question_id,
        "question_hash": canonical_hash({"question": item.question}),
        "question": item.question,
        "family_id": item.family_id,
        "family_name": item.family_name,
        "test_id": item.test_id,
        "research_dimension": item.research_dimension,
        "required_data": list(item.required_data),
        "lead_specialist": item.lead_specialist,
        "experiment_type": item.experiment_type,
        "required_output_kind": _OUTPUT_KIND[item.experiment_type],
        "expected_result_kind": item.expected_result_kind,
        "batch_id": batch_id,
        "status": "NOT_RUN",
        "attempt_count": 0,
        "receipt_hash": None,
        "failure_code": None,
    }
    row["row_hash"] = canonical_hash(row)
    return row


def _progress(plan: Mapping[str, Any]) -> dict[str, Any]:
    rows = list(plan["rows"])
    passed = [row for row in rows if row["status"] == "PASSED"]
    payload = {
        "schema_version": SCHEMA_VERSION,
        "artifact_type": "representative-progress",
        "plan_hash": plan["artifact_hash"],
        "status": "PASSED" if len(passed) == 25 else "IN_PROGRESS",
        "passed_count": len(passed),
        "required_count": 25,
        "family_passed_count": len({row["family_id"] for row in passed}),
        "dimension_passed_count": len({row["test_id"] for row in passed}),
        "batch_status": {
            batch: {
                "passed": sum(row["status"] == "PASSED" for row in rows if row["batch_id"] == batch),
                "required": sum(row["batch_id"] == batch for row in rows),
            }
            for batch in sorted({row["batch_id"] for row in rows})
        },
    }
    payload["artifact_hash"] = canonical_hash(payload)
    return payload


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
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
    parser = argparse.ArgumentParser(description="Manage the representative Research500 harness")
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--record-question", required=True)
    parser.add_argument("--receipt", required=True)
    args = parser.parse_args(argv)
    plan = RepresentativeResearchHarness().record_receipt(
        output_dir=args.output_dir,
        question_id=args.record_question,
        receipt_path=args.receipt,
    )
    progress = json.loads(
        (Path(args.output_dir) / "representative_progress.json").read_text(encoding="utf-8")
    )
    print(json.dumps({
        "plan_hash": plan["artifact_hash"],
        "status": progress["status"],
        "passed_count": progress["passed_count"],
        "family_passed_count": progress["family_passed_count"],
        "dimension_passed_count": progress["dimension_passed_count"],
        "progress_hash": progress["artifact_hash"],
    }, ensure_ascii=False, sort_keys=True))
    return 0 if progress["status"] == "PASSED" else 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = ["RepresentativeResearchHarness", "select_representatives"]
