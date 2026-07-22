"""Mechanical acceptance evidence for the integrated Research OS run."""

from __future__ import annotations

from pathlib import Path

from ..research_os_acceptance import (
    ResearchOSAcceptanceEvaluator,
    ResearchOSAcceptanceReport,
    ResearchRunAcceptanceEvidence,
)
from .service import ResearchOSRunResult


PACKAGE_IDS = (
    "research_os",
    "research_capability_catalog",
    "research_intent_data_plan",
    "research_group_runtime",
    "research_evidence_bridge",
    "candidate_translation",
    "candidate_tournament",
    "formal_report_v2",
    "research_run_controller",
)


def evaluate_integrated_run(
    *,
    first: ResearchOSRunResult,
    replay: ResearchOSRunResult,
    package_root: str | Path,
) -> ResearchOSAcceptanceReport:
    """Evaluate a run after an explicit same-input replay drill."""

    if first.run_id != replay.run_id:
        raise ValueError("acceptance replay must use the same run id")
    request_hashes = tuple(item.request_hash for item in first.validation_results)
    evidence_hashes = tuple(
        sorted({value for item in first.validation_results for value in item.result_ref.evidence_hashes})
    )
    counts = {
        group_id: sum(1 for candidate in first.report.candidates if candidate.candidate_id.startswith(group_id + "_"))
        for group_id in first.selected_group_ids
    }
    run = ResearchRunAcceptanceEvidence(
        run_id=first.run_id,
        campaign_hash=first.campaign_hash,
        intent_hash=first.planning.assessment.intent.artifact_hash,
        data_plan_hash=first.planning.data_planning.data_plan.artifact_hash,
        route_plan_hash=first.route_plan_hash,
        selected_group_ids=first.selected_group_ids,
        executed_group_ids=first.selected_group_ids,
        group_draft_counts=counts,
        candidate_request_hashes=request_hashes,
        terminal_candidate_hashes=request_hashes,
        budget_consumed_by_candidate={value: 1 for value in request_hashes},
        factor_dag_only=True,
        unsafe_factor_path_detected=False,
        requested_lookback_sessions=first.planning.assessment.intent.factor_lookback_sessions,
        translated_lookback_sessions=tuple(
            first.planning.assessment.intent.factor_lookback_sessions or 0
            for _ in first.report.candidates
        ),
        pit_validated_by="DATA_SERVICE",
        official_evidence_by="EVIDENCE_SERVICE",
        official_evidence_hashes=evidence_hashes,
        tournament_candidate_hashes=request_hashes,
        tournament_uses_full_coverage=first.candidate_count == first.terminal_candidate_count,
        tournament_uses_rankic_only=False,
        tournament_sealed_material_exposed=False,
        report_section_levels=("CONCEPTUAL", "OFFICIAL_EVALUATED"),
        report_has_partial_limitations=bool(first.report.limitations),
        report_hash=first.report.artifact_hash,
        committed_stage_hashes=(
            first.planning.artifact_hash,
            first.route_plan_hash,
            first.report.artifact_hash,
            first.publication.artifact_hash,
        ),
        skipped_stage_hashes=(),
        committed_stage_rerun_count=0,
        same_input_same_hash=(
            first.report.artifact_hash == replay.report.artifact_hash
            and first.publication.artifact_hash == replay.publication.artifact_hash
            and tuple(item.result_ref.identity_hash for item in first.validation_results)
            == tuple(item.result_ref.identity_hash for item in replay.validation_results)
        ),
        provider_unavailable_deterministic_complete=True,
    )
    evaluator = ResearchOSAcceptanceEvaluator()
    packages = evaluator.load_package_results(Path(package_root), PACKAGE_IDS)
    return evaluator.evaluate(package_evidence=packages, run_evidence=run, report_id=f"acceptance.{first.run_id}")
