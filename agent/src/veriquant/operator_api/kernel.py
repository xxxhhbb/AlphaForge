"""In-memory operator job kernel with append-only events and idempotency."""

from __future__ import annotations

from dataclasses import replace
from typing import Any, Mapping, Protocol

from veriquant.contracts import FailureCode
from veriquant.data import canonical_content_hash

from .models import (
    OperatorApiError,
    OperatorEventType,
    OperatorJob,
    OperatorJobCreateRequest,
    OperatorJobEvent,
    OperatorJobStatus,
    utc_now,
)
from .registry import OperatorRegistry


class CheckpointRecoveryKernel(Protocol):
    """Minimal integration hook to an existing checkpoint/recovery kernel.

    Implementations may plan, cancel, and resume offline work.  This protocol is
    intentionally tiny so the operator API does not copy evidence, promotion,
    portfolio, risk, paper execution, or ledger logic.
    """

    def plan(self, *, job_id: str, request: OperatorJobCreateRequest) -> Mapping[str, str]: ...

    def request_cancel(self, *, job_id: str) -> Mapping[str, str]: ...

    def resume(self, *, job_id: str) -> Mapping[str, str]: ...


class DeterministicFakeRecoveryKernel:
    """Offline fake used by package-local tests and smoke checks."""

    def plan(self, *, job_id: str, request: OperatorJobCreateRequest) -> Mapping[str, str]:
        return {
            "checkpoint_ref": f"checkpoint-{job_id}",
            "snapshot_id": request.snapshot_id,
            "profile_id": request.profile_id,
        }

    def request_cancel(self, *, job_id: str) -> Mapping[str, str]:
        return {"checkpoint_ref": f"checkpoint-{job_id}", "cancel": "accepted"}

    def resume(self, *, job_id: str) -> Mapping[str, str]:
        return {"checkpoint_ref": f"checkpoint-{job_id}", "resume": "accepted"}


class InMemoryOperatorJobStore:
    """Non-durable append-only store for offline API semantics tests."""

    def __init__(self) -> None:
        self._jobs: dict[str, OperatorJob] = {}
        self._events: dict[str, tuple[OperatorJobEvent, ...]] = {}
        self._idempotency: dict[str, tuple[str, str]] = {}

    def get_job(self, job_id: str) -> OperatorJob:
        try:
            return self._jobs[job_id]
        except KeyError as exc:
            raise OperatorApiError(FailureCode.INVALID_IDENTIFIER, "job_id is unknown") from exc

    def put_job(self, job: OperatorJob) -> None:
        self._jobs[job.job_id] = job

    def claim_idempotency(self, key: str, payload_hash: str) -> str | None:
        existing = self._idempotency.get(key)
        if existing is None:
            return None
        existing_hash, job_id = existing
        if existing_hash != payload_hash:
            raise OperatorApiError(FailureCode.ARTIFACT_COLLISION, "idempotency_key was reused with a different payload")
        return job_id

    def remember_idempotency(self, key: str, payload_hash: str, job_id: str) -> None:
        self._idempotency[key] = (payload_hash, job_id)

    def append_event(
        self,
        *,
        job_id: str,
        event_type: OperatorEventType,
        status: OperatorJobStatus,
        message: str = "",
        metadata: Mapping[str, str] | None = None,
    ) -> OperatorJobEvent:
        sequence = len(self._events.get(job_id, ())) + 1
        event = OperatorJobEvent(
            job_id=job_id,
            sequence=sequence,
            event_type=event_type,
            status=status,
            occurred_at=utc_now(),
            cursor=str(sequence),
            message=message,
            metadata=metadata or {},
        )
        self._events[job_id] = self._events.get(job_id, ()) + (event,)
        return event

    def read_events(self, job_id: str, *, after: str | int | None = None) -> tuple[OperatorJobEvent, ...]:
        self.get_job(job_id)
        cursor = 0 if after in (None, "") else _parse_cursor(after)
        return tuple(event for event in self._events.get(job_id, ()) if event.sequence > cursor)

    def job_count(self) -> int:
        return len(self._jobs)


def _parse_cursor(value: str | int | None) -> int:
    if isinstance(value, bool):
        raise OperatorApiError(FailureCode.INVALID_VALUE, "cursor must be a non-negative integer")
    if isinstance(value, int):
        cursor = value
    elif isinstance(value, str) and value.isdigit():
        cursor = int(value)
    else:
        raise OperatorApiError(FailureCode.INVALID_VALUE, "cursor must be a non-negative integer")
    if cursor < 0:
        raise OperatorApiError(FailureCode.INVALID_VALUE, "cursor must be non-negative")
    return cursor


