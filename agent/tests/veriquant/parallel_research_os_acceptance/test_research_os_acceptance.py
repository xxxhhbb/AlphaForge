from __future__ import annotations

from dataclasses import replace

import pytest

from veriquant.contracts import ContractValidationError
from veriquant.research_os_acceptance import (
    AcceptanceFailureCode,
    AcceptanceGate,
    AcceptanceGateResult,
    AcceptanceStatus,
    PackageEvidence,
    ResearchOSAcceptanceEvaluator,
    ResearchRunAcceptanceEvidence,
)


H0 = "sha256:" + "0" * 64
H1 = "sha256:" + "1" * 64
H2 = "sha256:" + "2" * 64
H3 = "sha256:" + "3" * 64
H4 = "sha256:" + "4" * 64
H5 = "sha256:" + "5" * 64
H6 = "sha256:" + "6" * 64


def pkg(package_id: str, symbols: tuple[str, ...]) -> PackageEvidence:
    return PackageEvidence(
        package_id=package_id,
        raw_status="READY_FOR_INTEGRATION",
        tests_run=(f"python -m pytest agent/tests/veriquant/parallel_{package_id}",),
        test_result="7 passed in 0.10s",
        exported_symbols=symbols,
        operational_smoke_status="IMPLEMENTED_AND_TESTED",
        artifact_hashes=(H6,),
    )


def packages() -> dict[str, PackageEvidence]:
    return {
        "research_os": pkg(
            "research_os",
            (
                "ResearchIntent",
                "DataRequirementPlan",
                "ResearchRoutePlan",
                "ResearchGroupSubmission",
                "CandidateValidationRequest",
                "AnswerSection",
            ),
        ),
        "research_intent_data_plan": pkg(
            "research_intent_data_plan",
            ("DeterministicResearchQuestionPlanner", "DeterministicDataPlanner"),
        ),
        "research_capability_catalog": pkg(
            "research_capability_catalog",
            ("ResearchCapabilityCatalog", "NormalizedVibeDataBridge"),
        ),
        "research_group_runtime": pkg(
            "research_group_runtime",
            ("DeterministicResearchGroupSelector", "ResearchGroupProposalRuntime"),
        ),
        "candidate_translation": pkg("candidate_translation", ("SafeCandidateTranslator",)),
        "candidate_tournament": pkg("candidate_tournament", ("CandidateTournament",)),
        "research_evidence_bridge": pkg("research_evidence_bridge", ("ResearchEvidenceBridge",)),
        "formal_report_v2": pkg("formal_report_v2", ("FormalReportV2Composer", "render_markdown_v2")),
        "research_run_controller": pkg("research_run_controller", ("ResearchRunController", "build_research_run_plan")),
    }


def run() -> ResearchRunAcceptanceEvidence:
    candidates = (H4, H5)
    return ResearchRunAcceptanceEvidence(
        run_id="research_run_1",
        campaign_hash=H1,
        intent_hash=H2,
        data_plan_hash=H3,
        route_plan_hash=H4,
        selected_group_ids=("market_structure", "meta_alpha"),
        executed_group_ids=("market_structure", "meta_alpha"),
        group_draft_counts={"market_structure": 1, "meta_alpha": 1},
        candidate_request_hashes=candidates,
        terminal_candidate_hashes=candidates,
        budget_consumed_by_candidate={H4: 1, H5: 1},
        factor_dag_only=True,
        unsafe_factor_path_detected=False,
        requested_lookback_sessions=50,
        translated_lookback_sessions=(50, 50),
        pit_validated_by="DATA_SERVICE",
        official_evidence_by="EVIDENCE_SERVICE",
        official_evidence_hashes=(H6,),
        tournament_candidate_hashes=candidates,
        tournament_uses_full_coverage=True,
        tournament_uses_rankic_only=False,
        tournament_sealed_material_exposed=False,
        report_section_levels=("CONCEPTUAL", "AUXILIARY_RESEARCH", "OFFICIAL_EVALUATED"),
        report_has_partial_limitations=True,
        report_hash=H0,
        committed_stage_hashes=(H1, H2, H3),
        skipped_stage_hashes=(H1,),
        committed_stage_rerun_count=0,
        same_input_same_hash=True,
        provider_unavailable_deterministic_complete=True,
    )


def gate(report, gate: AcceptanceGate):
    return next(item for item in report.gates if item.gate is gate)


def test_all_gates_pass_and_report_hash_is_deterministic() -> None:
    evaluator = ResearchOSAcceptanceEvaluator()
    first = evaluator.evaluate(package_evidence=packages(), run_evidence=run())
    second = evaluator.evaluate(package_evidence=packages(), run_evidence=run())
    assert first.status is AcceptanceStatus.IMPLEMENTED_AND_TESTED
    assert first.artifact_hash == second.artifact_hash
    assert all(item.status is AcceptanceStatus.IMPLEMENTED_AND_TESTED for item in first.gates)


