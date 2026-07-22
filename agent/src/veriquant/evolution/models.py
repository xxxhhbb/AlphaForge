"""Typed, visible-only contracts for bounded DRAFT evolution."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

from ..contracts import (
    CanonicalContract,
    ContractValidationError,
    EvolutionLineage,
    FailureCode,
)
from ..contracts.base import (
    require_finite_number,
    require_hash,
    require_identifier,
)

if TYPE_CHECKING:
    from ..adapters import DraftProposal


_SENSITIVE_TOKEN = re.compile(
    r"(^|[._:/-])(sealed|test|holdout|promotion)([._:/-]|$)",
    re.IGNORECASE,
)


def reject_sensitive_identifier(value: str, field_name: str) -> None:
    require_identifier(value, field_name)
    if _SENSITIVE_TOKEN.search(value):
        raise ContractValidationError(
            FailureCode.EVOLUTION_CONTEXT_REJECTED,
            f"{field_name} exposes a forbidden research token",
            details={"field": field_name},
        )


class EvolutionOperator(str, Enum):
    ORIGINAL = "ORIGINAL"
    MUTATION = "MUTATION"
    CROSSOVER = "CROSSOVER"

    @property
    def parent_count(self) -> int:
        return {
            EvolutionOperator.ORIGINAL: 0,
            EvolutionOperator.MUTATION: 1,
            EvolutionOperator.CROSSOVER: 2,
        }[self]


class GateOutcome(str, Enum):
    READY_DRAFT = "READY_DRAFT"
    PIT_REJECTION = "PIT_REJECTION"
    EVALUATION_REJECTION = "EVALUATION_REJECTION"


@dataclass(frozen=True, kw_only=True)
class EvolutionLimits(CanonicalContract):
    """Small offline defaults; a revision is a new budgeted child."""

    max_generations: int = 2
    max_children: int = 4
    family_budget: int = 4
    max_revisions: int = 1

    def __post_init__(self) -> None:
        for name in ("max_generations", "max_children", "family_budget"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ContractValidationError(
                    FailureCode.INVALID_VALUE,
                    f"{name} must be a positive integer",
                )
        if (
            isinstance(self.max_revisions, bool)
            or not isinstance(self.max_revisions, int)
            or self.max_revisions < 0
        ):
            raise ContractValidationError(
                FailureCode.INVALID_VALUE,
                "max_revisions must be a non-negative integer",
            )
        if self.family_budget > self.max_children:
            raise ContractValidationError(
                FailureCode.INVALID_VALUE,
                "family_budget cannot exceed max_children",
            )


@dataclass(frozen=True, kw_only=True)
class FrozenIdentityInputs(CanonicalContract):
    dataset_hash: str
    universe_hash: str
    split_hash: str
    label_hash: str
    cost_hash: str
    code_hash: str
    availability_hash: str

    def __post_init__(self) -> None:
        for name in (
            "dataset_hash",
            "universe_hash",
            "split_hash",
            "label_hash",
            "cost_hash",
            "code_hash",
            "availability_hash",
        ):
            require_hash(getattr(self, name), name)


@dataclass(frozen=True, kw_only=True)
class VisibleParentEvidence(CanonicalContract):
    """Derived train/validation evidence. RankIC is diagnostic, never a score input."""

    candidate_id: str
    candidate_hash: str
    evidence_hash: str
    net_icir: float
    stability: float
    cost: float
    novelty: float
    incrementality: float
    rank_ic_diagnostic: float
    source_partitions: tuple[str, ...] = ("TRAIN", "VALIDATION")

    def __post_init__(self) -> None:
        reject_sensitive_identifier(self.candidate_id, "candidate_id")
        require_hash(self.candidate_hash, "candidate_hash")
        require_hash(self.evidence_hash, "evidence_hash")
        for name in (
            "net_icir",
            "stability",
            "cost",
            "novelty",
            "incrementality",
            "rank_ic_diagnostic",
        ):
            require_finite_number(getattr(self, name), name)
        if self.source_partitions != ("TRAIN", "VALIDATION"):
            raise ContractValidationError(
                FailureCode.EVOLUTION_CONTEXT_REJECTED,
                "parent evidence must contain only TRAIN and VALIDATION partitions",
            )
        if not 0.0 <= self.stability <= 1.0:
            raise ContractValidationError(FailureCode.INVALID_VALUE, "stability is out of range")
        if self.cost < 0.0:
            raise ContractValidationError(FailureCode.INVALID_VALUE, "cost cannot be negative")
        if not 0.0 <= self.novelty <= 1.0:
            raise ContractValidationError(FailureCode.INVALID_VALUE, "novelty is out of range")


@dataclass(frozen=True, kw_only=True)
class ParentScore(CanonicalContract):
    candidate_id: str
    candidate_hash: str
    evidence_hash: str
    net_icir: float
    stability: float
    cost: float
    novelty: float
    incrementality: float
    composite: float

    def __post_init__(self) -> None:
        reject_sensitive_identifier(self.candidate_id, "candidate_id")
        require_hash(self.candidate_hash, "candidate_hash")
        require_hash(self.evidence_hash, "evidence_hash")
        for name in (
            "net_icir",
            "stability",
            "cost",
            "novelty",
            "incrementality",
            "composite",
        ):
            require_finite_number(getattr(self, name), name)


@dataclass(frozen=True, kw_only=True)
class EvolutionPromptContext(CanonicalContract):
    """The complete provider context; no raw evidence or arbitrary text is accepted."""

    operator: EvolutionOperator
    generation: int
    revision: int
    child_index: int
    parent_scores: tuple[ParentScore, ...]
    deterministic_seed: int

    def __post_init__(self) -> None:
        if not isinstance(self.operator, EvolutionOperator):
            raise ContractValidationError(FailureCode.INVALID_VALUE, "invalid evolution operator")
        for name in ("generation", "revision", "child_index", "deterministic_seed"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ContractValidationError(
                    FailureCode.INVALID_VALUE,
                    f"{name} must be a non-negative integer",
                )
        if len(self.parent_scores) != self.operator.parent_count:
            raise ContractValidationError(
                FailureCode.EVOLUTION_CONTEXT_REJECTED,
                "operator received the wrong number of visible parents",
            )
        if len({score.candidate_id for score in self.parent_scores}) != len(
            self.parent_scores
        ):
            raise ContractValidationError(
                FailureCode.EVOLUTION_CONTEXT_REJECTED,
                "parent ids must be unique",
            )

    @property
    def prompt_hash(self) -> str:
        return self.artifact_hash

    @property
    def parent_ids(self) -> tuple[str, ...]:
        return tuple(score.candidate_id for score in self.parent_scores)

    def prompt_text(self) -> str:
        """Canonical mechanical context, suitable for a provider prompt."""

        return self.canonical_json()


@dataclass(frozen=True, kw_only=True)
class EvolutionRequest(CanonicalContract):
    child_id: str
    family_id: str
    context: EvolutionPromptContext

    def __post_init__(self) -> None:
        reject_sensitive_identifier(self.child_id, "child_id")
        reject_sensitive_identifier(self.family_id, "family_id")
        if not isinstance(self.context, EvolutionPromptContext):
            raise ContractValidationError(
                FailureCode.EVOLUTION_CONTEXT_REJECTED,
                "request context must be typed",
            )


@dataclass(frozen=True, kw_only=True)
class GateResult(CanonicalContract):
    outcome: GateOutcome
    failure_code: FailureCode | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.outcome, GateOutcome):
            raise ContractValidationError(FailureCode.INVALID_VALUE, "invalid gate outcome")
        if (self.outcome is GateOutcome.READY_DRAFT) != (self.failure_code is None):
            raise ContractValidationError(
                FailureCode.INVALID_VALUE,
                "only a ready DRAFT omits a failure code",
            )


@dataclass(frozen=True, kw_only=True)
class EvolutionChildResult:
    child_id: str
    lineage: EvolutionLineage | None
    proposal: DraftProposal | None
    failure_code: FailureCode | None
    resumed: bool = False
    started: bool = True

    @property
    def committed(self) -> bool:
        return self.lineage is not None and self.lineage.outcome == "COMMITTED_DRAFT"


@dataclass(frozen=True, kw_only=True)
class EvolutionBatch:
    operator: EvolutionOperator
    generation: int
    children: tuple[EvolutionChildResult, ...]

    @property
    def committed_count(self) -> int:
        return sum(child.committed for child in self.children)
