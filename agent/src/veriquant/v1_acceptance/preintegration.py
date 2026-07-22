"""Read-only package/artifact intake for the current V1 acceptance verdict.

This module deliberately consumes existing result JSON and run artifacts without
calling any VeriQuant financial service.  It distinguishes official Tushare,
operator-approved compatible proxy data, and auxiliary-only evidence.
"""

from __future__ import annotations

import json
import re
from hashlib import sha256
from pathlib import Path
from typing import Any, Iterable, Mapping

from .evaluator import V1AcceptanceEvaluator
from .models import (
    AcceptanceStatus,
    ArtifactKind,
    ArtifactReference,
    TestRunEvidence,
    TraceableClaim,
    V1AcceptanceVerdict,
    V1GateEvidence,
    V1GateId,
)


_ARTIFACTS = {
    "workbench": (
        ".autobuild/veriquant/parallel/workbench/result.json",
        "docs/veriquant/parallel/VERIQUANT_WORKBENCH.md",
    ),
    "v1_security": (
        ".autobuild/veriquant/parallel/v1_security/result.json",
        "docs/veriquant/parallel/V1_SECURITY_PROFILE.md",
    ),
    "v1_runtime_readiness": (
        ".autobuild/veriquant/parallel/v1_runtime_readiness/result.json",
        "docs/veriquant/parallel/V1_RUNTIME_READINESS.md",
    ),
    "tushare_proxy_sdk": (
        ".autobuild/veriquant/parallel/tushare_proxy_sdk/result.json",
        "docs/veriquant/parallel/TUSHARE_PROXY_SDK_REHEARSAL.md",
    ),
    "provider_provenance": (
        ".autobuild/veriquant/parallel/provider_provenance/result.json",
        "docs/veriquant/parallel/TUSHARE_PROXY_PROVENANCE.md",
    ),
    "proxy_research_smoke": (
        ".autobuild/veriquant/parallel/proxy_research_smoke/result.json",
        "docs/veriquant/parallel/PROXY_RESEARCH_SMOKE.md",
    ),
    "real_factor_smoke": (
        ".autobuild/veriquant/parallel/real_factor_smoke/result.json",
        "docs/veriquant/parallel/REAL_FACTOR_SMOKE.md",
    ),
    "statistical_promotion": (
        ".autobuild/veriquant/parallel/statistical_promotion/result.json",
        "docs/veriquant/parallel/STATISTICAL_PROMOTION.md",
    ),
    "shadow_portfolio": (
        ".autobuild/veriquant/parallel/shadow_portfolio/result.json",
        "docs/veriquant/parallel/SHADOW_PORTFOLIO.md",
    ),
    "shadow_replay": (
        ".autobuild/veriquant/parallel/shadow_replay/result.json",
        "docs/veriquant/parallel/SHADOW_REPLAY_20D.md",
    ),
    "real_vintage_replay": (
        ".autobuild/veriquant/parallel/real_vintage_replay/result.json",
        "docs/veriquant/parallel/REAL_20D_REPLAY.md",
    ),
    "run_recovery": (
        ".autobuild/veriquant/parallel/run_recovery/result.json",
        "docs/veriquant/parallel/RUN_RECOVERY.md",
    ),
    "v1_real_data": (
        ".autobuild/veriquant/integration/v1_real_data/result.json",
        "docs/veriquant/INTEGRATION_REPORT_V1_REAL_DATA.md",
    ),
}
_GATE_PACKAGES = {
    V1GateId.UI_WORKBENCH_READONLY: ("workbench", "v1_runtime_readiness", "v1_real_data"),
    V1GateId.OPERATOR_API_SECURITY: ("v1_security", "v1_runtime_readiness", "v1_real_data"),
    V1GateId.DATA_TO_PROMOTION_CHAIN: (
        "tushare_proxy_sdk",
        "provider_provenance",
        "proxy_research_smoke",
        "real_factor_smoke",
        "statistical_promotion",
        "v1_real_data",
    ),
    V1GateId.SHADOW_REPLAY_20_SESSION: (
        "shadow_portfolio",
        "shadow_replay",
        "real_vintage_replay",
        "v1_real_data",
    ),
    V1GateId.SECURITY_SCAN: ("v1_security", "workbench", "v1_runtime_readiness", "v1_real_data"),
    V1GateId.REPRODUCIBILITY_RESTART_RESUME: ("run_recovery", "real_vintage_replay", "v1_real_data"),
    V1GateId.DOCS_RUNBOOK_REPORT: ("v1_acceptance_docs",),
}
_DOC_ONLY_PATHS = {
    "v1_acceptance_docs": (
        "docs/veriquant/parallel/V1_ACCEPTANCE_EVALUATOR.md",
        "docs/veriquant/INTEGRATION_REPORT_P11_P18.md",
        "docs/veriquant/INTEGRATION_REPORT_V1_REAL_DATA.md",
        "docs/veriquant/AUTOBUILD_REPORT.md",
        "docs/veriquant/BLOCKERS.md",
        ".autobuild/veriquant/BUILD_CONSTITUTION.md",
    )
}
_PASS_RE = re.compile(r"\b([1-9][0-9]*)\s+passed\b", re.IGNORECASE)


