"""Resumable top-level controller for Research Quality & Delivery Harness v1."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .acceptance import run_output_evaluator_acceptance
from .baseline import build_data_lineage_report, import_representative_baseline
from .qualification import build_qualification_plan
from .storage import atomic_json, atomic_jsonl, canonical_hash


class ResearchQualityDeliveryHarness:
    def __init__(self, *, workspace: str | Path, output_root: str | Path | None = None) -> None:
        self.workspace = Path(workspace).resolve()
        self.prior_root = self.workspace / ".autobuild" / "veriquant" / "research500_harness"
        self.root = Path(output_root).resolve() if output_root else self.workspace / ".autobuild" / "veriquant" / "research_quality_delivery_harness"

    def initialize(self) -> dict[str, Any]:
        self.root.mkdir(parents=True, exist_ok=True)
        baseline = import_representative_baseline(source_root=self.prior_root, output_root=self.root)
        lineage = build_data_lineage_report(
            frozen_data_path=self.prior_root / "product_data_registered_industry_v1" / "frozen_validation_data.json",
            preflight_path=self.prior_root / "representative_preflight.json",
            industry_receipt_path=self.prior_root / "product_data_registered_industry_v1" / "industry_panel_receipt.json",
            registered_receipt_path=self.prior_root / "product_data_registered_industry_v1" / "registered_baselines_receipt.json",
            output_root=self.root,
        )
        evaluators = run_output_evaluator_acceptance(output_root=self.root)
        plan = build_qualification_plan(
            catalog_path=self.workspace / ".autobuild" / "veriquant" / "factor_question_500" / "catalog.json",
            baseline_path=self.root / "baseline_import.json",
            output_root=self.root,
        )
        events = [
            _event("BASELINE_IMPORTED", baseline["artifact_hash"]),
            _event("DATA_LINEAGE_FROZEN", lineage["artifact_hash"]),
            _event("OUTPUT_EVALUATORS_ACCEPTED", evaluators["artifact_hash"]),
            _event("QUALIFICATION_PLAN_FROZEN", plan["artifact_hash"]),
        ]
        events_hash = atomic_jsonl(self.root / "events.jsonl", events)
        state = atomic_json(self.root / "state.json", {
            "schema_version": "alphaforge.research-quality-delivery-state/v1",
            "status": "IN_PROGRESS",
            "current_phase": "GROUP_QUALIFICATION",
            "recovery_cursor": "QB01/QD-001-market_structure",
            "artifact_hashes": {
                "baseline_import": baseline["artifact_hash"],
                "data_lineage_report": lineage["artifact_hash"],
                "output_evaluator_matrix": evaluators["artifact_hash"],
                "qualification_plan": plan["artifact_hash"],
                "events": events_hash,
            },
            "known_failures": [],
            "next_action": "RUN_QUALIFICATION_BATCH_QB01",
        })
        return state


def _event(event_type: str, artifact_hash: str) -> dict[str, Any]:
    value = {"event_type": event_type, "artifact_hash": artifact_hash}
    value["event_hash"] = canonical_hash(value)
    return value


__all__ = ["ResearchQualityDeliveryHarness"]
