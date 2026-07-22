"""Mechanical finalizer for the adaptive 25/20 Research500 Harness policy."""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any, Iterable, Mapping

from ..research_toolbox.contracts import canonical_hash


SCHEMA_VERSION = "alphaforge.research500-representative-final/v1"
_HASH = re.compile(r"^sha256:[0-9a-f]{64}$")
_SECRET_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9_-]{16,}"),
    re.compile(r"Bearer\s+[A-Za-z0-9._~-]{12,}", re.IGNORECASE),
    re.compile(r"(?:api[_-]?key|authorization|access[_-]?token)\s*[:=]\s*['\"][^'\"]{8,}", re.IGNORECASE),
)
_SEALED_PATTERNS = (
    re.compile(r"sealed_(?:metrics|rows|labels|test)", re.IGNORECASE),
    re.compile(r"['\"]partition['\"]\s*:\s*['\"]SEALED['\"]", re.IGNORECASE),
)
_FORBIDDEN_IMPORT_ROOTS = frozenset({
    "builtins", "ctypes", "http", "httpx", "importlib", "multiprocessing",
    "os", "pathlib", "pickle", "requests", "shutil", "socket", "subprocess",
    "sys", "urllib",
})
_FORBIDDEN_CALLS = frozenset({
    "__import__", "breakpoint", "compile", "eval", "exec", "input", "open",
})


