from __future__ import annotations

import hashlib
import shutil
import subprocess
from datetime import datetime, timezone

import pytest

from veriquant.agent_research_sandbox import (
    ExperimentCodeIdentity,
    ExperimentDataIdentity,
    ExperimentReviewGate,
    ExperimentReviewPolicy,
    ExperimentReviewRequest,
    SandboxAmendmentIdentity,
)
from veriquant.agent_research_sandbox.docker_runner import (
    DockerResearchConfig,
    DockerResearchRequest,
    DockerResearchRunner,
    DockerTrainingObservation,
)
from veriquant.agent_research_sandbox.process_runner import (
    ProcessFailureCode,
    ProcessIsolationLevel,
    ProcessRunStatus,
    ResearchProcessRequest,
)


IMAGE = "veriquant-m2-test:local"
AMENDMENT_HASH = "sha256:" + "a" * 64
SCORE_CODE = '''def run_research(rows, seed):
    row = rows[0]
    return {"rows": [{
        "contract_type": "ScoreRow",
        "schema_version": "alphaforge.agent-research-sandbox/v1",
        "symbol": row["symbol"],
        "decision_time": row["decision_time"],
        "source_available_at": row["available_at"],
        "score": row["close"] + seed,
        "label_horizon_sessions": 5
    }]}
'''


