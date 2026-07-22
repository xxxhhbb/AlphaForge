"""Typed, financial-authority-free contracts for the offline operator plane."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping


SCHEMA_VERSION = "veriquant.operator/v1"
ZERO_HASH = "sha256:" + "0" * 64


def canonical_hash(value: Any) -> str:
    rendered = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    return "sha256:" + hashlib.sha256(rendered.encode("utf-8")).hexdigest()


class OperatorFailureCode(str, Enum):
    INVALID_REQUEST = "INVALID_REQUEST"
    UNKNOWN_PROFILE = "UNKNOWN_PROFILE"
    IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"
    CONCURRENCY_LIMIT = "CONCURRENCY_LIMIT"
    PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
    PROVIDER_FAILED = "PROVIDER_FAILED"
    PLAN_INCOMPLETE = "PLAN_INCOMPLETE"
    CORRUPT_STATE = "CORRUPT_STATE"
    INVALID_TRANSITION = "INVALID_TRANSITION"
    NOT_FOUND = "NOT_FOUND"


class JobStatus(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    INTERRUPTED = "INTERRUPTED"
    CANCELLED = "CANCELLED"
    BLOCKED = "BLOCKED"


class EventType(str, Enum):
    JOB_CREATED = "JOB_CREATED"
    RUN_STARTED = "RUN_STARTED"
    STAGE_STARTED = "STAGE_STARTED"
    STAGE_COMMITTED = "STAGE_COMMITTED"
    RUN_INTERRUPTED = "RUN_INTERRUPTED"
    RUN_FAILED = "RUN_FAILED"
    CANCEL_REQUESTED = "CANCEL_REQUESTED"
    RESUME_REQUESTED = "RESUME_REQUESTED"
    RUN_COMPLETED = "RUN_COMPLETED"


class StageDisposition(str, Enum):
    COMMITTED = "COMMITTED"
    INTERRUPTED = "INTERRUPTED"
    FAILED = "FAILED"


class OperatorError(RuntimeError):
    def __init__(self, code: OperatorFailureCode, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ProviderProfile:
    profile_id: str
    provider_id: str
    stage_ids: tuple[str, ...]
    max_budget: int
    offline_only: bool = True
    requires_provider_key: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "profile_id": self.profile_id,
            "provider_id": self.provider_id,
            "stage_ids": list(self.stage_ids),
            "max_budget": self.max_budget,
            "offline_only": self.offline_only,
            "requires_provider_key": self.requires_provider_key,
        }

    @property
    def plan_hash(self) -> str:
        return canonical_hash(self.to_dict())


@dataclass(frozen=True)
class StageResult:
    disposition: StageDisposition
    artifact_hash: str | None = None
    failure_code: OperatorFailureCode | None = None


@dataclass(frozen=True)
class JobManifest:
    job_id: str
    profile_id: str
    provider_id: str
    seed: int
    budget: int
    idempotency_key_hash: str
    plan_hash: str
    stage_ids: tuple[str, ...]
    request_hash: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "job_id": self.job_id,
            "profile_id": self.profile_id,
            "provider_id": self.provider_id,
            "seed": self.seed,
            "budget": self.budget,
            "idempotency_key_hash": self.idempotency_key_hash,
            "plan_hash": self.plan_hash,
            "stage_ids": list(self.stage_ids),
            "request_hash": self.request_hash,
        }


@dataclass(frozen=True)
class JobState:
    job_id: str
    status: JobStatus
    completed_stages: tuple[str, ...] = ()
    artifact_hashes: Mapping[str, str] = field(default_factory=dict)
    last_event_sequence: int = 0
    last_event_hash: str = ZERO_HASH
    failure_code: OperatorFailureCode | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "job_id": self.job_id,
            "status": self.status.value,
            "completed_stages": list(self.completed_stages),
            "artifact_hashes": dict(sorted(self.artifact_hashes.items())),
            "last_event_sequence": self.last_event_sequence,
            "last_event_hash": self.last_event_hash,
            "failure_code": self.failure_code.value if self.failure_code else None,
        }


@dataclass(frozen=True)
class OperatorEvent:
    job_id: str
    sequence: int
    event_type: EventType
    status: JobStatus
    occurred_at: str
    previous_event_hash: str
    stage_id: str | None = None
    artifact_hash: str | None = None
    failure_code: OperatorFailureCode | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "job_id": self.job_id,
            "sequence": self.sequence,
            "event_type": self.event_type.value,
            "status": self.status.value,
            "occurred_at": self.occurred_at,
            "previous_event_hash": self.previous_event_hash,
            "stage_id": self.stage_id,
            "artifact_hash": self.artifact_hash,
            "failure_code": self.failure_code.value if self.failure_code else None,
        }

    @property
    def event_hash(self) -> str:
        return canonical_hash(self.to_dict())
