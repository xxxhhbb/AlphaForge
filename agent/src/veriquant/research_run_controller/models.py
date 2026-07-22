"""Research OS run-control contracts.

These contracts describe orchestration checkpoints only. They do not calculate
FactorDAG/PIT/Evidence, Promotion, Portfolio, Risk, Execution, broker, order, or
ledger truth.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import ClassVar

from veriquant.contracts import CanonicalContract, FailureCode
from veriquant.contracts.base import fail, require_aware_datetime, require_hash, require_identifier, require_text
from veriquant.run_recovery.api import RecoveryStatus, StageIdentity, StageTask


RESEARCH_RUN_CONTROLLER_SCHEMA_VERSION = "alphaforge.research-run-controller/v2"
LEGACY_RESEARCH_RUN_CONTROLLER_SCHEMA_VERSION = "alphaforge.research-run-controller/v1"
ZERO_HASH = "sha256:" + "0" * 64


class StrEnum(str, Enum):
    """Python 3.10-compatible stable string enum."""


class ResearchRunStageKind(StrEnum):
    PLANNING = "PLANNING"
    SELECTED_GROUP = "SELECTED_GROUP"
    TRANSLATION = "TRANSLATION"
    CANDIDATE_VALIDATION = "CANDIDATE_VALIDATION"
    TOURNAMENT = "TOURNAMENT"
    REPORT = "REPORT"


class ResearchRunLifecycle(StrEnum):
    PLANNING = "PLANNING"
    RESEARCHING = "RESEARCHING"
    CANDIDATE_PREFLIGHT = "CANDIDATE_PREFLIGHT"
    AWAITING_EVIDENCE = "AWAITING_EVIDENCE"
    COMPLETED = "COMPLETED"
    PARTIAL = "PARTIAL"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


def _safe_segment(value: str, field_name: str) -> None:
    require_identifier(value, field_name)
    if any(token in value for token in ("..", "/", "\\", ":")):
        fail(FailureCode.UNAUTHORIZED_CAPABILITY, f"{field_name} must be a single safe segment", field=field_name)


def _hash_tuple(value: tuple[str, ...], field_name: str, *, required: bool = False) -> None:
    if not isinstance(value, tuple) or (required and not value):
        fail(FailureCode.INVALID_VALUE, f"{field_name} must be a tuple", field=field_name)
    for item in value:
        require_hash(item, field_name)
    if len(value) != len(set(value)):
        fail(FailureCode.INVALID_VALUE, f"{field_name} contains duplicates", field=field_name)


def _segment_tuple(value: tuple[str, ...], field_name: str, *, required: bool = False) -> None:
    if not isinstance(value, tuple) or (required and not value):
        fail(FailureCode.INVALID_VALUE, f"{field_name} must be a tuple", field=field_name)
    for item in value:
        _safe_segment(item, field_name)
    if len(value) != len(set(value)):
        fail(FailureCode.INVALID_VALUE, f"{field_name} contains duplicates", field=field_name)


@dataclass(frozen=True, kw_only=True)
class ResearchRunManifest(CanonicalContract):
    SCHEMA_VERSION: ClassVar[str] = RESEARCH_RUN_CONTROLLER_SCHEMA_VERSION

    run_id: str
    campaign_hash: str
    intent_hash: str
    data_plan_hash: str
    route_plan_hash: str
    policy_hash: str
    code_hash: str
    selected_group_ids: tuple[str, ...]
    candidate_request_hashes: tuple[str, ...]
    created_at: datetime
    runtime_version: str = "research_run_controller_v2"
    provider_id: str = "deterministic_offline"
    model_id: str = "none"
    data_snapshot_hash: str = ZERO_HASH
    current_stage: ResearchRunLifecycle = ResearchRunLifecycle.PLANNING
    remaining_iterations: int = 0
    remaining_tool_calls: int = 0
    remaining_candidate_slots: int = 0
    candidate_count: int = 0
    trial_count: int = 0
    typed_failures: tuple[str, ...] = ()
    official_evidence_hash: str | None = None
    report_hash: str | None = None

    def __post_init__(self) -> None:
        _safe_segment(self.run_id, "run_id")
        for name in ("campaign_hash", "intent_hash", "data_plan_hash", "route_plan_hash", "policy_hash", "code_hash"):
            require_hash(getattr(self, name), name)
        _segment_tuple(self.selected_group_ids, "selected_group_ids", required=True)
        _hash_tuple(self.candidate_request_hashes, "candidate_request_hashes")
        require_aware_datetime(self.created_at, "created_at")
        for name in ("runtime_version", "provider_id", "model_id"):
            _safe_segment(getattr(self, name), name)
        require_hash(self.data_snapshot_hash, "data_snapshot_hash")
        if not isinstance(self.current_stage, ResearchRunLifecycle):
            object.__setattr__(self, "current_stage", ResearchRunLifecycle(self.current_stage))
        for name in (
            "remaining_iterations", "remaining_tool_calls", "remaining_candidate_slots",
            "candidate_count", "trial_count",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                fail(FailureCode.INVALID_VALUE, f"{name} must be a non-negative integer")
        _segment_tuple(self.typed_failures, "typed_failures")
        if self.official_evidence_hash is not None:
            require_hash(self.official_evidence_hash, "official_evidence_hash")
        if self.report_hash is not None:
            require_hash(self.report_hash, "report_hash")

    @property
    def identity_hash(self) -> str:
        import hashlib
        import json

        from veriquant.contracts.base import canonicalize

        material = {
            "run_id": self.run_id,
            "campaign_hash": self.campaign_hash,
            "intent_hash": self.intent_hash,
            "data_plan_hash": self.data_plan_hash,
            "route_plan_hash": self.route_plan_hash,
            "policy_hash": self.policy_hash,
            "code_hash": self.code_hash,
            "selected_group_ids": self.selected_group_ids,
            "candidate_request_hashes": self.candidate_request_hashes,
            "created_at": self.created_at,
            "runtime_version": self.runtime_version,
            "provider_id": self.provider_id,
            "model_id": self.model_id,
            "data_snapshot_hash": self.data_snapshot_hash,
        }
        rendered = json.dumps(canonicalize(material), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return "sha256:" + hashlib.sha256(rendered.encode("utf-8")).hexdigest()

    @classmethod
    def from_payload(cls, payload, *, component=None):
        raw = dict(payload)
        if raw.get("schema_version") == LEGACY_RESEARCH_RUN_CONTROLLER_SCHEMA_VERSION:
            raw["schema_version"] = cls.SCHEMA_VERSION
        return super().from_payload(raw, component=component)


@dataclass(frozen=True, kw_only=True)
class ResearchRunStage(CanonicalContract):
    SCHEMA_VERSION: ClassVar[str] = RESEARCH_RUN_CONTROLLER_SCHEMA_VERSION

    stage_key: str
    stage_kind: ResearchRunStageKind
    branch_id: str
    ordinal: int
    input_hash: str
    config_hash: str
    code_hash: str
    policy_hash: str
    expected_output_hash: str
    provider_id: str = "research_stage_provider"
    depends_on_stage_keys: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _safe_segment(self.stage_key, "stage_key")
        if not isinstance(self.stage_kind, ResearchRunStageKind):
            object.__setattr__(self, "stage_kind", ResearchRunStageKind(self.stage_kind))
        _safe_segment(self.branch_id, "branch_id")
        if isinstance(self.ordinal, bool) or not isinstance(self.ordinal, int) or self.ordinal < 0:
            fail(FailureCode.INVALID_VALUE, "ordinal must be a non-negative integer", field="ordinal")
        for name in ("input_hash", "config_hash", "code_hash", "policy_hash", "expected_output_hash"):
            require_hash(getattr(self, name), name)
        _safe_segment(self.provider_id, "provider_id")
        _segment_tuple(self.depends_on_stage_keys, "depends_on_stage_keys")

    def to_stage_identity(self, run_id: str) -> StageIdentity:
        return StageIdentity(
            run_id=run_id,
            branch_id=self.branch_id,
            stage_id=self.stage_key,
            ordinal=self.ordinal,
            input_hash=self.input_hash,
            config_hash=self.config_hash,
            code_hash=self.code_hash,
            policy_hash=self.policy_hash,
            expected_artifact_hash=self.expected_output_hash,
        )


@dataclass(frozen=True, kw_only=True)
class ResearchRunPlan(CanonicalContract):
    SCHEMA_VERSION: ClassVar[str] = RESEARCH_RUN_CONTROLLER_SCHEMA_VERSION

    manifest: ResearchRunManifest
    stages: tuple[ResearchRunStage, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.manifest, ResearchRunManifest):
            fail(FailureCode.INVALID_VALUE, "manifest must be ResearchRunManifest")
        if not isinstance(self.stages, tuple) or not self.stages:
            fail(FailureCode.INVALID_VALUE, "stages must be a non-empty tuple")
        by_key: dict[str, ResearchRunStage] = {}
        for stage in self.stages:
            if not isinstance(stage, ResearchRunStage):
                fail(FailureCode.INVALID_VALUE, "stages must contain ResearchRunStage")
            if stage.stage_key in by_key:
                fail(FailureCode.INVALID_VALUE, "duplicate stage_key")
            by_key[stage.stage_key] = stage
        for stage in self.stages:
            missing = tuple(key for key in stage.depends_on_stage_keys if key not in by_key)
            if missing:
                fail(FailureCode.INVALID_STATE, "stage dependency is missing", missing=missing)
            for dependency in stage.depends_on_stage_keys:
                if by_key[dependency].ordinal >= stage.ordinal:
                    fail(FailureCode.INVALID_STATE, "stage dependencies must have lower ordinal")

    def to_stage_tasks(self) -> tuple[StageTask, ...]:
        identities = {stage.stage_key: stage.to_stage_identity(self.manifest.run_id) for stage in self.stages}
        return tuple(
            StageTask(
                identity=identities[stage.stage_key],
                provider_id=stage.provider_id,
                depends_on=tuple(identities[key].artifact_hash for key in stage.depends_on_stage_keys),
            )
            for stage in self.stages
        )


@dataclass(frozen=True, kw_only=True)
class ResearchRunExecutionSummary(CanonicalContract):
    SCHEMA_VERSION: ClassVar[str] = RESEARCH_RUN_CONTROLLER_SCHEMA_VERSION

    run_id: str
    status: RecoveryStatus
    manifest_hash: str
    resume_plan_hash: str
    committed_stage_hashes: tuple[str, ...]
    skipped_stage_hashes: tuple[str, ...]
    runnable_stage_hashes: tuple[str, ...]
    failed_stage_hashes: tuple[str, ...]
    blocked_stage_hashes: tuple[str, ...]
    final_artifact_hash: str | None = None
    event_log_hash: str | None = None
    message: str = ""

    def __post_init__(self) -> None:
        _safe_segment(self.run_id, "run_id")
        if not isinstance(self.status, RecoveryStatus):
            object.__setattr__(self, "status", RecoveryStatus(self.status))
        require_hash(self.manifest_hash, "manifest_hash")
        require_hash(self.resume_plan_hash, "resume_plan_hash")
        for name in (
            "committed_stage_hashes",
            "skipped_stage_hashes",
            "runnable_stage_hashes",
            "failed_stage_hashes",
            "blocked_stage_hashes",
        ):
            _hash_tuple(getattr(self, name), name)
        if self.final_artifact_hash is not None:
            require_hash(self.final_artifact_hash, "final_artifact_hash")
        if self.event_log_hash is not None:
            require_hash(self.event_log_hash, "event_log_hash")
        if self.message:
            require_text(self.message, "message")