def _local_image_id() -> str | None:
    if shutil.which("docker") is None:
        return None
    try:
        result = subprocess.run(
            ("docker", "image", "inspect", IMAGE, "--format", "{{.Id}}"),
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    value = result.stdout.decode("ascii", errors="ignore").strip()
    return value if result.returncode == 0 and value.startswith("sha256:") else None


def _request(source: str = SCORE_CODE) -> ResearchProcessRequest:
    amendment = SandboxAmendmentIdentity(
        amendment_id="A02",
        amendment_version="research_sandbox_amendment_v1",
        constitution_hash=AMENDMENT_HASH,
        policy_hash="sha256:" + "b" * 64,
        authorized_at=datetime(2026, 7, 21, tzinfo=timezone.utc),
    )
    policy = ExperimentReviewPolicy()
    code_identity = ExperimentCodeIdentity(
        source_hash="sha256:" + hashlib.sha256(source.encode()).hexdigest(),
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
    return ResearchProcessRequest(
        run_id="run_1",
        experiment_id="experiment_1",
        code_text=source,
        code_hash=code_identity.source_hash,
        code_identity_hash=code_identity.artifact_hash,
        data_identity_hash=data_identity.artifact_hash,
        review_decision=decision,
        feature_rows=(
            {
                "symbol": "000001.SZ",
                "decision_time": "2026-01-05T00:00:00Z",
                "available_at": "2026-01-05T00:00:00Z",
                "close": 10.0,
            },
        ),
        output_kind="ScoreTable",
        seed=7,
        amendment_hash=amendment.artifact_hash,
    )


def _runner(tmp_path, *, timeout: float = 20.0, image_id: str | None = None) -> DockerResearchRunner:
    return DockerResearchRunner(
        DockerResearchConfig(
            sandbox_root=tmp_path,
            image_name=IMAGE,
            expected_image_id=image_id or ("sha256:" + "0" * 64),
            timeout_seconds=timeout,
        )
    )


def test_real_docker_smoke_or_typed_unavailable_without_skip(tmp_path) -> None:
    image_id = _local_image_id()
    result = _runner(tmp_path, image_id=image_id).execute(_request())
    if image_id is None:
        assert result.status is ProcessRunStatus.BLOCKED
        assert result.failure_code is ProcessFailureCode.OS_SANDBOX_UNAVAILABLE
        return
    assert result.status is ProcessRunStatus.COMPLETED_AUXILIARY
    assert result.isolation_level is ProcessIsolationLevel.OS_SANDBOXED
    assert result.official_evidence_eligible is True
    assert result.output["contract_type"] == "ScoreTable"
    assert result.output["rows"][0]["score"] == 17.0


def test_replay_is_stable_in_real_docker_when_available(tmp_path) -> None:
    image_id = _local_image_id()
    first = _runner(tmp_path, image_id=image_id).execute(_request())
    second = _runner(tmp_path, image_id=image_id).execute(_request())
    if image_id is None:
        assert first.failure_code is second.failure_code is ProcessFailureCode.OS_SANDBOX_UNAVAILABLE
        return
    assert first.output_hash == second.output_hash
    assert first.artifact_hash == second.artifact_hash


def test_command_contains_all_os_isolation_and_resource_flags(tmp_path) -> None:
    image_id = "sha256:" + "9" * 64
    runner = _runner(tmp_path, image_id=image_id)
    command = runner._docker_command("alphaforge-research-test", tmp_path.resolve())
    rendered = " ".join(command)
    for required in (
        "--network none",
        "--read-only",
        "--cap-drop ALL",
        "--security-opt no-new-privileges:true",
        "--user 65534:65534",
        "--pids-limit 32",
        "--memory 256m",
        "--cpus 0.5",
        "--ipc none",
        "--tmpfs /tmp:rw,noexec,nosuid,nodev,size=16m",
        "--pull never",
    ):
        assert required in rendered
    mounts = [item for item in command if item.startswith("type=bind")]
    assert len(mounts) == 1 and "target=/sandbox,readonly" in mounts[0]
    assert command[-4] == image_id


def test_mutated_or_missing_local_image_fails_closed(tmp_path) -> None:
    result = _runner(tmp_path, image_id="sha256:" + "0" * 64).execute(_request())
    assert result.status is ProcessRunStatus.BLOCKED
    assert result.failure_code is ProcessFailureCode.OS_SANDBOX_UNAVAILABLE
    assert result.official_evidence_eligible is False


def test_mechanical_gate_blocks_network_and_file_code_before_docker(tmp_path) -> None:
    for source in (
        "import socket\ndef run_research(rows, seed):\n    return {}\n",
        "def run_research(rows, seed):\n    return open('/etc/passwd').read()\n",
    ):
        with pytest.raises(ValueError, match=ProcessFailureCode.REVIEW_NOT_ACCEPTED.value):
            _request(source)


def test_contract_identity_smuggling_fails_closed(tmp_path) -> None:
    source = '''def run_research(rows, seed):
    return {"experiment_id": "forged", "rows": []}
'''
    image_id = _local_image_id()
    result = _runner(tmp_path, image_id=image_id).execute(_request(source))
    if image_id is None:
        assert result.failure_code is ProcessFailureCode.OS_SANDBOX_UNAVAILABLE
    else:
        assert result.failure_code is ProcessFailureCode.PROTOCOL_FAILED
        assert result.diagnostic.startswith("PROTOCOL_FAILED:RuntimeError:line")
        assert "forged" not in result.diagnostic
        assert result.official_evidence_eligible is False


def test_timeout_force_removes_unique_ephemeral_container(tmp_path) -> None:
    source = "def run_research(rows, seed):\n    while True:\n        pass\n"
    image_id = _local_image_id()
    result = _runner(tmp_path, timeout=0.3, image_id=image_id).execute(_request(source))
    if image_id is None:
        assert result.failure_code is ProcessFailureCode.OS_SANDBOX_UNAVAILABLE
        return
    assert result.status is ProcessRunStatus.TIMEOUT
    assert result.failure_code is ProcessFailureCode.TIMEOUT
    remaining = subprocess.run(
        ("docker", "ps", "-aq", "--filter", "name=^/alphaforge-research-"),
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        timeout=10,
    ).stdout.decode().strip()
    assert remaining == ""


def test_labels_never_enter_container_input() -> None:
    base = _request()
    with pytest.raises(ValueError, match=ProcessFailureCode.FORBIDDEN_INPUT.value):
        ResearchProcessRequest(
            run_id=base.run_id,
            experiment_id=base.experiment_id,
            code_text=base.code_text,
            code_hash=base.code_hash,
            code_identity_hash=base.code_identity_hash,
            data_identity_hash=base.data_identity_hash,
            review_decision=base.review_decision,
            feature_rows=({"symbol": "000001.SZ", "future_return_5d": 0.1},),
            output_kind=base.output_kind,
            seed=base.seed,
            amendment_hash=base.amendment_hash,
        )


def test_historical_training_outcome_is_separate_from_label_free_prediction_rows(tmp_path) -> None:
    source = '''def run_research(training_rows, prediction_rows, seed):
    average = sum(row["target_value"] for row in training_rows) / len(training_rows)
    row = prediction_rows[0]
    return {"rows": [{
        "contract_type": "ScoreRow",
        "schema_version": "alphaforge.agent-research-sandbox/v1",
        "symbol": row["symbol"],
        "decision_time": row["decision_time"],
        "source_available_at": row["available_at"],
        "score": row["close"] * average + seed,
        "label_horizon_sessions": 5
    }]}
'''
    base = _request(source)
    request = DockerResearchRequest(
        process_request=base,
        training_rows=(
            DockerTrainingObservation(
                symbol="000002.SZ",
                decision_time="2025-12-01T00:00:00Z",
                source_available_at="2025-12-01T00:00:00Z",
                outcome_available_at="2025-12-08T00:00:00Z",
                target_value=0.2,
                features={"close": 8.0},
            ),
        ),
        training_cutoff="2026-01-04T00:00:00Z",
    )
    image_id = _local_image_id()
    result = _runner(tmp_path, image_id=image_id).execute(request)
    if image_id is None:
        assert result.failure_code is ProcessFailureCode.OS_SANDBOX_UNAVAILABLE
        return
    assert result.output["rows"][0]["score"] == 9.0
    assert result.entrypoint_version == "TRAIN_PREDICT_V2"
    assert result.training_input_hash == request.training_input_hash
    assert result.prediction_input_hash == request.prediction_input_hash
    assert result.training_input_hash != result.prediction_input_hash
    assert all("target" not in key.lower() for key in base.feature_rows[0])


def test_training_outcome_unavailable_at_cutoff_is_rejected() -> None:
    base = _request()
    with pytest.raises(ValueError, match="unavailable at training_cutoff"):
        DockerResearchRequest(
            process_request=base,
            training_rows=(
                DockerTrainingObservation(
                    symbol="000002.SZ",
                    decision_time="2025-12-01T00:00:00Z",
                    source_available_at="2025-12-01T00:00:00Z",
                    outcome_available_at="2026-01-06T00:00:00Z",
                    target_value=0.2,
                    features={"close": 8.0},
                ),
            ),
            training_cutoff="2026-01-04T00:00:00Z",
        )
