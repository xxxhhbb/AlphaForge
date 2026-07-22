"""Stable contracts for bounded concurrent DRAFT proposal collection."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import ClassVar

from ..contracts import CanonicalContract, FailureCode
from ..contracts.base import fail, require_hash, require_identifier
from ..research_group_runtime.runtime import GroupProposalPayload
from ..research_os import ResearchGroupSubmission


SCHEMA_VERSION = "alphaforge.concurrent-research-group-runtime/v2"


class ConcurrentBranchStatus(str, Enum):
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    TIMED_OUT = "TIMED_OUT"


class ConcurrentBranchFailureCode(str, Enum):
    PROVIDER_FAILED = "PROVIDER_FAILED"
    PROVIDER_TIMEOUT = "PROVIDER_TIMEOUT"
    GROUP_MISMATCH = "GROUP_MISMATCH"
    BUDGET_EXCEEDED = "BUDGET_EXCEEDED"
    FORBIDDEN_PAYLOAD = "FORBIDDEN_PAYLOAD"


@dataclass(frozen=True, kw_only=True)
class ConcurrentGroupBranchResult(CanonicalContract):
    SCHEMA_VERSION: ClassVar[str] = SCHEMA_VERSION

    group_id: str
    status: ConcurrentBranchStatus
    proposal: GroupProposalPayload | None
    submission: ResearchGroupSubmission | None
    failure_code: ConcurrentBranchFailureCode | None = None
    failure_type: str = ""
    artifact_state: str = "DRAFT"

    def __post_init__(self) -> None:
        require_identifier(self.group_id, "group_id")
        if self.artifact_state != "DRAFT":
            fail(FailureCode.UNAUTHORIZED_CAPABILITY, "research group output must remain DRAFT")
        if self.status is ConcurrentBranchStatus.COMPLETED:
            if self.proposal is None or self.submission is None or self.failure_code is not None:
                fail(FailureCode.INVALID_STATE, "completed branch requires DRAFT proposal and submission")
        else:
            if self.proposal is not None or self.submission is not None or self.failure_code is None:
                fail(FailureCode.INVALID_STATE, "failed/timeout branch cannot carry a proposal")


@dataclass(frozen=True, kw_only=True)
class CommittedGroupBranch(CanonicalContract):
    SCHEMA_VERSION: ClassVar[str] = SCHEMA_VERSION

    group_id: str
    campaign_hash: str
    intent_hash: str
    context_hash: str
    assignment_hash: str
    branch: ConcurrentGroupBranchResult

    def __post_init__(self) -> None:
        require_identifier(self.group_id, "group_id")
        for name in ("campaign_hash", "intent_hash", "context_hash", "assignment_hash"):
            require_hash(getattr(self, name), name)
        if not isinstance(self.branch, ConcurrentGroupBranchResult) or self.branch.group_id != self.group_id:
            fail(FailureCode.INVALID_STATE, "committed branch identity mismatch")


@dataclass(frozen=True, kw_only=True)
class ConcurrentGroupRuntimeResult(CanonicalContract):
    SCHEMA_VERSION: ClassVar[str] = SCHEMA_VERSION

    campaign_hash: str
    branches: tuple[ConcurrentGroupBranchResult, ...]
    committed_branches: tuple[CommittedGroupBranch, ...]

    def __post_init__(self) -> None:
        require_hash(self.campaign_hash, "campaign_hash")
        ids = tuple(item.group_id for item in self.branches)
        committed_ids = tuple(item.group_id for item in self.committed_branches)
        if not ids or ids != tuple(sorted(ids)) or ids != committed_ids:
            fail(FailureCode.INVALID_STATE, "runtime branches must be unique, stable-sorted, and committed")
        if len(ids) != len(set(ids)) or not 1 <= len(ids) <= 6:
            fail(FailureCode.INVALID_VALUE, "runtime requires one to six selected groups")
