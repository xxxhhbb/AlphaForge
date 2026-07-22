"""Assemble product-acceptance evidence from immutable Research OS artifacts.

This module only reads content-addressed outputs.  It performs no research,
financial calculation, promotion, or mutation of the canonical run state.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable, Mapping

from ..llm_desks.safety import canonical_hash
from .models import (
    DataCapabilityStatus,
    EvidenceProvenance,
    ProductTerminal,
    ScenarioEvidence,
    SemanticCompileStatus,
)


_OUTCOME_TERMINALS = {
    "NEGATIVE_RESEARCH_RESULT": ProductTerminal.NEGATIVE_RESEARCH_RESULT,
    "DATA_OR_CAPABILITY_GAP_REPORT": ProductTerminal.DATA_GAP_REPORT,
    "PARTIAL_RESEARCH_RESULT": ProductTerminal.PARTIAL_RESEARCH_RESULT,
    "DIAGNOSTIC_REPORT": ProductTerminal.DIAGNOSTIC_REPORT,
    "SYSTEM_FAILURE_REPORT": ProductTerminal.SYSTEM_FAILED,
    "CANCELLED_RESEARCH_REPORT": ProductTerminal.CANCELLED,
}


def _read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def assemble_scenario_evidence(
    *,
    shard_root: str | Path,
    summary_item: Mapping[str, object],
    test_evidence_hashes: tuple[str, ...],
    semantic_failures: frozenset[str] = frozenset(),
) -> ScenarioEvidence:
    """Build one fail-closed evidence record from a completed shard item."""

    root = Path(shard_root)
    scenario_id = str(summary_item["scenario_id"])
    plan_id = str(summary_item["plan_id"])
    run_id = str(summary_item["run_id"])
    intent = _read(root / "plans" / plan_id / "intent.json")
    draft_path = root / "plans" / plan_id / "draft.json"
    draft = _read(draft_path) if draft_path.exists() else {}
    campaign_path = root / "plans" / plan_id / "campaign.json"
    campaign = _read(campaign_path) if campaign_path.exists() else None
    capability = _read(root / "plans" / plan_id / "data_capability.json")
    manifest = _read(root / "runtime" / "research_runs" / run_id / "manifest.json")
    result = _read(root / "products" / run_id / "result.json")
    report = _read(root / "reports" / run_id / "machine_report.json")
    evidence_path = root / "products" / run_id / "evidence.json"
    evidence = _read(evidence_path) if evidence_path.exists() else {}

    if scenario_id in semantic_failures:
        semantic_status = SemanticCompileStatus.FAILED
    elif intent.get("resolution") == "UNSUPPORTED":
        semantic_status = SemanticCompileStatus.UNSUPPORTED
    elif campaign is not None and not draft.get("unresolved_fields"):
        semantic_status = SemanticCompileStatus.COMPILED
    elif draft.get("unresolved_fields") or intent.get("clarification_questions"):
        semantic_status = SemanticCompileStatus.NEEDS_CLARIFICATION
    else:
        semantic_status = SemanticCompileStatus.COMPILED

    official_hash = evidence.get("official_evidence_hash")
    if official_hash:
        terminal = ProductTerminal.OFFICIAL_EVALUATED
    else:
        terminal = _OUTCOME_TERMINALS.get(
            str(report.get("outcome")), ProductTerminal.PARTIAL_RESEARCH_RESULT
        )

    terminals = tuple(evidence.get("terminals", ()))
    candidate_statuses = tuple(str(item.get("status", "UNKNOWN")) for item in terminals)
    candidate_hashes = tuple(canonical_hash(item) for item in terminals)
    source_value = summary_item.get("provenance", "")
    source = (
        str(source_value.get("source_label", ""))
        if isinstance(source_value, Mapping)
        else str(source_value)
    )
    provenance = (
        EvidenceProvenance.REAL_FROZEN
        if source == "APPROVED_PROXY_DATA_VENDOR"
        else EvidenceProvenance.NONE
    )
    manifest_hash = str(summary_item.get("manifest_hash") or canonical_hash(manifest))

    return ScenarioEvidence(
        scenario_id=scenario_id,
        semantic_status=semantic_status,
        semantic_compile_hash=canonical_hash({
            "intent": intent,
            "resolved_draft": draft,
            "confirmed_campaign": campaign,
        }),
        data_status=DataCapabilityStatus(str(capability["status"])),
        data_capability_hash=canonical_hash(capability),
        provenance=provenance,
        terminal=terminal,
        selected_specialists=tuple(str(value) for value in manifest.get("selected_group_ids", ())),
        specialist_result_hashes=tuple(str(value) for value in result.get("committed_branch_hashes", ())),
        candidate_terminal_statuses=candidate_statuses,
        candidate_terminal_hashes=candidate_hashes,
        official_evidence_hashes=(str(official_hash),) if official_hash else (),
        selection_hash=(str(summary_item["selection_hash"]) if summary_item.get("selection_hash") else None),
        report_hash=str(summary_item.get("report_hash") or canonical_hash(report)),
        canonical_manifest_hash=manifest_hash,
        api_manifest_hash=manifest_hash,
        workbench_manifest_hash=manifest_hash,
        report_manifest_hash=manifest_hash,
        test_evidence_hashes=test_evidence_hashes,
        trial_count=int(manifest.get("trial_count", 0)),
        claims_official=bool(official_hash),
    )


def assemble_matrix_evidence(
    *,
    shard_roots: Iterable[str | Path],
    test_evidence_hashes: tuple[str, ...],
    semantic_failures: frozenset[str] = frozenset(),
) -> tuple[ScenarioEvidence, ...]:
    assembled: list[ScenarioEvidence] = []
    for root_value in shard_roots:
        root = Path(root_value)
        summaries = sorted(root.glob("*summary.json"))
        if len(summaries) != 1:
            raise ValueError(f"expected one shard summary under {root}")
        summary = _read(summaries[0])
        records = summary.get("scenarios") or summary.get("records") or ()
        if isinstance(records, Mapping):
            raise ValueError(f"summary under {root} contains counts, not scenario records")
        for item in records:
            normalized = dict(item)
            normalized.setdefault(
                "provenance", summary.get("provenance") or summary.get("source_label") or summary.get("source")
            )
            assembled.append(
                assemble_scenario_evidence(
                    shard_root=root,
                    summary_item=normalized,
                    test_evidence_hashes=test_evidence_hashes,
                    semantic_failures=semantic_failures,
                )
            )
    return tuple(sorted(assembled, key=lambda item: item.scenario_id))


__all__ = ["assemble_matrix_evidence", "assemble_scenario_evidence"]
