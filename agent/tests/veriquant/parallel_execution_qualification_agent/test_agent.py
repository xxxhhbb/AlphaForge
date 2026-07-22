from __future__ import annotations

import json

from veriquant.execution_qualification_agent import QualificationInput, TaskStatus, schedule, write_run


def test_agent_queues_only_semantic_records_without_data_or_pit_gaps(tmp_path) -> None:
    run = schedule(
        (
            QualificationInput("ready", "a", "EXECUTION_READY", (), (), (), True),
            QualificationInput("queue", "b", "SEMANTICS_REVIEW_REQUIRED", ("UNKNOWN_CALL",), (), (), False),
            QualificationInput("data", "c", "DATA_FIELD_MISSING", (), ("amount",), (), False),
        ),
        projection_hash="sha256:" + "a" * 64,
    )
    assert run.product_gate == "NO_GO"
    assert [item.status for item in run.tasks] == [TaskStatus.BLOCKED, TaskStatus.QUEUED, TaskStatus.STATIC_READY]
    assert len(next(item for item in run.tasks if item.factor_id == "queue").required_evidence) == 8
    target = write_run(run, tmp_path)
    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload["authority"]["may_start_product_loop"] is False
    assert payload["run_hash"].startswith("sha256:")


def test_agent_never_reclassifies_a_blocked_factor_as_ready() -> None:
    run = schedule(
        (QualificationInput("pit", "a", "SEMANTICS_REVIEW_REQUIRED", (), (), ("industry",), False),),
        projection_hash="sha256:" + "b" * 64,
    )
    assert run.tasks[0].status is TaskStatus.BLOCKED


def test_agent_is_replay_deterministic_and_never_opens_product_gate() -> None:
    inputs = (
        QualificationInput("semantic", "a", "SEMANTICS_REVIEW_REQUIRED", ("PARITY",), (), (), False),
        QualificationInput("ready", "a", "EXECUTION_READY", (), (), (), True),
    )
    first = schedule(inputs, projection_hash="sha256:" + "c" * 64)
    second = schedule(tuple(reversed(inputs)), projection_hash="sha256:" + "c" * 64)
    assert first.payload()["run_hash"] == second.payload()["run_hash"]
    assert first.payload()["authority"]["may_create_official_evidence"] is False
