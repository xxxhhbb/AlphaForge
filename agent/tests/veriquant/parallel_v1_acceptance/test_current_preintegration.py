from __future__ import annotations

import json
from pathlib import Path

from src.veriquant.v1_acceptance.api import AcceptanceFailureCode, AcceptanceStatus, DataSourceClass, V1GateId
from src.veriquant.v1_acceptance.preintegration import build_current_preintegration_verdict


def _gate(verdict, gate_id: V1GateId):
    return next(item for item in verdict.gates if item.gate_id is gate_id)


def test_current_v1_intake_is_honest_non_passing_summary() -> None:
    repo_root = Path(__file__).resolve().parents[4]
    verdict = build_current_preintegration_verdict(repo_root)
    statuses = {gate.gate_id: gate.status for gate in verdict.gates}

    assert verdict.overall_status is AcceptanceStatus.FAILED
    assert statuses[V1GateId.UI_WORKBENCH_READONLY] is AcceptanceStatus.IMPLEMENTED_NOT_RUN
    assert statuses[V1GateId.OPERATOR_API_SECURITY] is AcceptanceStatus.IMPLEMENTED_AND_TESTED
    assert statuses[V1GateId.DATA_TO_PROMOTION_CHAIN] is AcceptanceStatus.IMPLEMENTED_NOT_RUN
    assert statuses[V1GateId.SHADOW_REPLAY_20_SESSION] is AcceptanceStatus.UNAVAILABLE
    assert statuses[V1GateId.SECURITY_SCAN] is AcceptanceStatus.FAILED
    assert statuses[V1GateId.REPRODUCIBILITY_RESTART_RESUME] is AcceptanceStatus.IMPLEMENTED_NOT_RUN
    assert statuses[V1GateId.DOCS_RUNBOOK_REPORT] is AcceptanceStatus.IMPLEMENTED_AND_TESTED


def test_current_intake_exposes_exact_earliest_blockers_and_proxy_class() -> None:
    repo_root = Path(__file__).resolve().parents[4]
    verdict = build_current_preintegration_verdict(repo_root)

    ui = _gate(verdict, V1GateId.UI_WORKBENCH_READONLY)
    data = _gate(verdict, V1GateId.DATA_TO_PROMOTION_CHAIN)
    replay = _gate(verdict, V1GateId.SHADOW_REPLAY_20_SESSION)
    security = _gate(verdict, V1GateId.SECURITY_SCAN)
    recovery = _gate(verdict, V1GateId.REPRODUCIBILITY_RESTART_RESUME)

    assert ui.earliest_blocker == AcceptanceFailureCode.FRONTEND_RUNTIME_NOT_RUN.value
    assert "ui_workbench_readonly.frontend_runtime_started=true" in ui.missing_evidence
    assert data.data_source_class is DataSourceClass.APPROVED_PROXY_DATA_VENDOR
    assert data.earliest_blocker == AcceptanceFailureCode.OFFICIAL_EVIDENCE_NOT_RUN.value
    assert AcceptanceFailureCode.APPROVED_PROXY_MASQUERADE_AS_OFFICIAL not in data.failure_codes
    assert replay.earliest_blocker == AcceptanceFailureCode.CHALLENGER_MISSING.value
    assert security.earliest_blocker == AcceptanceFailureCode.LIVE_ROUTE_PRESENT.value
    assert recovery.earliest_blocker == AcceptanceFailureCode.RESUME_DRILL_MISSING.value


def test_current_intake_parses_result_json_without_modifying_total_state() -> None:
    repo_root = Path(__file__).resolve().parents[4]
    state_path = repo_root / ".autobuild/veriquant/state.json"
    before = state_path.read_bytes()

    verdict = build_current_preintegration_verdict(repo_root)
    result_paths = [
        repo_root / ".autobuild/veriquant/parallel/workbench/result.json",
        repo_root / ".autobuild/veriquant/parallel/v1_security/result.json",
        repo_root / ".autobuild/veriquant/integration/v1_real_data/result.json",
    ]
    for path in result_paths:
        parsed = json.loads(path.read_text(encoding="utf-8-sig"))
        assert isinstance(parsed, dict)
        assert parsed.get("status") in {"READY_FOR_INTEGRATION", "BLOCKED"}

    assert verdict.schema_version == "veriquant.v1-acceptance/v2"
    assert state_path.read_bytes() == before
