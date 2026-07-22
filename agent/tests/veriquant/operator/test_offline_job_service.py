from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from veriquant.operator.models import (
    JobState,
    JobStatus,
    OperatorError,
    OperatorFailureCode,
    ProviderProfile,
    StageDisposition,
    StageResult,
    canonical_hash,
)
from veriquant.operator.registry import OperatorRegistry
from veriquant.operator.service import OfflineJobService


class RecordingProvider:
    def __init__(self, *, interrupt_once_at: str | None = None) -> None:
        self.calls: list[str] = []
        self.interrupt_once_at = interrupt_once_at

    def execute_stage(self, *, job_id, stage_id, seed, budget, committed_artifacts):
        self.calls.append(stage_id)
        if self.interrupt_once_at == stage_id:
            self.interrupt_once_at = None
            return StageResult(
                disposition=StageDisposition.INTERRUPTED,
                failure_code=OperatorFailureCode.PROVIDER_FAILED,
            )
        return StageResult(
            disposition=StageDisposition.COMMITTED,
            artifact_hash=canonical_hash(
                {"job_id": job_id, "stage_id": stage_id, "seed": seed, "budget": budget, "inputs": committed_artifacts}
            ),
        )


def service(tmp_path: Path, provider=None, *, stages=("one", "two", "three"), max_concurrent=1):
    selected = provider or RecordingProvider()
    registry = OperatorRegistry(
        profiles={
            "offline-v1": ProviderProfile(
                profile_id="offline-v1",
                provider_id="fixture",
                stage_ids=tuple(stages),
                max_budget=3,
            )
        },
        providers={"fixture": selected},
    )
    return OfflineJobService(root=tmp_path, registry=registry, max_concurrent_jobs=max_concurrent), selected


def request(key="operator-key-0001", **updates):
    body = {"profile_id": "offline-v1", "seed": 7, "budget": 2, "idempotency_key": key}
    body.update(updates)
    return body


def test_fixed_plan_completes_only_after_every_stage(tmp_path) -> None:
    jobs, provider = service(tmp_path)
    result = jobs.create(request())

    assert result["status"] == "COMPLETED"
    assert result["completed_stages"] == ["one", "two", "three"]
    assert result["financial_authority"] is False
    assert provider.calls == ["one", "two", "three"]
    events = jobs.events(result["job_id"])
    assert events["next_cursor"] == 9
    assert [item["sequence"] for item in events["events"]] == list(range(1, 10))


def test_idempotency_is_stable_and_conflicts_fail_closed(tmp_path) -> None:
    jobs, provider = service(tmp_path)
    first = jobs.create(request())
    second = jobs.create(request())
    assert second == first
    assert provider.calls == ["one", "two", "three"]

    with pytest.raises(OperatorError) as exc:
        jobs.create(request(seed=8))
    assert exc.value.code is OperatorFailureCode.IDEMPOTENCY_CONFLICT


@pytest.mark.parametrize(
    "field",
    ["path", "url", "token", "code", "python", "shell", "provider", "stage", "promotion", "orders", "sealed_metrics"],
)
def test_caller_cannot_inject_privileged_or_executable_fields(tmp_path, field) -> None:
    jobs, _ = service(tmp_path)
    with pytest.raises(OperatorError) as exc:
        jobs.create({**request(), field: "forbidden"})
    assert exc.value.code is OperatorFailureCode.INVALID_REQUEST


def test_unknown_profile_and_budget_fail_closed(tmp_path) -> None:
    jobs, _ = service(tmp_path)
    with pytest.raises(OperatorError) as unknown:
        jobs.create(request(profile_id="caller-provider"))
    assert unknown.value.code is OperatorFailureCode.UNKNOWN_PROFILE
    with pytest.raises(OperatorError) as budget:
        jobs.create(request(key="operator-key-0002", budget=99))
    assert budget.value.code is OperatorFailureCode.INVALID_REQUEST


def test_interrupt_resume_skips_committed_stage(tmp_path) -> None:
    provider = RecordingProvider(interrupt_once_at="two")
    jobs, _ = service(tmp_path, provider)
    first = jobs.create(request())
    assert first["status"] == "INTERRUPTED"
    assert first["completed_stages"] == ["one"]

    resumed = jobs.resume(first["job_id"])
    assert resumed["status"] == "COMPLETED"
    assert provider.calls == ["one", "two", "two", "three"]
    commit_events = [item for item in jobs.events(first["job_id"])["events"] if item["event_type"] == "STAGE_COMMITTED"]
    assert [item["stage_id"] for item in commit_events] == ["one", "two", "three"]