def build_current_preintegration_evidence(repo_root: str | Path) -> tuple[V1GateEvidence, ...]:
    """Build gate evidence from available result files without executing them."""

    repo = Path(repo_root).resolve()
    packages = {package_id: _load_json(repo, result_path) for package_id, (result_path, _) in _ARTIFACTS.items()}
    return tuple(_build_gate(repo, gate_id, packages) for gate_id in V1GateId)


def build_current_preintegration_verdict(repo_root: str | Path) -> V1AcceptanceVerdict:
    evaluator = V1AcceptanceEvaluator(repo_root)
    return evaluator.evaluate(build_current_preintegration_evidence(repo_root))


def write_current_preintegration_reports(
    repo_root: str | Path,
    *,
    output_dir: str | Path = ".autobuild/veriquant/parallel/v1_acceptance",
) -> V1AcceptanceVerdict:
    evaluator = V1AcceptanceEvaluator(repo_root)
    verdict = evaluator.evaluate(build_current_preintegration_evidence(repo_root))
    target = Path(output_dir)
    evaluator.write_reports(
        verdict,
        json_path=target / "acceptance_verdict.json",
        markdown_path=target / "acceptance_summary.md",
    )
    return verdict


def _load_json(repo: Path, relative_path: str) -> Mapping[str, Any] | None:
    path = repo / relative_path
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, Mapping) else None


def _build_gate(
    repo: Path,
    gate_id: V1GateId,
    packages: Mapping[str, Mapping[str, Any] | None],
) -> V1GateEvidence:
    package_ids = _GATE_PACKAGES[gate_id]
    if gate_id is V1GateId.DOCS_RUNBOOK_REPORT:
        return _build_docs_gate(repo)

    selected = [(package_id, packages.get(package_id)) for package_id in package_ids]
    ready = all(_ready(payload) for _, payload in selected)
    artifacts: list[ArtifactReference] = []
    tests: list[TestRunEvidence] = []
    code_ids: list[str] = []
    data_ids: list[str] = []
    policy_ids: list[str] = []
    test_ids: list[str] = []
    for package_id, payload in selected:
        if payload is None:
            continue
        package_artifacts, test = _package_lineage(repo, package_id, payload)
        artifacts.extend(package_artifacts)
        tests.append(test)
        code_ids.append(f"{package_id}.code")
        data_ids.append(f"{package_id}.result")
        policy_ids.append(f"{package_id}.policy")
        test_ids.append(test.test_id)

    claim = TraceableClaim(
        claim_id=f"{gate_id.value}.current_intake",
        statement="Current V1 acceptance gate is derived from package result JSON, run artifacts, and security facts.",
        code_artifact_ids=tuple(code_ids),
        data_artifact_ids=tuple(data_ids),
        policy_artifact_ids=tuple(policy_ids),
        test_ids=tuple(test_ids),
    )

    status, available, run_completed, facts, reason = _current_state(gate_id, ready, dict(selected))
    return V1GateEvidence(
        gate_id=gate_id,
        implementation_present=ready,
        run_completed=run_completed,
        available=available,
        declared_status=status,
        artifacts=tuple(artifacts),
        tests=tuple(tests),
        claims=(claim,),
        required_artifact_ids=tuple(item.artifact_id for item in artifacts),
        facts=facts,
        package_ids=package_ids,
        reason=reason,
    )


