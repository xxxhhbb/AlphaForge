from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from veriquant.product_acceptance.evaluator import ProductAcceptanceEvaluator, file_sha256
from veriquant.product_acceptance.models import (
    DataCapabilityStatus,
    EvidenceProvenance,
    ProductFailureCode,
    ProductTerminal,
    ScenarioAcceptanceStatus,
    ScenarioEvidence,
    SemanticCompileStatus,
)


REPO = Path(__file__).resolve().parents[5]
MATRIX = REPO / ".autobuild/veriquant/product_acceptance/scenarios/RESEARCH_SCENARIO_MATRIX.json"
H = lambda value: "sha256:" + value * 64
SPECIALISTS = ("fundamental", "expectations_events", "market_structure", "alternative_data", "macro_regime", "meta_alpha")


def _evidence(index: int) -> ScenarioEvidence:
    specialist = SPECIALISTS[(index - 1) % len(SPECIALISTS)]
    terminal = ProductTerminal.OFFICIAL_EVALUATED
    data_status = DataCapabilityStatus.READY
    candidates = (H("3"), H("4"))
    statuses = ("REJECT", "VALIDATED_RESEARCH")
    official = (H("5"),)
    claims = True
    provenance = EvidenceProvenance.REAL_FROZEN if index <= 20 else EvidenceProvenance.REGISTERED_EVIDENCE
    if index in {4, 9}:
        terminal, official, claims = ProductTerminal.NEGATIVE_RESEARCH_RESULT, (), False
    elif index == 18:
        terminal, data_status, candidates, statuses, official, claims = (
            ProductTerminal.DATA_GAP_REPORT, DataCapabilityStatus.UNAVAILABLE, (), (), (), False
        )
    elif index in {21, 22}:
        terminal, candidates, statuses, official, claims = ProductTerminal.DIAGNOSTIC_REPORT, (), (), (), False
    elif index == 23:
        terminal, official, claims = ProductTerminal.PARTIAL_RESEARCH_RESULT, (), False
    manifest = H("a")
    return ScenarioEvidence(
        scenario_id=f"S{index:02d}",
        semantic_status=SemanticCompileStatus.COMPILED,
        semantic_compile_hash=H("1"),
        data_status=data_status,
        data_capability_hash=H("2"),
        provenance=provenance,
        terminal=terminal,
        selected_specialists=(specialist,),
        specialist_result_hashes=(H("6"),),
        candidate_terminal_statuses=statuses,
        candidate_terminal_hashes=candidates,
        official_evidence_hashes=official,
        selection_hash=H("7") if candidates else None,
        report_hash=H("8"),
        canonical_manifest_hash=manifest,
        api_manifest_hash=manifest,
        workbench_manifest_hash=manifest,
        report_manifest_hash=manifest,
        test_evidence_hashes=(H("9"),),
        trial_count=len(candidates),
        claims_official=claims,
    )


def _full_fixture():
    return tuple(_evidence(index) for index in range(1, 25))


def test_24_scenarios_pass_with_20_plus_real_or_registered_and_six_groups() -> None:
    report = ProductAcceptanceEvaluator().evaluate(_full_fixture(), matrix_hash=file_sha256(MATRIX))
    assert report.status is ScenarioAcceptanceStatus.PASSED
    assert report.scenario_count == report.passed_count == 24
    assert report.real_or_registered_count == 24
    assert set(report.covered_specialists) == set(SPECIALISTS)
    assert not report.global_failure_codes


def test_data_gap_negative_partial_and_diagnostic_are_legal_not_official() -> None:
    report = ProductAcceptanceEvaluator().evaluate(_full_fixture(), matrix_hash=file_sha256(MATRIX))
    by_id = {item.scenario_id: item for item in report.verdicts}
    for scenario_id in ("S04", "S09", "S18", "S21", "S22", "S23"):
        assert by_id[scenario_id].status is ScenarioAcceptanceStatus.PASSED
    assert by_id["S18"].terminal is ProductTerminal.DATA_GAP_REPORT


