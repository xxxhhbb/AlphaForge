"""Capability boundary for sealed-test rows and redacted research channels."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Mapping, Sequence

from ..contracts import ContractValidationError, FailureCode
from .models import (
    CostModel,
    EvaluationRow,
    ExperimentIdentity,
    RegisteredFactor,
    ResearchSummary,
)
from .service import EvidenceService


class SealedActor(str, Enum):
    RESEARCHER = "RESEARCHER"
    EVOLUTION = "EVOLUTION"
    FEEDBACK = "FEEDBACK"
    PARENT_SELECTION = "PARENT_SELECTION"
    PROMOTION_CONTROLLER = "PROMOTION_CONTROLLER"


class ResearchDestination(str, Enum):
    TRACE = "TRACE"
    TRAJECTORY = "TRAJECTORY"
    LLM_CONTEXT = "LLM_CONTEXT"
    PARENT_SCORE = "PARENT_SCORE"
    NEXT_PROPOSAL = "NEXT_PROPOSAL"


@dataclass(frozen=True, kw_only=True)
class SealedVerdict:
    passed: bool


class SealedTestStore:
    """Rows are readable only through the Promotion Controller actor."""

    def __init__(self, folds: Mapping[str, Sequence[EvaluationRow]]) -> None:
        self.__folds = {key: tuple(value) for key, value in folds.items()}
        if not self.__folds or any(not rows for rows in self.__folds.values()):
            raise ValueError("sealed folds must be non-empty")

    def read(self, fold_id: str, *, actor: SealedActor) -> tuple[EvaluationRow, ...]:
        if actor is not SealedActor.PROMOTION_CONTROLLER:
            raise ContractValidationError(
                FailureCode.UNAUTHORIZED_CAPABILITY,
                f"{actor.value} cannot read sealed-test rows",
            )
        try:
            return self.__folds[fold_id]
        except KeyError as exc:
            raise ContractValidationError(
                FailureCode.INVALID_IDENTIFIER,
                "unknown sealed fold",
            ) from exc


class ResearchChannel:
    """Only an allowlisted train/validation summary can reach research surfaces."""

    def publish(
        self, summary: ResearchSummary, *, destination: ResearchDestination
    ) -> dict[str, object]:
        if not isinstance(summary, ResearchSummary):
            raise ContractValidationError(
                FailureCode.UNAUTHORIZED_CAPABILITY,
                "research channels accept only redacted ResearchSummary",
            )
        if not isinstance(destination, ResearchDestination):
            raise ContractValidationError(
                FailureCode.INVALID_VALUE,
                "unknown research destination",
            )
        return {
            "destination": destination.value,
            "summary": summary.to_dict(),
        }


class PromotionController:
    """The only component that asks the sealed store for rows."""

    def __init__(self, service: EvidenceService, store: SealedTestStore) -> None:
        self.__service = service
        self.__store = store

    def evaluate_sealed(
        self,
        *,
        fold_id: str,
        identity: ExperimentIdentity,
        cost_model: CostModel,
        registered_factors: Sequence[RegisteredFactor] = (),
        min_net_return: float = 0.0,
        min_rank_ic: float = 0.0,
    ) -> SealedVerdict:
        rows = self.__store.read(fold_id, actor=SealedActor.PROMOTION_CONTROLLER)
        evidence = self.__service.evaluate(
            identity=identity,
            rows=rows,
            partition="SEALED",
            cost_model=cost_model,
            registered_factors=registered_factors,
        )
        return SealedVerdict(
            passed=(
                evidence.metrics.net_return >= min_net_return
                and evidence.metrics.rank_ic >= min_rank_ic
            )
        )