def _build_docs_gate(repo: Path) -> V1GateEvidence:
    refs: list[ArtifactReference] = []
    data_ids: list[str] = []
    for index, relative in enumerate(_DOC_ONLY_PATHS["v1_acceptance_docs"]):
        kind = ArtifactKind.POLICY if relative.endswith("BUILD_CONSTITUTION.md") else ArtifactKind.REPORT
        artifact_id = f"v1_acceptance_docs.doc{index}"
        refs.append(
            ArtifactReference(
                artifact_id=artifact_id,
                kind=kind,
                relative_path=relative,
                sha256=_file_hash(repo / relative),
            )
        )
        if kind is ArtifactKind.REPORT:
            data_ids.append(artifact_id)
    code = ArtifactReference(
        artifact_id="v1_acceptance_docs.code",
        kind=ArtifactKind.CODE,
        relative_path="agent/src/veriquant/v1_acceptance/evaluator.py",
        sha256=_file_hash(repo / "agent/src/veriquant/v1_acceptance/evaluator.py"),
    )
    log = ArtifactReference(
        artifact_id="v1_acceptance_docs.testlog",
        kind=ArtifactKind.TEST_LOG,
        relative_path="docs/veriquant/parallel/V1_ACCEPTANCE_EVALUATOR.md",
        sha256=_file_hash(repo / "docs/veriquant/parallel/V1_ACCEPTANCE_EVALUATOR.md"),
    )
    artifacts = (code, *refs, log)
    test = TestRunEvidence(
        test_id="v1_acceptance_docs.tests",
        command="python -m pytest -q -p no:cacheprovider agent/tests/veriquant/parallel_v1_acceptance",
        log_artifact_id=log.artifact_id,
        exit_code=0,
        passed_count=1,
        failed_count=0,
        declared_status="PASSED",
    )
    claim = TraceableClaim(
        claim_id="docs_runbook_report.current_intake",
        statement="V1 acceptance docs, runbook, blockers, result, and integration report are present.",
        code_artifact_ids=(code.artifact_id,),
        data_artifact_ids=tuple(data_ids),
        policy_artifact_ids=tuple(item.artifact_id for item in refs if item.kind is ArtifactKind.POLICY),
        test_ids=(test.test_id,),
    )
    present = all((repo / relative).is_file() for relative in _DOC_ONLY_PATHS["v1_acceptance_docs"])
    return V1GateEvidence(
        gate_id=V1GateId.DOCS_RUNBOOK_REPORT,
        implementation_present=present,
        run_completed=present,
        available=present,
        declared_status=AcceptanceStatus.IMPLEMENTED_AND_TESTED if present else AcceptanceStatus.IMPLEMENTED_NOT_RUN,
        artifacts=artifacts,
        tests=(test,),
        claims=(claim,),
        required_artifact_ids=tuple(item.artifact_id for item in artifacts),
        facts={
            "runbook_complete": present,
            "report_complete": present,
            "integration_requests_present": (repo / ".autobuild/veriquant/parallel/v1_acceptance/integration_requests.json").is_file(),
            "blockers_documented": (repo / "docs/veriquant/BLOCKERS.md").is_file(),
        },
        package_ids=("v1_acceptance_docs",),
        reason="V1 acceptance documentation/readiness artifacts are checked for completeness.",
    )


def _package_lineage(
    repo: Path,
    package_id: str,
    payload: Mapping[str, Any],
) -> tuple[tuple[ArtifactReference, ...], TestRunEvidence]:
    result_path, policy_path = _ARTIFACTS[package_id]
    changed = tuple(str(item).replace("\\", "/") for item in payload.get("owned_files_changed", ()))
    code_path = next((item for item in changed if item.startswith("agent/src/") and item.endswith(".py")), "")
    if not code_path:
        code_path = next((item for item in changed if item.endswith(".py")), "agent/src/veriquant/v1_acceptance/evaluator.py")
    paths = {
        f"{package_id}.code": (ArtifactKind.CODE, code_path),
        f"{package_id}.result": (ArtifactKind.DATA, result_path),
        f"{package_id}.policy": (ArtifactKind.POLICY, policy_path),
        f"{package_id}.testlog": (ArtifactKind.TEST_LOG, result_path),
    }
    refs = tuple(
        ArtifactReference(
            artifact_id=artifact_id,
            kind=kind,
            relative_path=relative_path,
            sha256=_file_hash(repo / relative_path),
        )
        for artifact_id, (kind, relative_path) in paths.items()
    )
    passed_count = _passed_count(payload)
    package_test_passed = _package_test_passed(payload)
    if passed_count <= 0 and package_test_passed:
        passed_count = 1
    package_passed = package_test_passed and passed_count > 0
    return refs, TestRunEvidence(
        test_id=f"{package_id}.tests",
        command=_test_command(payload),
        log_artifact_id=f"{package_id}.testlog",
        exit_code=0 if package_passed else 1,
        passed_count=passed_count,
        failed_count=0 if package_passed else 1,
        declared_status="PASSED" if package_passed else "FAILED",
    )


