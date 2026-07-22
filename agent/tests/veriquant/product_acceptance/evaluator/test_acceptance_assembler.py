from __future__ import annotations

import json
from pathlib import Path

from veriquant.product_acceptance.assembler import assemble_scenario_evidence
from veriquant.product_acceptance.models import (
    DataCapabilityStatus,
    EvidenceProvenance,
    ProductTerminal,
    SemanticCompileStatus,
)


H = lambda value: "sha256:" + value * 64


def _write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_assembler_preserves_unavailable_capability_and_unresolved_clarification(tmp_path: Path) -> None:
    plan, run = "campaign.x", "run-x"
    _write(tmp_path / "plans" / plan / "intent.json", {"clarification_questions": ["horizon?"], "resolution": "COMPLETE_DRAFT"})
    _write(tmp_path / "plans" / plan / "data_capability.json", {"status": "UNAVAILABLE"})
    _write(tmp_path / "runtime" / "research_runs" / run / "manifest.json", {"selected_group_ids": ["market_structure"], "trial_count": 1})
    _write(tmp_path / "products" / run / "result.json", {"committed_branch_hashes": [H("a")]})
    _write(tmp_path / "products" / run / "evidence.json", {"official_evidence_hash": H("b"), "terminals": [{"status": "OFFICIAL_VISIBLE_EVALUATED", "candidate_id": "c1"}]})
    _write(tmp_path / "reports" / run / "machine_report.json", {"outcome": "EVALUATED_RESEARCH_RESULT"})
    evidence = assemble_scenario_evidence(
        shard_root=tmp_path,
        summary_item={"scenario_id": "S01", "plan_id": plan, "run_id": run, "provenance": "APPROVED_PROXY_DATA_VENDOR", "manifest_hash": H("c"), "selection_hash": H("d"), "report_hash": H("e")},
        test_evidence_hashes=(H("f"),),
    )
    assert evidence.semantic_status is SemanticCompileStatus.NEEDS_CLARIFICATION
    assert evidence.data_status is DataCapabilityStatus.UNAVAILABLE
    assert evidence.provenance is EvidenceProvenance.REAL_FROZEN
    assert evidence.terminal is ProductTerminal.OFFICIAL_EVALUATED
    assert evidence.claims_official is True


def test_known_semantic_failure_overrides_runtime_resolution(tmp_path: Path) -> None:
    plan, run = "campaign.x", "run-x"
    _write(tmp_path / "plans" / plan / "intent.json", {"clarification_questions": [], "resolution": "COMPLETE_DRAFT"})
    _write(tmp_path / "plans" / plan / "data_capability.json", {"status": "PARTIAL"})
    _write(tmp_path / "runtime" / "research_runs" / run / "manifest.json", {"selected_group_ids": [], "trial_count": 0})
    _write(tmp_path / "products" / run / "result.json", {"committed_branch_hashes": []})
    _write(tmp_path / "reports" / run / "machine_report.json", {"outcome": "PARTIAL_RESEARCH_RESULT"})
    evidence = assemble_scenario_evidence(
        shard_root=tmp_path,
        summary_item={"scenario_id": "S22", "plan_id": plan, "run_id": run, "provenance": "APPROVED_PROXY_DATA_VENDOR", "manifest_hash": H("c"), "report_hash": H("e")},
        test_evidence_hashes=(H("f"),),
        semantic_failures=frozenset({"S22"}),
    )
    assert evidence.semantic_status is SemanticCompileStatus.FAILED
