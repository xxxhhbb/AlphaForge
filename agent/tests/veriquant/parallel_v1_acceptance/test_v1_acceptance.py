from __future__ import annotations

import json
from dataclasses import replace
from hashlib import sha256
from pathlib import Path

from src.veriquant.v1_acceptance.api import (
    AcceptanceFailureCode,
    AcceptanceStatus,
    ArtifactKind,
    ArtifactReference,
    DataSourceClass,
    TestRunEvidence as RunEvidence,
    TraceableClaim,
    V1AcceptanceEvaluator,
    V1GateEvidence,
    V1GateId,
)


def _write(root: Path, relative: str, content: str, kind: ArtifactKind) -> ArtifactReference:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    digest = "sha256:" + sha256(path.read_bytes()).hexdigest()
    return ArtifactReference(
        artifact_id=relative.replace("/", "_"),
        kind=kind,
        relative_path=relative,
        sha256=digest,
    )


def _facts(gate_id: V1GateId) -> dict[str, object]:
    if gate_id is V1GateId.UI_WORKBENCH_READONLY:
        return {
            "workbench_read_only": True,
            "workbench_repository_readback": True,
            "backend_runtime_started": True,
            "frontend_runtime_started": True,
        }
    if gate_id is V1GateId.OPERATOR_API_SECURITY:
        return {
            "operator_api_started": True,
            "read_only_control_plane": True,
            "api_auth_enforced": True,
            "operator_actions_fail_closed": True,
            "live_route_present": False,
        }
    if gate_id is V1GateId.DATA_TO_PROMOTION_CHAIN:
        return {
            "data_source_class": "OFFICIAL_TUSHARE",
            "data_origin": "TUSHARE_REAL",
            "official_tushare_source": True,
            "synthetic": False,
            "frozen_snapshot": True,
            "fetch_timestamp_recorded": True,
            "calendar_frozen": True,
            "historical_universe": True,
            "factor_count": 2,
            "factor_dag_run": True,
            "pit_run": True,
            "snapshot_hash": "sha256:" + "1" * 64,
            "manifest_hash": "sha256:" + "2" * 64,
            "official_evidence_authority": "EVIDENCE_SERVICE",
            "official_evidence_run": True,
            "promotion_run": True,
            "promotion_decision": "CHALLENGER",
            "challenger_created": True,
            "qlib_evidence_kind": "AUXILIARY_RESEARCH_EVIDENCE",
            "qlib_changes_official_verdict": False,
        }
    if gate_id is V1GateId.SHADOW_REPLAY_20_SESSION:
        return {
            "challenger_available": True,
            "session_count": 20,
            "daily_vintage_count": 20,
            "historical_frozen_data": True,
            "as_of_enforced": True,
            "append_only_ledger": True,
            "idempotent_rerun": True,
            "revision_immutable": True,
            "reported_metrics": ["nav", "max_drawdown", "turnover", "cost", "attribution"],
        }
    if gate_id is V1GateId.SECURITY_SCAN:
        return {
            "live_route_present": False,
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
    if gate_id is V1GateId.REPRODUCIBILITY_RESTART_RESUME:
        return {
            "forced_interruption": True,
            "resume_completed": True,
            "checkpoint_resume": True,
            "same_input_same_hash": True,
            "failed_branch_isolation": True,
            "deterministic_provider_fallback": True,
            "provenance_complete": True,
            "duplicate_ledger_events": 0,
        }
    return {
        "runbook_complete": True,
        "report_complete": True,
        "integration_requests_present": True,
        "blockers_documented": True,
    }


def _evidence(root: Path, gate_id: V1GateId) -> V1GateEvidence:
    prefix = gate_id.value
    code = _write(root, f"{prefix}/code.py", "VALUE = 1\n", ArtifactKind.CODE)
    data = _write(root, f"{prefix}/data.json", '{"frozen":true}\n', ArtifactKind.DATA)
    policy = _write(root, f"{prefix}/policy.md", "# Frozen policy\n", ArtifactKind.POLICY)
    log = _write(root, f"{prefix}/test.log", "1 passed in 0.01s\n", ArtifactKind.TEST_LOG)
    test = RunEvidence(
        test_id=f"{prefix}_test",
        command=f"python -m pytest {prefix}",
        log_artifact_id=log.artifact_id,
        exit_code=0,
        passed_count=1,
        failed_count=0,
    )
    claim = TraceableClaim(
        claim_id=f"{prefix}_claim",
        statement=f"{prefix} gate meets its declared invariant",
        code_artifact_ids=(code.artifact_id,),
        data_artifact_ids=(data.artifact_id,),
        policy_artifact_ids=(policy.artifact_id,),
        test_ids=(test.test_id,),
    )
    artifacts = (code, data, policy, log)
    return V1GateEvidence(
        gate_id=gate_id,
        implementation_present=True,
        run_completed=True,
        declared_status=AcceptanceStatus.IMPLEMENTED_AND_TESTED,
        artifacts=artifacts,
        tests=(test,),
        claims=(claim,),
        required_artifact_ids=tuple(item.artifact_id for item in artifacts),
        facts=_facts(gate_id),
        package_ids=(gate_id.value,),
    )


def _all_evidence(root: Path) -> tuple[V1GateEvidence, ...]:
    return tuple(_evidence(root, gate_id) for gate_id in V1GateId)


def _gate(verdict, gate_id: V1GateId):
    return next(item for item in verdict.gates if item.gate_id is gate_id)


def test_complete_traceable_evidence_passes_all_seven_gates(tmp_path: Path) -> None:
    verdict = V1AcceptanceEvaluator(tmp_path).evaluate(_all_evidence(tmp_path))
    assert verdict.overall_status is AcceptanceStatus.IMPLEMENTED_AND_TESTED
    assert all(item.status is AcceptanceStatus.IMPLEMENTED_AND_TESTED for item in verdict.gates)


def test_missing_operator_api_reports_earliest_blocker(tmp_path: Path) -> None:
    items = list(_all_evidence(tmp_path))
    index = next(i for i, item in enumerate(items) if item.gate_id is V1GateId.OPERATOR_API_SECURITY)
    facts = dict(items[index].facts)
    facts["operator_api_started"] = False
    items[index] = replace(items[index], facts=facts, run_completed=False, declared_status=AcceptanceStatus.IMPLEMENTED_NOT_RUN)
    gate = _gate(V1AcceptanceEvaluator(tmp_path).evaluate(items), V1GateId.OPERATOR_API_SECURITY)
    assert gate.status is AcceptanceStatus.IMPLEMENTED_NOT_RUN
    assert gate.earliest_blocker == AcceptanceFailureCode.OPERATOR_API_NOT_STARTED.value
    assert "operator_api_security.operator_api_started=true" in gate.missing_evidence


def test_missing_official_evidence_reports_exact_blocker(tmp_path: Path) -> None:
    items = list(_all_evidence(tmp_path))
    index = next(i for i, item in enumerate(items) if item.gate_id is V1GateId.DATA_TO_PROMOTION_CHAIN)
    facts = dict(items[index].facts)
    facts.update(official_evidence_run=False, promotion_run=False, promotion_decision="")
    items[index] = replace(items[index], facts=facts, run_completed=False, declared_status=AcceptanceStatus.IMPLEMENTED_NOT_RUN)
    gate = _gate(V1AcceptanceEvaluator(tmp_path).evaluate(items), V1GateId.DATA_TO_PROMOTION_CHAIN)
    assert gate.status is AcceptanceStatus.IMPLEMENTED_NOT_RUN
    assert gate.earliest_blocker == AcceptanceFailureCode.OFFICIAL_EVIDENCE_NOT_RUN.value
    assert "data_to_promotion_chain.official_evidence_run=true by Evidence Service" in gate.missing_evidence


def test_missing_twenty_session_replay_reports_earliest_blocker(tmp_path: Path) -> None:
    items = list(_all_evidence(tmp_path))
    index = next(i for i, item in enumerate(items) if item.gate_id is V1GateId.SHADOW_REPLAY_20_SESSION)
    facts = dict(items[index].facts)
    facts.update(session_count=1, daily_vintage_count=1)
    items[index] = replace(items[index], facts=facts, available=False, run_completed=False, declared_status=AcceptanceStatus.UNAVAILABLE)
    gate = _gate(V1AcceptanceEvaluator(tmp_path).evaluate(items), V1GateId.SHADOW_REPLAY_20_SESSION)
    assert gate.status is AcceptanceStatus.UNAVAILABLE
    assert AcceptanceFailureCode.SINGLE_DAY_REPLAY_AS_TWENTY_DAY in gate.failure_codes
    assert gate.earliest_blocker == AcceptanceFailureCode.INSUFFICIENT_REPLAY_SESSIONS.value


def test_frontend_not_run_is_not_forged_as_pass(tmp_path: Path) -> None:
    items = list(_all_evidence(tmp_path))
    index = next(i for i, item in enumerate(items) if item.gate_id is V1GateId.UI_WORKBENCH_READONLY)
    facts = dict(items[index].facts)
    facts["frontend_runtime_started"] = False
    items[index] = replace(items[index], facts=facts, run_completed=False, declared_status=AcceptanceStatus.IMPLEMENTED_NOT_RUN)
    gate = _gate(V1AcceptanceEvaluator(tmp_path).evaluate(items), V1GateId.UI_WORKBENCH_READONLY)
    assert gate.status is AcceptanceStatus.IMPLEMENTED_NOT_RUN
    assert gate.earliest_blocker == AcceptanceFailureCode.FRONTEND_RUNTIME_NOT_RUN.value


def test_live_route_and_sealed_token_path_hits_fail(tmp_path: Path) -> None:
    items = list(_all_evidence(tmp_path))
    index = next(i for i, item in enumerate(items) if item.gate_id is V1GateId.SECURITY_SCAN)
    facts = dict(items[index].facts)
    facts.update(
        live_route_present=True,
        sealed_content_exposed=True,
        secret_or_token_exposed=True,
        path_traversal_allowed=True,
    )
    items[index] = replace(items[index], facts=facts)
    gate = _gate(V1AcceptanceEvaluator(tmp_path).evaluate(items), V1GateId.SECURITY_SCAN)
    assert gate.status is AcceptanceStatus.FAILED
    assert AcceptanceFailureCode.LIVE_ROUTE_PRESENT in gate.failure_codes
    assert AcceptanceFailureCode.SEALED_CONTENT_EXPOSED in gate.failure_codes
    assert AcceptanceFailureCode.SECRET_OR_TOKEN_EXPOSURE in gate.failure_codes
    assert AcceptanceFailureCode.PATH_TRAVERSAL_ALLOWED in gate.failure_codes


def test_approved_proxy_can_satisfy_input_gate_but_is_not_official_tushare(tmp_path: Path) -> None:
    items = list(_all_evidence(tmp_path))
    index = next(i for i, item in enumerate(items) if item.gate_id is V1GateId.DATA_TO_PROMOTION_CHAIN)
    facts = dict(items[index].facts)
    facts.update(
        data_source_class="APPROVED_PROXY_DATA_VENDOR",
        source_class="TUSHARE_COMPATIBLE_PROXY",
        data_origin="TUSHARE_COMPATIBLE_PROXY",
        official_tushare_source=False,
        operator_risk_accepted=True,
        approved_proxy_input_qualified=True,
        eligible_for_official_evidence_inputs=True,
    )
    items[index] = replace(items[index], facts=facts)
    gate = _gate(V1AcceptanceEvaluator(tmp_path).evaluate(items), V1GateId.DATA_TO_PROMOTION_CHAIN)
    assert gate.status is AcceptanceStatus.IMPLEMENTED_AND_TESTED
    assert gate.data_source_class is DataSourceClass.APPROVED_PROXY_DATA_VENDOR
    assert AcceptanceFailureCode.APPROVED_PROXY_MASQUERADE_AS_OFFICIAL not in gate.failure_codes


def test_approved_proxy_cannot_masquerade_as_official_tushare(tmp_path: Path) -> None:
    items = list(_all_evidence(tmp_path))
    index = next(i for i, item in enumerate(items) if item.gate_id is V1GateId.DATA_TO_PROMOTION_CHAIN)
    facts = dict(items[index].facts)
    facts.update(
        data_source_class="APPROVED_PROXY_DATA_VENDOR",
        official_tushare_source=True,
        operator_risk_accepted=True,
        approved_proxy_input_qualified=True,
        eligible_for_official_evidence_inputs=True,
    )
    items[index] = replace(items[index], facts=facts)
    gate = _gate(V1AcceptanceEvaluator(tmp_path).evaluate(items), V1GateId.DATA_TO_PROMOTION_CHAIN)
    assert gate.status is AcceptanceStatus.FAILED
    assert AcceptanceFailureCode.APPROVED_PROXY_MASQUERADE_AS_OFFICIAL in gate.failure_codes


def test_reject_and_revise_are_legal_promotion_results_but_not_challengers(tmp_path: Path) -> None:
    for decision in ("REJECT", "REVISE"):
        items = list(_all_evidence(tmp_path))
        data_index = next(i for i, item in enumerate(items) if item.gate_id is V1GateId.DATA_TO_PROMOTION_CHAIN)
        data_facts = dict(items[data_index].facts)
        data_facts.update(promotion_decision=decision, challenger_created=False)
        items[data_index] = replace(items[data_index], facts=data_facts)
        replay_index = next(i for i, item in enumerate(items) if item.gate_id is V1GateId.SHADOW_REPLAY_20_SESSION)
        replay_facts = dict(items[replay_index].facts)
        replay_facts["challenger_available"] = False
        items[replay_index] = replace(
            items[replay_index],
            facts=replay_facts,
            available=False,
            run_completed=False,
            declared_status=AcceptanceStatus.UNAVAILABLE,
        )
        verdict = V1AcceptanceEvaluator(tmp_path).evaluate(items)
        data_gate = _gate(verdict, V1GateId.DATA_TO_PROMOTION_CHAIN)
        replay_gate = _gate(verdict, V1GateId.SHADOW_REPLAY_20_SESSION)
        assert data_gate.status is AcceptanceStatus.IMPLEMENTED_AND_TESTED
        assert replay_gate.status is AcceptanceStatus.UNAVAILABLE
        assert replay_gate.earliest_blocker == AcceptanceFailureCode.CHALLENGER_MISSING.value


def test_qlib_auxiliary_cannot_masquerade_as_official_evidence(tmp_path: Path) -> None:
    items = list(_all_evidence(tmp_path))
    index = next(i for i, item in enumerate(items) if item.gate_id is V1GateId.DATA_TO_PROMOTION_CHAIN)
    facts = dict(items[index].facts)
    facts.update(official_evidence_authority="QLIB", qlib_evidence_kind="OFFICIAL_EVIDENCE")
    items[index] = replace(items[index], facts=facts)
    gate = _gate(V1AcceptanceEvaluator(tmp_path).evaluate(items), V1GateId.DATA_TO_PROMOTION_CHAIN)
    assert gate.status is AcceptanceStatus.FAILED
    assert AcceptanceFailureCode.QLIB_AUXILIARY_AS_OFFICIAL in gate.failure_codes


def test_missing_test_log_fails_closed(tmp_path: Path) -> None:
    items = list(_all_evidence(tmp_path))
    first = items[0]
    log = next(item for item in first.artifacts if item.kind is ArtifactKind.TEST_LOG)
    (tmp_path / log.relative_path).unlink()
    verdict = V1AcceptanceEvaluator(tmp_path).evaluate(items)
    gate = _gate(verdict, first.gate_id)
    assert gate.status is AcceptanceStatus.FAILED
    assert AcceptanceFailureCode.MISSING_TEST in gate.failure_codes


def test_declared_pass_without_run_is_rejected_as_forged_status(tmp_path: Path) -> None:
    items = list(_all_evidence(tmp_path))
    items[0] = replace(items[0], run_completed=False)
    gate = _gate(V1AcceptanceEvaluator(tmp_path).evaluate(items), items[0].gate_id)
    assert gate.status is AcceptanceStatus.FAILED
    assert AcceptanceFailureCode.FORGED_OR_INCONSISTENT_STATUS in gate.failure_codes


def test_untraceable_claim_fails_closed(tmp_path: Path) -> None:
    items = list(_all_evidence(tmp_path))
    claim = items[0].claims[0]
    items[0] = replace(items[0], claims=(replace(claim, policy_artifact_ids=()),))
    gate = _gate(V1AcceptanceEvaluator(tmp_path).evaluate(items), items[0].gate_id)
    assert gate.status is AcceptanceStatus.FAILED
    assert AcceptanceFailureCode.UNTRACEABLE_CLAIM in gate.failure_codes


def test_artifact_hash_mismatch_fails_closed(tmp_path: Path) -> None:
    items = list(_all_evidence(tmp_path))
    artifact = items[0].artifacts[0]
    corrupted = replace(artifact, sha256="sha256:" + "0" * 64)
    items[0] = replace(items[0], artifacts=(corrupted, *items[0].artifacts[1:]))
    gate = _gate(V1AcceptanceEvaluator(tmp_path).evaluate(items), items[0].gate_id)
    assert gate.status is AcceptanceStatus.FAILED
    assert AcceptanceFailureCode.ARTIFACT_HASH_MISMATCH in gate.failure_codes


def test_missing_mandatory_gate_fails_aggregate(tmp_path: Path) -> None:
    items = list(_all_evidence(tmp_path))[:-1]
    verdict = V1AcceptanceEvaluator(tmp_path).evaluate(items)
    assert verdict.overall_status is AcceptanceStatus.FAILED
    missing = _gate(verdict, V1GateId.DOCS_RUNBOOK_REPORT)
    assert AcceptanceFailureCode.MISSING_GATE_EVIDENCE in missing.failure_codes


def test_unavailable_and_deferred_are_distinct(tmp_path: Path) -> None:
    items = list(_all_evidence(tmp_path))
    items[0] = replace(
        items[0],
        run_completed=False,
        available=False,
        declared_status=AcceptanceStatus.UNAVAILABLE,
    )
    items[1] = replace(
        items[1],
        run_completed=False,
        deferred=True,
        declared_status=AcceptanceStatus.DEFERRED,
    )
    verdict = V1AcceptanceEvaluator(tmp_path).evaluate(items)
    assert _gate(verdict, items[0].gate_id).status is AcceptanceStatus.UNAVAILABLE
    assert _gate(verdict, items[1].gate_id).status is AcceptanceStatus.DEFERRED


def test_same_input_produces_same_verdict_and_report_hash(tmp_path: Path) -> None:
    evidence = _all_evidence(tmp_path)
    evaluator = V1AcceptanceEvaluator(tmp_path)
    first = evaluator.evaluate(evidence)
    second = evaluator.evaluate(tuple(reversed(evidence)))
    assert first.input_hash == second.input_hash
    assert first.report_hash == second.report_hash
    assert first.to_dict() == second.to_dict()


def test_machine_json_and_markdown_reports_are_written_atomically(tmp_path: Path) -> None:
    evaluator = V1AcceptanceEvaluator(tmp_path)
    verdict = evaluator.evaluate(_all_evidence(tmp_path))
    evaluator.write_reports(verdict, json_path="out/verdict.json", markdown_path="out/verdict.md")
    parsed = json.loads((tmp_path / "out/verdict.json").read_text(encoding="utf-8"))
    markdown = (tmp_path / "out/verdict.md").read_text(encoding="utf-8")
    assert parsed["report_hash"] == verdict.report_hash
    assert parsed["gates"][0]["missing_evidence"] == []
    assert "Earliest blocker" in markdown
    assert verdict.report_hash in markdown


def test_report_output_path_cannot_escape_repo(tmp_path: Path) -> None:
    evaluator = V1AcceptanceEvaluator(tmp_path)
    verdict = evaluator.evaluate(_all_evidence(tmp_path))
    try:
        evaluator.write_reports(verdict, json_path="../escape.json", markdown_path="out/report.md")
    except ValueError as exc:
        assert "escapes repository" in str(exc)
    else:  # pragma: no cover - fail-closed assertion branch.
        raise AssertionError("path traversal was not rejected")
