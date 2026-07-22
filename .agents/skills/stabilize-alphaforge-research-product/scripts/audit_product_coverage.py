#!/usr/bin/env python3
"""Deterministically audit AlphaForge catalog-to-product coverage.

This script is intentionally read-only except for an optional atomic JSON receipt.
It distinguishes static reachability from real execution and official authority.
"""

from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
from typing import Any, Mapping


SCHEMA_VERSION = "alphaforge.product-coverage-audit/v1"
STANDARD_OUTPUT_KINDS = (
    "ScoreTable",
    "RegressionTable",
    "RegimeComparisonTable",
    "EventTable",
    "DiagnosticTable",
    "DatasetAssessmentTable",
)


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def _hash(value: object) -> str:
    return "sha256:" + hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _read_json(path: Path, *, required: bool = True) -> Mapping[str, Any]:
    if not path.is_file():
        if required:
            raise FileNotFoundError(path)
        return {}
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, Mapping):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    handle, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _select_coverage_matrix(
    workspace: Path, *, catalog_hash: str, question_count: int
) -> tuple[Path, Mapping[str, Any]]:
    candidates = (
        workspace / ".autobuild" / "veriquant" / "product_stabilization_harness"
        / "coverage_proxy_ready" / "data_coverage_matrix.json",
        workspace / ".autobuild" / "veriquant" / "product_stabilization_harness"
        / "coverage_verified" / "data_coverage_matrix.json",
        workspace / ".autobuild" / "veriquant" / "product_stabilization_harness"
        / "coverage_recomputed" / "data_coverage_matrix.json",
        workspace / ".autobuild" / "veriquant" / "research500_harness"
        / "data_coverage_matrix.json",
    )
    for path in candidates:
        value = _read_json(path, required=False)
        rows = value.get("rows", ())
        if (
            value.get("catalog_hash") == catalog_hash
            and value.get("row_count") == question_count
            and isinstance(rows, list)
            and len(rows) == question_count
        ):
            return path, value
    raise ValueError("no identity-matching data coverage matrix is available")


def _planner() -> tuple[str, Any]:
    try:
        from veriquant.agent_research_product.planner import ResearchTaskPlanner

        planner = ResearchTaskPlanner()

        def compile_question(item: Any) -> tuple[tuple[str, ...], str]:
            plan = planner.compile_catalog_question(item)
            return tuple(plan.selected_specialists), str(plan.output_kind)

        return "STRUCTURED_RESEARCH_TASK_PLANNER", compile_question
    except ImportError:
        from veriquant.agent_research_product.terminal import _output_kind, _select_specialists

        def compile_question(item: Any) -> tuple[tuple[str, ...], str]:
            return tuple(_select_specialists(item.question)), str(_output_kind(item.question))

        return "LEGACY_TERMINAL_KEYWORD_ROUTER", compile_question


