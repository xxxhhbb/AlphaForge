from __future__ import annotations

import sys
from datetime import datetime, timezone

import pytest

from veriquant.agent_research_sandbox.process_runner import (
    ProcessFailureCode,
    ProcessFencedResearchRunner,
    ProcessRunStatus,
    ResearchProcessConfig,
    ResearchProcessRequest,
    canonical_hash,
)
from veriquant.agent_research_sandbox import (
    ExperimentCodeIdentity,
    ExperimentDataIdentity,
    ExperimentReviewGate,
    ExperimentReviewPolicy,
    ExperimentReviewRequest,
    SandboxAmendmentIdentity,
)


AMENDMENT_HASH = "sha256:" + "a" * 64
CODE = """def run_research(rows, seed):
    return [{"symbol": row["symbol"], "decision_time": row["decision_time"], "score": row["close"] + seed} for row in rows]
"""


def request(**overrides):
    source = overrides.get("code_text", CODE)
    amendment = SandboxAmendmentIdentity(
        amendment_id="A02",
        amendment_version="research_sandbox_amendment_v1",
        constitution_hash=AMENDMENT_HASH,
        policy_hash="sha256:" + "b" * 64,
        authorized_at=datetime(2026, 7, 21, tzinfo=timezone.utc),
    )
    policy = ExperimentReviewPolicy()
    code_identity = ExperimentCodeIdentity(
        source_hash="sha256:" + __import__("hashlib").sha256(source.encode()).hexdigest(),
        python_runtime_hash="sha256:" + "c" * 64,
        dependency_manifest_hash="sha256:" + "d" * 64,
        review_policy_hash=policy.artifact_hash,
        amendment_hash=amendment.artifact_hash,
    )
    data_identity = ExperimentDataIdentity(
        dataset_hash="sha256:" + "e" * 64,
        snapshot_hash="sha256:" + "f" * 64,
        universe_hash="sha256:" + "1" * 64,
        schema_hash="sha256:" + "2" * 64,
        availability_policy_hash="sha256:" + "3" * 64,
        decision_start=datetime(2026, 1, 5, tzinfo=timezone.utc),
        decision_end=datetime(2026, 1, 5, tzinfo=timezone.utc),
    )
    decision = ExperimentReviewGate().review(
        ExperimentReviewRequest(
            experiment_id="experiment_1",
            proposer_id="researcher_1",
            reviewer_id="reviewer_1",
            source_text=source,
            code_identity=code_identity,
            data_identity=data_identity,
            amendment=amendment,
            policy=policy,
        )
    )
    values = {
        "run_id": "run_1",
        "experiment_id": "experiment_1",
        "code_text": source,
        "code_hash": code_identity.source_hash,
        "code_identity_hash": code_identity.artifact_hash,
        "data_identity_hash": data_identity.artifact_hash,
        "review_decision": decision,
        "feature_rows": ({"symbol": "000001.SZ", "decision_time": "2026-01-05T07:00:00Z", "close": 10.0},),
        "output_kind": "ScoreTable",
        "seed": 7,
        "amendment_hash": amendment.artifact_hash,
    }
    values.update(overrides)
    return ResearchProcessRequest(**values)


def runner(tmp_path, **overrides):
    return ProcessFencedResearchRunner(
        ResearchProcessConfig(python_path=sys.executable, sandbox_root=tmp_path, **overrides)
    )


def test_process_runner_executes_reviewed_program_and_replays_stably(tmp_path) -> None:
    first = runner(tmp_path).execute(request())
    second = runner(tmp_path).execute(request())
    assert first.status is ProcessRunStatus.COMPLETED_AUXILIARY
    assert first.output == [{"decision_time": "2026-01-05T07:00:00Z", "score": 17.0, "symbol": "000001.SZ"}]
    assert first.output_hash == second.output_hash
    assert first.artifact_hash == second.artifact_hash
    assert first.official_evidence_eligible is False


def test_future_labels_and_authority_fields_never_enter_child_input() -> None:
    for field in ("future_return_5d", "sealed_metric", "official_metrics", "orders"):
        with pytest.raises(ValueError, match=ProcessFailureCode.FORBIDDEN_INPUT.value):
            request(feature_rows=({"symbol": "000001.SZ", field: 1.0},))


def test_os_sandbox_requirement_fails_closed(tmp_path) -> None:
    result = runner(tmp_path, require_os_sandbox=True).execute(request())
    assert result.status is ProcessRunStatus.BLOCKED
    assert result.failure_code is ProcessFailureCode.OS_SANDBOX_UNAVAILABLE
    assert result.output is None


def test_rejected_static_review_cannot_reach_process_runner() -> None:
    source = "import os\ndef run_research(rows, seed):\n    return []\n"
    with pytest.raises(ValueError, match=ProcessFailureCode.REVIEW_NOT_ACCEPTED.value):
        request(code_text=source)


def test_timeout_kills_child_and_returns_typed_failure(tmp_path) -> None:
    code = """def run_research(rows, seed):
    while True:
        pass
"""
    result = runner(tmp_path, timeout_seconds=0.1).execute(
        request(code_text=code)
    )
    assert result.status is ProcessRunStatus.TIMEOUT
    assert result.failure_code is ProcessFailureCode.TIMEOUT


def test_uncontracted_stdout_and_forbidden_result_authority_fail_closed(tmp_path) -> None:
    noisy = """def run_research(rows, seed):
    print("noise")
    return []
"""
    noisy_result = runner(tmp_path).execute(request(code_text=noisy))
    assert noisy_result.failure_code is ProcessFailureCode.OUTPUT_INVALID

    authority = """def run_research(rows, seed):
    return {"promotion": "PROMOTE"}
"""
    authority_result = runner(tmp_path).execute(request(code_text=authority))
    assert authority_result.failure_code is ProcessFailureCode.OUTPUT_INVALID
