from __future__ import annotations

import json
from pathlib import Path

import pytest

from veriquant.agent_research_sandbox.process_runner import canonical_hash
from veriquant.research_answer_harness import ResearchAnswerHarness


REPO_ROOT = Path(__file__).resolve().parents[4]


def _receipt(*, case_id: str, output_kind: str, mode: str) -> dict[str, object]:
    return {
        "case_id": case_id,
        "execution_status": "PASSED",
        "execution_mode": mode,
        "evidence_authority": "OFFICIAL_EVIDENCE",
        "answer_status": "ANSWERED",
        "report_status": "QUALIFIED",
        "output_kind": output_kind,
        "answer_hash": "sha256:" + "a" * 64,
        "report_hash": "sha256:" + "b" * 64,
    }


def test_representative_plan_has_four_profiles_for_every_output_kind(tmp_path: Path) -> None:
    harness = ResearchAnswerHarness(repo_root=REPO_ROOT, output_root=tmp_path)

    plan = harness._representative_plan()

    assert len(plan["cases"]) == 24
    by_kind: dict[str, set[str]] = {}
    for case in plan["cases"]:
        by_kind.setdefault(case["output_kind"], set()).add(case["profile"])
    assert set(by_kind) == {
        "ScoreTable",
        "RegressionTable",
        "RegimeComparisonTable",
        "EventTable",
        "DiagnosticTable",
        "DatasetAssessmentTable",
    }
    assert all(
        profiles == {"REAL_POSITIVE", "REAL_NEGATIVE", "DATA_GAP", "SECURITY"}
        for profiles in by_kind.values()
    )


def test_empty_real_receipts_are_partial_not_qualified(tmp_path: Path) -> None:
    harness = ResearchAnswerHarness(repo_root=REPO_ROOT, output_root=tmp_path)

    verdict = harness.run()

    assert verdict["status"] == "PARTIAL"
    assert "REAL_V3_REPRESENTATIVE_CASES_INCOMPLETE" in verdict["blocker_codes"]
    assert json.loads((tmp_path / "progress.json").read_text(encoding="utf-8"))["recovery_cursor"] == "RA-01-01"


def test_record_case_is_idempotent_and_rejects_conflicting_identity(tmp_path: Path) -> None:
    harness = ResearchAnswerHarness(repo_root=REPO_ROOT, output_root=tmp_path / "out")
    receipt_path = tmp_path / "receipt.json"
    receipt = _receipt(
        case_id="RA-01-01",
        output_kind="DatasetAssessmentTable",
        mode="REAL_PRODUCT_PATH",
    )
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")

    assert harness.record_case(receipt_path) == receipt
    assert harness.record_case(receipt_path) == receipt
    assert len((tmp_path / "out" / "case_results.jsonl").read_text(encoding="utf-8").splitlines()) == 1

    receipt["report_hash"] = "sha256:" + "c" * 64
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    with pytest.raises(ValueError, match="different receipt"):
        harness.record_case(receipt_path)


def test_only_exact_complete_matrix_can_qualify(tmp_path: Path) -> None:
    harness = ResearchAnswerHarness(repo_root=REPO_ROOT, output_root=tmp_path / "out")
    plan = harness._representative_plan()
    for index, case in enumerate(plan["cases"]):
        receipt = _receipt(
            case_id=case["case_id"],
            output_kind=case["output_kind"],
            mode="REAL_PRODUCT_PATH" if index < 12 else "DETERMINISTIC_NEGATIVE",
        )
        path = tmp_path / f"receipt-{index}.json"
        path.write_text(json.dumps(receipt), encoding="utf-8")
        harness.record_case(path)

    first = harness.run()
    second = harness.run()

    assert first["status"] == "QUALIFIED"
    assert first["artifact_hash"] == second["artifact_hash"]
    assert first["blocker_codes"] == []


def test_incomplete_or_unsupported_receipt_fails_closed(tmp_path: Path) -> None:
    harness = ResearchAnswerHarness(repo_root=REPO_ROOT, output_root=tmp_path / "out")
    path = tmp_path / "receipt.json"
    path.write_text(json.dumps({"case_id": "RA-01-01"}), encoding="utf-8")
    with pytest.raises(ValueError, match="incomplete"):
        harness.record_case(path)

    value = _receipt(case_id="RA-01-01", output_kind="UnknownTable", mode="REAL_PRODUCT_PATH")
    path.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(ValueError, match="unsupported"):
        harness.record_case(path)


def test_deterministic_gap_and_security_profiles_are_explicit_not_official(tmp_path: Path) -> None:
    harness = ResearchAnswerHarness(repo_root=REPO_ROOT, output_root=tmp_path)

    receipts = harness.run_deterministic_negative_cases()

    assert len(receipts) == 12
    assert {item["execution_mode"] for item in receipts} == {"DETERMINISTIC_NEGATIVE"}
    assert {item["evidence_authority"] for item in receipts} == {"NONE"}
    assert {item["answer_status"] for item in receipts} == {"UNANSWERABLE_WITH_REASON"}
    assert {item["failure_code"] for item in receipts} == {
        "STANDARD_OUTPUT_UNAVAILABLE",
        "PRIVILEGED_OR_SECRET_MATERIAL_REJECTED",
    }
    verdict = harness.run()
    assert verdict["status"] == "PARTIAL"
    assert verdict["qualified_representative_cases"] == 12
    assert verdict["qualified_real_cases"] == 0


def test_append_only_case_correction_chain_replays_latest_and_rejects_corruption() -> None:
    first = _receipt(
        case_id="RA-01-01",
        output_kind="DatasetAssessmentTable",
        mode="REAL_PRODUCT_PATH",
    )
    second = dict(first)
    second["report_hash"] = "sha256:" + "c" * 64
    second["supersedes_receipt_hash"] = canonical_hash(first)
    second["revision"] = 1

    assert ResearchAnswerHarness._effective_cases([first, second]) == [second]
    corrupt = dict(second)
    corrupt["supersedes_receipt_hash"] = "sha256:" + "d" * 64
    with pytest.raises(ValueError, match="corrupt"):
        ResearchAnswerHarness._effective_cases([first, corrupt])
