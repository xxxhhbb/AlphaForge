"""Mechanical milestone-A gate for six independently executed specialists."""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import ClassVar, Mapping

from ..contracts import CanonicalContract, FailureCode
from ..contracts.base import fail, freeze_mapping, require_hash, require_identifier
from .models import SCHEMA_VERSION, SpecialistAgentOutcome, SpecialistStage
from .profiles import SPECIALIST_PROFILES


@dataclass(frozen=True)
class SpecialistAcceptanceResult(CanonicalContract):
    specialist_id: str
    passed: bool
    outcome_hash: str
    plan_hash: str | None
    candidate_count: int
    family_count: int
    preflight_count: int
    failure_codes: tuple[str, ...]
    SCHEMA_VERSION: ClassVar[str] = SCHEMA_VERSION

    def __post_init__(self) -> None:
        require_identifier(self.specialist_id, "specialist_id")
        require_hash(self.outcome_hash, "outcome_hash")
        if self.plan_hash is not None:
            require_hash(self.plan_hash, "plan_hash")
        if not isinstance(self.passed, bool):
            fail(FailureCode.INVALID_VALUE, "passed must be boolean")
        for name in ("candidate_count", "family_count", "preflight_count"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                fail(FailureCode.INVALID_VALUE, f"{name} must be non-negative")


@dataclass(frozen=True)
class MilestoneAAcceptanceReport(CanonicalContract):
    passed: bool
    results: Mapping[str, SpecialistAcceptanceResult]
    SCHEMA_VERSION: ClassVar[str] = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if not isinstance(self.passed, bool):
            fail(FailureCode.INVALID_VALUE, "passed must be boolean")
        object.__setattr__(self, "results", freeze_mapping(self.results, "results"))
        if set(self.results) != set(SPECIALIST_PROFILES):
            fail(FailureCode.INCOMPLETE_RESULT, "acceptance report must cover all six specialists")
        if self.passed != all(item.passed for item in self.results.values()):
            fail(FailureCode.INVALID_STATE, "aggregate milestone verdict is inconsistent")


class MilestoneAAcceptanceGate:
    """Evaluate artifacts only; it does not run models or financial truth services."""

    def evaluate(self, outcomes: Mapping[str, SpecialistAgentOutcome]) -> MilestoneAAcceptanceReport:
        expected = set(SPECIALIST_PROFILES)
        if set(outcomes) != expected:
            fail(
                FailureCode.INCOMPLETE_RESULT,
                "milestone A requires one independent outcome for every specialist",
                missing=tuple(sorted(expected - set(outcomes))),
                unknown=tuple(sorted(set(outcomes) - expected)),
            )
        results: dict[str, SpecialistAcceptanceResult] = {}
        for specialist_id in sorted(expected):
            outcome = outcomes[specialist_id]
            if outcome.specialist_id != specialist_id:
                fail(FailureCode.INVALID_FIELD_OWNER, "specialist outcome is stored under the wrong key")
            plan = outcome.plan
            families = {item.family_id for item in outcome.candidates}
            preflight_ids = {item.candidate_id for item in outcome.preflight_results}
            candidate_ids = {item.candidate_id for item in outcome.candidates}
            failures = tuple(
                sorted(
                    {
                        *(item.failure_code for item in outcome.preflight_results if item.failure_code),
                        *(() if outcome.failure_code is None else (outcome.failure_code,)),
                    }
                )
            )
            passed = bool(
                outcome.status is SpecialistStage.COMPLETED
                and plan is not None
                and len(plan.directions) >= 2
                and len(families) >= 2
                and candidate_ids
                and candidate_ids <= preflight_ids
                and all(item.failure_code or item.status.value == "ACCEPTED" for item in outcome.preflight_results)
            )
            results[specialist_id] = SpecialistAcceptanceResult(
                specialist_id=specialist_id,
                passed=passed,
                outcome_hash=outcome.artifact_hash,
                plan_hash=None if plan is None else plan.artifact_hash,
                candidate_count=len(outcome.candidates),
                family_count=len(families),
                preflight_count=len(outcome.preflight_results),
                failure_codes=failures,
            )
        frozen = MappingProxyType(results)
        return MilestoneAAcceptanceReport(
            passed=all(item.passed for item in frozen.values()),
            results=frozen,
        )
