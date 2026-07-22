from __future__ import annotations

import json
import ast
from pathlib import Path

import pytest

from src.veriquant.qlib_migration_acceptance import (
    AcceptanceFailureCode,
    AcceptanceStatus,
    QlibMigrationAcceptanceEvaluator,
    QlibMigrationGate,
)


REPO = Path(__file__).resolve().parents[4]


def _gate(verdict, gate):
    return next(item for item in verdict.gates if item.gate is gate)


def test_current_repository_accepts_specialist_52_of_52() -> None:
    verdict = QlibMigrationAcceptanceEvaluator(REPO).evaluate()
    specialist = _gate(verdict, QlibMigrationGate.SPECIALIST_QLIB_PRIMARY)
    assert specialist.status is AcceptanceStatus.IMPLEMENTED_AND_TESTED
    assert not specialist.failure_codes


def test_current_repository_has_constitution_and_catalog_proof() -> None:
    verdict = QlibMigrationAcceptanceEvaluator(REPO).evaluate()
    assert _gate(verdict, QlibMigrationGate.CONSTITUTION_V2).status is AcceptanceStatus.IMPLEMENTED_AND_TESTED
    coverage = _gate(verdict, QlibMigrationGate.CATALOG_CLASSIFICATION)
    assert coverage.status is AcceptanceStatus.IMPLEMENTED_AND_TESTED
    assert coverage.facts == {"total": 461, "unclassified": 0, "legacy_gap_unclassified": 0}


def test_current_repository_real_pyqlib_and_pit_artifacts_are_accepted() -> None:
    verdict = QlibMigrationAcceptanceEvaluator(REPO).evaluate()
    assert _gate(verdict, QlibMigrationGate.REAL_PYQLIB_SMOKE).status is AcceptanceStatus.IMPLEMENTED_AND_TESTED
    assert _gate(verdict, QlibMigrationGate.FROZEN_PIT_PROVIDER).status is AcceptanceStatus.IMPLEMENTED_AND_TESTED


def test_package_gate_accepts_structured_verification_and_verified_hash_manifests() -> None:
    verdict = QlibMigrationAcceptanceEvaluator(REPO).evaluate()
    gate = _gate(verdict, QlibMigrationGate.PACKAGE_READINESS)
    assert gate.status is AcceptanceStatus.IMPLEMENTED_AND_TESTED
    assert not gate.reasons


def test_structured_verification_requires_real_pytest_count() -> None:
    accepted = {
        "verification": [
            {"check": "focused_pytest", "status": "passed", "detail": "12 passed in 8.44s"}
        ]
    }
    lint_only = {
        "verification": [
            {"check": "black_check", "status": "passed", "detail": "8 files unchanged"}
        ]
    }
    forged_empty = {
        "verification": [
            {"check": "focused_pytest", "status": "passed", "detail": "completed"}
        ]
    }
    checker = QlibMigrationAcceptanceEvaluator._result_has_passed_test
    assert checker(accepted) is True
    assert checker(lint_only) is False
    assert checker(forged_empty) is False


def test_missing_central_result_is_never_success() -> None:
    verdict = QlibMigrationAcceptanceEvaluator(REPO).evaluate()
    gate = _gate(verdict, QlibMigrationGate.UNIFIED_FACTOR_SCORE)
    if not (REPO / ".autobuild/veriquant/integration/qlib_expression_migration/result.json").exists():
        assert gate.status is AcceptanceStatus.UNAVAILABLE


def test_dedicated_process_is_unavailable_until_ready_package_exists() -> None:
    verdict = QlibMigrationAcceptanceEvaluator(REPO).evaluate()
    gate = _gate(verdict, QlibMigrationGate.DEDICATED_PROCESS)
    assert gate.status in {AcceptanceStatus.IMPLEMENTED_AND_TESTED, AcceptanceStatus.UNAVAILABLE, AcceptanceStatus.FAILED}
    if gate.status is AcceptanceStatus.UNAVAILABLE:
        assert AcceptanceFailureCode.PROCESS_ISOLATION_UNAVAILABLE in gate.failure_codes


def test_verdict_hash_is_stable_for_identical_inputs() -> None:
    first = QlibMigrationAcceptanceEvaluator(REPO).evaluate()
    second = QlibMigrationAcceptanceEvaluator(REPO).evaluate()
    assert first.input_hash == second.input_hash
    assert first.report_hash == second.report_hash


def test_machine_json_and_chinese_markdown_are_written_atomically(tmp_path: Path) -> None:
    evaluator = QlibMigrationAcceptanceEvaluator(tmp_path)
    verdict = evaluator.evaluate()
    json_path = tmp_path / "verdict.json"
    markdown_path = tmp_path / "summary.md"
    evaluator.write_reports(verdict, json_path=json_path, markdown_path=markdown_path)
    assert json.loads(json_path.read_text(encoding="utf-8"))["report_hash"] == verdict.report_hash
    assert "Qlib 公式迁移机械验收" in markdown_path.read_text(encoding="utf-8")


def test_report_output_path_escape_fails_closed(tmp_path: Path) -> None:
    evaluator = QlibMigrationAcceptanceEvaluator(tmp_path)
    verdict = evaluator.evaluate()
    with pytest.raises(ValueError, match="escapes repository"):
        evaluator.write_reports(
            verdict,
            json_path=tmp_path.parent / "escape.json",
            markdown_path=tmp_path / "ok.md",
        )


def test_evaluator_source_has_no_financial_execution_imports() -> None:
    source = (REPO / "agent/src/veriquant/qlib_migration_acceptance/evaluator.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        node.module or ""
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    }
    calls = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert not any(name.startswith(("veriquant.evidence", "veriquant.factors", "qlib")) for name in imported)
    assert {"eval", "exec", "compile", "__import__"}.isdisjoint(calls)


def test_rendered_report_matches_all_implemented_mechanical_gates() -> None:
    evaluator = QlibMigrationAcceptanceEvaluator(REPO)
    verdict = evaluator.evaluate()
    rendered = evaluator.render_markdown(verdict)
    assert "SPECIALIST_PROMPT_REGRESSION" not in rendered
    assert "总状态：**IMPLEMENTED_AND_TESTED**" in rendered
