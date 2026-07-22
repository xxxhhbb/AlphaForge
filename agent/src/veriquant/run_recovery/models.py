"""Generic run recovery contracts for checkpoint, resume, lease, and events.

This package is an orchestration layer only.  It does not implement PIT,
Evidence, Promotion, Portfolio, Risk, Execution, Shadow Portfolio, or Ledger
financial truth.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from types import MappingProxyType
from typing import Any, ClassVar, Mapping

from veriquant.contracts import CanonicalContract, FailureCode
from veriquant.contracts.base import (
    fail,
    freeze_mapping,
    require_aware_datetime,
    require_hash,
    require_identifier,
    require_nonempty_tuple,
    require_text,
)


RUN_RECOVERY_SCHEMA_VERSION = "veriquant.run-recovery/v1"
ZERO_HASH = "sha256:" + ("0" * 64)


class RecoveryStatus(str, Enum):
    PENDING = "PENDING"
    COMMITTED = "COMMITTED"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"
    CANCELLED = "CANCELLED"
    INTERRUPTED = "INTERRUPTED"


class RecoveryEventType(str, Enum):
    LEASE_ACQUIRED = "LEASE_ACQUIRED"
    LEASE_RELEASED = "LEASE_RELEASED"
    RUN_CANCEL_REQUESTED = "RUN_CANCEL_REQUESTED"
    RUN_INTERRUPTED = "RUN_INTERRUPTED"
    STAGE_STARTED = "STAGE_STARTED"
    STAGE_COMMITTED = "STAGE_COMMITTED"
    STAGE_FAILED = "STAGE_FAILED"
    STAGE_BLOCKED = "STAGE_BLOCKED"


def _non_negative_int(value: int, field_name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        fail(FailureCode.INVALID_VALUE, f"{field_name} must be a non-negative integer")


@dataclass(frozen=True, kw_only=True)
class StageIdentity(CanonicalContract):
    """Content-bound identity for one A-F stage or daily checkpoint."""

    SCHEMA_VERSION: ClassVar[str] = RUN_RECOVERY_SCHEMA_VERSION

    run_id: str
    branch_id: str
    stage_id: str
    ordinal: int
    input_hash: str
    config_hash: str
    code_hash: str
    policy_hash: str
    expected_artifact_hash: str

    def __post_init__(self) -> None:
        require_identifier(self.run_id, "run_id")
        require_identifier(self.branch_id, "branch_id")
        require_identifier(self.stage_id, "stage_id")
        _non_negative_int(self.ordinal, "ordinal")
        for name in ("input_hash", "config_hash", "code_hash", "policy_hash", "expected_artifact_hash"):
            require_hash(getattr(self, name), name)


@dataclass(frozen=True, kw_only=True)
class StageTask(CanonicalContract):
    """Controller plan entry.  Dependencies are stage identity hashes."""

    SCHEMA_VERSION: ClassVar[str] = RUN_RECOVERY_SCHEMA_VERSION

    identity: StageIdentity
    provider_id: str
    depends_on: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.identity, StageIdentity):
            fail(FailureCode.INVALID_VALUE, "identity must be StageIdentity")
        require_identifier(self.provider_id, "provider_id")
        if not isinstance(self.depends_on, tuple):
            fail(FailureCode.INVALID_VALUE, "depends_on must be a tuple")
        for item in self.depends_on:
            require_hash(item, "depends_on")
        if len(set(self.depends_on)) != len(self.depends_on):
            fail(FailureCode.INVALID_VALUE, "depends_on must be unique")


@dataclass(frozen=True, kw_only=True)
class LeaseToken(CanonicalContract):
    """Fencing token for one active recovery worker."""

    SCHEMA_VERSION: ClassVar[str] = RUN_RECOVERY_SCHEMA_VERSION

    run_id: str
    holder_id: str
    token_id: str
    fencing_token: int
    acquired_at: datetime
    expires_at: datetime

    def __post_init__(self) -> None:
        require_identifier(self.run_id, "run_id")
        require_identifier(self.holder_id, "holder_id")
        require_identifier(self.token_id, "token_id")
        if isinstance(self.fencing_token, bool) or not isinstance(self.fencing_token, int) or self.fencing_token < 1:
            fail(FailureCode.INVALID_VALUE, "fencing_token must be a positive integer")
        require_aware_datetime(self.acquired_at, "acquired_at")
        require_aware_datetime(self.expires_at, "expires_at")
        if self.acquired_at >= self.expires_at:
            fail(FailureCode.INVALID_TIME_ORDER, "lease expires_at must follow acquired_at")

    def is_valid_at(self, now: datetime) -> bool:
        require_aware_datetime(now, "now")
        return self.acquired_at <= now < self.expires_at


@dataclass(frozen=True, kw_only=True)
class StageExecutionResult(CanonicalContract):
    """Provider result before commit.  Artifact hash must match identity."""

    SCHEMA_VERSION: ClassVar[str] = RUN_RECOVERY_SCHEMA_VERSION

    stage_identity_hash: str
    output_artifact_hash: str
    payload_hash: str
    metadata: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("stage_identity_hash", "output_artifact_hash", "payload_hash"):
            require_hash(getattr(self, name), name)
        object.__setattr__(self, "metadata", freeze_mapping(self.metadata, "metadata"))


@dataclass(frozen=True, kw_only=True)
class StageCheckpoint(CanonicalContract):
    """Materialized checkpoint for a terminal stage state."""

    SCHEMA_VERSION: ClassVar[str] = RUN_RECOVERY_SCHEMA_VERSION

    identity: StageIdentity
    status: RecoveryStatus
    committed_artifact_hash: str
    payload_hash: str
    event_sequence: int
    committed_at: datetime
    failure_code: FailureCode | None = None
    message: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.identity, StageIdentity):
            fail(FailureCode.INVALID_VALUE, "identity must be StageIdentity")
        if not isinstance(self.status, RecoveryStatus):
            fail(FailureCode.INVALID_VALUE, "status must be RecoveryStatus")
        for name in ("committed_artifact_hash", "payload_hash"):
            require_hash(getattr(self, name), name)
        _non_negative_int(self.event_sequence, "event_sequence")
        require_aware_datetime(self.committed_at, "committed_at")
        if self.status is RecoveryStatus.COMMITTED:
            if self.committed_artifact_hash != self.identity.expected_artifact_hash:
                fail(FailureCode.INVALID_HASH, "committed artifact hash does not match identity")
            if self.failure_code is not None:
                fail(FailureCode.INVALID_VALUE, "committed checkpoint cannot have a failure code")
        elif self.status in {RecoveryStatus.FAILED, RecoveryStatus.BLOCKED, RecoveryStatus.CANCELLED}:
            if self.failure_code is None:
                fail(FailureCode.INVALID_VALUE, "non-committed terminal checkpoint requires a failure code")
        else:
            fail(FailureCode.INVALID_STATE, "checkpoint status must be terminal")
        if self.message:
            require_text(self.message, "message")


@dataclass(frozen=True, kw_only=True)
class RecoveryEvent(CanonicalContract):
    """Append-only event with hash-chain metadata."""

    SCHEMA_VERSION: ClassVar[str] = RUN_RECOVERY_SCHEMA_VERSION

    run_id: str
    sequence: int
    event_type: RecoveryEventType
    occurred_at: datetime
    previous_event_hash: str
    stage_identity_hash: str | None = None
    lease_token_id: str | None = None
    status: RecoveryStatus | None = None
    stage_artifact_hash: str | None = None
    payload_hash: str | None = None
    failure_code: FailureCode | None = None
    message: str = ""

    def __post_init__(self) -> None:
        require_identifier(self.run_id, "run_id")
        if isinstance(self.sequence, bool) or not isinstance(self.sequence, int) or self.sequence < 1:
            fail(FailureCode.INVALID_VALUE, "sequence must be a positive integer")
        if not isinstance(self.event_type, RecoveryEventType):
            fail(FailureCode.INVALID_VALUE, "event_type must be RecoveryEventType")
        require_aware_datetime(self.occurred_at, "occurred_at")
        require_hash(self.previous_event_hash, "previous_event_hash")
        if self.stage_identity_hash is not None:
            require_hash(self.stage_identity_hash, "stage_identity_hash")
        if self.lease_token_id is not None:
            require_identifier(self.lease_token_id, "lease_token_id")
        if self.status is not None and not isinstance(self.status, RecoveryStatus):
            fail(FailureCode.INVALID_VALUE, "status must be RecoveryStatus")
        for name in ("stage_artifact_hash", "payload_hash"):
            value = getattr(self, name)
            if value is not None:
                require_hash(value, name)
        if self.failure_code is not None and not isinstance(self.failure_code, FailureCode):
            fail(FailureCode.INVALID_VALUE, "failure_code must be FailureCode")
        if self.message:
            require_text(self.message, "message")


@dataclass(frozen=True, kw_only=True)
class MaterializedRunState(CanonicalContract):
    """Atomic checkpoint read model derived from append-only events."""

    SCHEMA_VERSION: ClassVar[str] = RUN_RECOVERY_SCHEMA_VERSION

    run_id: str
    committed: tuple[StageCheckpoint, ...] = ()
    failed: tuple[StageCheckpoint, ...] = ()
    blocked: tuple[StageCheckpoint, ...] = ()
    cancelled: bool = False
    last_event_sequence: int = 0

    def __post_init__(self) -> None:
        require_identifier(self.run_id, "run_id")
        for name in ("committed", "failed", "blocked"):
            value = getattr(self, name)
            if not isinstance(value, tuple) or any(not isinstance(item, StageCheckpoint) for item in value):
                fail(FailureCode.INVALID_VALUE, f"{name} must contain StageCheckpoint values")
        if type(self.cancelled) is not bool:
            fail(FailureCode.INVALID_VALUE, "cancelled must be boolean")
        _non_negative_int(self.last_event_sequence, "last_event_sequence")

    @property
    def committed_by_hash(self) -> Mapping[str, StageCheckpoint]:
        return MappingProxyType({item.identity.artifact_hash: item for item in self.committed})

    @property
    def failed_by_hash(self) -> Mapping[str, StageCheckpoint]:
        return MappingProxyType({item.identity.artifact_hash: item for item in self.failed})

    @property
    def blocked_by_hash(self) -> Mapping[str, StageCheckpoint]:
        return MappingProxyType({item.identity.artifact_hash: item for item in self.blocked})


@dataclass(frozen=True, kw_only=True)
class ResumePlan(CanonicalContract):
    """Summary returned after planning or executing recovery."""

    SCHEMA_VERSION: ClassVar[str] = RUN_RECOVERY_SCHEMA_VERSION

    run_id: str
    status: RecoveryStatus
    committed_stage_hashes: tuple[str, ...]
    skipped_stage_hashes: tuple[str, ...]
    runnable_stage_hashes: tuple[str, ...]
    failed_stage_hashes: tuple[str, ...]
    blocked_stage_hashes: tuple[str, ...]
    final_artifact_hash: str | None = None
    message: str = ""

    def __post_init__(self) -> None:
        require_identifier(self.run_id, "run_id")
        if not isinstance(self.status, RecoveryStatus):
            fail(FailureCode.INVALID_VALUE, "status must be RecoveryStatus")
        for name in (
            "committed_stage_hashes",
            "skipped_stage_hashes",
            "runnable_stage_hashes",
            "failed_stage_hashes",
            "blocked_stage_hashes",
        ):
            value = getattr(self, name)
            if not isinstance(value, tuple):
                fail(FailureCode.INVALID_VALUE, f"{name} must be a tuple")
            for item in value:
                require_hash(item, name)
        if self.final_artifact_hash is not None:
            require_hash(self.final_artifact_hash, "final_artifact_hash")
        if self.message:
            require_text(self.message, "message")


class RecoveryError(RuntimeError):
    """Machine-readable fail-closed recovery exception."""

    def __init__(self, code: FailureCode, message: str, *, details: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.details = MappingProxyType(dict(details or {}))


class LeaseUnavailable(RecoveryError):
    """Raised when another worker holds a non-expired lease."""


class ProviderUnavailable(RecoveryError):
    """Raised by a provider when an optional dependency is unavailable."""


class RecoveryInterrupted(RecoveryError):
    """Raised by a provider to simulate an interruption before commit."""