def test_missing_package_and_claimed_tests_fail_closed() -> None:
    evidence = packages()
    evidence.pop("candidate_translation")
    evidence["research_os"] = replace(evidence["research_os"], tests_run=(), test_result="claimed green")
    report = ResearchOSAcceptanceEvaluator().evaluate(package_evidence=evidence, run_evidence=run())
    assert report.status is AcceptanceStatus.FAILED
    assert AcceptanceFailureCode.MISSING_PACKAGE in gate(report, AcceptanceGate.FACTOR_DAG_ONLY).failure_codes
    assert AcceptanceFailureCode.TESTS_NOT_RUN in gate(report, AcceptanceGate.CONTRACTS).failure_codes
    assert AcceptanceFailureCode.EMPTY_OR_CLAIMED_TEST_LOG in gate(report, AcceptanceGate.CONTRACTS).failure_codes


def test_selected_only_group_isolation_gate_rejects_unselected_execution() -> None:
    bad = replace(run(), executed_group_ids=("market_structure", "fundamental"))
    report = ResearchOSAcceptanceEvaluator().evaluate(package_evidence=packages(), run_evidence=bad)
    assert AcceptanceFailureCode.GROUP_ISOLATION_BROKEN in gate(report, AcceptanceGate.SELECTED_GROUP_ISOLATION).failure_codes


def test_per_group_drafts_and_terminal_budget_are_required() -> None:
    bad = replace(
        run(),
        group_draft_counts={"market_structure": 4, "meta_alpha": 0},
        terminal_candidate_hashes=(H4,),
        budget_consumed_by_candidate={H4: 1, H5: 0},
    )
    report = ResearchOSAcceptanceEvaluator().evaluate(package_evidence=packages(), run_evidence=bad)
    assert AcceptanceFailureCode.DRAFT_COUNT_INVALID in gate(report, AcceptanceGate.PER_GROUP_DRAFTS).failure_codes
    terminal = gate(report, AcceptanceGate.TERMINAL_ACCOUNTING_BUDGET)
    assert AcceptanceFailureCode.CANDIDATE_NOT_TERMINAL in terminal.failure_codes
    assert AcceptanceFailureCode.BUDGET_NOT_CONSUMED in terminal.failure_codes


def test_factor_dag_only_and_dynamic_lookback_are_mechanical() -> None:
    bad = replace(
        run(),
        factor_dag_only=False,
        unsafe_factor_path_detected=True,
        translated_lookback_sessions=(20, 50),
    )
    report = ResearchOSAcceptanceEvaluator().evaluate(package_evidence=packages(), run_evidence=bad)
    assert AcceptanceFailureCode.UNSAFE_FACTOR_PATH in gate(report, AcceptanceGate.FACTOR_DAG_ONLY).failure_codes
    assert AcceptanceFailureCode.LOOKBACK_NOT_PRESERVED in gate(report, AcceptanceGate.DYNAMIC_LOOKBACK).failure_codes


def test_wrong_authority_rankic_only_sealed_and_bad_report_fail() -> None:
    bad = replace(
        run(),
        pit_validated_by="LLM",
        official_evidence_by="QLIB",
        official_evidence_hashes=(),
        tournament_uses_rankic_only=True,
        tournament_sealed_material_exposed=True,
        tournament_candidate_hashes=(H4,),
        report_section_levels=("CONCEPTUAL",),
        report_has_partial_limitations=False,
        report_hash=None,
    )
    report = ResearchOSAcceptanceEvaluator().evaluate(package_evidence=packages(), run_evidence=bad)
    assert AcceptanceFailureCode.WRONG_AUTHORITY in gate(report, AcceptanceGate.PIT_OFFICIAL_AUTHORITY).failure_codes
    tournament = gate(report, AcceptanceGate.TOURNAMENT)
    assert AcceptanceFailureCode.RANKIC_ONLY in tournament.failure_codes
    assert AcceptanceFailureCode.SEALED_LEAKAGE in tournament.failure_codes
    assert AcceptanceFailureCode.TOURNAMENT_INCOMPLETE in tournament.failure_codes
    assert AcceptanceFailureCode.REPORT_NOT_USEFUL in gate(report, AcceptanceGate.PARTIAL_USEFUL_REPORT).failure_codes


def test_resume_gate_requires_no_rerun_same_hash() -> None:
    bad = replace(run(), committed_stage_rerun_count=1, same_input_same_hash=False)
    report = ResearchOSAcceptanceEvaluator().evaluate(package_evidence=packages(), run_evidence=bad)
    assert AcceptanceFailureCode.RESUME_NOT_IDEMPOTENT in gate(report, AcceptanceGate.IDEMPOTENT_RESUME).failure_codes


def test_payload_loader_and_markdown_are_stable() -> None:
    report = ResearchOSAcceptanceEvaluator().evaluate_from_payload(
        packages={key: value.to_dict() for key, value in packages().items()},
        run_evidence=run().to_dict(),
    )
    markdown = ResearchOSAcceptanceEvaluator().render_markdown(report)
    assert "IMPLEMENTED_AND_TESTED" in markdown
    assert report.status is AcceptanceStatus.IMPLEMENTED_AND_TESTED


def test_contract_rejects_unknown_status_vocab() -> None:
    with pytest.raises(ValueError):
        AcceptanceGateResult(
            gate=AcceptanceGate.CONTRACTS,
            status="surprise",
            failure_codes=(AcceptanceFailureCode.PACKAGE_NOT_READY,),
        )
