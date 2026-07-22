"""Mechanical U2/U3 qualification over all six independent specialists."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import ClassVar, Mapping

from ..contracts import CanonicalContract, FailureCode
from ..contracts.base import fail, freeze_mapping, require_hash, require_identifier
from ..specialist_research import MilestoneAAcceptanceGate, PreflightStatus, SPECIALIST_PROFILES, SpecialistAgentOutcome
from .tools import FrozenResearchContext, ResearchCapabilityStatus


SCHEMA_VERSION = "alphaforge.research-os-v3-qualification/v1"


class SpecialistOperationalStatus(str, Enum):
    QUALIFIED = "QUALIFIED"
    DATA_GAP = "DATA_GAP"
    CAPABILITY_GAP = "CAPABILITY_GAP"
    FAILED = "FAILED"


@dataclass(frozen=True)
class SpecialistQualification(CanonicalContract):
    specialist_id: str
    runtime_qualified: bool
    operational_status: SpecialistOperationalStatus
    outcome_hash: str
    plan_hash: str
    candidate_count: int
    accepted_preflight_count: int
    missing_capabilities: tuple[str, ...]
    failure_codes: tuple[str, ...]
    SCHEMA_VERSION: ClassVar[str] = SCHEMA_VERSION

    def __post_init__(self) -> None:
        require_identifier(self.specialist_id, "specialist_id")
        if not isinstance(self.runtime_qualified, bool):
            fail(FailureCode.INVALID_VALUE, "runtime_qualified must be boolean")
        if not isinstance(self.operational_status, SpecialistOperationalStatus):
            fail(FailureCode.INVALID_VALUE, "operational_status is invalid")
        require_hash(self.outcome_hash, "outcome_hash")
        require_hash(self.plan_hash, "plan_hash")
        for name in ("candidate_count", "accepted_preflight_count"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                fail(FailureCode.INVALID_VALUE, f"{name} must be non-negative")
        for item in (*self.missing_capabilities, *self.failure_codes):
            require_identifier(item, "qualification item")


@dataclass(frozen=True)
class U2U3QualificationReport(CanonicalContract):
    context_hash: str
    runtime_passed: bool
    operationally_available_count: int
    typed_gap_count: int
    results: Mapping[str, SpecialistQualification]
    SCHEMA_VERSION: ClassVar[str] = SCHEMA_VERSION

    def __post_init__(self) -> None:
        require_hash(self.context_hash, "context_hash")
        if set(self.results) != set(SPECIALIST_PROFILES):
            fail(FailureCode.INCOMPLETE_RESULT, "qualification must cover all six specialists")
        object.__setattr__(self, "results", freeze_mapping(self.results, "results"))
        if self.runtime_passed != all(item.runtime_qualified for item in self.results.values()):
            fail(FailureCode.INVALID_STATE, "runtime aggregate is inconsistent")
        available = sum(item.operational_status is SpecialistOperationalStatus.QUALIFIED for item in self.results.values())
        gaps = sum(item.operational_status in {SpecialistOperationalStatus.DATA_GAP, SpecialistOperationalStatus.CAPABILITY_GAP} for item in self.results.values())
        if available != self.operationally_available_count or gaps != self.typed_gap_count:
            fail(FailureCode.INVALID_STATE, "operational aggregate is inconsistent")


class U2U3QualificationGate:
    """Qualify agent runtime separately from current frozen-data availability."""

    def evaluate(self, *, outcomes: Mapping[str, SpecialistAgentOutcome], context: FrozenResearchContext) -> U2U3QualificationReport:
        milestone = MilestoneAAcceptanceGate().evaluate(outcomes)
        results: dict[str, SpecialistQualification] = {}
        for specialist_id in sorted(SPECIALIST_PROFILES):
            outcome = outcomes[specialist_id]
            base = milestone.results[specialist_id]
            accepted = sum(item.status is PreflightStatus.ACCEPTED for item in outcome.preflight_results)
            runtime_ok = bool(base.passed and accepted >= 1 and outcome.plan is not None)
            capability, missing = context.capability(specialist_id)
            if not runtime_ok:
                status = SpecialistOperationalStatus.FAILED
            elif capability is ResearchCapabilityStatus.AVAILABLE:
                status = SpecialistOperationalStatus.QUALIFIED
            elif capability is ResearchCapabilityStatus.DATA_GAP:
                status = SpecialistOperationalStatus.DATA_GAP
            else:
                status = SpecialistOperationalStatus.CAPABILITY_GAP
            results[specialist_id] = SpecialistQualification(
                specialist_id=specialist_id,
                runtime_qualified=runtime_ok,
                operational_status=status,
                outcome_hash=outcome.artifact_hash,
                plan_hash=outcome.plan.artifact_hash if outcome.plan is not None else outcome.artifact_hash,
                candidate_count=len(outcome.candidates),
                accepted_preflight_count=accepted,
                missing_capabilities=missing,
                failure_codes=base.failure_codes,
            )
        frozen = MappingProxyType(results)
        return U2U3QualificationReport(
            context_hash=context.artifact_hash,
            runtime_passed=all(item.runtime_qualified for item in frozen.values()),
            operationally_available_count=sum(item.operational_status is SpecialistOperationalStatus.QUALIFIED for item in frozen.values()),
            typed_gap_count=sum(item.operational_status in {SpecialistOperationalStatus.DATA_GAP, SpecialistOperationalStatus.CAPABILITY_GAP} for item in frozen.values()),
            results=frozen,
        )


__all__ = ["SpecialistOperationalStatus", "SpecialistQualification", "U2U3QualificationGate", "U2U3QualificationReport"]
