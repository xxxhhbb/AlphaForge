"""Bounded safe combination validation and cross-specialist deduplication."""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import ClassVar, Mapping

from ..contracts import CanonicalContract, FailureCode
from ..contracts.base import fail, require_hash, require_identifier, require_text
from ..llm_desks.safety import canonical_hash
from .models import CandidateCombination, SCHEMA_VERSION, SpecialistCandidateDraft, SpecialistResearchRequest
from .preflight import SpecialistFactorPreflight


@dataclass(frozen=True)
class CanonicalCandidateIdentity(CanonicalContract):
    dataset_hash: str
    universe_hash: str
    horizon_sessions: int
    factor_dag_hash: str
    neutralization: tuple[str, ...]
    cost_policy_hash: str
    SCHEMA_VERSION: ClassVar[str] = SCHEMA_VERSION

    def __post_init__(self) -> None:
        for name in ("dataset_hash", "universe_hash", "factor_dag_hash", "cost_policy_hash"):
            require_hash(getattr(self, name), name)
        if isinstance(self.horizon_sessions, bool) or not isinstance(self.horizon_sessions, int) or self.horizon_sessions <= 0:
            fail(FailureCode.INVALID_VALUE, "horizon_sessions must be positive")
        if len(self.neutralization) != len(set(self.neutralization)):
            fail(FailureCode.INVALID_VALUE, "neutralization must be unique")


class CandidateCanonicalizer:
    def identity(
        self,
        *,
        dataset_hash: str,
        universe_hash: str,
        horizon_sessions: int,
        factor_dag_hash: str,
        neutralization: tuple[str, ...],
        cost_policy_hash: str,
    ) -> CanonicalCandidateIdentity:
        return CanonicalCandidateIdentity(
            dataset_hash=dataset_hash,
            universe_hash=universe_hash,
            horizon_sessions=horizon_sessions,
            factor_dag_hash=factor_dag_hash,
            neutralization=tuple(sorted(neutralization)),
            cost_policy_hash=cost_policy_hash,
        )


@dataclass(frozen=True)
class DuplicateDecision(CanonicalContract):
    identity_hash: str
    first_group_id: str
    first_candidate_id: str
    duplicate_group_id: str
    duplicate_candidate_id: str
    consume_trial: bool
    SCHEMA_VERSION: ClassVar[str] = SCHEMA_VERSION

    def __post_init__(self) -> None:
        require_hash(self.identity_hash, "identity_hash")
        for name in ("first_group_id", "first_candidate_id", "duplicate_group_id", "duplicate_candidate_id"):
            require_identifier(getattr(self, name), name)


class CrossGroupDuplicateDetector:
    """Track canonical identities without owning Trial Ledger state."""

    def __init__(self) -> None:
        self._seen: dict[str, tuple[str, str, bool]] = {}

    def register(
        self,
        *,
        group_id: str,
        candidate_id: str,
        identity_hash: str,
        already_submitted: bool,
    ) -> DuplicateDecision | None:
        require_identifier(group_id, "group_id")
        require_identifier(candidate_id, "candidate_id")
        require_hash(identity_hash, "identity_hash")
        prior = self._seen.get(identity_hash)
        if prior is None:
            self._seen[identity_hash] = (group_id, candidate_id, already_submitted)
            return None
        first_group, first_candidate, first_submitted = prior
        return DuplicateDecision(
            identity_hash=identity_hash,
            first_group_id=first_group,
            first_candidate_id=first_candidate,
            duplicate_group_id=group_id,
            duplicate_candidate_id=candidate_id,
            consume_trial=bool(already_submitted or first_submitted),
        )

    @property
    def snapshot(self) -> Mapping[str, tuple[str, str, bool]]:
        return MappingProxyType(dict(sorted(self._seen.items())))


class SafeCombinationExplorer:
    """Validate model-planned combinations; never executes or invents Python."""

    def __init__(self, preflight: SpecialistFactorPreflight | None = None) -> None:
        self._preflight = preflight or SpecialistFactorPreflight()

    def expand(
        self,
        *,
        request: SpecialistResearchRequest,
        combinations: tuple[CandidateCombination, ...],
    ) -> tuple[CandidateCombination, ...]:
        if len(combinations) > request.budget.max_combinations:
            fail(FailureCode.PROVIDER_BUDGET_EXCEEDED, "combination budget exhausted")
        seen: set[str] = set()
        accepted: list[CandidateCombination] = []
        for item in sorted(combinations, key=lambda value: (value.family_id, value.combination_id)):
            missing = set(item.required_fields) - set(request.available_fields)
            if missing:
                fail(
                    FailureCode.UNKNOWN_FIELD,
                    "combination requires unavailable fields",
                    fields=tuple(sorted(missing)),
                )
            fingerprint = canonical_hash(
                {
                    "expression": "".join(item.expression.split()),
                    "required_fields": tuple(sorted(item.required_fields)),
                    "horizon": request.label_horizon_sessions,
                }
            )
            if fingerprint not in seen:
                seen.add(fingerprint)
                accepted.append(item)
        return tuple(accepted)

    def preflight(
        self,
        *,
        request: SpecialistResearchRequest,
        candidates: tuple[SpecialistCandidateDraft, ...],
    ):
        if len(candidates) > request.budget.max_combinations:
            fail(FailureCode.PROVIDER_BUDGET_EXCEEDED, "combination budget exhausted")
        return tuple(self._preflight.check(request, candidate) for candidate in candidates)