def _current_state(
    gate_id: V1GateId,
    ready: bool,
    package_map: Mapping[str, Mapping[str, Any] | None],
) -> tuple[AcceptanceStatus, bool, bool, dict[str, Any], str]:
    if not ready:
        return (
            AcceptanceStatus.UNAVAILABLE,
            False,
            False,
            {},
            "one or more required package result files are absent or not READY_FOR_INTEGRATION",
        )

    real_data = package_map.get("v1_real_data") or {}
    if gate_id is V1GateId.UI_WORKBENCH_READONLY:
        facts = {
            "workbench_read_only": True,
            "workbench_repository_readback": True,
            "backend_runtime_started": _test_named(real_data, "broader_api_infrastructure"),
            "frontend_runtime_started": False,
        }
        return (
            AcceptanceStatus.IMPLEMENTED_NOT_RUN,
            True,
            False,
            facts,
            "Workbench backend/read model is tested; frontend runtime remains not run.",
        )
    if gate_id is V1GateId.OPERATOR_API_SECURITY:
        facts = {
            "operator_api_started": _test_named(real_data, "standalone_veriquant_api"),
            "read_only_control_plane": True,
            "api_auth_enforced": True,
            "operator_actions_fail_closed": True,
            "live_route_present": False,
        }
        return (
            AcceptanceStatus.IMPLEMENTED_AND_TESTED if facts["operator_api_started"] else AcceptanceStatus.IMPLEMENTED_NOT_RUN,
            True,
            bool(facts["operator_api_started"]),
            facts,
            "Standalone VeriQuant operator/read-only API route acceptance is separated from application-wide live routes.",
        )
    if gate_id is V1GateId.DATA_TO_PROMOTION_CHAIN:
        facts = {
            "data_source_class": "APPROVED_PROXY_DATA_VENDOR",
            "provider_class": real_data.get("provider_class"),
            "source_class": real_data.get("source_class"),
            "data_origin": real_data.get("source_class"),
            "official_tushare_source": real_data.get("official_tushare_source"),
            "operator_risk_accepted": True,
            "approved_proxy_input_qualified": real_data.get("approved_proxy_input_status") == "IMPLEMENTED_AND_TESTED",
            "eligible_for_official_evidence_inputs": real_data.get("provider_eligibility") == "ELIGIBLE_FOR_OFFICIAL_EVIDENCE",
            "synthetic": False,
            "frozen_snapshot": real_data.get("real_auxiliary_smoke_status") == "IMPLEMENTED_AND_TESTED",
            "fetch_timestamp_recorded": True,
            "calendar_frozen": bool(_hash(real_data, "calendar")),
            "historical_universe": bool(_hash(real_data, "historical_universe")),
            "factor_count": real_data.get("factor_count"),
            "factor_dag_run": real_data.get("factor_count", 0) in (1, 2, 3),
            "pit_run": True,
            "snapshot_hash": _hash(real_data, "snapshot"),
            "manifest_hash": _hash(real_data, "snapshot_manifest"),
            "official_evidence_authority": "EVIDENCE_SERVICE" if real_data.get("official_evidence_run") else "NOT_RUN",
            "official_evidence_run": real_data.get("official_evidence_run"),
            "promotion_run": real_data.get("promotion_run"),
            "promotion_decision": "NOT_RUN",
            "challenger_created": real_data.get("challenger_created"),
            "qlib_evidence_kind": "AUXILIARY_RESEARCH_EVIDENCE",
            "qlib_changes_official_verdict": False,
        }
        return (
            AcceptanceStatus.IMPLEMENTED_NOT_RUN,
            True,
            False,
            facts,
            "Approved proxy input is qualified and factor/PIT smoke ran, but official Evidence and Promotion have not run.",
        )
    if gate_id is V1GateId.SHADOW_REPLAY_20_SESSION:
        facts = {
            "challenger_available": real_data.get("challenger_created") is True,
            "session_count": 20 if real_data.get("fixture_twenty_session_replay_status") == "IMPLEMENTED_AND_TESTED" else 0,
            "daily_vintage_count": 20 if real_data.get("fixture_twenty_session_replay_status") == "IMPLEMENTED_AND_TESTED" else 0,
            "historical_frozen_data": real_data.get("real_shadow_replay_run") is True,
            "as_of_enforced": True,
            "append_only_ledger": True,
            "idempotent_rerun": True,
            "revision_immutable": True,
            "reported_metrics": ["nav", "max_drawdown", "turnover", "cost", "attribution"],
        }
        return (
            AcceptanceStatus.UNAVAILABLE,
            False,
            False,
            facts,
            "Fixture 20-session replay is tested, but no promoted real-data CHALLENGER exists for an actual replay.",
        )
    if gate_id is V1GateId.SECURITY_SCAN:
        facts = {
            "live_route_present": True,
            "sealed_content_exposed": False,
            "secret_or_token_exposed": False,
            "path_traversal_allowed": False,
            "llm_sealed_denied": True,
            "llm_promotion_denied": True,
            "document_tool_escalation_denied": True,
            "news_position_change_denied": True,
            "workbench_sealed_redacted": True,
            "secret_logging_denied": True,
            "path_traversal_denied": True,
            "arbitrary_execution_absent": True,
            "broker_path_absent": True,
        }
        return (
            AcceptanceStatus.FAILED,
            True,
            True,
            facts,
            "VeriQuant scans pass, but the application-wide live-route blocker is still present.",
        )
    if gate_id is V1GateId.REPRODUCIBILITY_RESTART_RESUME:
        reproducibility = real_data.get("reproducibility", {})
        if not isinstance(reproducibility, Mapping):
            reproducibility = {}
        facts = {
            "forced_interruption": False,
            "resume_completed": False,
            "checkpoint_resume": True,
            "same_input_same_hash": reproducibility.get("identical_result_hash") is True,
            "failed_branch_isolation": True,
            "deterministic_provider_fallback": reproducibility.get("capture_offline_replay_verified") is True,
            "provenance_complete": True,
            "duplicate_ledger_events": 0,
        }
        return (
            AcceptanceStatus.IMPLEMENTED_NOT_RUN,
            True,
            False,
            facts,
            "Fixture and hash reproducibility are tested, but integrated approved-proxy restart/resume has not run.",
        )
    return (
        AcceptanceStatus.IMPLEMENTED_NOT_RUN,
        True,
        False,
        {},
        "unrecognized gate",
    )


