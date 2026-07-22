"""Route-neutral contracts for durable AlphaForge research jobs."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from types import MappingProxyType
from typing import Any, ClassVar, Mapping

from veriquant.contracts import CanonicalContract, FailureCode
from veriquant.contracts.base import (
    fail,
    require_aware_datetime,
    require_hash,
    require_identifier,
    require_text,
)
from veriquant.data import canonical_content_hash
from veriquant.run_recovery.api import RecoveryStatus, StageIdentity, StageTask


RESEARCH_JOB_SCHEMA_VERSION = "alphaforge.research-job-service/v2"
DEFAULT_CREATED_AT = datetime(2026, 7, 19, 9, 30, tzinfo=timezone.utc)
ZERO_HASH = "sha256:" + "0" * 64


class JobState(str, Enum):
    DRAFT = "DRAFT"
    CONFIRMED = "CONFIRMED"
    RUNNING = "RUNNING"
    INTERRUPTED = "INTERRUPTED"
    COMPLETED = "COMPLETED"
    PARTIAL = "PARTIAL"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class ConfirmationAuthority(str, Enum):
    USER = "USER"
    OPERATOR = "OPERATOR"


class ProviderMode(str, Enum):
    CONFIGURED_PROVIDER = "CONFIGURED_PROVIDER"
    DETERMINISTIC_FALLBACK = "DETERMINISTIC_FALLBACK"


class JobEventType(str, Enum):
    DRAFT_CREATED = "DRAFT_CREATED"
    JOB_CONFIRMED = "JOB_CONFIRMED"
    RUN_STARTED = "RUN_STARTED"
    PROVIDER_FALLBACK_USED = "PROVIDER_FALLBACK_USED"
    RUN_FINISHED = "RUN_FINISHED"
    CANCEL_REQUESTED = "CANCEL_REQUESTED"
    REPORT_ATTACHED = "REPORT_ATTACHED"


def require_safe_segment(value: str, field_name: str) -> None:
    require_identifier(value, field_name)
    if any(token in value for token in ("..", "/", "\\", ":")):
        fail(FailureCode.UNAUTHORIZED_CAPABILITY, f"{field_name} must be one safe path segment")


def _hash_tuple(value: tuple[str, ...], field_name: str) -> None:
    if not isinstance(value, tuple):
        fail(FailureCode.INVALID_VALUE, f"{field_name} must be a tuple")
    for item in value:
        require_hash(item, field_name)
    if len(set(value)) != len(value):
        fail(FailureCode.INVALID_VALUE, f"{field_name} contains duplicates")


def _segment_tuple(value: tuple[str, ...], field_name: str) -> None:
    if not isinstance(value, tuple):
        fail(FailureCode.INVALID_VALUE, f"{field_name} must be a tuple")
    for item in value:
        require_safe_segment(item, field_name)
    if len(set(value)) != len(value):
        fail(FailureCode.INVALID_VALUE, f"{field_name} contains duplicates")


@dataclass(frozen=True, kw_only=True)
class ResearchJobStage(CanonicalContract):
    SCHEMA_VERSION: ClassVar[str] = RESEARCH_JOB_SCHEMA_VERSION

    stage_id: str
    branch_id: str
    ordinal: int
    input_hash: str
    config_hash: str
    code_hash: str
    policy_hash: str
    provider_id: str = "bounded_research_provider"
    depends_on_stage_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        require_safe_segment(self.stage_id, "stage_id")
        require_safe_segment(self.branch_id, "branch_id")
        if isinstance(self.ordinal, bool) or not isinstance(self.ordinal, int) or self.ordinal < 0:
            fail(FailureCode.INVALID_VALUE, "ordinal must be a non-negative integer")
        for name in ("input_hash", "config_hash", "code_hash", "policy_hash"):
            require_hash(getattr(self, name), name)
        require_safe_segment(self.provider_id, "provider_id")
        _segment_tuple(self.depends_on_stage_ids, "depends_on_stage_ids")

    @property
    def expected_output_hash(self) -> str:
        return canonical_content_hash(
            {
                "stage_id": self.stage_id,
                "branch_id": self.branch_id,
                "ordinal": self.ordinal,
                "input_hash": self.input_hash,
                "config_hash": self.config_hash,
                "code_hash": self.code_hash,
                "policy_hash": self.policy_hash,
                "depends_on_stage_ids": self.depends_on_stage_ids,
            }
        )


@dataclass(frozen=True, kw_only=True)
class ResearchJobDraft(CanonicalContract):
    SCHEMA_VERSION: ClassVar[str] = RESEARCH_JOB_SCHEMA_VERSION

    run_id: str
    question: str
    campaign_draft_hash: str
    frozen_input_hash: str
    stages: tuple[ResearchJobStage, ...]
    created_at: datetime = DEFAULT_CREATED_AT

    def __post_init__(self) -> None:
        require_safe_segment(self.run_id, "run_id")
        require_text(self.question, "question")
        for name in ("campaign_draft_hash", "frozen_input_hash"):
            require_hash(getattr(self, name), name)
        require_aware_datetime(self.created_at, "created_at")
        if not isinstance(self.stages, tuple) or not self.stages:
            fail(FailureCode.MISSING_REQUIRED_FIELD, "at least one stage is required")
        by_id: dict[str, ResearchJobStage] = {}
        for stage in self.stages:
            if not isinstance(stage, ResearchJobStage):
                fail(FailureCode.INVALID_VALUE, "stages must contain ResearchJobStage")
            if stage.stage_id in by_id:
                fail(FailureCode.INVALID_VALUE, "duplicate stage_id")
            by_id[stage.stage_id] = stage
        for stage in self.stages:
            for dependency in stage.depends_on_stage_ids:
                parent = by_id.get(dependency)
                if parent is None:
                    fail(FailureCode.INVALID_STATE, "stage dependency is missing")
                if parent.ordinal >= stage.ordinal:
                    fail(FailureCode.INVALID_STATE, "stage dependency must have lower ordinal")


@dataclass(frozen=True, kw_only=True)
class ResearchJobManifest(CanonicalContract):
    SCHEMA_VERSION: ClassVar[str] = RESEARCH_JOB_SCHEMA_VERSION

    run_id: str
    draft_hash: str
    campaign_hash: str
    frozen_input_hash: str
    stages: tuple[ResearchJobStage, ...]
    confirmation_authority: ConfirmationAuthority
    confirmation_hash: str
    confirmed_at: datetime = DEFAULT_CREATED_AT

    def __post_init__(self) -> None:
        require_safe_segment(self.run_id, "run_id")
        for name in ("draft_hash", "campaign_hash", "frozen_input_hash", "confirmation_hash"):
            require_hash(getattr(self, name), name)
        if not isinstance(self.confirmation_authority, ConfirmationAuthority):
            object.__setattr__(self, "confirmation_authority", ConfirmationAuthority(self.confirmation_authority))
        require_aware_datetime(self.confirmed_at, "confirmed_at")
        if not isinstance(self.stages, tuple) or not self.stages:
            fail(FailureCode.INVALID_VALUE, "manifest stages are required")

    def to_tasks(self) -> tuple[StageTask, ...]:
        identities = {
            stage.stage_id: StageIdentity(
                run_id=self.run_id,
                branch_id=stage.branch_id,
                stage_id=stage.stage_id,
                ordinal=stage.ordinal,
                input_hash=stage.input_hash,
                config_hash=stage.config_hash,
                code_hash=stage.code_hash,
                policy_hash=stage.policy_hash,
                expected_artifact_hash=stage.expected_output_hash,
            )
            for stage in self.stages
        }
        return tuple(
            StageTask(
                identity=identities[stage.stage_id],
                provider_id=stage.provider_id,
                depends_on=tuple(identities[item].artifact_hash for item in stage.depends_on_stage_ids),
            )
            for stage in self.stages
        )


@dataclass(frozen=True, kw_only=True)
class ResearchReportReference(CanonicalContract):
    SCHEMA_VERSION: ClassVar[str] = RESEARCH_JOB_SCHEMA_VERSION

    run_id: str
    report_hash: str
    manifest_hash: str
    relative_root: str

    def __post_init__(self) -> None:
        require_safe_segment(self.run_id, "run_id")
        require_hash(self.report_hash, "report_hash")
        require_hash(self.manifest_hash, "manifest_hash")
        require_safe_segment(self.relative_root, "relative_root")
        if self.relative_root != self.run_id:
            fail(FailureCode.UNAUTHORIZED_CAPABILITY, "report reference must remain under its run root")


@dataclass(frozen=True, kw_only=True)
class ResearchJobEvent(CanonicalContract):
    SCHEMA_VERSION: ClassVar[str] = RESEARCH_JOB_SCHEMA_VERSION

    run_id: str
    sequence: int
    event_type: JobEventType
    occurred_at: datetime
    previous_event_hash: str
    payload_hash: str
    payload: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        require_safe_segment(self.run_id, "run_id")
        if isinstance(self.sequence, bool) or not isinstance(self.sequence, int) or self.sequence < 1:
            fail(FailureCode.INVALID_VALUE, "sequence must be positive")
        if not isinstance(self.event_type, JobEventType):
            object.__setattr__(self, "event_type", JobEventType(self.event_type))
        require_aware_datetime(self.occurred_at, "occurred_at")
        require_hash(self.previous_event_hash, "previous_event_hash")
        require_hash(self.payload_hash, "payload_hash")
        if canonical_content_hash(self.payload) != self.payload_hash:
            fail(FailureCode.LEDGER_CORRUPTION, "job event payload hash mismatch")
        object.__setattr__(self, "payload", MappingProxyType(dict(sorted(self.payload.items()))))


@dataclass(frozen=True, kw_only=True)
class ResearchJobStatus(CanonicalContract):
    SCHEMA_VERSION: ClassVar[str] = RESEARCH_JOB_SCHEMA_VERSION

    run_id: str
    state: JobState
    draft_hash: str
    manifest_hash: str | None
    committed_stage_hashes: tuple[str, ...]
    failed_stage_hashes: tuple[str, ...]
    blocked_stage_hashes: tuple[str, ...]
    provider_modes: tuple[ProviderMode, ...]
    event_log_hash: str
    report_reference: ResearchReportReference | None = None
    message: str = ""

    def __post_init__(self) -> None:
        require_safe_segment(self.run_id, "run_id")
        if not isinstance(self.state, JobState):
            object.__setattr__(self, "state", JobState(self.state))
        require_hash(self.draft_hash, "draft_hash")
        if self.manifest_hash is not None:
            require_hash(self.manifest_hash, "manifest_hash")
        for name in ("committed_stage_hashes", "failed_stage_hashes", "blocked_stage_hashes"):
            _hash_tuple(getattr(self, name), name)
        if not isinstance(self.provider_modes, tuple):
            fail(FailureCode.INVALID_VALUE, "provider_modes must be tuple")
        for item in self.provider_modes:
            if not isinstance(item, ProviderMode):
                fail(FailureCode.INVALID_VALUE, "provider_modes contains invalid value")
        require_hash(self.event_log_hash, "event_log_hash")
        if self.report_reference is not None and not isinstance(self.report_reference, ResearchReportReference):
            fail(FailureCode.INVALID_VALUE, "report_reference must be ResearchReportReference")
        if self.message:
            require_text(self.message, "message")


def map_recovery_status(status: RecoveryStatus, *, has_failed: bool, has_blocked: bool) -> JobState:
    if status is RecoveryStatus.CANCELLED:
        return JobState.CANCELLED
    if status is RecoveryStatus.INTERRUPTED:
        return JobState.INTERRUPTED
    if status is RecoveryStatus.FAILED or has_failed:
        return JobState.PARTIAL if has_failed else JobState.FAILED
    if status is RecoveryStatus.BLOCKED or has_blocked:
        return JobState.PARTIAL if has_blocked else JobState.BLOCKED
    if status is RecoveryStatus.COMMITTED:
        return JobState.COMPLETED
    return JobState.RUNNING


__all__ = [
    "ConfirmationAuthority",
    "JobEventType",
    "JobState",
    "ProviderMode",
    "ResearchJobDraft",
    "ResearchJobEvent",
    "ResearchJobManifest",
    "ResearchJobStage",
    "ResearchJobStatus",
    "ResearchReportReference",
    "ZERO_HASH",
]