class OperatorJobService:
    """Package-local service behind the standalone route facade."""

    def __init__(
        self,
        *,
        registry: OperatorRegistry,
        store: InMemoryOperatorJobStore | None = None,
        recovery_kernel: CheckpointRecoveryKernel | None = None,
    ) -> None:
        self.registry = registry
        self.store = store or InMemoryOperatorJobStore()
        self.recovery_kernel = recovery_kernel or DeterministicFakeRecoveryKernel()

    def create_job(self, *, payload: Mapping[str, Any], idempotency_key: str) -> OperatorJob:
        from .models import _require_idempotency_key

        _require_idempotency_key(idempotency_key)
        request = OperatorJobCreateRequest.from_api_payload(payload)
        snapshot = self.registry.require_snapshot(request.snapshot_id)
        profile = self.registry.require_profile(request.profile_id)
        self.registry.require_budget_within_profile(budget=request.budget, profile=profile)
        payload_hash = canonical_content_hash(
            {
                "request": request.to_dict(),
                "snapshot_hash": snapshot.content_hash,
                "idempotency_key": idempotency_key,
            }
        )
        existing_job_id = self.store.claim_idempotency(idempotency_key, payload_hash)
        if existing_job_id is not None:
            return self.store.get_job(existing_job_id)
        job_id = "vqjob-" + canonical_content_hash({"payload": payload_hash})[-16:]
        plan = self.recovery_kernel.plan(job_id=job_id, request=request)
        now = utc_now()
        job = OperatorJob(
            job_id=job_id,
            status=OperatorJobStatus.QUEUED,
            snapshot_id=request.snapshot_id,
            profile_id=request.profile_id,
            seed=request.seed,
            budget=request.budget,
            created_at=now,
            updated_at=now,
            checkpoint_ref=str(plan.get("checkpoint_ref", "")),
        )
        self.store.put_job(job)
        self.store.remember_idempotency(idempotency_key, payload_hash, job.job_id)
        self.store.append_event(
            job_id=job.job_id,
            event_type=OperatorEventType.JOB_CREATED,
            status=job.status,
            message="offline operator job accepted",
            metadata={
                "snapshot_id": request.snapshot_id,
                "profile_id": request.profile_id,
                "seed": str(request.seed),
            },
        )
        return job

    def get_job(self, job_id: str) -> OperatorJob:
        return self.store.get_job(job_id)

    def get_events(self, job_id: str, *, after: str | int | None = None) -> tuple[OperatorJobEvent, ...]:
        return self.store.read_events(job_id, after=after)

    def cancel_job(self, job_id: str) -> OperatorJob:
        job = self.store.get_job(job_id)
        if job.status not in {OperatorJobStatus.QUEUED, OperatorJobStatus.RUNNING}:
            raise OperatorApiError(FailureCode.INVALID_STATE_TRANSITION, "job cannot be cancelled from its current state")
        self.recovery_kernel.request_cancel(job_id=job_id)
        updated = replace(job, status=OperatorJobStatus.CANCELLED, updated_at=utc_now())
        self.store.put_job(updated)
        self.store.append_event(
            job_id=job_id,
            event_type=OperatorEventType.JOB_CANCELLED,
            status=updated.status,
            message="offline cancellation accepted",
        )
        return updated

    def resume_job(self, job_id: str) -> OperatorJob:
        job = self.store.get_job(job_id)
        if job.status not in {OperatorJobStatus.QUEUED, OperatorJobStatus.CANCELLED}:
            raise OperatorApiError(FailureCode.INVALID_STATE_TRANSITION, "job cannot be resumed from its current state")
        plan = self.recovery_kernel.resume(job_id=job_id)
        updated = replace(
            job,
            status=OperatorJobStatus.RUNNING,
            updated_at=utc_now(),
            checkpoint_ref=str(plan.get("checkpoint_ref", job.checkpoint_ref)),
        )
        self.store.put_job(updated)
        self.store.append_event(
            job_id=job_id,
            event_type=OperatorEventType.JOB_RESUMED,
            status=updated.status,
            message="offline resume accepted",
        )
        return updated