def finalize_representative_harness(*, root: str | Path) -> Mapping[str, Any]:
    harness_root = Path(root).resolve()
    plan = _load_hashed(harness_root / "representative_plan.json")
    progress = _load_hashed(harness_root / "representative_progress.json")
    preflight = _load_hashed(harness_root / "representative_preflight.json")
    static = _load_hashed(harness_root / "final_verdict.json")
    industry = _load_hashed(
        harness_root / "product_data_registered_industry_v1" / "industry_panel_receipt.json"
    )
    resume = _load_hashed(harness_root / "first_real_e2e_canary.json")

    failures: list[str] = []
    static_valid = (
        static.get("level1_static_status") == "PASSED"
        and static.get("question_status_counts") == {"COVERED_NOT_RUN": 500}
    ) or (
        static.get("schema_version") == SCHEMA_VERSION
        and static.get("static_question_count") == 500
        and static.get("static_question_status") == "COVERED_NOT_RUN"
    )
    if not static_valid:
        failures.append("STATIC_500_COVERAGE_INVALID")
    if progress.get("plan_hash") != plan.get("artifact_hash"):
        failures.append("PROGRESS_PLAN_HASH_MISMATCH")
    if progress.get("status") != "PASSED" or progress.get("passed_count") != 25:
        failures.append("REPRESENTATIVE_PROGRESS_INCOMPLETE")
    if progress.get("family_passed_count") != 25 or progress.get("dimension_passed_count") != 20:
        failures.append("REPRESENTATIVE_COVERAGE_INCOMPLETE")
    if preflight.get("plan_hash") != plan.get("artifact_hash") or preflight.get("status_counts") != {"READY": 25}:
        failures.append("PREFLIGHT_NOT_CONTENT_ADDRESSED_TO_FINAL_PLAN")
    if industry.get("status") != "PASSED" or industry.get("replay_verified") is not True:
        failures.append("INDUSTRY_PANEL_REPLAY_INVALID")
    if float(industry.get("industry_coverage", 0.0)) < 0.80:
        failures.append("INDUSTRY_COVERAGE_BELOW_POLICY")
    if resume.get("status") != "PASSED" or not str(resume.get("recovery_status", "")).startswith("RESUMED_"):
        failures.append("RESUME_DRILL_MISSING")

    receipt_index = _receipt_index(harness_root / "representative_runs")
    artifact_checks = []
    security_findings: list[dict[str, str]] = []
    for row in plan.get("rows", ()):
        if row.get("status") != "PASSED":
            failures.append(f"REPRESENTATIVE_NOT_PASSED:{row.get('question_id')}")
            continue
        receipt_hash = str(row.get("receipt_hash") or "")
        located = receipt_index.get(receipt_hash)
        if located is None:
            failures.append(f"ACCEPTANCE_RECEIPT_MISSING:{row.get('question_id')}")
            continue
        receipt_path, receipt = located
        try:
            artifact_checks.append(_verify_representative(
                harness_root=harness_root,
                row=row,
                receipt_path=receipt_path,
                receipt=receipt,
                security_findings=security_findings,
            ))
        except ValueError as exc:
            failures.append(f"ARTIFACT_REPLAY_FAILED:{row.get('question_id')}:{exc}")

    failures.extend(
        f"SECURITY_FINDING:{item['question_id']}:{item['code']}"
        for item in security_findings
    )
    failure_clusters = _failure_clusters(harness_root, plan)
    security = {
        "schema_version": SCHEMA_VERSION,
        "artifact_type": "security-scan",
        "status": "PASSED" if not security_findings else "FAILED",
        "source_count": len(artifact_checks),
        "secret_pattern_hits": sum(item["code"] == "SECRET_PATTERN" for item in security_findings),
        "sealed_leakage_hits": sum(item["code"] == "SEALED_LEAKAGE" for item in security_findings),
        "arbitrary_execution_hits": sum(item["code"] == "ARBITRARY_EXECUTION" for item in security_findings),
        "network_or_path_hits": sum(item["code"] == "NETWORK_OR_PATH_CAPABILITY" for item in security_findings),
        "findings": security_findings,
    }
    security["artifact_hash"] = canonical_hash(security)
    replay = {
        "schema_version": SCHEMA_VERSION,
        "artifact_type": "replay-results",
        "status": "PASSED" if not [item for item in failures if item.startswith("ARTIFACT_REPLAY_FAILED")] else "FAILED",
        "verified_representative_count": len(artifact_checks),
        "unique_question_hash_count": len({item["question_hash"] for item in artifact_checks}),
        "unique_receipt_hash_count": len({item["receipt_hash"] for item in artifact_checks}),
        "industry_panel_replay_verified": industry.get("replay_verified") is True,
        "resume_drill_verified": str(resume.get("recovery_status", "")).startswith("RESUMED_"),
        "duplicate_receipts": len(artifact_checks) - len({item["receipt_hash"] for item in artifact_checks}),
        "checks_hash": canonical_hash(artifact_checks),
    }
    replay["artifact_hash"] = canonical_hash(replay)
    if replay["duplicate_receipts"]:
        failures.append("DUPLICATE_REPRESENTATIVE_RECEIPT")

    final = {
        "schema_version": SCHEMA_VERSION,
        "artifact_type": "final-verdict",
        "status": "PASSED" if not failures else "FAILED",
        "acceptance_policy": "ADAPTIVE_MINIMUM_25_FAMILY_FULL_20_DIMENSION_COVER",
        "static_question_count": 500,
        "static_question_status": "COVERED_NOT_RUN",
        "full_500_real_execution_status": "NOT_RUN_BY_USER_DECISION",
        "representative_real_execution_count": len(artifact_checks),
        "family_coverage": progress.get("family_passed_count"),
        "dimension_coverage": progress.get("dimension_passed_count"),
        "official_evidence_count": sum(item["authority"] == "OFFICIAL_EVIDENCE" for item in artifact_checks),
        "auxiliary_result_count": sum(item["authority"] == "AUXILIARY_ONLY" for item in artifact_checks),
        "data_source": "APPROVED_PROXY_DATA_VENDOR",
        "official_tushare": False,
        "industry_coverage": industry.get("industry_coverage"),
        "artifact_hashes": {
            "plan": plan["artifact_hash"],
            "progress": progress["artifact_hash"],
            "preflight": preflight["artifact_hash"],
            "industry_panel": industry["artifact_hash"],
            "security": security["artifact_hash"],
            "replay": replay["artifact_hash"],
            "failure_clusters": failure_clusters["artifact_hash"],
        },
        "failure_codes": sorted(set(failures)),
    }
    final["artifact_hash"] = canonical_hash(final)

    _atomic_json(harness_root / "security_scan.json", security)
    _atomic_json(harness_root / "replay_results.json", replay)
    _atomic_json(harness_root / "failure_clusters.json", failure_clusters)
    _atomic_json(harness_root / "final_verdict.json", final)
    _atomic_json(harness_root / "state.json", _final_state(final, progress))
    _atomic_jsonl(harness_root / "repair_history.jsonl", _repair_history(plan, industry))
    return final