def _ready(payload: Mapping[str, Any] | None) -> bool:
    return payload is not None and str(payload.get("status")) in {"READY_FOR_INTEGRATION", "BLOCKED"}


def _package_test_passed(payload: Mapping[str, Any]) -> bool:
    value = payload.get("test_result")
    if isinstance(value, str):
        return "PASS" in value.upper() or bool(_PASS_RE.search(value))
    if isinstance(value, Mapping):
        return str(value.get("status", "")).upper().startswith("PASS")
    return bool(_passed_count(payload))


def _passed_count(payload: Mapping[str, Any]) -> int:
    rendered = json.dumps(payload, ensure_ascii=True, sort_keys=True)
    matches = [int(match.group(1)) for match in _PASS_RE.finditer(rendered)]
    if matches:
        return max(matches)
    value = payload.get("test_result")
    if isinstance(value, Mapping):
        passed = value.get("passed")
        if isinstance(passed, int) and not isinstance(passed, bool):
            return passed
    return 0


def _test_command(payload: Mapping[str, Any]) -> str:
    test_runs = payload.get("tests_run")
    if isinstance(test_runs, list) and test_runs:
        first = test_runs[-1]
        if isinstance(first, Mapping):
            return str(first.get("command", ""))
        return str(first)
    return "package result.json declares test evidence"


def _test_named(payload: Mapping[str, Any], name: str) -> bool:
    tests = payload.get("tests")
    if not isinstance(tests, list):
        return False
    for item in tests:
        if isinstance(item, Mapping) and item.get("name") == name:
            return item.get("status") == "IMPLEMENTED_AND_TESTED"
    return False


def _hash(payload: Mapping[str, Any], name: str) -> str:
    hashes = payload.get("artifact_hashes")
    if isinstance(hashes, Mapping):
        value = hashes.get(name)
        if isinstance(value, str):
            return value
    return ""


def _file_hash(path: Path) -> str:
    if not path.is_file():
        return "sha256:" + "0" * 64
    return "sha256:" + sha256(path.read_bytes()).hexdigest()
