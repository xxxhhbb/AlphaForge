from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from src.veriquant.acceptance_reconcile import (
    AcceptanceReconcileFailureCode,
    AcceptanceReconciler,
)


FINAL_PATH = ".autobuild/veriquant/integration/final_acceptance.json"
UI_PATH = ".autobuild/veriquant/integration/ui_acceptance/acceptance_verdict.json"
C8_RESULT_PATH = (
    ".autobuild/veriquant/integration/c8_real_ashare_v1/"
    "c8-real-ashare-v1/result.json"
)
C8_REPORT_PATH = (
    ".autobuild/veriquant/integration/c8_real_ashare_v1/"
    "c8-real-ashare-v1/report.md"
)
STATE_PATH = ".autobuild/veriquant/state.json"


def test_old_final_failed_new_c8_passed_needs_rebase_not_v1_passed(
    tmp_path: Path,
) -> None:
    _build_fixture(tmp_path)

    verdict = AcceptanceReconciler(tmp_path).reconcile()

    assert verdict.verdict == "NEEDS_FINAL_ACCEPTANCE_REBASE"
    assert verdict.verdict != "V1_PASSED"
    assert all(status == "IMPLEMENTED_AND_TESTED" for status in verdict.gates.values())
    assert any(conflict.stale for conflict in verdict.conflicts)
    assert not verdict.blockers


def test_missing_c8_result_or_hash_fails_closed(tmp_path: Path) -> None:
    _build_fixture(tmp_path, include_c8=False)

    verdict = AcceptanceReconciler(tmp_path).reconcile()

    codes = {blocker["code"] for blocker in verdict.blockers}
    assert verdict.verdict == "FAILED_CLOSED"
    assert AcceptanceReconcileFailureCode.MISSING_C8_RESULT.value in codes

    _build_fixture(tmp_path, include_c8=True, include_c8_hash=False)
    missing_hash_verdict = AcceptanceReconciler(tmp_path).reconcile()
    missing_hash_codes = {blocker["code"] for blocker in missing_hash_verdict.blockers}
    assert missing_hash_verdict.verdict == "FAILED_CLOSED"
    assert AcceptanceReconcileFailureCode.MISSING_ARTIFACT_HASH.value in missing_hash_codes


def test_stale_ui_blocker_is_listed_without_overriding_c8_security_fact(
    tmp_path: Path,
) -> None:
    _build_fixture(tmp_path)

    verdict = AcceptanceReconciler(tmp_path).reconcile()

    stale_ui_conflicts = [
        conflict
        for conflict in verdict.conflicts
        if conflict.conflict_id == "ui_acceptance.security_authority"
    ]
    assert stale_ui_conflicts
    assert stale_ui_conflicts[0].stale is True
    assert verdict.gates["security_authority"] == "IMPLEMENTED_AND_TESTED"


def test_synthetic_demo_cannot_masquerade_as_real_smoke(tmp_path: Path) -> None:
    _build_fixture(
        tmp_path,
        c8_overrides={
            "data_source_class": "SYNTHETIC_FIXTURE",
            "source_class": "SYNTHETIC_FIXTURE",
        },
        state_scope_overrides={
            "approved_proxy_data_vendor": False,
            "real_approved_proxy_snapshot_run": False,
        },
    )

    verdict = AcceptanceReconciler(tmp_path).reconcile()

    codes = {blocker["code"] for blocker in verdict.blockers}
    assert verdict.verdict == "FAILED_CLOSED"
    assert verdict.gates["approved_proxy_real_ashare_smoke"] != "IMPLEMENTED_AND_TESTED"
    assert AcceptanceReconcileFailureCode.SYNTHETIC_NOT_REAL.value in codes


def test_official_tushare_label_fails_closed_for_approved_proxy_evidence(
    tmp_path: Path,
) -> None:
    _build_fixture(tmp_path, c8_overrides={"data_source_class": "OFFICIAL_TUSHARE"})

    verdict = AcceptanceReconciler(tmp_path).reconcile()

    codes = {blocker["code"] for blocker in verdict.blockers}
    assert verdict.verdict == "FAILED_CLOSED"
    assert AcceptanceReconcileFailureCode.FORBIDDEN_OFFICIAL_TUSHARE.value in codes


