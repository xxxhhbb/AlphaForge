"""Typed DRAFT-only contracts for the AlphaForge research-agent runtime."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import ClassVar, Mapping

from ..contracts import CanonicalContract, FailureCode
from ..contracts.base import (
    fail,
    freeze_mapping,
    require_aware_datetime,
    require_hash,
    require_identifier,
    require_text,
)


SCHEMA_VERSION = "alphaforge.research-agent-runtime/v1"


class ResearchAgentStatus(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    PARTIAL = "PARTIAL"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class ResearchAgentFailureCode(str, Enum):
    CANCELLED = "CANCELLED"
    DEPENDENCY_BLOCKED = "DEPENDENCY_BLOCKED"
    TOOL_UNAVAILABLE = "TOOL_UNAVAILABLE"
    TOOL_UNAUTHORIZED = "TOOL_UNAUTHORIZED"
    PROVIDER_FAILED = "PROVIDER_FAILED"
    INVALID_MODEL_OUTPUT = "INVALID_MODEL_OUTPUT"
    CHECKPOINT_CORRUPTION = "CHECKPOINT_CORRUPTION"
    TASK_IDENTITY_MISMATCH = "TASK_IDENTITY_MISMATCH"
    RUNTIME_FAILED = "RUNTIME_FAILED"
    ARTIFACT_STORE_UNAVAILABLE = "ARTIFACT_STORE_UNAVAILABLE"


class ResearchDraftKind(str, Enum):
    PLAN = "PLAN"
    INVESTIGATION = "INVESTIGATION"
    DATA_GAP = "DATA_GAP"
    PROXY_SPEC = "PROXY_SPEC"
    CODE = "CODE"


@dataclass(frozen=True)
class ResearchDataGap(CanonicalContract):
    """A model-observed data limitation; never an authorization decision."""

    concept: str
    reason_code: str
    requested_fields: tuple[str, ...] = ()
    attempted_tools: tuple[str, ...] = ()
    detail: str = ""
    SCHEMA_VERSION: ClassVar[str] = SCHEMA_VERSION

    def __post_init__(self) -> None:
        require_text(self.concept, "concept")
        require_identifier(self.reason_code, "reason_code")
        for name in ("requested_fields", "attempted_tools"):
            values = getattr(self, name)
            if tuple(sorted(values)) != values or len(set(values)) != len(values):
                fail(FailureCode.INVALID_VALUE, f"{name} must be unique and stable-sorted")
            for value in values:
                require_identifier(value, name)
        if self.detail:
            require_text(self.detail, "detail")


@dataclass(frozen=True)
class ResearchProxySpec(CanonicalContract):
    """A transparent DRAFT proxy selected by the researcher for a missing concept."""

    requested_concept: str
    proxy_name: str
    source_fields: tuple[str, ...]
    rationale: str
    limitations: tuple[str, ...]
    SCHEMA_VERSION: ClassVar[str] = SCHEMA_VERSION

    def __post_init__(self) -> None:
        require_text(self.requested_concept, "requested_concept")
        require_identifier(self.proxy_name, "proxy_name")
        if not self.source_fields or tuple(sorted(self.source_fields)) != self.source_fields:
            fail(FailureCode.INVALID_VALUE, "source_fields must be non-empty and stable-sorted")
        for value in self.source_fields:
            require_identifier(value, "source_fields")
        require_text(self.rationale, "rationale")
        if not self.limitations or tuple(sorted(self.limitations)) != self.limitations:
            fail(FailureCode.INVALID_VALUE, "limitations must be non-empty and stable-sorted")
        for value in self.limitations:
            require_text(value, "limitations")


@dataclass(frozen=True)
class ResearchAgentTask(CanonicalContract):
    """One specialist task.  It grants investigation and DRAFT authority only."""

    task_id: str
    run_id: str
    specialist_id: str
    question: str
    campaign_hash: str
    data_snapshot_hash: str
    decision_time: datetime
    allowed_tools: tuple[str, ...]
    output_kind: str = "ScoreTable"
    answer_requirement_ids: tuple[str, ...] = ()
    required_answer_semantics: tuple[str, ...] = ()
    comparison_required: bool = False
    # Zero is retained only for replay of pre-contract outcomes.  Current
    # product tasks set an explicit minimum of one (or two for comparisons).
    minimum_experiments: int = 0
    max_experiments: int = 3
    depends_on: tuple[str, ...] = ()
    input_from: Mapping[str, str] = field(default_factory=dict)
    max_iterations: int = 12
    timeout_seconds: float = 300.0
    authority: str = "DRAFT_ONLY"
    SCHEMA_VERSION: ClassVar[str] = SCHEMA_VERSION

    def __post_init__(self) -> None:
        for name in ("task_id", "run_id", "specialist_id"):
            require_identifier(getattr(self, name), name)
        require_text(self.question, "question")
        require_hash(self.campaign_hash, "campaign_hash")
        require_hash(self.data_snapshot_hash, "data_snapshot_hash")
        require_aware_datetime(self.decision_time, "decision_time")
        if tuple(sorted(self.allowed_tools)) != self.allowed_tools or len(set(self.allowed_tools)) != len(self.allowed_tools):
            fail(FailureCode.INVALID_VALUE, "allowed_tools must be unique and stable-sorted")
        for name in self.allowed_tools:
            require_identifier(name, "allowed_tools")
        if self.output_kind not in {
            "ScoreTable", "EventTable", "RegressionTable", "RegimeComparisonTable",
            "DiagnosticTable", "DatasetAssessmentTable",
        }:
            fail(FailureCode.INVALID_VALUE, "unsupported research output kind")
        for name in ("answer_requirement_ids", "required_answer_semantics"):
            values = getattr(self, name)
            if tuple(sorted(set(values))) != values:
                fail(FailureCode.INVALID_VALUE, f"{name} must be unique and stable-sorted")
            for value in values:
                require_identifier(value, name)
        if isinstance(self.minimum_experiments, bool) or not 0 <= self.minimum_experiments <= 12:
            fail(FailureCode.INVALID_VALUE, "minimum_experiments must be 0..12")
        if isinstance(self.max_experiments, bool) or not self.minimum_experiments <= self.max_experiments <= 12:
            fail(FailureCode.INVALID_VALUE, "max_experiments must be minimum..12")
        if self.comparison_required and self.minimum_experiments < 2:
            fail(FailureCode.INVALID_VALUE, "comparison requires at least two experiments")
        if tuple(sorted(self.depends_on)) != self.depends_on or len(set(self.depends_on)) != len(self.depends_on):
            fail(FailureCode.INVALID_VALUE, "depends_on must be unique and stable-sorted")
        for name in self.depends_on:
            require_identifier(name, "depends_on")
            if name == self.task_id:
                fail(FailureCode.INVALID_STATE, "task cannot depend on itself")
        object.__setattr__(self, "input_from", freeze_mapping(self.input_from, "input_from"))
        for key, value in self.input_from.items():
            require_identifier(key, "input_from key")
            require_identifier(value, "input_from value")
            if value not in self.depends_on:
                fail(FailureCode.INVALID_STATE, "input_from must reference a declared dependency")
        if isinstance(self.max_iterations, bool) or not 1 <= self.max_iterations <= 50:
            fail(FailureCode.INVALID_VALUE, "max_iterations must be 1..50")
        if isinstance(self.timeout_seconds, bool) or not 1.0 <= float(self.timeout_seconds) <= 3600.0:
            fail(FailureCode.INVALID_VALUE, "timeout_seconds must be 1..3600")
        if self.authority != "DRAFT_ONLY":
            fail(FailureCode.UNAUTHORIZED_CAPABILITY, "research agent task is DRAFT only")


@dataclass(frozen=True)
class InvestigationReceipt(CanonicalContract):
    tool_name: str
    arguments_hash: str
    result_hash: str
    available_at: datetime
    SCHEMA_VERSION: ClassVar[str] = SCHEMA_VERSION

    def __post_init__(self) -> None:
        require_identifier(self.tool_name, "tool_name")
        require_hash(self.arguments_hash, "arguments_hash")
        require_hash(self.result_hash, "result_hash")
        require_aware_datetime(self.available_at, "available_at")


@dataclass(frozen=True)
class ResearchDraftArtifact(CanonicalContract):
    kind: ResearchDraftKind
    content_hash: str
    media_type: str
    SCHEMA_VERSION: ClassVar[str] = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if not isinstance(self.kind, ResearchDraftKind):
            fail(FailureCode.INVALID_VALUE, "invalid research draft kind")
        require_hash(self.content_hash, "content_hash")
        require_text(self.media_type, "media_type")


@dataclass(frozen=True)
class ResearchAgentOutcome(CanonicalContract):
    task_id: str
    task_hash: str
    specialist_id: str
    status: ResearchAgentStatus
    plan_hash: str | None
    investigation_receipts: tuple[InvestigationReceipt, ...]
    draft_artifacts: tuple[ResearchDraftArtifact, ...]
    provider_calls: int
    tool_calls: int
    data_gaps: tuple[ResearchDataGap, ...] = ()
    proxy_specs: tuple[ResearchProxySpec, ...] = ()
    failure_code: ResearchAgentFailureCode | None = None
    failure_origin: str | None = None
    authority: str = "DRAFT_ONLY"
    official_evidence_hash: None = None
    promotion_hash: None = None
    SCHEMA_VERSION: ClassVar[str] = SCHEMA_VERSION

    def __post_init__(self) -> None:
        require_identifier(self.task_id, "task_id")
        require_hash(self.task_hash, "task_hash")
        require_identifier(self.specialist_id, "specialist_id")
        if not isinstance(self.status, ResearchAgentStatus):
            fail(FailureCode.INVALID_VALUE, "invalid research agent status")
        if self.plan_hash is not None:
            require_hash(self.plan_hash, "plan_hash")
        if tuple(sorted(self.investigation_receipts, key=lambda item: (item.tool_name, item.arguments_hash))) != self.investigation_receipts:
            fail(FailureCode.INVALID_STATE, "investigation receipts must be stable-sorted")
        if tuple(sorted(self.draft_artifacts, key=lambda item: (item.kind.value, item.content_hash))) != self.draft_artifacts:
            fail(FailureCode.INVALID_STATE, "draft artifacts must be stable-sorted")
        if tuple(sorted(self.data_gaps, key=lambda item: (item.concept, item.reason_code))) != self.data_gaps:
            fail(FailureCode.INVALID_STATE, "data_gaps must be stable-sorted")
        if tuple(sorted(self.proxy_specs, key=lambda item: (item.requested_concept, item.proxy_name))) != self.proxy_specs:
            fail(FailureCode.INVALID_STATE, "proxy_specs must be stable-sorted")
        for name in ("provider_calls", "tool_calls"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                fail(FailureCode.INVALID_VALUE, f"{name} must be non-negative")
        if self.failure_origin is not None:
            require_identifier(self.failure_origin, "failure_origin")
        terminal_failure = self.status in {
            ResearchAgentStatus.BLOCKED,
            ResearchAgentStatus.FAILED,
            ResearchAgentStatus.CANCELLED,
        }
        if terminal_failure != (self.failure_code is not None):
            fail(FailureCode.INVALID_STATE, "terminal failure status and failure_code disagree")
        if self.status is ResearchAgentStatus.COMPLETED and self.plan_hash is None:
            fail(FailureCode.INCOMPLETE_RESULT, "completed research outcome needs a plan")
        if self.authority != "DRAFT_ONLY" or self.official_evidence_hash is not None or self.promotion_hash is not None:
            fail(FailureCode.UNAUTHORIZED_CAPABILITY, "research agents have no Evidence or Promotion authority")


@dataclass(frozen=True)
class ResearchSwarmResult(CanonicalContract):
    run_id: str
    manifest_hash: str
    outcomes: tuple[ResearchAgentOutcome, ...]
    status: ResearchAgentStatus
    authority: str = "DRAFT_ONLY"
    SCHEMA_VERSION: ClassVar[str] = SCHEMA_VERSION

    def __post_init__(self) -> None:
        require_identifier(self.run_id, "run_id")
        require_hash(self.manifest_hash, "manifest_hash")
        ids = tuple(item.task_id for item in self.outcomes)
        if not ids or ids != tuple(sorted(ids)) or len(ids) != len(set(ids)):
            fail(FailureCode.INVALID_STATE, "outcomes must be unique and stable-sorted")
        if not isinstance(self.status, ResearchAgentStatus):
            fail(FailureCode.INVALID_VALUE, "invalid swarm result status")
        if self.authority != "DRAFT_ONLY":
            fail(FailureCode.UNAUTHORIZED_CAPABILITY, "research swarm is DRAFT only")


__all__ = [
    "InvestigationReceipt",
    "ResearchDataGap",
    "ResearchAgentFailureCode",
    "ResearchAgentOutcome",
    "ResearchAgentStatus",
    "ResearchAgentTask",
    "ResearchDraftArtifact",
    "ResearchDraftKind",
    "ResearchProxySpec",
    "ResearchSwarmResult",
]