def _verify_representative(
    *, harness_root: Path, row: Mapping[str, Any], receipt_path: Path,
    receipt: Mapping[str, Any], security_findings: list[dict[str, str]],
) -> Mapping[str, str]:
    question_id = str(row["question_id"])
    if receipt.get("question_hash") != row.get("question_hash"):
        raise ValueError("question hash mismatch")
    if receipt.get("output_kind") is not None and receipt.get("output_kind") != row.get("required_output_kind"):
        raise ValueError("output contract mismatch")
    expected_authority = "OFFICIAL_EVIDENCE" if row["required_output_kind"] == "ScoreTable" else "AUXILIARY_ONLY"
    if receipt.get("evidence_authority") is not None and receipt.get("evidence_authority") != expected_authority:
        raise ValueError("evidence authority mismatch")
    attempt_root = receipt_path.parent.resolve()
    if harness_root not in attempt_root.parents:
        raise ValueError("acceptance receipt path escaped harness root")
    report_path = Path(str(receipt.get("report_path") or "")).resolve()
    if harness_root not in report_path.parents or not report_path.is_file():
        raise ValueError("report path escaped harness root or is absent")
    report_json = _load_json(report_path.with_suffix(".json"))
    report_hash = report_json.pop("report_hash", None)
    if report_hash != canonical_hash(report_json) or report_hash != receipt.get("report_hash"):
        raise ValueError("report hash mismatch")

    source_hash = str(receipt.get("source_hash") or "")
    if _HASH.fullmatch(source_hash) is None:
        raise ValueError("source hash absent")
    source_path = attempt_root / "draft_sources" / source_hash[7:9] / source_hash[9:]
    source = source_path.read_bytes()
    if "sha256:" + hashlib.sha256(source).hexdigest() != source_hash:
        raise ValueError("source hash mismatch")
    _scan_source(source.decode("utf-8"), question_id=question_id, findings=security_findings)

    experiment_files = tuple((attempt_root / "research_runs").glob("*/experiments/*"))
    if len(experiment_files) != 1:
        raise ValueError("representative must have exactly one committed experiment")
    experiment = experiment_files[0]
    review = _load_json(experiment / "review.json")
    if canonical_hash(review) != receipt.get("review_hash"):
        raise ValueError("review hash mismatch")
    if review.get("status") != "ACCEPTED" or review.get("grants_execution_authority") is not False:
        raise ValueError("review authority boundary invalid")
    runner = _load_json(experiment / "runner.json")
    if runner.get("artifact_hash") != receipt.get("runner_receipt_hash") or _HASH.fullmatch(str(runner.get("artifact_hash") or "")) is None:
        raise ValueError("runner hash mismatch")
    if runner.get("output_kind") != row.get("required_output_kind"):
        raise ValueError("runner output contract mismatch")
    if runner.get("isolation_level") != "OS_SANDBOXED" or _HASH.fullmatch(str(runner.get("image_id") or "")) is None:
        raise ValueError("runner isolation identity invalid")
    evidence = _load_json(experiment / "evidence.json")
    if evidence.get("artifact_hash") != receipt.get("evidence_result_hash") or _HASH.fullmatch(str(evidence.get("artifact_hash") or "")) is None:
        raise ValueError("evidence result hash mismatch")
    if evidence.get("status") != expected_authority:
        raise ValueError("evidence authority mismatch")
    if evidence.get("official_evidence_hash") != receipt.get("official_evidence_hash"):
        raise ValueError("official evidence hash mismatch")
    if expected_authority == "OFFICIAL_EVIDENCE" and runner.get("official_evidence_eligible") is not True:
        raise ValueError("official evidence used an ineligible runner")
    _scan_text_artifacts(attempt_root, question_id=question_id, findings=security_findings)
    return {
        "question_id": question_id,
        "question_hash": str(row["question_hash"]),
        "receipt_hash": str(row["receipt_hash"]),
        "source_hash": source_hash,
        "authority": expected_authority,
        "report_hash": str(receipt["report_hash"]),
    }


def _scan_source(source: str, *, question_id: str, findings: list[dict[str, str]]) -> None:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        findings.append({"question_id": question_id, "code": "ARBITRARY_EXECUTION"})
        return
    for node in ast.walk(tree):
        roots: Iterable[str] = ()
        if isinstance(node, ast.Import):
            roots = (item.name.split(".", 1)[0] for item in node.names)
        elif isinstance(node, ast.ImportFrom):
            roots = ((node.module or "").split(".", 1)[0],)
        if any(root in _FORBIDDEN_IMPORT_ROOTS for root in roots):
            findings.append({"question_id": question_id, "code": "NETWORK_OR_PATH_CAPABILITY"})
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _FORBIDDEN_CALLS:
            findings.append({"question_id": question_id, "code": "ARBITRARY_EXECUTION"})


def _scan_text_artifacts(root: Path, *, question_id: str, findings: list[dict[str, str]]) -> None:
    seen = set()
    for path in root.rglob("*"):
        if not path.is_file() or path.stat().st_size > 10_000_000:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for code, patterns in (("SECRET_PATTERN", _SECRET_PATTERNS), ("SEALED_LEAKAGE", _SEALED_PATTERNS)):
            if any(pattern.search(text) for pattern in patterns) and code not in seen:
                findings.append({"question_id": question_id, "code": code})
                seen.add(code)