def test_same_inputs_generate_same_reconciliation_report_hash(tmp_path: Path) -> None:
    _build_fixture(tmp_path)

    first = AcceptanceReconciler(tmp_path).reconcile()
    second = AcceptanceReconciler(tmp_path).reconcile()

    assert first.report_hash == second.report_hash
    assert first.to_dict()["report_hash"] == second.to_dict()["report_hash"]


def _build_fixture(
    root: Path,
    *,
    include_c8: bool = True,
    include_c8_hash: bool = True,
    c8_overrides: dict[str, Any] | None = None,
    state_scope_overrides: dict[str, Any] | None = None,
) -> None:
    final = {
        "schema_version": "fixture/v1",
        "status": "FAILED",
        "v1_gates": {
            "offline_end_to_end": "IMPLEMENTED_AND_TESTED",
            "real_ashare_smoke": "UNAVAILABLE",
            "twenty_session_shadow_replay": "UNAVAILABLE",
            "security_authority": "FAILED",
            "recoverability_reproducibility": "IMPLEMENTED_NOT_RUN",
        },
    }
    ui = {
        "schema_version": "fixture/v1",
        "overall_status": "FAILED",
        "gates": {
            "offline_end_to_end": {"status": "IMPLEMENTED_AND_TESTED"},
            "real_ashare_smoke": {"status": "IMPLEMENTED_AND_TESTED"},
            "twenty_session_shadow_replay": {"status": "UNAVAILABLE"},
            "security_authority": {"status": "FAILED"},
            "recoverability_reproducibility": {"status": "IMPLEMENTED_NOT_RUN"},
        },
    }
    c8 = {
        "schema_version": "fixture/v1",
        "status": "PASSED",
        "artifact_hash": "sha256:" + "a" * 64,
        "data_source_class": "APPROVED_PROXY_DATA_VENDOR",
        "source_class": "TUSHARE_COMPATIBLE_PROXY",
        "official_tushare_source": False,
        "snapshot": {
            "snapshot_hash": "sha256:" + "b" * 64,
            "calendar_hash": "sha256:" + "c" * 64,
            "universe_hash": "sha256:" + "d" * 64,
            "row_count": 116,
        },
        "promotion": {"promotion_run": True, "challenger_created": True},
        "shadow_replay": {
            "sessions_committed": 20,
            "daily_vintage_count": 20,
            "forced_interruption": True,
            "resume_completed": True,
            "rerun_no_duplicate_journal": True,
            "journal_tip_hash": "sha256:" + "e" * 64,
        },
        "security": {"status": "PASSED"},
        "invariants": {"security_probe_passed": True},
    }
    if c8_overrides:
        c8.update(c8_overrides)

    _write_json(root / FINAL_PATH, final)
    _write_json(root / UI_PATH, ui)
    if include_c8:
        _write_json(root / C8_RESULT_PATH, c8)
    _write_text(root / C8_REPORT_PATH, "# C8\n\nSource: APPROVED_PROXY_DATA_VENDOR\n")

    artifact_hashes = {
        FINAL_PATH: _hash(root / FINAL_PATH),
        UI_PATH: _hash(root / UI_PATH),
        C8_REPORT_PATH: _hash(root / C8_REPORT_PATH),
    }
    if include_c8 and include_c8_hash:
        artifact_hashes[C8_RESULT_PATH] = _hash(root / C8_RESULT_PATH)

    scope_guard = {
        "approved_proxy_data_vendor": True,
        "official_tushare_source": False,
        "real_approved_proxy_snapshot_run": True,
        "real_approved_proxy_official_visible_evidence_run": True,
        "real_statistical_promotion_run": True,
        "real_challenger_created": True,
        "twenty_session_shadow_replay": True,
        "midrun_resume_tested": True,
        "shadow_replay_rerun_no_duplicate_journal": True,
        "standalone_veriquant_api_no_live_routes": True,
        "veriquant_real_broker_enabled": False,
        "real_orders": False,
        "real_funds": False,
        "secrets_accessed": False,
    }
    if state_scope_overrides:
        scope_guard.update(state_scope_overrides)

    state = {
        "schema_version": "veriquant-build-state/v1",
        "scope_guard": scope_guard,
        "tests": {
            "integration_p11_p18_offline_demo": {
                "status": "PASSED",
                "command": "python -m veriquant.integration.demo --offline",
            }
        },
        "artifact_hashes": artifact_hashes,
    }
    _write_json(root / STATE_PATH, state)


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


def _write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")


def _hash(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
