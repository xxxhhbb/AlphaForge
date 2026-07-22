"""Mechanical evidence builder for Agent-F V1 acceptance."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any, Mapping, Sequence

from veriquant.v1_acceptance.api import (
    AcceptanceStatus,
    ArtifactKind,
    ArtifactReference,
    TestRunEvidence,
    TraceableClaim,
    V1AcceptanceEvaluator,
    V1AcceptanceVerdict,
    V1GateEvidence,
    V1GateId,
)


@dataclass(frozen=True, kw_only=True)
class EvidenceArtifactInput:
    artifact_id: str
    kind: ArtifactKind
    relative_path: str


@dataclass(frozen=True, kw_only=True)
class ExecutedTestInput:
    test_id: str
    command: str
    log_artifact_id: str
    exit_code: int
    passed_count: int
    failed_count: int = 0
    skipped_count: int = 0
    xfailed_count: int = 0


@dataclass(frozen=True, kw_only=True)
class GateEvidenceInput:
    gate_id: V1GateId
    implementation_present: bool
    run_completed: bool
    available: bool
    declared_status: AcceptanceStatus
    artifacts: tuple[EvidenceArtifactInput, ...]
    tests: tuple[ExecutedTestInput, ...]
    facts: Mapping[str, Any]
    package_ids: tuple[str, ...]
    claim_statement: str
    reason: str = ""
    deferred: bool = False


class V1AcceptanceEvidenceBuilder:
    """Hash actual artifacts and bind every claim to code/data/policy/tests."""

    def __init__(self, repo_root: str | Path) -> None:
        self.repo_root = Path(repo_root).resolve()

    def build_gate(self, item: GateEvidenceInput) -> V1GateEvidence:
        references = tuple(self._reference(value) for value in item.artifacts)
        ids = {value.artifact_id for value in references}
        if len(ids) != len(references):
            raise ValueError("artifact ids must be unique")
        tests = tuple(
            TestRunEvidence(
                test_id=value.test_id,
                command=value.command,
                log_artifact_id=value.log_artifact_id,
                exit_code=value.exit_code,
                passed_count=value.passed_count,
                failed_count=value.failed_count,
                skipped_count=value.skipped_count,
                xfailed_count=value.xfailed_count,
                declared_status="PASSED" if value.exit_code == 0 and value.failed_count == 0 else "FAILED",
            )
            for value in item.tests
        )
        if any(test.log_artifact_id not in ids for test in tests):
            raise ValueError("test log must reference an included artifact")
        by_kind = {
            kind: tuple(value.artifact_id for value in references if value.kind is kind)
            for kind in ArtifactKind
        }
        if not by_kind[ArtifactKind.CODE] or not by_kind[ArtifactKind.POLICY]:
            raise ValueError("gate evidence requires code and policy artifacts")
        data_ids = tuple(
            value.artifact_id
            for value in references
            if value.kind in {ArtifactKind.DATA, ArtifactKind.MANIFEST, ArtifactKind.REPORT}
        )
        if not data_ids or not tests:
            raise ValueError("gate evidence requires data/report and executed test evidence")
        claim = TraceableClaim(
            claim_id=f"{item.gate_id.value}-traceable-claim",
            statement=item.claim_statement,
            code_artifact_ids=by_kind[ArtifactKind.CODE],
            data_artifact_ids=data_ids,
            policy_artifact_ids=by_kind[ArtifactKind.POLICY],
            test_ids=tuple(value.test_id for value in tests),
        )
        return V1GateEvidence(
            gate_id=item.gate_id,
            implementation_present=item.implementation_present,
            run_completed=item.run_completed,
            available=item.available,
            deferred=item.deferred,
            declared_status=item.declared_status,
            artifacts=references,
            tests=tests,
            claims=(claim,),
            required_artifact_ids=tuple(value.artifact_id for value in references),
            facts=dict(item.facts),
            package_ids=tuple(item.package_ids),
            reason=item.reason,
        )

    def evaluate_and_write(
        self,
        inputs: Sequence[GateEvidenceInput],
        *,
        json_path: str | Path,
        markdown_path: str | Path,
    ) -> V1AcceptanceVerdict:
        evidence = tuple(self.build_gate(item) for item in inputs)
        evaluator = V1AcceptanceEvaluator(self.repo_root)
        verdict = evaluator.evaluate(evidence)
        evaluator.write_reports(verdict, json_path=json_path, markdown_path=markdown_path)
        return verdict

    def _reference(self, item: EvidenceArtifactInput) -> ArtifactReference:
        path = self._resolve(item.relative_path)
        content = path.read_bytes()
        if not content:
            raise ValueError(f"acceptance artifact is empty: {item.artifact_id}")
        return ArtifactReference(
            artifact_id=item.artifact_id,
            kind=item.kind,
            relative_path=item.relative_path.replace("\\", "/"),
            sha256="sha256:" + sha256(content).hexdigest(),
        )

    def _resolve(self, value: str) -> Path:
        candidate = Path(value.replace("\\", "/"))
        if candidate.is_absolute() or any(part in {"", ".", ".."} for part in candidate.parts):
            raise ValueError("acceptance evidence path must be repository-relative")
        resolved = (self.repo_root / candidate).resolve()
        try:
            resolved.relative_to(self.repo_root)
        except ValueError as exc:
            raise ValueError("acceptance evidence path escapes repository") from exc
        if not resolved.is_file():
            raise ValueError(f"acceptance evidence file is missing: {value}")
        return resolved


def tencent_gate2_unavailable_facts() -> dict[str, Any]:
    """Tencent is operational rehearsal only and cannot close real Tushare Gate 2."""

    return {
        "data_origin": "TENCENT_OPERATIONAL_REHEARSAL",
        "synthetic": False,
        "frozen_snapshot": False,
        "runner_success": False,
        "official_evidence_authority": "EVIDENCE_SERVICE",
        "qlib_evidence_kind": "NOT_RUN",
        "qlib_changes_official_verdict": False,
        "gate2_reason": "A successful bounded real Tushare snapshot is not available",
    }