def _receipt_index(root: Path) -> dict[str, tuple[Path, Mapping[str, Any]]]:
    result = {}
    for path in root.rglob("acceptance_receipt.json"):
        value = _load_hashed(path)
        receipt_hash = value["artifact_hash"]
        existing = result.get(receipt_hash)
        if existing is not None and existing[0] != path:
            raise ValueError("duplicate acceptance receipt hash at multiple paths")
        result[receipt_hash] = (path, value)
    return result


def _failure_clusters(root: Path, plan: Mapping[str, Any]) -> dict[str, Any]:
    codes: dict[str, int] = {}
    for item in plan.get("superseded_rows", ()):
        code = str(item.get("failure_code") or item.get("reason_code") or "UNKNOWN")
        codes[code] = codes.get(code, 0) + 1
    for path in (root / "representative_runs").rglob("acceptance_receipt.json"):
        value = _load_hashed(path)
        if value.get("status") == "BLOCKED":
            code = str(value.get("failure_code") or "UNKNOWN")
            codes[code] = codes.get(code, 0) + 1
    result = {
        "schema_version": SCHEMA_VERSION,
        "artifact_type": "failure-clusters",
        "clusters": [{"failure_code": code, "count": count} for code, count in sorted(codes.items())],
        "all_final_representatives_passed": all(row.get("status") == "PASSED" for row in plan.get("rows", ())),
    }
    result["artifact_hash"] = canonical_hash(result)
    return result


def _repair_history(plan: Mapping[str, Any], industry: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows = [{
        "event_type": "REPRESENTATIVE_REPLACED",
        "question_id": item["question_id"],
        "replacement_question_id": item["replacement_question_id"],
        "reason_code": item["reason_code"],
    } for item in plan.get("superseded_rows", ())]
    rows.append({
        "event_type": "DATED_INDUSTRY_PANEL_QUALIFIED",
        "industry_coverage": industry["industry_coverage"],
        "history_hash": industry["industry_history_hash"],
        "data_hash": industry["data_hash"],
    })
    for row in rows:
        row["event_hash"] = canonical_hash(row)
    return rows


def _final_state(final: Mapping[str, Any], progress: Mapping[str, Any]) -> Mapping[str, Any]:
    value = {
        "schema_version": "alphaforge.research500-harness-state/v2",
        "status": final["status"],
        "current_phase": "COMPLETE" if final["status"] == "PASSED" else "BLOCKED",
        "acceptance_policy": final["acceptance_policy"],
        "gates": {
            "static_500": "COVERED_NOT_RUN",
            "representative_25": "PASSED" if progress.get("passed_count") == 25 else "FAILED",
            "dimensions_20": "PASSED" if progress.get("dimension_passed_count") == 20 else "FAILED",
            "security": "PASSED" if not final["failure_codes"] else "FAILED",
            "replay": "PASSED" if not final["failure_codes"] else "FAILED",
            "full_500_real_execution": "NOT_RUN_BY_USER_DECISION",
        },
        "counts": {
            "static_questions": 500,
            "representatives_passed": progress.get("passed_count"),
            "families_passed": progress.get("family_passed_count"),
            "dimensions_passed": progress.get("dimension_passed_count"),
        },
        "final_verdict_hash": final["artifact_hash"],
        "known_failures": list(final["failure_codes"]),
        "next_action": "REVIEW_REPORTS_OR_RUN_USER_SELECTED_QUESTIONS" if final["status"] == "PASSED" else "REPAIR_TYPED_FAILURES",
    }
    value["artifact_hash"] = canonical_hash(value)
    return value


def _load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON object required: {path.name}")
    return value


def _load_hashed(path: Path) -> dict[str, Any]:
    value = _load_json(path)
    claimed = value.pop("artifact_hash", None)
    if claimed != canonical_hash(value):
        raise ValueError(f"artifact hash mismatch: {path.name}")
    value["artifact_hash"] = claimed
    return value


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _atomic_jsonl(path: Path, values: Iterable[Mapping[str, Any]]) -> None:
    content = "".join(json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True) + "\n" for value in values)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Finalize the adaptive Research500 Harness")
    parser.add_argument("--root", required=True)
    args = parser.parse_args(argv)
    result = finalize_representative_harness(root=args.root)
    print(json.dumps(result, ensure_ascii=False, allow_nan=False, sort_keys=True))
    return 0 if result["status"] == "PASSED" else 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = ["finalize_representative_harness"]
