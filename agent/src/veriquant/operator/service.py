"""Offline-only operator job control with fixed plans and no financial authority."""

from __future__ import annotations

import re
import threading
from pathlib import Path
from typing import Any, Mapping

from .models import (
    EventType,
    JobManifest,
    JobState,
    JobStatus,
    OperatorError,
    OperatorFailureCode,
    StageDisposition,
    StageResult,
    canonical_hash,
)
from .registry import OperatorRegistry
from .store import OperatorStore


HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
ALLOWED_REQUEST_FIELDS = frozenset({"profile_id", "seed", "budget", "idempotency_key"})
FORBIDDEN_FIELD_TOKENS = (
    "path", "url", "token", "secret", "authorization", "cookie", "code", "python", "shell",
    "provider", "stage", "promotion", "risk", "portfolio", "weight", "order", "broker", "sealed",
)


class OfflineJobService:
    """Runs only pre-registered orchestration profiles.

    The service records orchestration hashes. It neither computes nor accepts
    evidence, promotion, portfolio, risk, order, fill, or ledger values.
    """

    def __init__(self, *, root: str | Path, registry: OperatorRegistry, max_concurrent_jobs: int = 1) -> None:
        if isinstance(max_concurrent_jobs, bool) or not isinstance(max_concurrent_jobs, int) or max_concurrent_jobs < 1:
            raise OperatorError(OperatorFailureCode.INVALID_REQUEST, "max_concurrent_jobs must be positive")
        self.store = OperatorStore(root)
        self.registry = registry
        self.max_concurrent_jobs = max_concurrent_jobs
        self._lock = threading.RLock()
        self._active_jobs: set[str] = set()

    def create(self, payload: Mapping[str, Any], *, auto_start: bool = True) -> dict[str, Any]:
        request = self._validate_request(payload)
        profile = self.registry.profile(request["profile_id"])
        if request["budget"] > profile.max_budget:
            raise OperatorError(OperatorFailureCode.INVALID_REQUEST, "budget exceeds registered profile limit")
        key_hash = canonical_hash({"idempotency_key": request["idempotency_key"]})
        public_request = {
            "profile_id": request["profile_id"],
            "seed": request["seed"],
            "budget": request["budget"],
            "idempotency_key_hash": key_hash,
        }
        request_hash = canonical_hash(public_request)
        job_id = "job-" + request_hash.removeprefix("sha256:")[:24]
        with self._lock:
            existing = self.store.read_idempotency(key_hash)
            if existing is not None:
                if existing.get("request_hash") != request_hash:
                    raise OperatorError(OperatorFailureCode.IDEMPOTENCY_CONFLICT, "idempotency key was used for a different request")
                return self.get(str(existing["job_id"]))
            manifest = JobManifest(
                job_id=job_id,
                profile_id=profile.profile_id,
                provider_id=profile.provider_id,
                seed=request["seed"],
                budget=request["budget"],
                idempotency_key_hash=key_hash,
                plan_hash=profile.plan_hash,
                stage_ids=profile.stage_ids,
                request_hash=request_hash,
            )
            self.store.write_manifest(manifest)
            event = self.store.append_event(job_id=job_id, event_type=EventType.JOB_CREATED, status=JobStatus.PENDING)
            self.store.write_state(
                JobState(
                    job_id=job_id,
                    status=JobStatus.PENDING,
                    last_event_sequence=event.sequence,
                    last_event_hash=event.event_hash,
                )
            )
            self.store.write_idempotency(key_hash, job_id=job_id, request_hash=request_hash)
        return self._execute(job_id) if auto_start else self.get(job_id)

    def get(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            manifest, state = self._load_verified(job_id)
            return self._public_status(manifest, state)

    def events(self, job_id: str, *, after: int = 0, limit: int = 200) -> dict[str, Any]:
        if isinstance(after, bool) or not isinstance(after, int) or after < 0:
            raise OperatorError(OperatorFailureCode.INVALID_REQUEST, "event cursor must be non-negative")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 200:
            raise OperatorError(OperatorFailureCode.INVALID_REQUEST, "event limit must be 1..200")
        with self._lock:
            self._load_verified(job_id)
            events = self.store.read_events(job_id, after=after, limit=limit)
        return {
            "job_id": job_id,
            "events": [event.to_dict() for event in events],
            "next_cursor": events[-1].sequence if events else after,
        }

    def cancel(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            manifest, state = self._load_verified(job_id)
            if state.status is JobStatus.COMPLETED:
                raise OperatorError(OperatorFailureCode.INVALID_TRANSITION, "completed job cannot be cancelled")
            if state.status is JobStatus.CANCELLED:
                return self._public_status(manifest, state)
            event = self.store.append_event(job_id=job_id, event_type=EventType.CANCEL_REQUESTED, status=JobStatus.CANCELLED)
            self.store.write_state(
                JobState(
                    job_id=job_id,
                    status=JobStatus.CANCELLED,
                    completed_stages=state.completed_stages,
                    artifact_hashes=state.artifact_hashes,
                    last_event_sequence=event.sequence,
                    last_event_hash=event.event_hash,
                )
            )
            return self.get(job_id)

    def resume(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            manifest, state = self._load_verified(job_id)
            if state.status is JobStatus.COMPLETED:
                return self._public_status(manifest, state)
            if state.status not in {JobStatus.CANCELLED, JobStatus.INTERRUPTED, JobStatus.BLOCKED, JobStatus.PENDING}:
                raise OperatorError(OperatorFailureCode.INVALID_TRANSITION, "job is not resumable")
            event = self.store.append_event(job_id=job_id, event_type=EventType.RESUME_REQUESTED, status=JobStatus.PENDING)
            self.store.write_state(
                JobState(
                    job_id=job_id,
                    status=JobStatus.PENDING,
                    completed_stages=state.completed_stages,
                    artifact_hashes=state.artifact_hashes,
                    last_event_sequence=event.sequence,
                    last_event_hash=event.event_hash,
                )
            )
        return self._execute(job_id)

    def start(self, job_id: str) -> dict[str, Any]:
        """Start a newly-created pending job from a trusted worker."""

        with self._lock:
            manifest, state = self._load_verified(job_id)
            if state.status is JobStatus.COMPLETED:
                return self._public_status(manifest, state)
            if state.status is not JobStatus.PENDING:
                raise OperatorError(OperatorFailureCode.INVALID_TRANSITION, "only a pending job may be started")
        return self._execute(job_id)

    def profiles(self) -> tuple[dict[str, object], ...]:
        return self.registry.public_profiles()

    def _execute(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            manifest, state = self._load_verified(job_id)
            if state.status is JobStatus.COMPLETED:
                return self._public_status(manifest, state)
            if len(self._active_jobs) >= self.max_concurrent_jobs and job_id not in self._active_jobs:
                event = self.store.append_event(
                    job_id=job_id,
                    event_type=EventType.RUN_FAILED,
                    status=JobStatus.BLOCKED,
                    failure_code=OperatorFailureCode.CONCURRENCY_LIMIT,
                )
                self.store.write_state(
                    JobState(
                        job_id=job_id,
                        status=JobStatus.BLOCKED,
                        completed_stages=state.completed_stages,
                        artifact_hashes=state.artifact_hashes,
                        last_event_sequence=event.sequence,
                        last_event_hash=event.event_hash,
                        failure_code=OperatorFailureCode.CONCURRENCY_LIMIT,
                    )
                )
                return self.get(job_id)
            self._active_jobs.add(job_id)
            event = self.store.append_event(job_id=job_id, event_type=EventType.RUN_STARTED, status=JobStatus.RUNNING)
            state = JobState(
                job_id=job_id,
                status=JobStatus.RUNNING,
                completed_stages=state.completed_stages,
                artifact_hashes=state.artifact_hashes,
                last_event_sequence=event.sequence,
                last_event_hash=event.event_hash,
            )
            self.store.write_state(state)
        try:
            provider = self.registry.provider(manifest.provider_id)
            for stage_id in manifest.stage_ids:
                with self._lock:
                    current = self.store.read_state(job_id)
                    if current.status is JobStatus.CANCELLED:
                        return self._public_status(manifest, current)
                    if stage_id in current.completed_stages:
                        continue
                    started = self.store.append_event(
                        job_id=job_id,
                        event_type=EventType.STAGE_STARTED,
                        status=JobStatus.RUNNING,
                        stage_id=stage_id,
                    )
                    self.store.write_state(
                        JobState(
                            job_id=job_id,
                            status=JobStatus.RUNNING,
                            completed_stages=current.completed_stages,
                            artifact_hashes=current.artifact_hashes,
                            last_event_sequence=started.sequence,
                            last_event_hash=started.event_hash,
                        )
                    )
                try:
                    result = provider.execute_stage(
                        job_id=job_id,
                        stage_id=stage_id,
                        seed=manifest.seed,
                        budget=manifest.budget,
                        committed_artifacts=dict(current.artifact_hashes),
                    )
                except Exception:
                    result = StageResult(
                        disposition=StageDisposition.FAILED,
                        failure_code=OperatorFailureCode.PROVIDER_FAILED,
                    )
                with self._lock:
                    current = self.store.read_state(job_id)
                    if current.status is JobStatus.CANCELLED:
                        return self._public_status(manifest, current)
                    if not isinstance(result, StageResult):
                        result = StageResult(disposition=StageDisposition.FAILED, failure_code=OperatorFailureCode.PROVIDER_FAILED)
                    if result.disposition is StageDisposition.INTERRUPTED:
                        return self._terminal(
                            manifest,
                            current,
                            event_type=EventType.RUN_INTERRUPTED,
                            status=JobStatus.INTERRUPTED,
                            failure_code=result.failure_code or OperatorFailureCode.PROVIDER_FAILED,
                            stage_id=stage_id,
                        )
                    if result.disposition is StageDisposition.FAILED or not result.artifact_hash or not HASH_RE.fullmatch(result.artifact_hash):
                        return self._terminal(
                            manifest,
                            current,
                            event_type=EventType.RUN_FAILED,
                            status=JobStatus.FAILED,
                            failure_code=result.failure_code or OperatorFailureCode.PROVIDER_FAILED,
                            stage_id=stage_id,
                        )
                    committed_stages = current.completed_stages + (stage_id,)
                    artifacts = {**current.artifact_hashes, stage_id: result.artifact_hash}
                    committed = self.store.append_event(
                        job_id=job_id,
                        event_type=EventType.STAGE_COMMITTED,
                        status=JobStatus.RUNNING,
                        stage_id=stage_id,
                        artifact_hash=result.artifact_hash,
                    )
                    self.store.write_state(
                        JobState(
                            job_id=job_id,
                            status=JobStatus.RUNNING,
                            completed_stages=committed_stages,
                            artifact_hashes=artifacts,
                            last_event_sequence=committed.sequence,
                            last_event_hash=committed.event_hash,
                        )
                    )
            with self._lock:
                current = self.store.read_state(job_id)
                if current.completed_stages != manifest.stage_ids or set(current.artifact_hashes) != set(manifest.stage_ids):
                    return self._terminal(
                        manifest,
                        current,
                        event_type=EventType.RUN_FAILED,
                        status=JobStatus.BLOCKED,
                        failure_code=OperatorFailureCode.PLAN_INCOMPLETE,
                    )
                completed = self.store.append_event(job_id=job_id, event_type=EventType.RUN_COMPLETED, status=JobStatus.COMPLETED)
                final = JobState(
                    job_id=job_id,
                    status=JobStatus.COMPLETED,
                    completed_stages=current.completed_stages,
                    artifact_hashes=current.artifact_hashes,
                    last_event_sequence=completed.sequence,
                    last_event_hash=completed.event_hash,
                )
                self.store.write_state(final)
                return self._public_status(manifest, final)
        finally:
            with self._lock:
                self._active_jobs.discard(job_id)

    def _terminal(
        self,
        manifest: JobManifest,
        state: JobState,
        *,
        event_type: EventType,
        status: JobStatus,
        failure_code: OperatorFailureCode,
        stage_id: str | None = None,
    ) -> dict[str, Any]:
        event = self.store.append_event(
            job_id=manifest.job_id,
            event_type=event_type,
            status=status,
            stage_id=stage_id,
            failure_code=failure_code,
        )
        final = JobState(
            job_id=manifest.job_id,
            status=status,
            completed_stages=state.completed_stages,
            artifact_hashes=state.artifact_hashes,
            last_event_sequence=event.sequence,
            last_event_hash=event.event_hash,
            failure_code=failure_code,
        )
        self.store.write_state(final)
        return self._public_status(manifest, final)

    def _load_verified(self, job_id: str) -> tuple[JobManifest, JobState]:
        manifest = self.store.read_manifest(job_id)
        state = self.store.read_state(job_id)
        profile = self.registry.profile(manifest.profile_id)
        if manifest.provider_id != profile.provider_id or manifest.plan_hash != profile.plan_hash or manifest.stage_ids != profile.stage_ids:
            raise OperatorError(OperatorFailureCode.CORRUPT_STATE, "stored job plan no longer matches registry")
        events = self.store.read_events(job_id)
        if state.last_event_sequence != len(events):
            raise OperatorError(OperatorFailureCode.CORRUPT_STATE, "job state is not synchronized with its event chain")
        expected_head = events[-1].event_hash if events else "sha256:" + "0" * 64
        if state.last_event_hash != expected_head:
            raise OperatorError(OperatorFailureCode.CORRUPT_STATE, "job state event-chain head mismatch")
        if state.status is JobStatus.COMPLETED and (
            state.completed_stages != manifest.stage_ids or set(state.artifact_hashes) != set(manifest.stage_ids)
        ):
            raise OperatorError(OperatorFailureCode.CORRUPT_STATE, "incomplete plan cannot be completed")
        return manifest, state

    @staticmethod
    def _public_status(manifest: JobManifest, state: JobState) -> dict[str, Any]:
        return {
            "schema_version": "veriquant.operator-public/v1",
            "job_id": manifest.job_id,
            "profile_id": manifest.profile_id,
            "status": state.status.value,
            "completed_stages": list(state.completed_stages),
            "stage_count": len(manifest.stage_ids),
            "last_event_sequence": state.last_event_sequence,
            "failure_code": state.failure_code.value if state.failure_code else None,
            "plan_hash": manifest.plan_hash,
            "request_hash": manifest.request_hash,
            "artifact_hashes": dict(sorted(state.artifact_hashes.items())),
            "offline_only": True,
            "financial_authority": False,
        }

    @staticmethod
    def _validate_request(payload: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, Mapping):
            raise OperatorError(OperatorFailureCode.INVALID_REQUEST, "request body must be an object")
        fields = set(payload)
        unknown = fields - ALLOWED_REQUEST_FIELDS
        missing = ALLOWED_REQUEST_FIELDS - fields
        if unknown or missing:
            suspicious = tuple(sorted(key for key in unknown if any(token in key.lower() for token in FORBIDDEN_FIELD_TOKENS)))
            suffix = f"; forbidden fields={suspicious}" if suspicious else ""
            raise OperatorError(OperatorFailureCode.INVALID_REQUEST, "request fields do not match the closed schema" + suffix)
        profile_id = payload["profile_id"]
        key = payload["idempotency_key"]
        seed = payload["seed"]
        budget = payload["budget"]
        if not isinstance(profile_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", profile_id):
            raise OperatorError(OperatorFailureCode.INVALID_REQUEST, "invalid profile_id")
        if not isinstance(key, str) or not 8 <= len(key) <= 128 or any(ch in key for ch in "\r\n\x00"):
            raise OperatorError(OperatorFailureCode.INVALID_REQUEST, "invalid idempotency key")
        if isinstance(seed, bool) or not isinstance(seed, int) or not 0 <= seed <= 2**31 - 1:
            raise OperatorError(OperatorFailureCode.INVALID_REQUEST, "invalid seed")
        if isinstance(budget, bool) or not isinstance(budget, int) or budget < 1:
            raise OperatorError(OperatorFailureCode.INVALID_REQUEST, "invalid budget")
        return {"profile_id": profile_id, "idempotency_key": key, "seed": seed, "budget": budget}