def build_audit(workspace: Path) -> dict[str, Any]:
    agent_root = workspace / "agent"
    source_root = workspace / "agent" / "src"
    for import_root in (source_root, agent_root):
        if str(import_root) not in sys.path:
            sys.path.insert(0, str(import_root))

    from veriquant.research500_harness import load_catalog
    from veriquant.research500_harness.representative import _OUTPUT_KIND

    catalog_path = workspace / ".autobuild" / "veriquant" / "factor_question_500" / "catalog.json"
    research500 = workspace / ".autobuild" / "veriquant" / "research500_harness"
    quality = workspace / ".autobuild" / "veriquant" / "research_quality_delivery_harness"
    catalog = load_catalog(catalog_path)
    planner_name, compile_question = _planner()

    route_misses: list[dict[str, str]] = []
    output_misses: list[dict[str, str]] = []
    planned_output_counts: Counter[str] = Counter()
    expected_output_counts: Counter[str] = Counter()
    for item in catalog.questions:
        specialists, output_kind = compile_question(item)
        expected_kind = _OUTPUT_KIND.get(item.experiment_type)
        if expected_kind is None:
            output_misses.append({
                "question_id": item.question_id,
                "expected": "UNMAPPED_EXPERIMENT_TYPE",
                "actual": output_kind,
            })
        elif output_kind != expected_kind:
            output_misses.append({
                "question_id": item.question_id,
                "expected": expected_kind,
                "actual": output_kind,
            })
        if item.lead_specialist not in specialists:
            route_misses.append({
                "question_id": item.question_id,
                "expected_lead": item.lead_specialist,
                "actual_specialists": ",".join(specialists),
            })
        planned_output_counts[output_kind] += 1
        expected_output_counts[str(expected_kind or "UNMAPPED")] += 1

    coverage_path, coverage = _select_coverage_matrix(
        workspace,
        catalog_hash=catalog.catalog_hash,
        question_count=len(catalog.questions),
    )
    coverage_rows = coverage.get("rows", ())
    coverage_counts = Counter(
        str(row.get("status", "UNKNOWN"))
        for row in coverage_rows
        if isinstance(row, Mapping)
    )
    unresolved_sources = Counter()
    for row in coverage_rows:
        if isinstance(row, Mapping):
            for source in row.get("unresolved_data", ()):
                unresolved_sources[str(source)] += 1

    evaluator_matrix = _read_json(quality / "output_evaluator_matrix.json")
    evaluator_rows = evaluator_matrix.get("rows", ())
    implemented_evaluators = sorted(
        str(row.get("output_kind"))
        for row in evaluator_rows
        if isinstance(row, Mapping)
        and row.get("implementation_status") == "IMPLEMENTED_AND_TESTED"
        and isinstance(row.get("positive_canary"), Mapping)
        and row["positive_canary"].get("authority") == "OFFICIAL_EVIDENCE"
    )

    try:
        from veriquant.agent_research_product.evidence_gateway import (
            supported_evaluated_output_kinds,
            supported_official_output_kinds,
        )

        product_official_kinds = sorted(supported_official_output_kinds())
        product_evaluated_kinds = sorted(supported_evaluated_output_kinds())
    except (ImportError, AttributeError):
        product_official_kinds = ["ScoreTable"]
        product_evaluated_kinds = ["ScoreTable"]

    qualification = _read_json(quality / "group_qualification_matrix.json")
    group_rows = qualification.get("rows", ())
    group_counts = Counter(
        str(row.get("status", "UNKNOWN"))
        for row in group_rows
        if isinstance(row, Mapping)
    )
    progress = _read_json(research500 / "representative_progress.json", required=False)
    representative_passed = int(progress.get("passed_count", 0) or 0)
    representative_required = int(progress.get("required_count", 0) or 0)

    gates = {
        "catalog_500_parseable": len(catalog.questions) == 500,
        "lead_specialist_routing_500": not route_misses,
        "output_contract_selection_500": not output_misses,
        "six_evaluators_implemented": set(implemented_evaluators) == set(STANDARD_OUTPUT_KINDS),
        "six_evaluators_connected_to_product": set(STANDARD_OUTPUT_KINDS).issubset(product_evaluated_kinds),
        "official_authority_boundary_safe": set(product_official_kinds) == {
            "ScoreTable", "EventTable", "DatasetAssessmentTable"
        },
        "six_specialists_qualified": group_counts.get("QUALIFIED", 0) == 6,
        "no_unresolved_catalog_data": coverage_counts.get("UNRESOLVED", 0) == 0,
        "representative_suite_passed": (
            representative_required > 0 and representative_passed == representative_required
        ),
    }
    blockers: list[str] = []
    if route_misses:
        blockers.append("TASK_PLANNER_LEAD_SPECIALIST_MISMATCH")
    if output_misses:
        blockers.append("TASK_PLANNER_OUTPUT_CONTRACT_MISMATCH")
    if not gates["six_evaluators_connected_to_product"]:
        blockers.append("PRODUCT_EVIDENCE_GATEWAY_INCOMPLETE")
    if not gates["official_authority_boundary_safe"]:
        blockers.append("PRODUCT_EVIDENCE_AUTHORITY_UNSAFE")
    if coverage_counts.get("UNRESOLVED", 0):
        blockers.append("OFFICIAL_DATA_COVERAGE_INCOMPLETE")
    if group_counts.get("QUALIFIED", 0) != 6:
        blockers.append("SPECIALIST_QUALIFICATION_INCOMPLETE")

    hard_gate_names = (
        "catalog_500_parseable",
        "lead_specialist_routing_500",
        "output_contract_selection_500",
        "six_evaluators_implemented",
        "six_evaluators_connected_to_product",
        "official_authority_boundary_safe",
        "six_specialists_qualified",
        "representative_suite_passed",
    )
    status = "QUALIFIED" if all(gates[name] for name in hard_gate_names) and not blockers else "PARTIAL"
    result: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "artifact_type": "product-coverage-audit",
        "status": status,
        "planner": planner_name,
        "catalog": {
            "count": len(catalog.questions),
            "hash": catalog.catalog_hash,
            "family_count": len(catalog.families),
            "dimension_count": len(catalog.dimensions),
        },
        "routing": {
            "passed_count": len(catalog.questions) - len(route_misses),
            "failed_count": len(route_misses),
            "failures": route_misses,
        },
        "output_contracts": {
            "passed_count": len(catalog.questions) - len(output_misses),
            "failed_count": len(output_misses),
            "expected_counts": dict(sorted(expected_output_counts.items())),
            "planned_counts": dict(sorted(planned_output_counts.items())),
            "failures": output_misses,
        },
        "data_readiness": {
            "coverage_matrix_path": str(coverage_path.relative_to(workspace)).replace("\\", "/"),
            "coverage_matrix_hash": coverage.get("artifact_hash"),
            "counts": dict(sorted(coverage_counts.items())),
            "unresolved_sources": dict(sorted(unresolved_sources.items())),
        },
        "evaluators": {
            "implemented_and_tested": implemented_evaluators,
            "connected_to_product_official_gateway": product_official_kinds,
            "connected_to_product_evaluation_gateway": product_evaluated_kinds,
        },
        "specialist_qualification": {
            "counts": dict(sorted(group_counts.items())),
            "rows": list(group_rows),
        },
        "representative_execution": {
            "passed_count": representative_passed,
            "required_count": representative_required,
            "status": progress.get("status", "NOT_AVAILABLE"),
        },
        "gates": gates,
        "blockers": blockers,
        "next_recovery_point": blockers[0] if blockers else "NONE",
    }
    result["artifact_hash"] = _hash(result)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    result = build_audit(args.workspace.resolve())
    if args.output:
        _atomic_json(args.output.resolve(), result)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    return 0 if result["status"] == "QUALIFIED" else 2


if __name__ == "__main__":
    raise SystemExit(main())
