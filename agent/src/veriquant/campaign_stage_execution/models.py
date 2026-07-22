"""Contracts for executing exactly one Campaign research stage.

This package is deliberately below the final orchestrator and above the
existing deterministic truth services.  It never calculates official metrics or
promotion decisions; it only records a single stage hand-off and its redacted
artifact references.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Any, ClassVar, Mapping, Protocol

from ..contracts import CanonicalContract, Component, FailureCode
from ..contracts.base import (
    canonicalize,
    fail,
    freeze_mapping,
    require_hash,
    require_identifier,
    require_text,
)
from ..llm_desks.safety import assert_no_forbidden_content
from ..research_campaign import CampaignState


CAMPAIGN_STAGE_EXECUTION_SCHEMA_VERSION = "alphaforge.campaign-stage-execution/v1"


class StrEnum(str, Enum):
    """Python 3.10-compatible string enum with stable JSON values."""


class ProviderMode(StrEnum):
    DETERMINISTIC = "DETERMINISTIC"
    REAL_LLM = "REAL_LLM"


class CampaignStageStatus(StrEnum):
    DRAFT = "DRAFT"
    EXPLANATION = "EXPLANATION"
    CONTROL_REFERENCE = "CONTROL_REFERENCE"
    BLOCKED = "BLOCKED"
    SKIPPED_UPSTREAM = "SKIPPED_UPSTREAM"
    UNAVAILABLE = "UNAVAILABLE"
    CANCELLED = "CANCELLED"


class CampaignStageFailure(StrEnum):
    PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
    STAGE_FAILED_CLOSED = "STAGE_FAILED_CLOSED"
    UPSTREAM_BLOCKED = "UPSTREAM_BLOCKED"
    AUTHORITY_VIOLATION = "AUTHORITY_VIOLATION"
    CAMPAIGN_NOT_CONFIRMED = "CAMPAIGN_NOT_CONFIRMED"
    ROUTE_NOT_READY = "ROUTE_NOT_READY"


class CampaignStageName(StrEnum):
    HYPOTHESIS = "hypothesis"
    FACTOR_CONSTRUCT = "factor_construct"
    FACTOR_DAG_PIT = "factor_dag_pit"
    OFFICIAL_EVALUATE = "official_evaluate"
    REDACTED_FEEDBACK = "redacted_feedback"
    PREREGISTER_FALSIFICATION = "preregister_falsification"
    PIT_EVALUATE = "pit_evaluate"
    VALIDATION_CONCLUSION = "validation_conclusion"
    SCOPE_PROBLEM = "scope_problem"
    RANKED_DIAGNOSIS = "ranked_diagnosis"


_LLM_COMPONENTS = frozenset({Component.LLM_RESEARCHER, Component.RESEARCH_DESK})
_LLM_STATUSES = frozenset({CampaignStageStatus.DRAFT, CampaignStageStatus.EXPLANATION})
_CONTROL_STAGE_TOKENS = (
    "pit",
    "evaluate",
    "evidence",
    "retrieve_registered_evidence",
    "attribution",
    "regime",
    "coverage",
    "reproduce",
    "drift",
)
_EXPLANATION_STAGE_TOKENS = ("feedback", "conclusion", "diagnosis", "assessment", "report", "recommend")


def stage_key_for(route_task_id: str, stage_id: str) -> str:
    require_identifier(route_task_id, "route_task_id")
    require_identifier(stage_id, "stage_id")
    return f"{route_task_id}:{stage_id}"


def _stable_hash(value: Any) -> str:
    rendered = json.dumps(canonicalize(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(rendered.encode("utf-8")).hexdigest()


def _require_identifier_tuple(value: tuple[str, ...], field_name: str, *, nonempty: bool = True) -> None:
    if not isinstance(value, tuple) or (nonempty and not value):
        fail(FailureCode.INVALID_VALUE, f"{field_name} must be a tuple")
    for item in value:
        require_identifier(item, field_name)
    if len(set(value)) != len(value):
        fail(FailureCode.INVALID_VALUE, f"{field_name} contains duplicates")


@dataclass(frozen=True, kw_only=True)
class StageExecutionPolicy(CanonicalContract):
    SCHEMA_VERSION: ClassVar[str] = CAMPAIGN_STAGE_EXECUTION_SCHEMA_VERSION

    policy_id: str = "stage-policy.v1"
    provider_mode: ProviderMode = ProviderMode.DETERMINISTIC
    allow_real_provider: bool = False
    allow_deterministic_fallback: bool = True
    max_prompt_bytes: int = 12000
    redact_sealed: bool = True

    def __post_init__(self) -> None:
        require_identifier(self.policy_id, "policy_id")
        if not isinstance(self.provider_mode, ProviderMode):
            object.__setattr__(self, "provider_mode", ProviderMode(self.provider_mode))
        for name in ("allow_real_provider", "allow_deterministic_fallback", "redact_sealed"):
            if not isinstance(getattr(self, name), bool):
                fail(FailureCode.INVALID_VALUE, f"{name} must be boolean")
        if isinstance(self.max_prompt_bytes, bool) or not isinstance(self.max_prompt_bytes, int) or self.max_prompt_bytes < 1:
            fail(FailureCode.INVALID_VALUE, "max_prompt_bytes must be positive")
        if self.provider_mode is ProviderMode.REAL_LLM and self.allow_deterministic_fallback:
            fail(
                FailureCode.UNAUTHORIZED_CAPABILITY,
                "REAL_LLM stage execution cannot silently fall back to deterministic",
                stage_failure_code=CampaignStageFailure.AUTHORITY_VIOLATION.value,
            )


@dataclass(frozen=True, kw_only=True)
class StageArtifactRef(CanonicalContract):
    SCHEMA_VERSION: ClassVar[str] = CAMPAIGN_STAGE_EXECUTION_SCHEMA_VERSION

    ref_id: str
    artifact_type: str
    content_hash: str
    produced_by: Component
    visibility: str = "RESEARCH_VISIBLE"
    locator: str | None = None

    def __post_init__(self) -> None:
        require_identifier(self.ref_id, "ref_id")
        require_identifier(self.artifact_type, "artifact_type")
        require_hash(self.content_hash, "content_hash")
        if not isinstance(self.produced_by, Component):
            object.__setattr__(self, "produced_by", Component(self.produced_by))
        require_identifier(self.visibility, "visibility")
        if self.locator is not None:
            require_text(self.locator, "locator")
            normalized = self.locator.replace("\\", "/")
            if normalized.startswith("/") or "://" in normalized or any(part in {"", ".", ".."} for part in normalized.split("/")):
                fail(FailureCode.UNAUTHORIZED_CAPABILITY, "artifact locator must be relative and traversal-free")


@dataclass(frozen=True, kw_only=True)
class CampaignStageRequest(CanonicalContract):
    SCHEMA_VERSION: ClassVar[str] = CAMPAIGN_STAGE_EXECUTION_SCHEMA_VERSION

    run_id: str
    campaign_hash: str
    route_hash: str
    protocol_plan_hash: str
    campaign_state: CampaignState
    route_state: CampaignState
    selected_desks: tuple[str, ...]
    route_task_id: str
    desk_id: str
    stage_id: str
    stage_index: int
    stage_sequence: tuple[str, ...]
    input_hash: str
    artifact_namespace: str
    context: Mapping[str, Any] = field(default_factory=dict)
    upstream_satisfied: bool = True

    def __post_init__(self) -> None:
        for name in ("run_id", "route_task_id", "desk_id", "stage_id", "artifact_namespace"):
            require_identifier(getattr(self, name), name)
        for name in ("campaign_hash", "route_hash", "protocol_plan_hash", "input_hash"):
            require_hash(getattr(self, name), name)
        if not isinstance(self.campaign_state, CampaignState):
            object.__setattr__(self, "campaign_state", CampaignState(self.campaign_state))
        if not isinstance(self.route_state, CampaignState):
            object.__setattr__(self, "route_state", CampaignState(self.route_state))
        if self.campaign_state is not CampaignState.CONFIRMED:
            fail(
                FailureCode.INVALID_STATE,
                "stage execution requires a confirmed Campaign",
                stage_failure_code=CampaignStageFailure.CAMPAIGN_NOT_CONFIRMED.value,
            )
        if self.route_state is not CampaignState.ROUTED:
            fail(
                FailureCode.INVALID_STATE,
                "stage execution requires a routed CampaignRoute",
                stage_failure_code=CampaignStageFailure.ROUTE_NOT_READY.value,
            )
        _require_identifier_tuple(self.selected_desks, "selected_desks")
        if self.desk_id not in self.selected_desks:
            fail(FailureCode.UNAUTHORIZED_CAPABILITY, "desk was not selected by Campaign Router")
        if isinstance(self.stage_index, bool) or not isinstance(self.stage_index, int) or self.stage_index < 0:
            fail(FailureCode.INVALID_VALUE, "stage_index must be a non-negative integer")
        _require_identifier_tuple(self.stage_sequence, "stage_sequence")
        if self.stage_id not in self.stage_sequence or self.stage_sequence[self.stage_index] != self.stage_id:
            fail(FailureCode.INVALID_STATE, "stage_id must match the fixed protocol stage_sequence")
        if not isinstance(self.upstream_satisfied, bool):
            fail(FailureCode.INVALID_VALUE, "upstream_satisfied must be boolean")
        context = freeze_mapping(self.context, "context")
        assert_no_forbidden_content(context, where="CampaignStageRequest.context")
        object.__setattr__(self, "context", MappingProxyType(dict(sorted(context.items()))))

    @property
    def stage_key(self) -> str:
        return stage_key_for(self.route_task_id, self.stage_id)


@dataclass(frozen=True, kw_only=True)
class CampaignStageResult(CanonicalContract):
    SCHEMA_VERSION: ClassVar[str] = CAMPAIGN_STAGE_EXECUTION_SCHEMA_VERSION

    run_id: str
    stage_key: str
    route_task_id: str
    desk_id: str
    stage_id: str
    stage_index: int
    status: CampaignStageStatus
    produced_by: Component
    input_hash: str
    output_hash: str | None
    prompt_hash: str | None
    trace_hash: str | None
    artifact_refs: tuple[StageArtifactRef, ...] = ()
    failure_code: CampaignStageFailure | None = None
    failure_detail_code: FailureCode | None = None
    message: str | None = None

    def __post_init__(self) -> None:
        for name in ("run_id", "stage_key", "route_task_id", "desk_id", "stage_id"):
            require_identifier(getattr(self, name), name)
        if self.stage_key != stage_key_for(self.route_task_id, self.stage_id):
            fail(FailureCode.INVALID_VALUE, "stage_key must match route_task_id and stage_id")
        if isinstance(self.stage_index, bool) or not isinstance(self.stage_index, int) or self.stage_index < 0:
            fail(FailureCode.INVALID_VALUE, "stage_index must be non-negative")
        if not isinstance(self.status, CampaignStageStatus):
            object.__setattr__(self, "status", CampaignStageStatus(self.status))
        if not isinstance(self.produced_by, Component):
            object.__setattr__(self, "produced_by", Component(self.produced_by))
        for name in ("input_hash", "output_hash", "prompt_hash", "trace_hash"):
            value = getattr(self, name)
            if value is not None:
                require_hash(value, name)
        if not isinstance(self.artifact_refs, tuple) or not all(isinstance(item, StageArtifactRef) for item in self.artifact_refs):
            fail(FailureCode.INVALID_VALUE, "artifact_refs must contain StageArtifactRef")
        if self.failure_code is not None and not isinstance(self.failure_code, CampaignStageFailure):
            object.__setattr__(self, "failure_code", CampaignStageFailure(self.failure_code))
        if self.failure_detail_code is not None and not isinstance(self.failure_detail_code, FailureCode):
            object.__setattr__(self, "failure_detail_code", FailureCode(self.failure_detail_code))
        if self.message is not None:
            require_text(self.message, "message")
            assert_no_forbidden_content(self.message, where="CampaignStageResult.message")
        if self.produced_by in _LLM_COMPONENTS and self.status not in _LLM_STATUSES:
            fail(
                FailureCode.UNAUTHORIZED_CAPABILITY,
                "LLM/Research Desk stages may emit only DRAFT or EXPLANATION",
                stage_failure_code=CampaignStageFailure.AUTHORITY_VIOLATION.value,
            )
        if self.status in {CampaignStageStatus.DRAFT, CampaignStageStatus.EXPLANATION, CampaignStageStatus.CONTROL_REFERENCE}:
            if self.output_hash is None:
                fail(FailureCode.INVALID_HASH, "successful stage requires output_hash")
        if self.status in {
            CampaignStageStatus.BLOCKED,
            CampaignStageStatus.SKIPPED_UPSTREAM,
            CampaignStageStatus.UNAVAILABLE,
            CampaignStageStatus.CANCELLED,
        } and self.failure_code is None:
            fail(FailureCode.INVALID_VALUE, "non-success stage requires failure_code")


class CampaignStageExecutor(Protocol):
    def run_stage(self, request: CampaignStageRequest, policy: StageExecutionPolicy) -> CampaignStageResult: ...


def stage_kind(stage_id: str) -> tuple[CampaignStageStatus, Component]:
    lowered = stage_id.lower()
    if any(token in lowered for token in _CONTROL_STAGE_TOKENS):
        return CampaignStageStatus.CONTROL_REFERENCE, Component.EVIDENCE_SERVICE
    if any(token in lowered for token in _EXPLANATION_STAGE_TOKENS):
        return CampaignStageStatus.EXPLANATION, Component.RESEARCH_DESK
    return CampaignStageStatus.DRAFT, Component.RESEARCH_DESK


def deterministic_artifact_ref(request: CampaignStageRequest, status: CampaignStageStatus, produced_by: Component) -> StageArtifactRef:
    material = {
        "run_id": request.run_id,
        "campaign_hash": request.campaign_hash,
        "stage_key": request.stage_key,
        "status": status.value,
        "produced_by": produced_by.value,
    }
    return StageArtifactRef(
        ref_id=f"artifact.{request.route_task_id}.{request.stage_id}",
        artifact_type=status.value.lower(),
        content_hash=_stable_hash(material),
        produced_by=produced_by,
        visibility="CONTROL_REFERENCE" if status is CampaignStageStatus.CONTROL_REFERENCE else "RESEARCH_VISIBLE",
        locator=f"{request.run_id}/{request.route_task_id}/{request.stage_id}.json",
    )
