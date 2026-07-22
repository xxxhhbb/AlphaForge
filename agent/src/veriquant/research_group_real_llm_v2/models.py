"""Typed boundaries for selected-only AlphaForge research-group proposals."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, ClassVar, Mapping

from ..contracts import CanonicalContract, FailureCode
from ..contracts.base import fail, require_hash, require_identifier, require_text
from ..research_group_llm_adapter import ProviderMode
from ..research_group_runtime.selector import SelectedGroupContext
from ..research_os import ResearchIntent


SCHEMA_VERSION = "alphaforge.research-group-real-llm-v2/v1"


class GroupProposalProvenance(str, Enum):
    REAL_LLM = "REAL_LLM"
    DETERMINISTIC_FALLBACK = "DETERMINISTIC_FALLBACK"


class GroupAttemptStatus(str, Enum):
    COMPLETED = "COMPLETED"
    FALLBACK_COMPLETED = "FALLBACK_COMPLETED"
    FAILED = "FAILED"


@dataclass(frozen=True)
class GroupExecutionPolicy(CanonicalContract):
    group_id: str
    allowed_fields: tuple[str, ...]
    tool_whitelist: tuple[str, ...]
    max_tokens: int = 2048
    max_iterations: int = 2
    timeout_seconds: float = 30.0
    candidate_budget: int = 2
    max_revisions: int = 1
    schema_version: ClassVar[str] = SCHEMA_VERSION

    def __post_init__(self) -> None:
        require_identifier(self.group_id, "group_id")
        for name in ("allowed_fields", "tool_whitelist"):
            values = getattr(self, name)
            if not values or len(values) != len(set(values)):
                fail(FailureCode.INVALID_VALUE, f"{name} must be unique and non-empty")
            for value in values:
                require_identifier(value, name)
        forbidden_tools = {"shell", "python", "write_file", "promotion", "order", "broker"}
        if any(any(token in tool.lower() for token in forbidden_tools) for tool in self.tool_whitelist):
            fail(FailureCode.UNAUTHORIZED_CAPABILITY, "research-group tools must be read-only")
        for name in ("max_tokens", "max_iterations", "candidate_budget"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                fail(FailureCode.INVALID_VALUE, f"{name} must be a positive integer")
        if self.max_iterations > 6 or not 1 <= self.candidate_budget <= 3:
            fail(FailureCode.PROVIDER_BUDGET_EXCEEDED, "group iteration/candidate budget is out of bounds")
        if self.max_revisions not in (0, 1):
            fail(FailureCode.PROVIDER_BUDGET_EXCEEDED, "at most one bounded revision is allowed")
        if not 0 < float(self.timeout_seconds) <= 300:
            fail(FailureCode.INVALID_VALUE, "timeout_seconds must be in (0, 300]")


@dataclass(frozen=True)
class GroupLLMCallRequest(CanonicalContract):
    context: SelectedGroupContext
    intent: ResearchIntent
    policy: GroupExecutionPolicy
    selected_group_ids: tuple[str, ...]
    attempt_number: int
    repair_code: str | None = None
    prior_payload_hash: str | None = None
    schema_version: ClassVar[str] = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.context.group_id != self.policy.group_id:
            fail(FailureCode.INVALID_FIELD_OWNER, "policy belongs to another research group")
        if self.context.group_id not in self.selected_group_ids:
            fail(FailureCode.UNAUTHORIZED_CAPABILITY, "unselected group cannot call the provider")
        if not set(self.context.tool_whitelist).issubset(self.policy.tool_whitelist):
            fail(FailureCode.UNAUTHORIZED_CAPABILITY, "context contains undeclared tools")
        if self.attempt_number not in (1, 2):
            fail(FailureCode.PROVIDER_BUDGET_EXCEEDED, "only an initial call and one revision are allowed")
        if self.attempt_number == 1 and (self.repair_code or self.prior_payload_hash):
            fail(FailureCode.INVALID_STATE, "initial call cannot contain repair material")
        if self.attempt_number == 2:
            if not self.repair_code or not self.prior_payload_hash:
                fail(FailureCode.MISSING_REQUIRED_FIELD, "revision requires typed repair code and prior hash")
            require_identifier(self.repair_code, "repair_code")
            require_hash(self.prior_payload_hash, "prior_payload_hash")


@dataclass(frozen=True)
class GroupLLMCallResponse:
    payload: Mapping[str, Any]
    input_tokens: int = 0
    output_tokens: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.payload, Mapping):
            fail(FailureCode.PROVIDER_SCHEMA_REJECTED, "provider payload must be an object")
        for name in ("input_tokens", "output_tokens"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                fail(FailureCode.PROVIDER_SCHEMA_REJECTED, f"{name} must be a non-negative integer")


@dataclass(frozen=True)
class GroupProposalAttempt(CanonicalContract):
    group_id: str
    status: GroupAttemptStatus
    provenance: GroupProposalProvenance
    provider_calls: int
    revisions_used: int
    tokens_used: int
    proposal_hash: str | None
    failure_code: str | None = None
    schema_version: ClassVar[str] = SCHEMA_VERSION

    def __post_init__(self) -> None:
        require_identifier(self.group_id, "group_id")
        if self.provider_calls < 0 or self.revisions_used not in (0, 1) or self.tokens_used < 0:
            fail(FailureCode.INVALID_VALUE, "attempt accounting cannot be negative")
        if self.proposal_hash is not None:
            require_hash(self.proposal_hash, "proposal_hash")
        if self.status is GroupAttemptStatus.FAILED and not self.failure_code:
            fail(FailureCode.MISSING_REQUIRED_FIELD, "failed attempt requires failure_code")
        if self.status is not GroupAttemptStatus.FAILED and self.proposal_hash is None:
            fail(FailureCode.MISSING_REQUIRED_FIELD, "completed attempt requires proposal_hash")
        if self.provenance is GroupProposalProvenance.DETERMINISTIC_FALLBACK and self.status is not GroupAttemptStatus.FALLBACK_COMPLETED:
            fail(FailureCode.INVALID_STATE, "fallback provenance must remain explicit")

