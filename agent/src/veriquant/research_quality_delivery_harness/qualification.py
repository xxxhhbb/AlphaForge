"""Deterministic six-group qualification planning and progress contracts."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from ..research500_harness.catalog import load_catalog
from .contracts import CaseState
from .storage import atomic_json, canonical_hash, load_hashed


_TASKS = {
    "market_structure": (
        ("Q001", "NATIVE_OFFICIAL", "ScoreTable", "IMPORTED_REAL_RECEIPT"),
        ("Q106", "ROBUSTNESS_OR_SPECIAL", "RegressionTable", "IMPORTED_REAL_RECEIPT"),
        ("SAFE_MARKET_STRUCTURE", "FAIL_CLOSED", "ScoreTable", "NEGATIVE_FIXTURE"),
    ),
    "fundamental": (
        ("Q232", "NATIVE_OFFICIAL", "ScoreTable", "IMPORTED_REAL_RECEIPT"),
        ("Q316", "ROBUSTNESS_OR_SPECIAL", "RegressionTable", "IMPORTED_REAL_RECEIPT"),
        ("SAFE_FUNDAMENTAL", "FAIL_CLOSED", "ScoreTable", "NEGATIVE_FIXTURE"),
    ),
    "expectations_events": (
        ("Q321", "NATIVE_OFFICIAL", "EventTable", "NEW_REAL_RUN"),
        ("Q337", "ROBUSTNESS_OR_SPECIAL", "RegressionTable", "IMPORTED_REAL_RECEIPT"),
        ("SAFE_EXPECTATIONS_EVENTS", "FAIL_CLOSED", "EventTable", "NEGATIVE_FIXTURE"),
    ),
    "alternative_data": (
        ("Q401", "NATIVE_OFFICIAL", "ScoreTable", "NEW_REAL_RUN"),
        ("Q404", "ROBUSTNESS_OR_SPECIAL", "RegressionTable", "IMPORTED_REAL_RECEIPT"),
        ("SAFE_ALTERNATIVE_DATA", "FAIL_CLOSED", "DatasetAssessmentTable", "NEGATIVE_FIXTURE"),
    ),
    "macro_regime": (
        ("Q422", "NATIVE_OFFICIAL", "ScoreTable", "IMPORTED_REAL_RECEIPT"),
        ("Q443", "ROBUSTNESS_OR_SPECIAL", "RegimeComparisonTable", "NEW_REAL_RUN"),
        ("SAFE_MACRO_REGIME", "FAIL_CLOSED", "RegimeComparisonTable", "NEGATIVE_FIXTURE"),
    ),
    "meta_alpha": (
        ("Q485", "NATIVE_OFFICIAL", "ScoreTable", "IMPORTED_REAL_RECEIPT"),
        ("Q486", "ROBUSTNESS_OR_SPECIAL", "RegressionTable", "NEW_REAL_RUN"),
        ("SAFE_META_ALPHA", "FAIL_CLOSED", "DiagnosticTable", "NEGATIVE_FIXTURE"),
    ),
}


def build_qualification_plan(
    *, catalog_path: str | Path, baseline_path: str | Path, output_root: str | Path,
) -> dict[str, Any]:
    catalog = load_catalog(catalog_path)
    by_id = {item.question_id: item for item in catalog.questions}
    baseline = load_hashed(baseline_path)
    imported = {item["question_id"]: item for item in baseline["representatives"]}
    cases = []
    sequence = 0
    for group_id, tasks in _TASKS.items():
        for question_id, role, output_kind, source_mode in tasks:
            sequence += 1
            if question_id.startswith("SAFE_"):
                question = f"{group_id} 越权、缺字段或非法时钟输入必须被机械拒绝。"
                question_hash = canonical_hash({"question": question})
                required_data = []
                catalog_identity = None
            else:
                item = by_id[question_id]
                question = item.question
                question_hash = canonical_hash({"question": question})
                required_data = list(item.required_data)
                catalog_identity = canonical_hash(item.to_dict())
                if source_mode == "IMPORTED_REAL_RECEIPT" and question_id not in imported:
                    raise ValueError(f"imported qualification receipt is missing: {question_id}")
            trial_id = f"QD-{sequence:03d}-{group_id}"
            identity = {
                "trial_id": trial_id, "question_hash": question_hash,
                "group_id": group_id, "role": role, "output_kind": output_kind,
                "source_mode": source_mode,
            }
            cases.append({
                "case_id": trial_id,
                "trial_id": trial_id,
                "idempotency_key": canonical_hash(identity),
                "batch_id": f"QB{((sequence - 1) // 3) + 1:02d}",
                "question_id": question_id,
                "question": question,
                "question_hash": question_hash,
                "catalog_identity_hash": catalog_identity,
                "group_id": group_id,
                "qualification_role": role,
                "output_kind": output_kind,
                "source_mode": source_mode,
                "required_data": required_data,
                "imported_receipt": imported.get(question_id),
                "expected_safe_failure": _safe_failure(group_id) if role == "FAIL_CLOSED" else None,
            })
    plan = {
        "schema_version": "alphaforge.qualification-plan/v1",
        "artifact_type": "qualification-plan",
        "catalog_hash": catalog.catalog_hash,
        "baseline_import_hash": baseline["artifact_hash"],
        "case_count": len(cases),
        "group_count": len(_TASKS),
        "batch_size_max": 3,
        "cases": cases,
    }
    saved = atomic_json(Path(output_root) / "qualification_plan.json", plan)
    progress = {
        "schema_version": "alphaforge.qualification-progress/v1",
        "artifact_type": "qualification-progress",
        "plan_hash": saved["artifact_hash"],
        "status": "IN_PROGRESS",
        "current_batch": "QB01",
        "recovery_cursor": cases[0]["case_id"],
        "active_leases": [],
        "cases": [
            CaseState(
                case_id=item["case_id"], trial_id=item["trial_id"],
                idempotency_key=item["idempotency_key"], group_id=item["group_id"],
                role=item["qualification_role"], output_kind=item["output_kind"],
            ).to_dict()
            for item in cases
        ],
    }
    atomic_json(Path(output_root) / "qualification_progress.json", progress)
    atomic_json(Path(output_root) / "blocked_clusters.json", {
        "schema_version": "alphaforge.blocked-clusters/v1",
        "artifact_type": "blocked-clusters", "clusters": [],
        "paused_groups": [], "harness_paused": False,
    })
    return saved


def _safe_failure(group_id: str) -> str:
    return {
        "market_structure": "FIELD_NOT_IN_FROZEN_SCHEMA",
        "fundamental": "PIT_AVAILABILITY_VIOLATION",
        "expectations_events": "EVENT_CLOCK_VIOLATION",
        "alternative_data": "UNAUTHORIZED_PATH_OR_NETWORK",
        "macro_regime": "REGIME_SAMPLE_INSUFFICIENT",
        "meta_alpha": "SELF_APPROVAL_FORBIDDEN",
    }[group_id]


__all__ = ["build_qualification_plan"]
