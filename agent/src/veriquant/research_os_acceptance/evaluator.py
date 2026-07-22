"""Mechanical, fail-closed Research OS acceptance evaluator."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from veriquant.contracts import FailureCode
from veriquant.contracts.base import canonicalize, fail

from .models import (
    AcceptanceFailureCode,
    AcceptanceGate,
    AcceptanceGateResult,
    AcceptanceStatus,
    PackageEvidence,
    ResearchOSAcceptanceReport,
    ResearchRunAcceptanceEvidence,
)


REQUIRED_PACKAGES: Mapping[AcceptanceGate, tuple[str, ...]] = {
    AcceptanceGate.CONTRACTS: ("research_os",),
    AcceptanceGate.CAPABILITY_DATA_PLANNING: ("research_capability_catalog", "research_intent_data_plan"),
    AcceptanceGate.SELECTED_GROUP_ISOLATION: ("research_group_runtime",),
    AcceptanceGate.PER_GROUP_DRAFTS: ("research_group_runtime",),
    AcceptanceGate.TERMINAL_ACCOUNTING_BUDGET: ("research_evidence_bridge",),
    AcceptanceGate.FACTOR_DAG_ONLY: ("candidate_translation",),
    AcceptanceGate.DYNAMIC_LOOKBACK: ("candidate_translation", "research_intent_data_plan"),
    AcceptanceGate.PIT_OFFICIAL_AUTHORITY: ("research_evidence_bridge",),
    AcceptanceGate.TOURNAMENT: ("candidate_tournament",),
    AcceptanceGate.PARTIAL_USEFUL_REPORT: ("formal_report_v2",),
    AcceptanceGate.IDEMPOTENT_RESUME: ("research_run_controller",),
}

REQUIRED_SYMBOLS: Mapping[str, tuple[str, ...]] = {
    "research_os": (
        "ResearchIntent",
        "DataRequirementPlan",
        "ResearchRoutePlan",
        "ResearchGroupSubmission",
        "CandidateValidationRequest",
        "AnswerSection",
    ),
    "research_intent_data_plan": ("DeterministicResearchQuestionPlanner", "DeterministicDataPlanner"),
    "research_capability_catalog": ("ResearchCapabilityCatalog", "NormalizedVibeDataBridge"),
    "research_group_runtime": ("DeterministicResearchGroupSelector", "ResearchGroupProposalRuntime"),
    "candidate_translation": ("SafeCandidateTranslator",),
    "candidate_tournament": ("CandidateTournament",),
    "research_evidence_bridge": ("ResearchEvidenceBridge",),
    "formal_report_v2": ("FormalReportV2Composer", "render_markdown_v2"),
    "research_run_controller": ("ResearchRunController", "build_research_run_plan"),
}


class ResearchOSAcceptanceEvaluator:
    """Evaluate packages and run artifacts without running financial compute."""

    def evaluate(
        self,
        *,
        package_evidence: Mapping[str, PackageEvidence],
        run_evidence: ResearchRunAcceptanceEvidence,
        report_id: str = "research_os_acceptance",
    ) -> ResearchOSAcceptanceReport:
        if not isinstance(package_evidence, Mapping):
            fail(FailureCode.INVALID_VALUE, "package_evidence must be a mapping")
        if not isinstance(run_evidence, ResearchRunAcceptanceEvidence):
            fail(FailureCode.INVALID_VALUE, "run_evidence must be ResearchRunAcceptanceEvidence")
        gates = tuple(self._gate(gate, package_evidence, run_evidence) for gate in AcceptanceGate)
        status = AcceptanceStatus.IMPLEMENTED_AND_TESTED if all(g.status is AcceptanceStatus.IMPLEMENTED_AND_TESTED for g in gates) else AcceptanceStatus.FAILED
        return ResearchOSAcceptanceReport(
            report_id=report_id,
            status=status,
            gates=gates,
            package_hashes=tuple(sorted(pkg.artifact_hash for pkg in package_evidence.values())),
            run_evidence_hash=run_evidence.artifact_hash,
        )

    def evaluate_from_payload(
        self,
        *,
        packages: Mapping[str, Mapping[str, Any]],
        run_evidence: Mapping[str, Any],
        report_id: str = "research_os_acceptance",
    ) -> ResearchOSAcceptanceReport:
        package_evidence = {
            package_id: PackageEvidence.from_result_payload(package_id, payload)
            for package_id, payload in packages.items()
        }
        return self.evaluate(
            package_evidence=package_evidence,
            run_evidence=ResearchRunAcceptanceEvidence.from_payload(run_evidence),
            report_id=report_id,
        )

    def load_package_results(self, root: str | Path, package_ids: tuple[str, ...]) -> dict[str, PackageEvidence]:
        base = Path(root)
        loaded: dict[str, PackageEvidence] = {}
        for package_id in package_ids:
            path = base / package_id / "result.json"
            if not path.exists():
                continue
            payload = json.loads(path.read_text(encoding="utf-8"))
            loaded[package_id] = PackageEvidence.from_result_payload(package_id, payload)
        return loaded

    def render_markdown(self, report: ResearchOSAcceptanceReport) -> str:
        lines = [
            "# AlphaForge Research OS Acceptance",
            "",
            f"- Status: `{report.status.value}`",
            f"- Report hash: `{report.artifact_hash}`",
            "",
            "| Gate | Status | Failure codes |",
            "|---|---:|---|",
        ]
        for gate in report.gates:
            failures = ", ".join(code.value for code in gate.failure_codes) or "none"
            lines.append(f"| `{gate.gate.value}` | `{gate.status.value}` | {failures} |")
        return "\n".join(lines) + "\n"

    def _gate(
        self,
        gate: AcceptanceGate,
        packages: Mapping[str, PackageEvidence],
        run: ResearchRunAcceptanceEvidence,
    ) -> AcceptanceGateResult:
        failures: list[AcceptanceFailureCode] = []
        evidence_hashes: list[str] = [run.artifact_hash]
        self._check_packages(gate, packages, failures, evidence_hashes)
        if not failures:
            _RUN_CHECKS[gate](run, failures)
        status = AcceptanceStatus.IMPLEMENTED_AND_TESTED if not failures else AcceptanceStatus.FAILED
        return AcceptanceGateResult(
            gate=gate,
            status=status,
            failure_codes=tuple(dict.fromkeys(failures)),
            evidence_hashes=tuple(sorted(dict.fromkeys(evidence_hashes))),
            message="accepted" if not failures else "failed closed",
        )

    @staticmethod
    def _check_packages(
        gate: AcceptanceGate,
        packages: Mapping[str, PackageEvidence],
        failures: list[AcceptanceFailureCode],
        evidence_hashes: list[str],
    ) -> None:
        for package_id in REQUIRED_PACKAGES[gate]:
            package = packages.get(package_id)
            if package is None:
                failures.append(AcceptanceFailureCode.MISSING_PACKAGE)
                continue
            evidence_hashes.append(package.artifact_hash)
            if package.raw_status not in {"READY_FOR_INTEGRATION", "IMPLEMENTED_AND_TESTED"}:
                failures.append(AcceptanceFailureCode.PACKAGE_NOT_READY)
            if not package.tests_run:
                failures.append(AcceptanceFailureCode.TESTS_NOT_RUN)
            if not package.has_real_tests:
                failures.append(AcceptanceFailureCode.EMPTY_OR_CLAIMED_TEST_LOG)
            for symbol in REQUIRED_SYMBOLS.get(package_id, ()):
                if symbol not in package.exported_symbols:
                    failures.append(AcceptanceFailureCode.MISSING_SYMBOL)


def _contracts(run: ResearchRunAcceptanceEvidence, failures: list[AcceptanceFailureCode]) -> None:
    if not (run.campaign_hash and run.intent_hash and run.data_plan_hash and run.route_plan_hash):
        failures.append(AcceptanceFailureCode.CONTRACT_EVIDENCE_MISSING)


def _capability_data_planning(run: ResearchRunAcceptanceEvidence, failures: list[AcceptanceFailureCode]) -> None:
    if run.requested_lookback_sessions is None or not run.selected_group_ids:
        failures.append(AcceptanceFailureCode.DATA_PLANNING_INCOMPLETE)
    if not run.provider_unavailable_deterministic_complete:
        failures.append(AcceptanceFailureCode.DATA_PLANNING_INCOMPLETE)


def _selected_group_isolation(run: ResearchRunAcceptanceEvidence, failures: list[AcceptanceFailureCode]) -> None:
    if set(run.executed_group_ids) != set(run.selected_group_ids):
        failures.append(AcceptanceFailureCode.GROUP_ISOLATION_BROKEN)


def _per_group_drafts(run: ResearchRunAcceptanceEvidence, failures: list[AcceptanceFailureCode]) -> None:
    for group_id in run.selected_group_ids:
        count = int(run.group_draft_counts.get(group_id, 0))
        if count < 1 or count > 3:
            failures.append(AcceptanceFailureCode.DRAFT_COUNT_INVALID)


def _terminal_accounting(run: ResearchRunAcceptanceEvidence, failures: list[AcceptanceFailureCode]) -> None:
    if set(run.terminal_candidate_hashes) != set(run.candidate_request_hashes):
        failures.append(AcceptanceFailureCode.CANDIDATE_NOT_TERMINAL)
    for candidate_hash in run.candidate_request_hashes:
        if int(run.budget_consumed_by_candidate.get(candidate_hash, 0)) < 1:
            failures.append(AcceptanceFailureCode.BUDGET_NOT_CONSUMED)


def _factor_dag_only(run: ResearchRunAcceptanceEvidence, failures: list[AcceptanceFailureCode]) -> None:
    if not run.factor_dag_only or run.unsafe_factor_path_detected:
        failures.append(AcceptanceFailureCode.UNSAFE_FACTOR_PATH)


def _dynamic_lookback(run: ResearchRunAcceptanceEvidence, failures: list[AcceptanceFailureCode]) -> None:
    if run.requested_lookback_sessions is None:
        failures.append(AcceptanceFailureCode.LOOKBACK_NOT_PRESERVED)
        return
    if not run.translated_lookback_sessions or any(value != run.requested_lookback_sessions for value in run.translated_lookback_sessions):
        failures.append(AcceptanceFailureCode.LOOKBACK_NOT_PRESERVED)


def _pit_authority(run: ResearchRunAcceptanceEvidence, failures: list[AcceptanceFailureCode]) -> None:
    if run.pit_validated_by != "DATA_SERVICE" or run.official_evidence_by != "EVIDENCE_SERVICE" or not run.official_evidence_hashes:
        failures.append(AcceptanceFailureCode.WRONG_AUTHORITY)


def _tournament(run: ResearchRunAcceptanceEvidence, failures: list[AcceptanceFailureCode]) -> None:
    if set(run.tournament_candidate_hashes) != set(run.terminal_candidate_hashes) or not run.tournament_uses_full_coverage:
        failures.append(AcceptanceFailureCode.TOURNAMENT_INCOMPLETE)
    if run.tournament_uses_rankic_only:
        failures.append(AcceptanceFailureCode.RANKIC_ONLY)
    if run.tournament_sealed_material_exposed:
        failures.append(AcceptanceFailureCode.SEALED_LEAKAGE)


def _partial_report(run: ResearchRunAcceptanceEvidence, failures: list[AcceptanceFailureCode]) -> None:
    levels = set(run.report_section_levels)
    useful_evidence = {"AUXILIARY_RESEARCH", "OFFICIAL_EVALUATED"}
    if run.report_hash is None or "CONCEPTUAL" not in levels or not (levels & useful_evidence):
        failures.append(AcceptanceFailureCode.REPORT_NOT_USEFUL)
    if not run.report_has_partial_limitations:
        failures.append(AcceptanceFailureCode.REPORT_NOT_USEFUL)


def _idempotent_resume(run: ResearchRunAcceptanceEvidence, failures: list[AcceptanceFailureCode]) -> None:
    if not run.committed_stage_hashes or run.committed_stage_rerun_count != 0 or not run.same_input_same_hash:
        failures.append(AcceptanceFailureCode.RESUME_NOT_IDEMPOTENT)


_RUN_CHECKS = {
    AcceptanceGate.CONTRACTS: _contracts,
    AcceptanceGate.CAPABILITY_DATA_PLANNING: _capability_data_planning,
    AcceptanceGate.SELECTED_GROUP_ISOLATION: _selected_group_isolation,
    AcceptanceGate.PER_GROUP_DRAFTS: _per_group_drafts,
    AcceptanceGate.TERMINAL_ACCOUNTING_BUDGET: _terminal_accounting,
    AcceptanceGate.FACTOR_DAG_ONLY: _factor_dag_only,
    AcceptanceGate.DYNAMIC_LOOKBACK: _dynamic_lookback,
    AcceptanceGate.PIT_OFFICIAL_AUTHORITY: _pit_authority,
    AcceptanceGate.TOURNAMENT: _tournament,
    AcceptanceGate.PARTIAL_USEFUL_REPORT: _partial_report,
    AcceptanceGate.IDEMPOTENT_RESUME: _idempotent_resume,
}
