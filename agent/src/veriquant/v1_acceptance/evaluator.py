"""Fail-closed VeriQuant V1 acceptance evaluator.

The evaluator verifies content-addressed evidence and mechanical invariants.  It
does not import or execute financial-truth services.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from hashlib import sha256
from pathlib import Path
from typing import Any, Iterable, Mapping

from .models import (
    SCHEMA_VERSION,
    AcceptanceFailureCode,
    AcceptanceStatus,
    ArtifactKind,
    ArtifactReference,
    DataSourceClass,
    GateVerdict,
    TestRunEvidence,
    TraceableClaim,
    V1AcceptanceVerdict,
    V1GateEvidence,
    V1GateId,
    canonical_hash,
    canonical_json,
)


_HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_PYTEST_PASS_RE = re.compile(r"\b(?P<count>[1-9][0-9]*)\s+passed\b", re.IGNORECASE)
_DATA_CHAIN_STAGES = ("frozen_data", "factor_dag", "pit", "official_evidence", "promotion")
_REPLAY_METRICS = {"nav", "max_drawdown", "turnover", "cost", "attribution"}
_OPERATOR_API_FACTS = (
    "operator_api_started",
    "read_only_control_plane",
    "api_auth_enforced",
    "operator_actions_fail_closed",
)
_SECURITY_SCAN_FACTS = (
    "llm_sealed_denied",
    "llm_promotion_denied",
    "document_tool_escalation_denied",
    "news_position_change_denied",
    "workbench_sealed_redacted",
    "secret_logging_denied",
    "path_traversal_denied",
    "arbitrary_execution_absent",
    "broker_path_absent",
)
_RECOVERY_FACTS = (
    "forced_interruption",
    "resume_completed",
    "checkpoint_resume",
    "same_input_same_hash",
    "failed_branch_isolation",
    "deterministic_provider_fallback",
    "provenance_complete",
)
_DOC_FACTS = ("runbook_complete", "report_complete", "integration_requests_present", "blockers_documented")
_FATAL_CODES = frozenset(
    {
        AcceptanceFailureCode.DUPLICATE_GATE_EVIDENCE,
        AcceptanceFailureCode.INVALID_SCHEMA,
        AcceptanceFailureCode.MISSING_ARTIFACT,
        AcceptanceFailureCode.EMPTY_ARTIFACT,
        AcceptanceFailureCode.INVALID_ARTIFACT_HASH,
        AcceptanceFailureCode.ARTIFACT_HASH_MISMATCH,
        AcceptanceFailureCode.ARTIFACT_PATH_ESCAPE,
        AcceptanceFailureCode.MISSING_REQUIRED_ARTIFACT,
        AcceptanceFailureCode.MISSING_TEST,
        AcceptanceFailureCode.EMPTY_TEST_LOG,
        AcceptanceFailureCode.TEST_NOT_EXECUTED,
        AcceptanceFailureCode.TEST_FAILED,
        AcceptanceFailureCode.TEST_SKIP_OR_XFAIL,
        AcceptanceFailureCode.FORGED_OR_INCONSISTENT_STATUS,
        AcceptanceFailureCode.UNTRACEABLE_CLAIM,
        AcceptanceFailureCode.UI_WORKBENCH_NOT_READONLY,
        AcceptanceFailureCode.MANUAL_ARTIFACT_COPY,
        AcceptanceFailureCode.SYNTHETIC_DATA_AS_REAL,
        AcceptanceFailureCode.DATA_SOURCE_NOT_APPROVED,
        AcceptanceFailureCode.APPROVED_PROXY_MASQUERADE_AS_OFFICIAL,
        AcceptanceFailureCode.QLIB_AUXILIARY_AS_OFFICIAL,
        AcceptanceFailureCode.LIVE_ROUTE_PRESENT,
        AcceptanceFailureCode.SEALED_CONTENT_EXPOSED,
        AcceptanceFailureCode.SECRET_OR_TOKEN_EXPOSURE,
        AcceptanceFailureCode.PATH_TRAVERSAL_ALLOWED,
    }
)


class V1AcceptanceEvaluator:
    """Evaluate exactly five mandatory V1 gates from frozen evidence files."""

    def __init__(self, repo_root: str | Path) -> None:
        self.repo_root = Path(repo_root).resolve()

    def evaluate(self, evidence: Iterable[V1GateEvidence]) -> V1AcceptanceVerdict:
        supplied = tuple(evidence)
        by_gate: dict[V1GateId, list[V1GateEvidence]] = {gate: [] for gate in V1GateId}
        for item in supplied:
            if isinstance(item.gate_id, V1GateId):
                by_gate[item.gate_id].append(item)

        gate_verdicts: list[GateVerdict] = []
        for gate_id in V1GateId:
            candidates = by_gate[gate_id]
            if not candidates:
                gate_verdicts.append(self._missing_gate(gate_id))
            elif len(candidates) > 1:
                gate_verdicts.append(self._duplicate_gate(gate_id, candidates))
            else:
                gate_verdicts.append(self._evaluate_gate(candidates[0]))

        input_hash = canonical_hash(
            sorted(supplied, key=lambda item: getattr(item.gate_id, "value", str(item.gate_id)))
        )
        return V1AcceptanceVerdict.build(gates=tuple(gate_verdicts), input_hash=input_hash)

    def write_reports(
        self,
        verdict: V1AcceptanceVerdict,
        *,
        json_path: str | Path,
        markdown_path: str | Path,
    ) -> None:
        json_target = self._safe_output_path(json_path)
        markdown_target = self._safe_output_path(markdown_path)
        self._atomic_write(json_target, canonical_json(verdict.to_dict()) + "\n")
        self._atomic_write(markdown_target, self.render_markdown(verdict))
        parsed = json.loads(json_target.read_text(encoding="utf-8"))
        if parsed.get("report_hash") != verdict.report_hash:
            raise ValueError("written acceptance verdict hash does not match")

    @staticmethod
    def render_markdown(verdict: V1AcceptanceVerdict) -> str:
        lines = [
            "# VeriQuant V1 Mechanical Acceptance",
            "",
            f"Overall status: **{verdict.overall_status.value}**",
            "",
            f"Report hash: `{verdict.report_hash}`",
            "",
            "| Gate | Status | Failure codes |",
            "| --- | --- | --- |",
        ]
        for gate in verdict.gates:
            codes = ", ".join(code.value for code in gate.failure_codes) or "none"
            lines.append(f"| `{gate.gate_id.value}` | `{gate.status.value}` | {codes} |")
        lines.extend(["", "## Evidence summary", ""])
        for gate in verdict.gates:
            lines.append(f"### {gate.gate_id.value}")
            lines.append("")
            lines.append(f"Status: `{gate.status.value}`  ")
            lines.append(f"Earliest blocker: `{gate.earliest_blocker or 'none'}`  ")
            if gate.data_source_class is not None:
                lines.append(f"Data source class: `{gate.data_source_class.value}`  ")
            lines.append(f"Evidence hash: `{gate.evidence_hash}`  ")
            lines.append(
                "Verified artifacts: "
                + (", ".join(f"`{item}`" for item in gate.verified_artifact_ids) or "none")
            )
            if gate.missing_evidence:
                lines.append("")
                lines.append("Missing evidence:")
                for item in gate.missing_evidence:
                    lines.append(f"- `{item}`")
            if gate.reasons:
                lines.append("")
                for reason in gate.reasons:
                    lines.append(f"- {reason}")
            lines.append("")
        return "\n".join(lines).rstrip() + "\n"

    def _missing_gate(self, gate_id: V1GateId) -> GateVerdict:
        return GateVerdict(
            gate_id=gate_id,
            status=AcceptanceStatus.FAILED,
            failure_codes=(AcceptanceFailureCode.MISSING_GATE_EVIDENCE,),
            reasons=("mandatory gate evidence is absent",),
            verified_artifact_ids=(),
            evidence_hash=canonical_hash({"gate_id": gate_id.value, "missing": True}),
            earliest_blocker="missing_gate_evidence",
            missing_evidence=(f"{gate_id.value}: V1GateEvidence",),
        )

    def _duplicate_gate(
        self, gate_id: V1GateId, candidates: Iterable[V1GateEvidence]
    ) -> GateVerdict:
        materialized = tuple(candidates)
        return GateVerdict(
            gate_id=gate_id,
            status=AcceptanceStatus.FAILED,
            failure_codes=(AcceptanceFailureCode.DUPLICATE_GATE_EVIDENCE,),
            reasons=("multiple evidence records claim the same mandatory gate",),
            verified_artifact_ids=(),
            evidence_hash=canonical_hash(materialized),
            earliest_blocker="duplicate_gate_evidence",
            missing_evidence=(f"{gate_id.value}: exactly one evidence record",),
        )

    def _evaluate_gate(self, evidence: V1GateEvidence) -> GateVerdict:
        codes: list[AcceptanceFailureCode] = []
        reasons: list[str] = []
        if evidence.schema_version != SCHEMA_VERSION:
            self._add(codes, reasons, AcceptanceFailureCode.INVALID_SCHEMA, "unknown evidence schema")

        artifacts, artifact_text, verified = self._verify_artifacts(evidence, codes, reasons)
        valid_tests = self._verify_tests(evidence.tests, artifacts, artifact_text, verified, codes, reasons)
        self._verify_claims(evidence.claims, artifacts, valid_tests, verified, codes, reasons)

        if evidence.declared_status is AcceptanceStatus.IMPLEMENTED_AND_TESTED and (
            not evidence.run_completed or not evidence.implementation_present or not evidence.available
        ):
            self._add(
                codes,
                reasons,
                AcceptanceFailureCode.FORGED_OR_INCONSISTENT_STATUS,
                "IMPLEMENTED_AND_TESTED was declared without an available completed run",
            )

        missing: list[str] = []
        data_source_class = self._data_source_class(evidence.facts)
        self._evaluate_semantics(evidence, codes, reasons, missing)
        if not evidence.run_completed and evidence.declared_status is not AcceptanceStatus.IMPLEMENTED_AND_TESTED:
            self._evaluate_always_fatal_facts(evidence, codes, reasons)
        else:
            self._evaluate_always_fatal_facts(evidence, codes, reasons)

        earliest = self._earliest_blocker(codes, missing)
        status = self._derive_status(evidence, codes)
        return GateVerdict(
            gate_id=evidence.gate_id,
            status=status,
            failure_codes=tuple(sorted(set(codes), key=lambda item: item.value)),
            reasons=tuple(dict.fromkeys(reasons)),
            verified_artifact_ids=tuple(sorted(verified)),
            evidence_hash=canonical_hash(evidence),
            earliest_blocker=earliest,
            missing_evidence=tuple(dict.fromkeys(missing)),
            data_source_class=data_source_class,
        )

    def _verify_artifacts(
        self,
        evidence: V1GateEvidence,
        codes: list[AcceptanceFailureCode],
        reasons: list[str],
    ) -> tuple[dict[str, ArtifactReference], dict[str, str], set[str]]:
        artifacts: dict[str, ArtifactReference] = {}
        text: dict[str, str] = {}
        verified: set[str] = set()
        for artifact in evidence.artifacts:
            if not artifact.artifact_id or artifact.artifact_id in artifacts:
                self._add(
                    codes,
                    reasons,
                    AcceptanceFailureCode.MISSING_ARTIFACT,
                    "artifact ids must be non-empty and unique",
                )
                continue
            artifacts[artifact.artifact_id] = artifact
            try:
                path = self._resolve_input_path(artifact.relative_path)
            except ValueError:
                self._add(
                    codes,
                    reasons,
                    AcceptanceFailureCode.ARTIFACT_PATH_ESCAPE,
                    f"artifact path escapes repository: {artifact.artifact_id}",
                )
                continue
            if not path.is_file():
                self._add(
                    codes,
                    reasons,
                    AcceptanceFailureCode.MISSING_ARTIFACT,
                    f"artifact is missing: {artifact.artifact_id}",
                )
                continue
            content = path.read_bytes()
            if not content:
                self._add(
                    codes,
                    reasons,
                    AcceptanceFailureCode.EMPTY_ARTIFACT,
                    f"artifact is empty: {artifact.artifact_id}",
                )
                continue
            if not _HASH_RE.fullmatch(artifact.sha256):
                self._add(
                    codes,
                    reasons,
                    AcceptanceFailureCode.INVALID_ARTIFACT_HASH,
                    f"artifact hash is not canonical SHA-256: {artifact.artifact_id}",
                )
                continue
            actual = "sha256:" + sha256(content).hexdigest()
            if actual != artifact.sha256:
                self._add(
                    codes,
                    reasons,
                    AcceptanceFailureCode.ARTIFACT_HASH_MISMATCH,
                    f"artifact content hash mismatch: {artifact.artifact_id}",
                )
                continue
            verified.add(artifact.artifact_id)
            if artifact.kind is ArtifactKind.TEST_LOG:
                text[artifact.artifact_id] = content.decode("utf-8", errors="replace")

        for required_id in evidence.required_artifact_ids:
            if required_id not in verified:
                self._add(
                    codes,
                    reasons,
                    AcceptanceFailureCode.MISSING_REQUIRED_ARTIFACT,
                    f"required artifact is not verified: {required_id}",
                )
        return artifacts, text, verified

    def _verify_tests(
        self,
        tests: tuple[TestRunEvidence, ...],
        artifacts: Mapping[str, ArtifactReference],
        artifact_text: Mapping[str, str],
        verified: set[str],
        codes: list[AcceptanceFailureCode],
        reasons: list[str],
    ) -> set[str]:
        valid: set[str] = set()
        if not tests:
            self._add(codes, reasons, AcceptanceFailureCode.MISSING_TEST, "no executable test evidence")
            return valid
        seen: set[str] = set()
        for test in tests:
            if not test.test_id or test.test_id in seen:
                self._add(codes, reasons, AcceptanceFailureCode.MISSING_TEST, "test ids must be unique")
                continue
            seen.add(test.test_id)
            log_ref = artifacts.get(test.log_artifact_id)
            log = artifact_text.get(test.log_artifact_id, "")
            if (
                log_ref is None
                or log_ref.kind is not ArtifactKind.TEST_LOG
                or test.log_artifact_id not in verified
            ):
                self._add(
                    codes,
                    reasons,
                    AcceptanceFailureCode.MISSING_TEST,
                    f"test log is missing or unverified: {test.test_id}",
                )
                continue
            if not log.strip():
                self._add(codes, reasons, AcceptanceFailureCode.EMPTY_TEST_LOG, f"empty test log: {test.test_id}")
                continue
            if not test.command.strip() or test.exit_code is None:
                self._add(
                    codes,
                    reasons,
                    AcceptanceFailureCode.TEST_NOT_EXECUTED,
                    f"test lacks command or exit code: {test.test_id}",
                )
                continue
            matches = [int(match.group("count")) for match in _PYTEST_PASS_RE.finditer(log)]
            machine_passed = "PASSED" in log.upper()
            if (
                test.declared_status != "PASSED"
                or test.exit_code != 0
                or test.passed_count <= 0
                or test.failed_count != 0
                or (not matches and not machine_passed)
                or (matches and max(matches) < test.passed_count)
            ):
                self._add(codes, reasons, AcceptanceFailureCode.TEST_FAILED, f"test did not pass: {test.test_id}")
                if test.declared_status == "PASSED":
                    self._add(
                        codes,
                        reasons,
                        AcceptanceFailureCode.FORGED_OR_INCONSISTENT_STATUS,
                        f"declared PASS conflicts with test log or counters: {test.test_id}",
                    )
                continue
            if test.skipped_count or test.xfailed_count:
                self._add(
                    codes,
                    reasons,
                    AcceptanceFailureCode.TEST_SKIP_OR_XFAIL,
                    f"critical test contains skip/xfail: {test.test_id}",
                )
                continue
            valid.add(test.test_id)
        return valid

    def _verify_claims(
        self,
        claims: tuple[TraceableClaim, ...],
        artifacts: Mapping[str, ArtifactReference],
        valid_tests: set[str],
        verified: set[str],
        codes: list[AcceptanceFailureCode],
        reasons: list[str],
    ) -> None:
        if not claims:
            self._add(codes, reasons, AcceptanceFailureCode.UNTRACEABLE_CLAIM, "no traceable claims supplied")
            return
        seen: set[str] = set()
        for claim in claims:
            ok = bool(claim.claim_id and claim.statement.strip() and claim.claim_id not in seen)
            seen.add(claim.claim_id)
            ok = ok and self._refs_match(claim.code_artifact_ids, ArtifactKind.CODE, artifacts, verified)
            ok = ok and self._refs_match(claim.data_artifact_ids, None, artifacts, verified, data=True)
            ok = ok and self._refs_match(claim.policy_artifact_ids, ArtifactKind.POLICY, artifacts, verified)
            ok = ok and bool(claim.test_ids) and all(item in valid_tests for item in claim.test_ids)
            if not ok:
                self._add(
                    codes,
                    reasons,
                    AcceptanceFailureCode.UNTRACEABLE_CLAIM,
                    f"claim lacks verified code/data/policy/test lineage: {claim.claim_id or '<empty>'}",
                )

    @staticmethod
    def _refs_match(
        refs: tuple[str, ...],
        expected_kind: ArtifactKind | None,
        artifacts: Mapping[str, ArtifactReference],
        verified: set[str],
        *,
        data: bool = False,
    ) -> bool:
        if not refs:
            return False
        allowed = {ArtifactKind.DATA, ArtifactKind.MANIFEST, ArtifactKind.REPORT}
        for ref in refs:
            artifact = artifacts.get(ref)
            if artifact is None or ref not in verified:
                return False
            if data and artifact.kind not in allowed:
                return False
            if expected_kind is not None and artifact.kind is not expected_kind:
                return False
        return True

    def _evaluate_semantics(
        self,
        evidence: V1GateEvidence,
        codes: list[AcceptanceFailureCode],
        reasons: list[str],
        missing: list[str],
    ) -> None:
        if evidence.gate_id is V1GateId.UI_WORKBENCH_READONLY:
            self._ui_semantics(evidence.facts, codes, reasons, missing)
        elif evidence.gate_id is V1GateId.OPERATOR_API_SECURITY:
            self._operator_api_semantics(evidence.facts, codes, reasons, missing)
        elif evidence.gate_id is V1GateId.DATA_TO_PROMOTION_CHAIN:
            self._data_chain_semantics(evidence.facts, codes, reasons, missing)
        elif evidence.gate_id is V1GateId.SHADOW_REPLAY_20_SESSION:
            self._replay_semantics(evidence.facts, codes, reasons, missing)
        elif evidence.gate_id is V1GateId.SECURITY_SCAN:
            self._security_semantics(evidence.facts, codes, reasons, missing)
        elif evidence.gate_id is V1GateId.REPRODUCIBILITY_RESTART_RESUME:
            self._recovery_semantics(evidence.facts, codes, reasons, missing)
        elif evidence.gate_id is V1GateId.DOCS_RUNBOOK_REPORT:
            self._docs_semantics(evidence.facts, codes, reasons, missing)

    def _evaluate_always_fatal_facts(
        self,
        evidence: V1GateEvidence,
        codes: list[AcceptanceFailureCode],
        reasons: list[str],
    ) -> None:
        facts = evidence.facts
        if facts.get("live_route_present") is True:
            self._add(codes, reasons, AcceptanceFailureCode.LIVE_ROUTE_PRESENT, "live route is present")
        if facts.get("sealed_content_exposed") is True:
            self._add(codes, reasons, AcceptanceFailureCode.SEALED_CONTENT_EXPOSED, "sealed content is exposed")
        if facts.get("secret_or_token_exposed") is True:
            self._add(codes, reasons, AcceptanceFailureCode.SECRET_OR_TOKEN_EXPOSURE, "secret or token evidence is exposed")
        if facts.get("path_traversal_allowed") is True:
            self._add(codes, reasons, AcceptanceFailureCode.PATH_TRAVERSAL_ALLOWED, "path traversal is allowed")
        if evidence.gate_id is V1GateId.DATA_TO_PROMOTION_CHAIN:
            origin = str(facts.get("data_origin", "")).upper()
            if origin in {"SYNTHETIC", "FIXTURE", "FAKE"} and evidence.declared_status is AcceptanceStatus.IMPLEMENTED_AND_TESTED:
                self._add(codes, reasons, AcceptanceFailureCode.SYNTHETIC_DATA_AS_REAL, "synthetic data claimed as real smoke")
            if facts.get("official_evidence_authority") in {"QLIB", "AUXILIARY_RESEARCH_EVIDENCE"}:
                self._add(codes, reasons, AcceptanceFailureCode.QLIB_AUXILIARY_AS_OFFICIAL, "Qlib/auxiliary evidence claimed official")

    def _ui_semantics(
        self,
        facts: Mapping[str, Any],
        codes: list[AcceptanceFailureCode],
        reasons: list[str],
        missing: list[str],
    ) -> None:
        if facts.get("workbench_read_only") is not True:
            self._add(codes, reasons, AcceptanceFailureCode.UI_WORKBENCH_NOT_READONLY, "Workbench is not proven read-only")
            missing.append("ui_workbench_readonly.workbench_read_only=true")
        if facts.get("workbench_repository_readback") is not True:
            self._add(codes, reasons, AcceptanceFailureCode.WORKBENCH_RUNTIME_NOT_STARTED, "Workbench repository readback did not run")
            missing.append("ui_workbench_readonly.workbench_repository_readback=true")
        if facts.get("backend_runtime_started") is not True:
            self._add(codes, reasons, AcceptanceFailureCode.WORKBENCH_RUNTIME_NOT_STARTED, "Workbench backend runtime startup is not proven")
            missing.append("ui_workbench_readonly.backend_runtime_started=true")
        if facts.get("frontend_runtime_started") is not True:
            self._add(codes, reasons, AcceptanceFailureCode.FRONTEND_RUNTIME_NOT_RUN, "Workbench frontend runtime/Vitest is not run")
            missing.append("ui_workbench_readonly.frontend_runtime_started=true")

    def _operator_api_semantics(
        self,
        facts: Mapping[str, Any],
        codes: list[AcceptanceFailureCode],
        reasons: list[str],
        missing: list[str],
    ) -> None:
        for item in _OPERATOR_API_FACTS:
            if facts.get(item) is not True:
                code = (
                    AcceptanceFailureCode.OPERATOR_API_NOT_STARTED
                    if item == "operator_api_started"
                    else AcceptanceFailureCode.OPERATOR_API_CONTROL_MISSING
                )
                self._add(codes, reasons, code, f"operator API fact is missing: {item}")
                missing.append(f"operator_api_security.{item}=true")
        if facts.get("live_route_present") is not False:
            self._add(codes, reasons, AcceptanceFailureCode.LIVE_ROUTE_PRESENT, "operator API route table contains or does not exclude live routes")
            missing.append("operator_api_security.live_route_present=false")

    def _data_chain_semantics(
        self,
        facts: Mapping[str, Any],
        codes: list[AcceptanceFailureCode],
        reasons: list[str],
        missing: list[str],
    ) -> None:
        source_class = self._data_source_class(facts)
        if source_class is None:
            self._add(codes, reasons, AcceptanceFailureCode.DATA_SOURCE_NOT_APPROVED, "data source class is missing or unrecognized")
            missing.append("data_to_promotion_chain.data_source_class in OFFICIAL_TUSHARE/APPROVED_PROXY_DATA_VENDOR/AUXILIARY")
        elif source_class is DataSourceClass.AUXILIARY:
            self._add(codes, reasons, AcceptanceFailureCode.DATA_SOURCE_NOT_APPROVED, "auxiliary data cannot satisfy the V1 data-to-promotion input gate")
            missing.append("data_to_promotion_chain.approved data source")
        elif source_class is DataSourceClass.OFFICIAL_TUSHARE:
            if facts.get("official_tushare_source") is not True:
                self._add(codes, reasons, AcceptanceFailureCode.DATA_SOURCE_NOT_APPROVED, "OFFICIAL_TUSHARE class lacks official source proof")
                missing.append("data_to_promotion_chain.official_tushare_source=true")
        elif source_class is DataSourceClass.APPROVED_PROXY_DATA_VENDOR:
            if facts.get("official_tushare_source") is True:
                self._add(
                    codes,
                    reasons,
                    AcceptanceFailureCode.APPROVED_PROXY_MASQUERADE_AS_OFFICIAL,
                    "approved proxy data is masquerading as official Tushare",
                )
            for item in (
                "operator_risk_accepted",
                "approved_proxy_input_qualified",
                "eligible_for_official_evidence_inputs",
            ):
                if facts.get(item) is not True:
                    self._add(codes, reasons, AcceptanceFailureCode.DATA_SOURCE_NOT_APPROVED, f"approved proxy fact is missing: {item}")
                    missing.append(f"data_to_promotion_chain.{item}=true")

        origin = str(facts.get("data_origin", facts.get("source_class", ""))).upper()
        if origin in {"SYNTHETIC", "FIXTURE", "FAKE"}:
            self._add(codes, reasons, AcceptanceFailureCode.SYNTHETIC_DATA_AS_REAL, "synthetic or fixture data claimed as V1 real input")
        if facts.get("frozen_snapshot") is not True or facts.get("fetch_timestamp_recorded") is not True:
            self._add(codes, reasons, AcceptanceFailureCode.REAL_SNAPSHOT_NOT_FROZEN, "real snapshot/fetch timestamp is not frozen")
            missing.append("data_to_promotion_chain.frozen_snapshot=true and fetch_timestamp_recorded=true")
        if facts.get("calendar_frozen") is not True or facts.get("historical_universe") is not True:
            self._add(
                codes,
                reasons,
                AcceptanceFailureCode.REAL_CALENDAR_OR_UNIVERSE_MISSING,
                "frozen calendar or historical universe is missing",
            )
            missing.append("data_to_promotion_chain.calendar_frozen=true and historical_universe=true")
        factor_count = facts.get("factor_count")
        if (
            not isinstance(factor_count, int)
            or isinstance(factor_count, bool)
            or not 1 <= factor_count <= 3
            or facts.get("factor_dag_run") is not True
            or facts.get("pit_run") is not True
        ):
            self._add(codes, reasons, AcceptanceFailureCode.REAL_FACTOR_RUN_MISSING, "one to three real-data factors did not run successfully")
            missing.append("data_to_promotion_chain.factor_count=1..3, factor_dag_run=true, pit_run=true")
        for field in ("snapshot_hash", "manifest_hash"):
            if not _HASH_RE.fullmatch(str(facts.get(field, ""))):
                self._add(codes, reasons, AcceptanceFailureCode.MISSING_REQUIRED_FACT, f"missing canonical {field}")
                missing.append(f"data_to_promotion_chain.{field}=sha256:<64 hex>")
        official = str(facts.get("official_evidence_authority", "")).upper()
        qlib_kind = str(facts.get("qlib_evidence_kind", "AUXILIARY_RESEARCH_EVIDENCE")).upper()
        if official in {"QLIB", "AUXILIARY_RESEARCH_EVIDENCE"} or qlib_kind not in {
            "AUXILIARY_RESEARCH_EVIDENCE",
            "UNAVAILABLE",
            "NOT_RUN",
        } or facts.get("qlib_changes_official_verdict") is True:
            self._add(
                codes,
                reasons,
                AcceptanceFailureCode.QLIB_AUXILIARY_AS_OFFICIAL,
                "Qlib auxiliary evidence is presented as official or changes the verdict",
            )
        if facts.get("official_evidence_run") is not True or official not in {"EVIDENCE_SERVICE", "OFFICIAL_EVIDENCE_SERVICE"}:
            self._add(codes, reasons, AcceptanceFailureCode.OFFICIAL_EVIDENCE_NOT_RUN, "official Evidence Service has not run on the accepted frozen data input")
            missing.append("data_to_promotion_chain.official_evidence_run=true by Evidence Service")
        if facts.get("promotion_run") is not True:
            self._add(codes, reasons, AcceptanceFailureCode.PROMOTION_NOT_RUN, "mechanical promotion has not run")
            missing.append("data_to_promotion_chain.promotion_run=true")
        decision = str(facts.get("promotion_decision", "")).upper()
        if facts.get("promotion_run") is True and decision not in {"CHALLENGER", "PROMOTE_CHALLENGER", "REJECT", "REVISE"}:
            self._add(codes, reasons, AcceptanceFailureCode.PROMOTION_NOT_RUN, "promotion decision is missing or not one of CHALLENGER/REJECT/REVISE")
            missing.append("data_to_promotion_chain.promotion_decision in CHALLENGER/REJECT/REVISE")

    def _replay_semantics(
        self,
        facts: Mapping[str, Any],
        codes: list[AcceptanceFailureCode],
        reasons: list[str],
        missing: list[str],
    ) -> None:
        if facts.get("challenger_available") is not True:
            self._add(codes, reasons, AcceptanceFailureCode.CHALLENGER_MISSING, "20-session Shadow replay requires an eligible CHALLENGER")
            missing.append("shadow_replay_20_session.challenger_available=true")
        sessions = facts.get("session_count")
        vintages = facts.get("daily_vintage_count")
        if sessions == 1:
            self._add(
                codes,
                reasons,
                AcceptanceFailureCode.SINGLE_DAY_REPLAY_AS_TWENTY_DAY,
                "single-day run_once cannot satisfy the 20-session gate",
            )
        if not isinstance(sessions, int) or isinstance(sessions, bool) or sessions < 20:
            self._add(codes, reasons, AcceptanceFailureCode.INSUFFICIENT_REPLAY_SESSIONS, "fewer than 20 sessions were replayed")
            missing.append("shadow_replay_20_session.session_count>=20")
        if not isinstance(vintages, int) or isinstance(vintages, bool) or not isinstance(sessions, int) or vintages < sessions:
            self._add(codes, reasons, AcceptanceFailureCode.REPLAY_INVARIANT_FAILED, "one frozen vintage per session is not proven")
            missing.append("shadow_replay_20_session.daily_vintage_count>=session_count")
        invariants = (
            "historical_frozen_data",
            "as_of_enforced",
            "append_only_ledger",
            "idempotent_rerun",
            "revision_immutable",
        )
        if any(facts.get(item) is not True for item in invariants):
            self._add(codes, reasons, AcceptanceFailureCode.REPLAY_INVARIANT_FAILED, "replay PIT/ledger/idempotency/revision invariant failed")
            missing.extend(f"shadow_replay_20_session.{item}=true" for item in invariants if facts.get(item) is not True)
        metrics = {str(item).lower() for item in facts.get("reported_metrics", ())}
        if not _REPLAY_METRICS.issubset(metrics):
            self._add(codes, reasons, AcceptanceFailureCode.REPLAY_INVARIANT_FAILED, "NAV/drawdown/turnover/cost/attribution report is incomplete")
            missing.append("shadow_replay_20_session.reported_metrics include nav,max_drawdown,turnover,cost,attribution")

    def _security_semantics(
        self,
        facts: Mapping[str, Any],
        codes: list[AcceptanceFailureCode],
        reasons: list[str],
        missing: list[str],
    ) -> None:
        if facts.get("live_route_present") is not False:
            self._add(codes, reasons, AcceptanceFailureCode.LIVE_ROUTE_PRESENT, "VeriQuant-only route table contains or does not exclude live routes")
            missing.append("security_scan.live_route_present=false")
        if facts.get("sealed_content_exposed") is True:
            self._add(codes, reasons, AcceptanceFailureCode.SEALED_CONTENT_EXPOSED, "sealed content exposure was detected")
        if facts.get("secret_or_token_exposed") is True:
            self._add(codes, reasons, AcceptanceFailureCode.SECRET_OR_TOKEN_EXPOSURE, "secret/token/path-bearing output was detected")
        if facts.get("path_traversal_allowed") is True:
            self._add(codes, reasons, AcceptanceFailureCode.PATH_TRAVERSAL_ALLOWED, "path traversal was permitted")
        missing_items = [item for item in _SECURITY_SCAN_FACTS if facts.get(item) is not True]
        if missing_items:
            self._add(codes, reasons, AcceptanceFailureCode.SECURITY_PROBE_FAILED, "security probes are incomplete: " + ", ".join(missing_items))
            missing.extend(f"security_scan.{item}=true" for item in missing_items)

    def _recovery_semantics(
        self,
        facts: Mapping[str, Any],
        codes: list[AcceptanceFailureCode],
        reasons: list[str],
        missing: list[str],
    ) -> None:
        if facts.get("forced_interruption") is not True or facts.get("resume_completed") is not True:
            self._add(codes, reasons, AcceptanceFailureCode.RESUME_DRILL_MISSING, "forced interruption/resume drill is missing")
            missing.append("reproducibility_restart_resume.forced_interruption=true and resume_completed=true")
        missing_items = [item for item in _RECOVERY_FACTS if facts.get(item) is not True]
        duplicates = facts.get("duplicate_ledger_events")
        if missing_items or duplicates != 0:
            self._add(
                codes,
                reasons,
                AcceptanceFailureCode.RECOVERY_INVARIANT_FAILED,
                "recovery/reproducibility invariant failed"
                + ((": " + ", ".join(missing_items)) if missing_items else ""),
            )
            missing.extend(f"reproducibility_restart_resume.{item}=true" for item in missing_items)
        if duplicates != 0:
            reasons.append("duplicate ledger event count is not zero")
            missing.append("reproducibility_restart_resume.duplicate_ledger_events=0")

    def _docs_semantics(
        self,
        facts: Mapping[str, Any],
        codes: list[AcceptanceFailureCode],
        reasons: list[str],
        missing: list[str],
    ) -> None:
        for item in _DOC_FACTS:
            if facts.get(item) is not True:
                code = (
                    AcceptanceFailureCode.RUNBOOK_MISSING
                    if item == "runbook_complete"
                    else AcceptanceFailureCode.REPORT_INCOMPLETE
                    if item == "report_complete"
                    else AcceptanceFailureCode.DOCS_INCOMPLETE
                )
                self._add(codes, reasons, code, f"documentation fact is missing: {item}")
                missing.append(f"docs_runbook_report.{item}=true")

    @staticmethod
    def _derive_status(
        evidence: V1GateEvidence, codes: list[AcceptanceFailureCode]
    ) -> AcceptanceStatus:
        fatal = {code for code in codes if code in _FATAL_CODES}
        # An honestly declared unavailable input (for example the Tencent
        # rehearsal) may report that its source is not approved without being
        # a forged V1 success.  Integrity, masquerade, sealed, secret, path,
        # test, and execution failures remain fatal in every status.
        if (
            not evidence.available
            and evidence.declared_status is AcceptanceStatus.UNAVAILABLE
        ):
            fatal.discard(AcceptanceFailureCode.DATA_SOURCE_NOT_APPROVED)
        if fatal:
            return AcceptanceStatus.FAILED
        if evidence.deferred:
            return AcceptanceStatus.DEFERRED
        if not evidence.available:
            return AcceptanceStatus.UNAVAILABLE
        if not evidence.implementation_present:
            return AcceptanceStatus.UNAVAILABLE
        if codes or not evidence.run_completed:
            return AcceptanceStatus.IMPLEMENTED_NOT_RUN
        return AcceptanceStatus.IMPLEMENTED_AND_TESTED

    @staticmethod
    def _earliest_blocker(
        codes: list[AcceptanceFailureCode], missing: list[str]
    ) -> str | None:
        if not codes and not missing:
            return None
        order = (
            AcceptanceFailureCode.MISSING_GATE_EVIDENCE,
            AcceptanceFailureCode.FRONTEND_RUNTIME_NOT_RUN,
            AcceptanceFailureCode.WORKBENCH_RUNTIME_NOT_STARTED,
            AcceptanceFailureCode.OPERATOR_API_NOT_STARTED,
            AcceptanceFailureCode.LIVE_ROUTE_PRESENT,
            AcceptanceFailureCode.SEALED_CONTENT_EXPOSED,
            AcceptanceFailureCode.SECRET_OR_TOKEN_EXPOSURE,
            AcceptanceFailureCode.PATH_TRAVERSAL_ALLOWED,
            AcceptanceFailureCode.DATA_SOURCE_NOT_APPROVED,
            AcceptanceFailureCode.APPROVED_PROXY_MASQUERADE_AS_OFFICIAL,
            AcceptanceFailureCode.REAL_SNAPSHOT_NOT_FROZEN,
            AcceptanceFailureCode.REAL_CALENDAR_OR_UNIVERSE_MISSING,
            AcceptanceFailureCode.REAL_FACTOR_RUN_MISSING,
            AcceptanceFailureCode.OFFICIAL_EVIDENCE_NOT_RUN,
            AcceptanceFailureCode.PROMOTION_NOT_RUN,
            AcceptanceFailureCode.CHALLENGER_MISSING,
            AcceptanceFailureCode.INSUFFICIENT_REPLAY_SESSIONS,
            AcceptanceFailureCode.RESUME_DRILL_MISSING,
            AcceptanceFailureCode.RUNBOOK_MISSING,
            AcceptanceFailureCode.REPORT_INCOMPLETE,
        )
        unique = set(codes)
        for code in order:
            if code in unique:
                return code.value
        if codes:
            return sorted(set(codes), key=lambda item: item.value)[0].value
        return missing[0]

    @staticmethod
    def _data_source_class(facts: Mapping[str, Any]) -> DataSourceClass | None:
        raw = str(
            facts.get("data_source_class")
            or facts.get("provider_class")
            or facts.get("source_class")
            or ""
        ).upper()
        aliases = {
            "TUSHARE": DataSourceClass.OFFICIAL_TUSHARE,
            "REAL_TUSHARE": DataSourceClass.OFFICIAL_TUSHARE,
            "TUSHARE_REAL": DataSourceClass.OFFICIAL_TUSHARE,
            "OFFICIAL_TUSHARE": DataSourceClass.OFFICIAL_TUSHARE,
            "APPROVED_PROXY": DataSourceClass.APPROVED_PROXY_DATA_VENDOR,
            "APPROVED_PROXY_DATA_VENDOR": DataSourceClass.APPROVED_PROXY_DATA_VENDOR,
            "TUSHARE_COMPATIBLE_PROXY": DataSourceClass.APPROVED_PROXY_DATA_VENDOR,
            "AUXILIARY": DataSourceClass.AUXILIARY,
            "AUXILIARY_ONLY": DataSourceClass.AUXILIARY,
            "AUXILIARY_RESEARCH_EVIDENCE": DataSourceClass.AUXILIARY,
            "TENCENT_OPERATIONAL_REHEARSAL": DataSourceClass.AUXILIARY,
        }
        return aliases.get(raw)

    def _resolve_input_path(self, relative_path: str) -> Path:
        if not isinstance(relative_path, str) or not relative_path.strip() or "\x00" in relative_path:
            raise ValueError("invalid evidence path")
        candidate = Path(relative_path.replace("\\", "/"))
        if candidate.is_absolute() or any(part in {"", ".", ".."} for part in candidate.parts):
            raise ValueError("evidence paths must be canonical repository-relative paths")
        resolved = (self.repo_root / candidate).resolve()
        try:
            resolved.relative_to(self.repo_root)
        except ValueError as exc:
            raise ValueError("evidence path escapes repository") from exc
        return resolved

    def _safe_output_path(self, value: str | Path) -> Path:
        candidate = Path(value)
        resolved = candidate.resolve() if candidate.is_absolute() else (self.repo_root / candidate).resolve()
        try:
            resolved.relative_to(self.repo_root)
        except ValueError as exc:
            raise ValueError("report output path escapes repository") from exc
        return resolved

    @staticmethod
    def _atomic_write(path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            newline="\n",
            delete=False,
            dir=str(path.parent),
            prefix=f".{path.name}.",
            suffix=".tmp",
        )
        name = handle.name
        try:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
            handle.close()
            os.replace(name, path)
        finally:
            if not handle.closed:
                handle.close()
            if os.path.exists(name):
                os.unlink(name)

    @staticmethod
    def _add(
        codes: list[AcceptanceFailureCode],
        reasons: list[str],
        code: AcceptanceFailureCode,
        reason: str,
    ) -> None:
        codes.append(code)
        reasons.append(reason)
