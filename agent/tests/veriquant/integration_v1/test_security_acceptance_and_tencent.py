from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from veriquant.contracts import ContractValidationError, FailureCode
from veriquant.data import canonical_content_hash
from veriquant.integration.v1_acceptance import (
    EvidenceArtifactInput,
    ExecutedTestInput,
    GateEvidenceInput,
    V1AcceptanceEvidenceBuilder,
    tencent_gate2_unavailable_facts,
)
from veriquant.integration.v1_security import run_veriquant_only_security_acceptance
from veriquant.integration.v1_tencent import reject_tencent_official_use, tencent_workbench_projection
from veriquant.tencent_rehearsal.api import (
    AdjustmentSafetyVerdict,
    TencentBoundedRequest,
    TencentSnapshotManifest,
)
from veriquant.v1_acceptance.api import AcceptanceStatus, ArtifactKind, V1AcceptanceEvaluator, V1GateId


def H(value):
    return canonical_content_hash(value)


def _tencent_manifest() -> TencentSnapshotManifest:
    calendar_hash = H({"calendar": "frozen-sse-szse"})
    request = TencentBoundedRequest(
        dataset_id="tencent-rehearsal",
        request_id="bounded-sample",
        codes=("000001.SZ", "600000.SH"),
        start_date="2024-01-02",
        end_date="2024-03-29",
        calendar_id="cn-ashare-sse-szse",
        calendar_hash=calendar_hash,
    )
    now = datetime(2024, 4, 1, 9, 0, tzinfo=timezone.utc)
    return TencentSnapshotManifest(
        request=request,
        request_hash=request.artifact_hash,
        fetched_at=now,
        frozen_at=now,
        raw_content_hash=H({"raw": "qfq"}),
        normalized_content_hash=H({"records": "qfq"}),
        calendar_hash=calendar_hash,
        row_count=2,
        rows_by_code={"000001.SZ": 1, "600000.SH": 1},
        adjustment_verdict=AdjustmentSafetyVerdict.QFQ_ONLY,
        adjustment_attestation_hash=None,
        raw_capture_kind="LEGACY_LOADER_CANONICAL_OUTPUT",
        availability_semantics_verified=False,
        pit_eligible=False,
    )


def test_tencent_projection_is_auxiliary_and_cannot_change_official_authority() -> None:
    manifest = _tencent_manifest()
    projection = tencent_workbench_projection(manifest)

    assert projection["authority_class"] == "AUXILIARY_RESEARCH_EVIDENCE"
    assert projection["adjustment_verdict"] == "QFQ_ONLY"
    assert projection["promotion_eligible"] is False
    assert projection["official_v1_tushare_smoke"] is False
    with pytest.raises(ContractValidationError) as caught:
        reject_tencent_official_use(manifest, requested_authority="OFFICIAL_EVIDENCE")
    assert caught.value.code is FailureCode.UNAUTHORIZED_CAPABILITY


def _write(root: Path, relative: str, content: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_acceptance_evidence_keeps_gate2_unavailable_for_tencent(tmp_path: Path) -> None:
    _write(tmp_path, "code.py", "VALUE = 1\n")
    _write(tmp_path, "policy.md", "# Tushare required\n")
    _write(tmp_path, "tencent.json", '{"authority":"AUXILIARY_RESEARCH_EVIDENCE"}\n')
    _write(tmp_path, "test.log", "1 passed in 0.01s\n")
    item = GateEvidenceInput(
        gate_id=V1GateId.REAL_ASHARE_SMOKE,
        implementation_present=True,
        run_completed=False,
        available=False,
        declared_status=AcceptanceStatus.UNAVAILABLE,
        artifacts=(
            EvidenceArtifactInput(artifact_id="code", kind=ArtifactKind.CODE, relative_path="code.py"),
            EvidenceArtifactInput(artifact_id="policy", kind=ArtifactKind.POLICY, relative_path="policy.md"),
            EvidenceArtifactInput(artifact_id="tencent", kind=ArtifactKind.DATA, relative_path="tencent.json"),
            EvidenceArtifactInput(artifact_id="log", kind=ArtifactKind.TEST_LOG, relative_path="test.log"),
        ),
        tests=(
            ExecutedTestInput(
                test_id="typed-unavailable",
                command="python -m pytest typed-unavailable",
                log_artifact_id="log",
                exit_code=0,
                passed_count=1,
            ),
        ),
        facts=tencent_gate2_unavailable_facts(),
        package_ids=("tencent_rehearsal", "real_ashare_data"),
        claim_statement="Tencent remains auxiliary and the mandatory Tushare smoke is unavailable",
    )
    evidence = V1AcceptanceEvidenceBuilder(tmp_path).build_gate(item)
    verdict = V1AcceptanceEvaluator(tmp_path).evaluate((evidence,))
    gate = next(value for value in verdict.gates if value.gate_id is V1GateId.REAL_ASHARE_SMOKE)
    assert gate.status is AcceptanceStatus.UNAVAILABLE


def test_veriquant_only_security_probe_passes_clean_app_and_rejects_sealed(tmp_path: Path) -> None:
    import veriquant_api_server

    app = veriquant_api_server.create_app(artifact_root=tmp_path)
    manifest = veriquant_api_server.build_route_manifest(app, artifact_root=tmp_path)
    report = run_veriquant_only_security_acceptance(
        manifest=manifest,
        workbench_payload={"sealed_status": "PASSED_REDACTED", "evidence": {"net_icir": 1.0}},
        source_texts={"veriquant_api_server.py": Path(veriquant_api_server.__file__).read_text(encoding="utf-8")},
    )
    assert report.passed is True

    leaked = run_veriquant_only_security_acceptance(
        manifest=manifest,
        workbench_payload={"sealed_raw": {"rank": 1}},
        source_texts={"safe.py": "ROUTES = ('/healthz',)\n"},
    )
    assert leaked.passed is True
    assert "sealed_raw" not in str(leaked.to_dict())

