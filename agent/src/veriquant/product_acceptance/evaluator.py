"""Fail-closed mechanical evaluator for the frozen 24-scenario matrix."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Iterable

from ..llm_desks.safety import canonical_hash
from .models import (
    DataCapabilityStatus,
    EvidenceProvenance,
    ProductAcceptanceReport,
    ProductFailureCode,
    ProductTerminal,
    ScenarioAcceptanceStatus,
    ScenarioEvidence,
    ScenarioVerdict,
    SemanticCompileStatus,
)


REQUIRED_SPECIALISTS = frozenset(
    {"fundamental", "expectations_events", "market_structure", "alternative_data", "macro_regime", "meta_alpha"}
)
LEGAL_NON_OFFICIAL = frozenset(
    {
        ProductTerminal.NEGATIVE_RESEARCH_RESULT,
        ProductTerminal.DATA_GAP_REPORT,
        ProductTerminal.PARTIAL_RESEARCH_RESULT,
        ProductTerminal.DIAGNOSTIC_REPORT,
        ProductTerminal.UNSUPPORTED_SAFELY,
    }
)


class ProductAcceptanceEvaluator:
    def __init__(self, *, expected_scenario_ids: tuple[str, ...] | None = None) -> None:
        self.expected_ids = expected_scenario_ids or tuple(f"S{index:02d}" for index in range(1, 25))

    def evaluate(self, evidence: Iterable[ScenarioEvidence], *, matrix_hash: str) -> ProductAcceptanceReport:
        items = tuple(evidence)
        counts = {scenario_id: sum(item.scenario_id == scenario_id for item in items) for scenario_id in self.expected_ids}
        global_failures: set[ProductFailureCode] = set()
        if any(value == 0 for value in counts.values()):
            global_failures.add(ProductFailureCode.MISSING_SCENARIO)
        if any(value > 1 for value in counts.values()) or len({item.scenario_id for item in items}) != len(items):
            global_failures.add(ProductFailureCode.DUPLICATE_SCENARIO)
        verdicts = tuple(sorted((self._scenario(item) for item in items), key=lambda item: item.scenario_id))
        real_count = sum(item.provenance in {EvidenceProvenance.REAL_FROZEN, EvidenceProvenance.REGISTERED_EVIDENCE} for item in items)
        if real_count < 20:
            global_failures.add(ProductFailureCode.REAL_OR_REGISTERED_COVERAGE_LOW)
        covered = tuple(sorted({specialist for item in items for specialist in item.selected_specialists}))
        if not REQUIRED_SPECIALISTS <= set(covered):
            global_failures.add(ProductFailureCode.SPECIALIST_COVERAGE_INCOMPLETE)
        passed = sum(item.status is ScenarioAcceptanceStatus.PASSED for item in verdicts)
        status = (
            ScenarioAcceptanceStatus.PASSED
            if not global_failures and len(verdicts) == 24 and passed == 24
            else ScenarioAcceptanceStatus.FAILED
        )
        return ProductAcceptanceReport(
            matrix_hash=matrix_hash,
            status=status,
            verdicts=verdicts,
            scenario_count=len(verdicts),
            passed_count=passed,
            real_or_registered_count=real_count,
            covered_specialists=covered,
            global_failure_codes=tuple(sorted(global_failures, key=lambda item: item.value)),
        )

    def _scenario(self, item: ScenarioEvidence) -> ScenarioVerdict:
        failures: set[ProductFailureCode] = set()
        if item.semantic_compile_hash is None or item.data_capability_hash is None or item.report_hash is None:
            failures.add(ProductFailureCode.MISSING_CRITICAL_HASH)
        if item.semantic_status is SemanticCompileStatus.FAILED:
            failures.add(ProductFailureCode.SEMANTIC_COMPILE_FAILED)
        if item.semantic_status is SemanticCompileStatus.NEEDS_CLARIFICATION and item.terminal not in {
            ProductTerminal.PARTIAL_RESEARCH_RESULT, ProductTerminal.UNSUPPORTED_SAFELY
        }:
            failures.add(ProductFailureCode.AMBIGUITY_EXECUTED)
        if len(item.selected_specialists) != len(item.specialist_result_hashes):
            failures.add(ProductFailureCode.SPECIALIST_EVIDENCE_MISMATCH)
        if len(item.candidate_terminal_statuses) != len(item.candidate_terminal_hashes):
            failures.add(ProductFailureCode.CANDIDATE_TERMINAL_MISMATCH)
        if item.trial_count != len(item.candidate_terminal_hashes):
            failures.add(ProductFailureCode.TRIAL_COUNT_MISMATCH)
        manifests = (
            item.canonical_manifest_hash, item.api_manifest_hash,
            item.workbench_manifest_hash, item.report_manifest_hash,
        )
        if any(value is None for value in manifests) or len(set(manifests)) != 1:
            failures.add(ProductFailureCode.MANIFEST_STATE_MISMATCH)
        if not item.test_evidence_hashes:
            failures.add(ProductFailureCode.TEST_EVIDENCE_MISSING)
        if item.claims_official or item.terminal is ProductTerminal.OFFICIAL_EVALUATED:
            if item.data_status in {DataCapabilityStatus.AUXILIARY_ONLY, DataCapabilityStatus.UNAVAILABLE}:
                failures.add(ProductFailureCode.OFFICIAL_WITHOUT_DATA_CAPABILITY)
            if item.provenance in {EvidenceProvenance.SYNTHETIC, EvidenceProvenance.FIXTURE, EvidenceProvenance.NONE}:
                failures.add(ProductFailureCode.SYNTHETIC_AS_REAL)
            if not item.official_evidence_hashes:
                failures.add(ProductFailureCode.OFFICIAL_WITHOUT_EVIDENCE)
            if item.selection_hash is None:
                failures.add(ProductFailureCode.OFFICIAL_WITHOUT_SELECTION)
        elif item.terminal in LEGAL_NON_OFFICIAL:
            if item.claims_official:
                failures.add(ProductFailureCode.ILLEGAL_TERMINAL)
        else:
            failures.add(ProductFailureCode.ILLEGAL_TERMINAL)
        if item.candidate_terminal_hashes and item.selection_hash is None:
            failures.add(ProductFailureCode.OFFICIAL_WITHOUT_SELECTION)
        ordered = tuple(sorted(failures, key=lambda value: value.value))
        return ScenarioVerdict(
            scenario_id=item.scenario_id,
            status=ScenarioAcceptanceStatus.FAILED if ordered else ScenarioAcceptanceStatus.PASSED,
            terminal=item.terminal,
            failure_codes=ordered,
            evidence_hash=item.artifact_hash,
        )


def file_sha256(path: str | Path) -> str:
    return "sha256:" + hashlib.sha256(Path(path).read_bytes()).hexdigest()


__all__ = ["ProductAcceptanceEvaluator", "file_sha256"]