def test_cancelled_pending_job_can_resume(tmp_path) -> None:
    jobs, provider = service(tmp_path)
    pending = jobs.create(request(), auto_start=False)
    cancelled = jobs.cancel(pending["job_id"])
    assert cancelled["status"] == "CANCELLED"
    assert provider.calls == []
    resumed = jobs.resume(pending["job_id"])
    assert resumed["status"] == "COMPLETED"


class BlockingProvider(RecordingProvider):
    def __init__(self) -> None:
        super().__init__()
        self.entered = threading.Event()
        self.release = threading.Event()

    def execute_stage(self, **kwargs):
        self.entered.set()
        assert self.release.wait(5)
        return super().execute_stage(**kwargs)


def test_concurrency_limit_blocks_second_job_without_starting_provider(tmp_path) -> None:
    provider = BlockingProvider()
    jobs, _ = service(tmp_path, provider, stages=("only",), max_concurrent=1)
    results: list[dict] = []
    worker = threading.Thread(target=lambda: results.append(jobs.create(request("operator-key-first"))))
    worker.start()
    assert provider.entered.wait(2)
    second = jobs.create(request("operator-key-second", seed=8))
    assert second["status"] == "BLOCKED"
    assert second["failure_code"] == "CONCURRENCY_LIMIT"
    provider.release.set()
    worker.join(5)
    assert results[0]["status"] == "COMPLETED"
    resumed = jobs.resume(second["job_id"])
    assert resumed["status"] == "COMPLETED"


def test_event_tamper_and_partial_completed_state_fail_closed(tmp_path) -> None:
    jobs, _ = service(tmp_path)
    result = jobs.create(request())
    event_path = tmp_path / "runs" / result["job_id"] / "events.jsonl"
    lines = event_path.read_text(encoding="utf-8").splitlines()
    raw = json.loads(lines[1])
    raw["status"] = "FAILED"
    lines[1] = json.dumps(raw)
    event_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    with pytest.raises(OperatorError) as corrupt:
        jobs.get(result["job_id"])
    assert corrupt.value.code is OperatorFailureCode.CORRUPT_STATE

    tail_jobs, _ = service(tmp_path / "tail")
    tail = tail_jobs.create(request("operator-key-tail"))
    tail_path = tmp_path / "tail" / "runs" / tail["job_id"] / "events.jsonl"
    tail_lines = tail_path.read_text(encoding="utf-8").splitlines()
    tail_raw = json.loads(tail_lines[-1])
    tail_raw["status"] = "FAILED"
    tail_lines[-1] = json.dumps(tail_raw)
    tail_path.write_text("\n".join(tail_lines) + "\n", encoding="utf-8")
    with pytest.raises(OperatorError) as tail_corrupt:
        tail_jobs.get(tail["job_id"])
    assert tail_corrupt.value.code is OperatorFailureCode.CORRUPT_STATE

    other, _ = service(tmp_path / "partial")
    pending = other.create(request("operator-key-partial"), auto_start=False)
    other.store.write_state(
        JobState(
            job_id=pending["job_id"],
            status=JobStatus.COMPLETED,
            completed_stages=("one",),
            artifact_hashes={"one": canonical_hash({"one": 1})},
            last_event_sequence=1,
        )
    )
    with pytest.raises(OperatorError) as partial:
        other.get(pending["job_id"])
    assert partial.value.code is OperatorFailureCode.CORRUPT_STATE


def test_cursor_and_identifier_validation_and_no_plaintext_key(tmp_path) -> None:
    jobs, _ = service(tmp_path)
    key = "operator-sensitive-key-not-a-secret"
    result = jobs.create(request(key))
    assert len(jobs.events(result["job_id"], after=5, limit=2)["events"]) == 2
    with pytest.raises(OperatorError):
        jobs.get("../outside")
    all_text = "\n".join(path.read_text(encoding="utf-8") for path in tmp_path.rglob("*.*"))
    assert key not in all_text
