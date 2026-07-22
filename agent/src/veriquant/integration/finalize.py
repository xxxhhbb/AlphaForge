"""Atomic final acceptance/state writer for VeriQuant parallel integration."""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo


ALLOWED_ACCEPTANCE_STATUSES = frozenset(
    {
        "IMPLEMENTED_AND_TESTED",
        "IMPLEMENTED_NOT_RUN",
        "UNAVAILABLE",
        "DEFERRED",
        "FAILED",
    }
)


def _sha256_file(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    """Write JSON by same-directory fsync + replace, leaving no partial target."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _prompt_records(completed_at: str) -> list[dict[str, Any]]:
    return [
        {
            "id": "prompt_11",
            "status": "IMPLEMENTED_AND_TESTED",
            "implemented": "IMPLEMENTED_AND_TESTED",
            "tested": "IMPLEMENTED_AND_TESTED",
            "smoke": "UNAVAILABLE",
            "integration_status": "IMPLEMENTED_AND_TESTED",
            "reason": (
                "Agent A offline Real Research Bridge is integrated and tested; a successful "
                "real Prompt 10/Tushare frozen-snapshot smoke remains unavailable."
            ),
            "completed_at": completed_at,
        },
        {
            "id": "prompt_12",
            "status": "IMPLEMENTED_AND_TESTED",
            "implemented": "IMPLEMENTED_AND_TESTED",
            "tested": "IMPLEMENTED_AND_TESTED",
            "smoke": "IMPLEMENTED_AND_TESTED",
            "integration_status": "IMPLEMENTED_AND_TESTED",
            "reason": "Official visible Evidence is integrated with the statistical sealed promotion gate.",
            "completed_at": completed_at,
        },
        {
            "id": "prompt_13",
            "status": "IMPLEMENTED_AND_TESTED",
            "implemented": "IMPLEMENTED_AND_TESTED",
            "tested": "IMPLEMENTED_AND_TESTED",
            "smoke": "IMPLEMENTED_AND_TESTED",
            "integration_status": "IMPLEMENTED_AND_TESTED",
            "reason": (
                "Six bounded desks are integrated with the deterministic provider; a real LLM "
                "provider is intentionally unavailable and is not required."
            ),
            "completed_at": completed_at,
        },
        {
            "id": "prompt_14",
            "status": "IMPLEMENTED_AND_TESTED",
            "implemented": "IMPLEMENTED_AND_TESTED",
            "tested": "IMPLEMENTED_AND_TESTED",
            "smoke": "IMPLEMENTED_AND_TESTED",
            "integration_status": "IMPLEMENTED_AND_TESTED",
            "reason": (
                "Frozen cited documents are integrated through DRAFT-only Proposal Adapter, "
                "FactorDAG, PIT and visible Evidence; network fetch remains disabled."
            ),
            "completed_at": completed_at,
        },
        {
            "id": "prompt_15",
            "status": "DEFERRED",
            "implemented": "DEFERRED",
            "tested": "DEFERRED",
            "smoke": "DEFERRED",
            "integration_status": "DEFERRED",
            "reason": "Prompt 15 scheduler/live operations remain explicitly out of scope.",
            "completed_at": completed_at,
        },
        {
            "id": "prompt_17",
            "status": "IMPLEMENTED_AND_TESTED",
            "implemented": "IMPLEMENTED_AND_TESTED",
            "tested": "IMPLEMENTED_AND_TESTED",
            "smoke": "IMPLEMENTED_AND_TESTED",
            "integration_status": "IMPLEMENTED_AND_TESTED",
            "reason": (
                "CHALLENGER to paper-only one-day Shadow Portfolio run_once is integrated, "
                "tested and idempotent; the V1 20-session durable replay gate is unavailable."
            ),
            "completed_at": completed_at,
        },
        {
            "id": "prompt_18",
            "status": "IMPLEMENTED_NOT_RUN",
            "implemented": "IMPLEMENTED_AND_TESTED",
            "tested": "IMPLEMENTED_NOT_RUN",
            "smoke": "IMPLEMENTED_AND_TESTED",
            "integration_status": "IMPLEMENTED_NOT_RUN",
            "reason": (
                "Workbench repository, redaction, API/router/nav/i18n wiring and repository smoke "
                "are implemented; actual FastAPI and frontend Vitest runs are unavailable locally."
            ),
            "completed_at": completed_at,
        },
    ]


def _replace_prompt_records(
    history: list[Any], records: list[dict[str, Any]]
) -> list[Any]:
    replacement_ids = {record["id"] for record in records}
    preserved = [
        item
        for item in history
        if not (isinstance(item, dict) and item.get("id") in replacement_ids)
    ]
    preserved.extend(records)
    return preserved


def _test_records() -> list[dict[str, str]]:
    return [
        {
            "name": "integration_p11_p18_ready_package_tests",
            "status": "IMPLEMENTED_AND_TESTED",
            "evidence": (
                "Fresh isolated A-F rerun: real_research 7, statistical_promotion 11, "
                "llm_desks 15, research_memory 18, shadow_portfolio 13, workbench Python 18; "
                "82 passed total, no pass claimed for frontend Vitest."
            ),
        },
        {
            "name": "integration_p11_p18_hooks_and_e2e",
            "status": "IMPLEMENTED_AND_TESTED",
            "evidence": (
                "python -m pytest -q -p no:cacheprovider --confcutdir=tests/veriquant "
                "tests/veriquant/test_integration_acceptance.py "
                "tests/veriquant/test_integration_hooks.py tests/veriquant/test_integration_e2e.py "
                "=> 21 passed in 1.02s"
            ),
        },
        {
            "name": "integration_p11_p18_full_veriquant",
            "status": "IMPLEMENTED_AND_TESTED",
            "evidence": (
                "python -m pytest -q -p no:cacheprovider --confcutdir=tests/veriquant "
                "tests/veriquant => 258 passed in 8.39s"
            ),
        },
        {
            "name": "integration_p11_p18_offline_demo",
            "status": "IMPLEMENTED_AND_TESTED",
            "evidence": (
                "python -m src.veriquant.integration.demo --offline --seed 7 ran twice, exit 0, "
                "artifact hash sha256:7a0539561338c4ef786ddfcf1d107f66e72af57dabf54403c23a1a31a076eccf"
            ),
        },
        {
            "name": "integration_p11_p18_api_runtime",
            "status": "UNAVAILABLE",
            "evidence": (
                "python -m pytest -q -p no:cacheprovider tests/test_api_infrastructure.py "
                "failed during collection: ModuleNotFoundError: No module named 'fastapi'."
            ),
        },
        {
            "name": "integration_p11_p18_frontend_runtime",
            "status": "IMPLEMENTED_NOT_RUN",
            "evidence": (
                "pnpm offline Vitest did not start because local frontend dependencies/metadata, "
                "including @tailwindcss/typography, are absent; no network install was attempted."
            ),
        },
        {
            "name": "integration_p11_p18_security_scans",
            "status": "IMPLEMENTED_AND_TESTED",
            "evidence": (
                "Secret, production Workbench sealed leakage, arbitrary executable factor, "
                "unauthorized integration tool, and integration TODO/stub/skip/xfail scans: 0 matches; "
                "path traversal and unknown artifact rejection tests passed."
            ),
        },
        {
            "name": "v1_real_ashare_smoke",
            "status": "UNAVAILABLE",
            "evidence": (
                "Frozen SSE/SZSE calendar exists, but isolated Tushare attempt returned typed "
                "ADAPTER_UNAVAILABLE because credentials are not configured; no successful real snapshot."
            ),
        },
        {
            "name": "v1_twenty_session_shadow_replay",
            "status": "UNAVAILABLE",
            "evidence": (
                "One-day ShadowPortfolioService.run_once is tested and idempotent; no durable "
                "20-session vintage/revision/checkpoint replay exists."
            ),
        },
    ]


def finalize(repo_root: Path, *, completed_at: str | None = None) -> dict[str, Any]:
    root = repo_root.resolve()
    state_path = root / ".autobuild" / "veriquant" / "state.json"
    final_path = root / ".autobuild" / "veriquant" / "integration" / "final_acceptance.json"
    demo_path = root / ".autobuild" / "veriquant" / "integration" / "v1-offline-seed-7" / "result.json"
    intake_path = root / ".autobuild" / "veriquant" / "integration" / "acceptance_result.json"
    report_path = root / "docs" / "veriquant" / "INTEGRATION_REPORT_P11_P18.md"

    if not state_path.is_file() or not demo_path.is_file() or not intake_path.is_file() or not report_path.is_file():
        raise FileNotFoundError("required state/intake/demo/report artifact is missing")

    timestamp = completed_at or datetime.now(ZoneInfo("Asia/Shanghai")).isoformat()
    state = json.loads(state_path.read_text(encoding="utf-8"))
    demo = json.loads(demo_path.read_text(encoding="utf-8"))
    intake = json.loads(intake_path.read_text(encoding="utf-8"))

    package_ids = sorted(
        package["package_id"]
        for package in intake["capability_packages"]
        if package.get("eligible") is True
    )
    if package_ids != [
        "llm_desks",
        "real_research",
        "research_memory",
        "shadow_portfolio",
        "statistical_promotion",
        "workbench",
    ]:
        raise ValueError("all six exact READY packages are required for finalization")
    if demo.get("status") != "PASSED" or demo.get("promotion", {}).get("state") != "CHALLENGER":
        raise ValueError("offline demo invariant failed")

    final_acceptance: dict[str, Any] = {
        "schema_version": "veriquant-integration-acceptance/v1",
        "status": "FAILED",
        "generated_at": timestamp,
        "reason": "V1 gates 2, 3, application-wide 4, and full 5 are incomplete.",
        "ready_packages": package_ids,
        "package_test_count": 82,
        "integration_test_count": 21,
        "full_veriquant_test_count": 258,
        "offline_demo": {
            "status": "IMPLEMENTED_AND_TESTED",
            "command": "python -m src.veriquant.integration.demo --offline --seed 7",
            "artifact_hash": demo["artifact_hash"],
            "result": ".autobuild/veriquant/integration/v1-offline-seed-7/result.json",
            "report": ".autobuild/veriquant/integration/v1-offline-seed-7/report.md",
        },
        "v1_gates": {
            "offline_end_to_end": "IMPLEMENTED_AND_TESTED",
            "real_ashare_smoke": "UNAVAILABLE",
            "twenty_session_shadow_replay": "UNAVAILABLE",
            "security_authority": "FAILED",
            "recoverability_reproducibility": "IMPLEMENTED_NOT_RUN",
        },
        "security_authority_reason": (
            "VeriQuant namespace tests pass, but agent/api_server.py still registers the pre-existing "
            "src.api.live_routes application-wide live runner/order surface."
        ),
        "prompt_15": "DEFERRED",
        "availability": demo["capability_availability"],
        "artifact_hashes": demo["artifact_hashes"],
    }
    for value in final_acceptance["v1_gates"].values():
        if value not in ALLOWED_ACCEPTANCE_STATUSES:
            raise ValueError(f"invalid acceptance status: {value}")
    atomic_write_json(final_path, final_acceptance)

    prompt_records = _prompt_records(timestamp)
    state["prompt_history"] = _replace_prompt_records(state.get("prompt_history", []), prompt_records)
    state["current_prompt"] = {
        "id": "integration_p11_p18",
        "status": "FAILED",
        "scope": (
            "A-F thin integration is implemented and offline-tested; aggregate operational V1 "
            "fails mandatory real-data, 20-session replay, application-wide live-route isolation, "
            "and interruption/resume gates."
        ),
        "started_at": state.get("current_prompt", {}).get("started_at", timestamp),
        "completed_at": timestamp,
        "repair_round_limit": 3,
        "repair_rounds_used": 1,
    }

    state["parallel_integration"] = {
        "status": "FAILED",
        "ready_packages": package_ids,
        "ready_package_count": 6,
        "package_test_count": 82,
        "integration_test_count": 21,
        "full_veriquant_test_count": 258,
        "demo_artifact_hash": demo["artifact_hash"],
        "demo_result": ".autobuild/veriquant/integration/v1-offline-seed-7/result.json",
        "workbench_root": ".autobuild/veriquant/integration/workbench_artifacts",
        "final_acceptance": ".autobuild/veriquant/integration/final_acceptance.json",
        "v1_gates": final_acceptance["v1_gates"],
        "availability": demo["capability_availability"],
        "prompt_states": {record["id"]: record["status"] for record in prompt_records},
    }

    prior_tests = [
        item
        for item in state.get("tests", [])
        if not (isinstance(item, dict) and str(item.get("name", "")).startswith("integration_"))
        and not (isinstance(item, dict) and str(item.get("name", "")).startswith("v1_"))
    ]
    state["tests"] = prior_tests + _test_records()

    changed = [
        ".autobuild/veriquant/state.json",
        ".autobuild/veriquant/integration/acceptance_result.json",
        ".autobuild/veriquant/integration/final_acceptance.json",
        ".autobuild/veriquant/integration/v1-offline-seed-7/result.json",
        ".autobuild/veriquant/integration/v1-offline-seed-7/report.md",
        ".autobuild/veriquant/integration/workbench_artifacts/index.json",
        "agent/src/veriquant/integration/__init__.py",
        "agent/src/veriquant/integration/acceptance.py",
        "agent/src/veriquant/integration/demo.py",
        "agent/src/veriquant/integration/e2e.py",
        "agent/src/veriquant/integration/finalize.py",
        "agent/src/veriquant/integration/workbench.py",
        "agent/src/veriquant/integration/llm_desks.py",
        "agent/src/veriquant/integration/research_memory.py",
        "agent/src/veriquant/integration/statistical.py",
        "agent/src/veriquant/integration/statistical.py",
        "agent/api_server.py",
        "agent/tests/veriquant/test_integration_acceptance.py",
        "agent/tests/veriquant/test_integration_e2e.py",
        "agent/tests/veriquant/test_integration_hooks.py",
        "frontend/src/router.tsx",
        "frontend/src/components/layout/Layout.tsx",
        "frontend/src/i18n/locales/en.json",
        "frontend/src/i18n/locales/zh-CN.json",
        "frontend/src/i18n/locales/ja.json",
        "frontend/src/i18n/locales/ko.json",
        "frontend/src/i18n/locales/ar.json",
        "docs/veriquant/INTEGRATION_REPORT_P11_P18.md",
    ]
    state["files_changed"] = list(dict.fromkeys(state.get("files_changed", []) + changed))

    artifact_hashes = dict(state.get("artifact_hashes", {}))
    for relative in changed:
        candidate = root / relative
        if candidate.is_file() and relative != ".autobuild/veriquant/state.json":
            artifact_hashes[relative] = _sha256_file(candidate)
    state["artifact_hashes"] = artifact_hashes

    stale_fragments = (
        "real_research_bridge, shadow_portfolio, and workbench_artifacts READY result.json files are absent",
        "WorkBench E2E remains BLOCKED because Agent F",
        "Workbench E2E remains BLOCKED because Agent F",
        "frontend local tests were not run because no frontend route/nav/i18n",
    )
    known = [
        item
        for item in state.get("known_failures", [])
        if not any(fragment.lower() in str(item).lower() for fragment in stale_fragments)
    ]
    current_failures = [
        "No successful real Tushare frozen snapshot or real internal/Qlib data runner artifact exists; V1 Gate 2 is UNAVAILABLE.",
        "No durable 20-session Shadow Portfolio replay/checkpoint controller exists; V1 Gate 3 is UNAVAILABLE.",
        "The pre-existing application API registers src.api.live_routes with live runner/order surfaces; VeriQuant did not use it, but the application-wide no-live-order claim FAILS.",
        "Mid-run interruption/resume and durable cross-process replay are not implemented; V1 Gate 5 is only partially tested.",
        "FastAPI runtime tests are UNAVAILABLE in this process and frontend Vitest is IMPLEMENTED_NOT_RUN because locked local dependencies are absent.",
    ]
    state["known_failures"] = list(dict.fromkeys(known + current_failures))
    state["next_action"] = {
        "status": "FAILED",
        "action": (
            "Securely configure the bounded Tushare provider, produce and bridge one real frozen "
            "snapshot, implement durable 20-session shadow replay plus checkpoints/resume, decide "
            "how a VeriQuant-only deployment excludes pre-existing live routes, then rerun API/frontend/security acceptance."
        ),
    }

    scope = dict(state.get("scope_guard", {}))
    scope.update(
        {
            "integration_p11_p18_started": True,
            "integration_p11_p18_passed": False,
            "ready_parallel_packages": 6,
            "public_api_route_connected": True,
            "frontend_workbench_connected": True,
            "offline_end_to_end_demo_only": True,
            "offline_a_to_f_implemented_and_tested": True,
            "real_tushare_snapshot_run": False,
            "real_pyqlib_run": False,
            "twenty_session_shadow_replay": False,
            "midrun_resume_tested": False,
            "prompt_15_deferred": True,
            "live_broker_path_modified": False,
            "veriquant_real_broker_enabled": False,
            "application_live_routes_present": True,
            "secrets_accessed": False,
            "network_used": False,
        }
    )
    state["scope_guard"] = scope
    state["state_write"] = {
        "atomic": True,
        "method": "same-directory temporary file, fsync, os.replace",
        "updated_at": timestamp,
        "writer": "VeriQuant Integration Agent",
    }

    atomic_write_json(state_path, state)
    return final_acceptance


def main() -> int:
    repo_root = Path(__file__).resolve().parents[4]
    result = finalize(repo_root)
    print(json.dumps(result, ensure_ascii=True, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
