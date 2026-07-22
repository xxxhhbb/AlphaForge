"""Stable U4 contracts for Campaign-routed multi-specialist DRAFT research."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import ClassVar

from ..contracts import CanonicalContract, FailureCode
from ..contracts.base import fail, require_hash, require_identifier


SCHEMA_VERSION = "alphaforge.research-os-v3-multi/v1"


class MultiSpecialistBranchStatus(str, Enum):
    COMPLETED_DRAFT = "COMPLETED_DRAFT"
    NEGATIVE_RESULT = "NEGATIVE_RESULT"
    DATA_GAP = "DATA_GAP"
    BUDGET_BLOCKED = "BUDGET_BLOCKED"
    PARTIAL_RESULT = "PARTIAL_RESULT"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class MultiSpecialistRunStatus(str, Enum):
    COMPLETED = "COMPLETED"
    PARTIAL = "PARTIAL"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


@dataclass(frozen=True)
class MultiSpecialistCandidateRef(CanonicalContract):
    specialist_id: str
    candidate_id: str
    candidate_hash: str
    preflight_status: str
    factor_dag_hash: str | None
    failure_code: str | None
    formula_program_hash: str | None = None
    formula_program_kind: str = "LEGACY_FACTOR_DAG"
    formula_manifest_hash: str | None = None
    SCHEMA_VERSION: ClassVar[str] = SCHEMA_VERSION

    def __post_init__(self) -> None:
        require_identifier(self.specialist_id, "specialist_id")
        require_identifier(self.candidate_id, "candidate_id")
        require_hash(self.candidate_hash, "candidate_hash")
        require_identifier(self.preflight_status, "preflight_status")
        if self.factor_dag_hash is not None:
            require_hash(self.factor_dag_hash, "factor_dag_hash")
        if self.formula_program_hash is not None:
            require_hash(self.formula_program_hash, "formula_program_hash")
        if self.formula_program_kind not in {"LEGACY_FACTOR_DAG", "CERTIFIED_QLIB_EXPRESSION"}:
            fail(FailureCode.INVALID_VALUE, "unsupported formula program kind")
        if self.formula_manifest_hash is not None:
            require_hash(self.formula_manifest_hash, "formula_manifest_hash")
        if self.formula_program_kind == "CERTIFIED_QLIB_EXPRESSION":
            if self.factor_dag_hash is not None or self.formula_program_hash is None or self.formula_manifest_hash is None:
                fail(FailureCode.MISSING_REQUIRED_FIELD, "Qlib candidate reference requires program and manifest hashes")
        elif self.formula_program_hash is not None:
            fail(FailureCode.INVALID_STATE, "legacy candidate cannot carry Qlib program hash")
        if self.failure_code is not None:
            require_identifier(self.failure_code, "failure_code")

    def to_dict(self) -> dict[str, object]:
        payload = super().to_dict()
        if self.formula_program_kind == "LEGACY_FACTOR_DAG":
            payload.pop("formula_program_hash", None)
            payload.pop("formula_program_kind", None)
            payload.pop("formula_manifest_hash", None)
        return payload


@dataclass(frozen=True)
class MultiSpecialistBranchResult(CanonicalContract):
    specialist_id: str
    branch_identity_hash: str
    status: MultiSpecialistBranchStatus
    context_hash: str
    outcome_hash: str | None
    candidate_refs: tuple[MultiSpecialistCandidateRef, ...]
    typed_failures: tuple[str, ...]
    missing_capabilities: tuple[str, ...]
    provider_calls: int
    failure_origins: tuple[str, ...] = ()
    authority: str = "DRAFT_ONLY"
    SCHEMA_VERSION: ClassVar[str] = SCHEMA_VERSION

    def __post_init__(self) -> None:
        require_identifier(self.specialist_id, "specialist_id")
        require_hash(self.branch_identity_hash, "branch_identity_hash")
        require_hash(self.context_hash, "context_hash")
        if not isinstance(self.status, MultiSpecialistBranchStatus):
            fail(FailureCode.INVALID_VALUE, "branch status is invalid")
        if self.outcome_hash is not None:
            require_hash(self.outcome_hash, "outcome_hash")
        if tuple(sorted(self.candidate_refs, key=lambda item: (item.specialist_id, item.candidate_id, item.candidate_hash))) != self.candidate_refs:
            fail(FailureCode.INVALID_STATE, "candidate refs must be stable-sorted")
        if any(item.specialist_id != self.specialist_id for item in self.candidate_refs):
            fail(FailureCode.INVALID_FIELD_OWNER, "candidate belongs to another specialist")
        for item in (*self.typed_failures, *self.missing_capabilities):
            require_identifier(item, "branch detail")
        for item in self.failure_origins:
            require_identifier(item, "failure_origins")
        if isinstance(self.provider_calls, bool) or not isinstance(self.provider_calls, int) or self.provider_calls < 0:
            fail(FailureCode.INVALID_VALUE, "provider_calls must be non-negative")
        if self.authority != "DRAFT_ONLY":
            fail(FailureCode.UNAUTHORIZED_CAPABILITY, "multi-specialist branches are DRAFT only")
        if self.status in {MultiSpecialistBranchStatus.DATA_GAP, MultiSpecialistBranchStatus.BUDGET_BLOCKED, MultiSpecialistBranchStatus.CANCELLED}:
            if self.outcome_hash is not None or self.candidate_refs or self.provider_calls:
                fail(FailureCode.INVALID_STATE, "non-started branch cannot carry model output")
        if self.status is MultiSpecialistBranchStatus.COMPLETED_DRAFT and not self.candidate_refs:
            fail(FailureCode.INCOMPLETE_RESULT, "completed DRAFT branch needs candidates")

    def to_dict(self) -> dict[str, object]:
        payload = super().to_dict()
        # Preserve hashes of immutable U4/U5 branch artifacts created before
        # the optional diagnostic field existed.
        if not self.failure_origins:
            payload.pop("failure_origins", None)
        return payload


@dataclass(frozen=True)
class MultiSpecialistRunResult(CanonicalContract):
    run_id: str
    manifest_hash: str
    campaign_hash: str
    route_hash: str
    context_hash: str
    status: MultiSpecialistRunStatus
    branches: tuple[MultiSpecialistBranchResult, ...]
    candidate_refs: tuple[MultiSpecialistCandidateRef, ...]
    committed_branch_hashes: tuple[str, ...]
    candidate_count: int
    accepted_preflight_count: int
    typed_failures: tuple[str, ...]
    authority: str = "DRAFT_ONLY"
    official_evidence_hash: None = None
    promotion_hash: None = None
    SCHEMA_VERSION: ClassVar[str] = SCHEMA_VERSION

    def __post_init__(self) -> None:
        require_identifier(self.run_id, "run_id")
        for name in ("manifest_hash", "campaign_hash", "route_hash", "context_hash"):
            require_hash(getattr(self, name), name)
        if not isinstance(self.status, MultiSpecialistRunStatus):
            fail(FailureCode.INVALID_VALUE, "run status is invalid")
        ids = tuple(item.specialist_id for item in self.branches)
        if not ids or ids != tuple(sorted(ids)) or len(ids) != len(set(ids)) or len(ids) > 6:
            fail(FailureCode.INVALID_STATE, "branches must be one-to-six unique stable-sorted specialists")
        expected_refs = tuple(sorted((item for branch in self.branches for item in branch.candidate_refs), key=lambda item: (item.specialist_id, item.candidate_id, item.candidate_hash)))
        if self.candidate_refs != expected_refs or self.candidate_count != len(expected_refs):
            fail(FailureCode.INVALID_STATE, "aggregate candidates are inconsistent")
        if self.committed_branch_hashes != tuple(branch.artifact_hash for branch in self.branches):
            fail(FailureCode.INVALID_STATE, "committed branch hashes are inconsistent")
        accepted = sum(item.preflight_status == "ACCEPTED" for item in expected_refs)
        if self.accepted_preflight_count != accepted:
            fail(FailureCode.INVALID_STATE, "accepted preflight count is inconsistent")
        for item in self.typed_failures:
            require_identifier(item, "typed_failures")
        if self.authority != "DRAFT_ONLY" or self.official_evidence_hash is not None or self.promotion_hash is not None:
            fail(FailureCode.UNAUTHORIZED_CAPABILITY, "U4 has no Evidence or Promotion authority")


__all__ = [
    "MultiSpecialistBranchResult",
    "MultiSpecialistBranchStatus",
    "MultiSpecialistCandidateRef",
    "MultiSpecialistRunResult",
    "MultiSpecialistRunStatus",
]