def test_synthetic_cannot_claim_official() -> None:
    evidence = list(_full_fixture())
    evidence[0] = replace(evidence[0], provenance=EvidenceProvenance.SYNTHETIC)
    report = ProductAcceptanceEvaluator().evaluate(evidence, matrix_hash=file_sha256(MATRIX))
    verdict = next(item for item in report.verdicts if item.scenario_id == "S01")
    assert report.status is ScenarioAcceptanceStatus.FAILED
    assert ProductFailureCode.SYNTHETIC_AS_REAL in verdict.failure_codes


def test_unavailable_data_capability_cannot_claim_official() -> None:
    evidence = list(_full_fixture())
    evidence[0] = replace(evidence[0], data_status=DataCapabilityStatus.UNAVAILABLE)
    report = ProductAcceptanceEvaluator().evaluate(evidence, matrix_hash=file_sha256(MATRIX))
    verdict = next(item for item in report.verdicts if item.scenario_id == "S01")
    assert report.status is ScenarioAcceptanceStatus.FAILED
    assert ProductFailureCode.OFFICIAL_WITHOUT_DATA_CAPABILITY in verdict.failure_codes


def test_missing_hash_test_selection_and_evidence_fail_closed() -> None:
    evidence = list(_full_fixture())
    evidence[0] = replace(
        evidence[0], semantic_compile_hash=None, official_evidence_hashes=(),
        selection_hash=None, test_evidence_hashes=(),
    )
    verdict = ProductAcceptanceEvaluator().evaluate(evidence, matrix_hash=file_sha256(MATRIX)).verdicts[0]
    assert {
        ProductFailureCode.MISSING_CRITICAL_HASH,
        ProductFailureCode.OFFICIAL_WITHOUT_EVIDENCE,
        ProductFailureCode.OFFICIAL_WITHOUT_SELECTION,
        ProductFailureCode.TEST_EVIDENCE_MISSING,
    } <= set(verdict.failure_codes)


def test_manifest_and_trial_count_must_match() -> None:
    evidence = list(_full_fixture())
    evidence[0] = replace(evidence[0], api_manifest_hash=H("b"), trial_count=9)
    verdict = ProductAcceptanceEvaluator().evaluate(evidence, matrix_hash=file_sha256(MATRIX)).verdicts[0]
    assert ProductFailureCode.MANIFEST_STATE_MISMATCH in verdict.failure_codes
    assert ProductFailureCode.TRIAL_COUNT_MISMATCH in verdict.failure_codes


def test_global_coverage_gates_fail_closed() -> None:
    reduced = tuple(replace(item, provenance=EvidenceProvenance.FIXTURE, selected_specialists=("market_structure",), specialist_result_hashes=(H("6"),)) for item in _full_fixture())
    report = ProductAcceptanceEvaluator().evaluate(reduced, matrix_hash=file_sha256(MATRIX))
    assert ProductFailureCode.REAL_OR_REGISTERED_COVERAGE_LOW in report.global_failure_codes
    assert ProductFailureCode.SPECIALIST_COVERAGE_INCOMPLETE in report.global_failure_codes


def test_same_input_produces_same_report_hash() -> None:
    evaluator = ProductAcceptanceEvaluator()
    left = evaluator.evaluate(_full_fixture(), matrix_hash=file_sha256(MATRIX))
    right = evaluator.evaluate(_full_fixture(), matrix_hash=file_sha256(MATRIX))
    assert left.canonical_json() == right.canonical_json()
    assert left.artifact_hash == right.artifact_hash


def test_system_failure_report_exists_but_cannot_pass_product_acceptance() -> None:
    evidence = list(_full_fixture())
    evidence[0] = replace(
        evidence[0],
        terminal=ProductTerminal.SYSTEM_FAILED,
        official_evidence_hashes=(),
        selection_hash=None,
        claims_official=False,
    )
    report = ProductAcceptanceEvaluator().evaluate(evidence, matrix_hash=file_sha256(MATRIX))
    verdict = next(item for item in report.verdicts if item.scenario_id == "S01")
    assert verdict.status is ScenarioAcceptanceStatus.FAILED
    assert ProductFailureCode.ILLEGAL_TERMINAL in verdict.failure_codes
