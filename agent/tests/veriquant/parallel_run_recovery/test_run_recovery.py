from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))

from veriquant.contracts import FailureCode
from veriquant.data import canonical_content_hash
from veriquant.run_recovery.api import (
    LeaseUnavailable,
    ProviderUnavailable,
    RecoveryError,
    RecoveryEventType,
    RecoveryInterrupted,
    RecoveryStatus,
    RunCheckpointStore,
    RunRecoveryController,
    StageExecutionResult,
    StageIdentity,
    StageTask,
)


class DeterministicClock:
    def __init__(self) -> None:
        self.current = datetime(2026, 7, 18, 0, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        value = self.current
        self.current = self.current + timedelta(seconds=1)
        return value


def _h(value) -> str:
    return canonical_content_hash(value)


def _daily_tasks(run_id: str, *, days: int = 20, branch_id: str = "C.daily", provider_id: str = "daily"):
    tasks: list[StageTask] = []
    previous_artifact = _h({"seed": run_id, "branch": branch_id})
    previous_identity_hash = None
    for day in range(1, days + 1):
        expected = _h({"branch": branch_id, "day": day, "input": previous_artifact})
        identity = StageIdentity(
            run_id=run_id,
            branch_id=branch_id,
            stage_id=f"C.day.{day:03d}",
            ordinal=day,
            input_hash=previous_artifact,
            config_hash=_h({"config": "daily", "day": day}),
            code_hash=_h({"code": "fake-provider/v1"}),
            policy_hash=_h({"policy": "checkpoint-daily/v1"}),
            expected_artifact_hash=expected,
        )
        tasks.append(
            StageTask(
                identity=identity,
                provider_id=provider_id,
                depends_on=() if previous_identity_hash is None else (previous_identity_hash,),
            )
        )
        previous_identity_hash = identity.artifact_hash
        previous_artifact = expected
    return tuple(tasks)


class DailyProvider:
    def __init__(self, *, interrupt_day: int | None = None) -> None:
        self.interrupt_day = interrupt_day
        self.executed_days: list[int] = []

    def execute(self, task: StageTask, *, committed_inputs):
        day = int(task.identity.stage_id.rsplit(".", 1)[-1])
        if self.interrupt_day == day:
            raise RecoveryInterrupted(FailureCode.PROVIDER_TIMEOUT, f"forced interrupt day {day}")
        self.executed_days.append(day)
        return StageExecutionResult(
            stage_identity_hash=task.identity.artifact_hash,
            output_artifact_hash=task.identity.expected_artifact_hash,
            payload_hash=_h(
                {
                    "stage": task.identity.stage_id,
                    "input": task.identity.input_hash,
                    "committed_inputs": sorted(committed_inputs),
                }
            ),
        )


class UnavailableProvider:
    def execute(self, task: StageTask, *, committed_inputs):
        raise ProviderUnavailable(FailureCode.ADAPTER_UNAVAILABLE, "provider is intentionally unavailable")


def _controller(tmp_path, providers):
    return RunRecoveryController(
        store=RunCheckpointStore(tmp_path, clock=DeterministicClock()),
        providers=providers,
        lease_ttl_seconds=120,
    )


def test_interrupt_day_8_then_resume_to_day_20_matches_uninterrupted(tmp_path) -> None:
    tasks = _daily_tasks("resume-run")
    interrupted_provider = DailyProvider(interrupt_day=8)
    controller = _controller(tmp_path / "interrupted", {"daily": interrupted_provider})
    interrupted = controller.run(run_id="resume-run", tasks=tasks, holder_id="worker-a")

    assert interrupted.status is RecoveryStatus.INTERRUPTED
    assert interrupted_provider.executed_days == list(range(1, 8))
    assert len(interrupted.committed_stage_hashes) == 7

    resume_provider = DailyProvider()
    resumed = RunRecoveryController(
        store=controller.store,
        providers={"daily": resume_provider},
        lease_ttl_seconds=120,
    ).run(run_id="resume-run", tasks=tasks, holder_id="worker-b")

    uninterrupted_provider = DailyProvider()
    uninterrupted = _controller(tmp_path / "uninterrupted", {"daily": uninterrupted_provider}).run(
        run_id="resume-run",
        tasks=_daily_tasks("resume-run"),
        holder_id="worker-c",
    )
    assert resumed.status is RecoveryStatus.COMMITTED
    assert resume_provider.executed_days == list(range(8, 21))
    assert len(resumed.committed_stage_hashes) == 20
    assert resumed.final_artifact_hash == tasks[-1].identity.expected_artifact_hash
    assert uninterrupted.final_artifact_hash == resumed.final_artifact_hash
    assert uninterrupted_provider.executed_days == list(range(1, 21))


def test_committed_days_are_not_committed_twice(tmp_path) -> None:
    tasks = _daily_tasks("idempotent-run")
    controller = _controller(tmp_path, {"daily": DailyProvider(interrupt_day=8)})
    controller.run(run_id="idempotent-run", tasks=tasks, holder_id="worker-a")
    RunRecoveryController(store=controller.store, providers={"daily": DailyProvider()}, lease_ttl_seconds=120).run(
        run_id="idempotent-run",
        tasks=tasks,
        holder_id="worker-b",
    )
    events = controller.store.read_events("idempotent-run")
    committed = [event.stage_identity_hash for event in events if event.event_type is RecoveryEventType.STAGE_COMMITTED]

    assert len(committed) == 20
    assert len(set(committed)) == 20


def test_corrupt_checkpoint_fails_closed(tmp_path) -> None:
    store = RunCheckpointStore(tmp_path, clock=DeterministicClock())
    state_path = store.state_path("corrupt-run")
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text('{"schema_version": "broken", "state_hash": "sha256:' + ("1" * 64) + '"}', encoding="utf-8")

    with pytest.raises(RecoveryError) as exc:
        store.load_state("corrupt-run")
    assert exc.value.code is FailureCode.LEDGER_CORRUPTION


def test_two_recovery_workers_cannot_hold_same_lease(tmp_path) -> None:
    store = RunCheckpointStore(tmp_path, clock=DeterministicClock())
    first = store.acquire_lease("lease-run", holder_id="worker-a", ttl_seconds=120)

    with pytest.raises(LeaseUnavailable):
        store.acquire_lease("lease-run", holder_id="worker-b", ttl_seconds=120)

    store.assert_lease(first)
    store.release_lease(first)
    second = store.acquire_lease("lease-run", holder_id="worker-b", ttl_seconds=120)
    with pytest.raises(RecoveryError):
        store.assert_lease(first)
    store.release_lease(second)


def test_failed_independent_branch_does_not_drag_down_other_branch(tmp_path) -> None:
    good = _daily_tasks("branch-run", days=3, branch_id="C.good", provider_id="good")
    bad = _daily_tasks("branch-run", days=2, branch_id="C.bad", provider_id="bad")
    tasks = tuple(sorted(good + bad, key=lambda task: (task.identity.ordinal, task.identity.branch_id)))
    good_provider = DailyProvider()
    plan = _controller(
        tmp_path,
        {"good": good_provider, "bad": UnavailableProvider()},
    ).run(run_id="branch-run", tasks=tasks, holder_id="worker-a")

    assert plan.status is RecoveryStatus.FAILED
    assert len(plan.failed_stage_hashes) == 1
    assert len(plan.blocked_stage_hashes) == 1
    assert all(task.identity.artifact_hash in plan.committed_stage_hashes for task in good)
    assert good_provider.executed_days == [1, 2, 3]


def test_cancelled_run_does_not_execute_provider(tmp_path) -> None:
    tasks = _daily_tasks("cancel-run", days=2)
    provider = DailyProvider()
    store = RunCheckpointStore(tmp_path, clock=DeterministicClock())
    store.request_cancel("cancel-run", holder_id="operator")
    plan = RunRecoveryController(store=store, providers={"daily": provider}).run(
        run_id="cancel-run",
        tasks=tasks,
        holder_id="worker-a",
    )

    assert plan.status is RecoveryStatus.CANCELLED
    assert provider.executed_days == []
