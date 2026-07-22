"""Product-level research intent contracts with explicit time semantics."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import ClassVar

from ..contracts import CanonicalContract, FailureCode
from ..contracts.base import fail, require_aware_datetime, require_identifier, require_text
from ..research_campaign.models import ResearchDepth, ResearchMode
from ..research_os.models import ResearchIntent


SCHEMA_VERSION = "alphaforge.research-intent/v2"


class IntentResolution(str, Enum):
    COMPLETE_DRAFT = "COMPLETE_DRAFT"
    NEEDS_CLARIFICATION = "NEEDS_CLARIFICATION"
    UNSUPPORTED_SAFELY = "UNSUPPORTED_SAFELY"


def _texts(values: tuple[str, ...], name: str, *, required: bool = False) -> None:
    if not isinstance(values, tuple) or (required and not values):
        fail(FailureCode.INVALID_VALUE, f"{name} must be a tuple")
    for value in values:
        require_text(value, name)
    if len(values) != len(set(values)):
        fail(FailureCode.INVALID_VALUE, f"{name} contains duplicates")


def _sessions(values: tuple[int, ...], name: str) -> None:
    if not isinstance(values, tuple):
        fail(FailureCode.INVALID_VALUE, f"{name} must be a tuple")
    if any(isinstance(value, bool) or not isinstance(value, int) or value < 1 for value in values):
        fail(FailureCode.INVALID_VALUE, f"{name} must contain positive sessions")
    if tuple(sorted(set(values))) != values:
        fail(FailureCode.INVALID_VALUE, f"{name} must be unique and sorted")


@dataclass(frozen=True, kw_only=True)
class ResearchIntentV2(CanonicalContract):
    question: str
    mode: ResearchMode
    research_subject: str
    factor_lookbacks: tuple[int, ...]
    forecast_horizons: tuple[int, ...]
    universe: tuple[str, ...]
    decision_time: datetime
    target_variable: str
    required_data: tuple[str, ...]
    optional_data: tuple[str, ...]
    existing_factor_ids: tuple[str, ...]
    portfolio_id: str | None
    neutralization_requirements: tuple[str, ...]
    cost_capacity_requirements: tuple[str, ...]
    depth: ResearchDepth
    assumptions: tuple[str, ...]
    clarification_questions: tuple[str, ...]
    resolution: IntentResolution
    confirmed: bool = False
    authority: str = "DRAFT_ONLY"
    schema_version: ClassVar[str] = SCHEMA_VERSION

    def __post_init__(self) -> None:
        require_text(self.question, "question")
        if not isinstance(self.mode, ResearchMode):
            fail(FailureCode.INVALID_VALUE, "mode must be ResearchMode")
        require_text(self.research_subject, "research_subject")
        _sessions(self.factor_lookbacks, "factor_lookbacks")
        _sessions(self.forecast_horizons, "forecast_horizons")
        _texts(self.universe, "universe", required=True)
        require_aware_datetime(self.decision_time, "decision_time")
        require_identifier(self.target_variable, "target_variable")
        for name in (
            "required_data", "optional_data", "existing_factor_ids",
            "neutralization_requirements", "cost_capacity_requirements",
            "assumptions", "clarification_questions",
        ):
            _texts(getattr(self, name), name)
        if set(self.required_data) & set(self.optional_data):
            fail(FailureCode.INVALID_VALUE, "required and optional data overlap")
        if self.portfolio_id is not None:
            require_identifier(self.portfolio_id, "portfolio_id")
        if not isinstance(self.depth, ResearchDepth):
            fail(FailureCode.INVALID_VALUE, "depth must be ResearchDepth")
        if not isinstance(self.resolution, IntentResolution):
            fail(FailureCode.INVALID_VALUE, "resolution must be IntentResolution")
        if self.resolution is IntentResolution.COMPLETE_DRAFT and self.clarification_questions:
            fail(FailureCode.INVALID_STATE, "complete intent cannot retain clarifications")
        if self.resolution is IntentResolution.NEEDS_CLARIFICATION and not self.clarification_questions:
            fail(FailureCode.MISSING_REQUIRED_FIELD, "ambiguous intent needs clarification")
        if self.confirmed:
            fail(FailureCode.UNAUTHORIZED_CAPABILITY, "planner cannot confirm ResearchIntent")
        if self.authority != "DRAFT_ONLY":
            fail(FailureCode.UNAUTHORIZED_CAPABILITY, "planner has no Evidence or Promotion authority")

    @property
    def execution_allowed(self) -> bool:
        return False

    def to_legacy(self) -> ResearchIntent:
        """Compatibility projection for the existing deterministic data planner.

        Suggested horizons remain explicitly unconfirmed in V2.  The V1 view is
        planning-only and cannot make the Campaign executable.
        """

        return ResearchIntent(
            question=self.question,
            mode=self.mode,
            depth=self.depth,
            factor_lookback_sessions=self.factor_lookbacks[0] if self.factor_lookbacks else None,
            label_horizon_sessions=self.forecast_horizons[0] if self.forecast_horizons else 5,
            requested_outputs=_outputs(self.mode),
        )


def _outputs(mode: ResearchMode) -> tuple[str, ...]:
    mapping = {
        ResearchMode.NEW_ALPHA_EXPLORATION: ("candidate_set", "formal_report"),
        ResearchMode.VALIDATE_IDEA: ("validation_conclusion", "formal_report"),
        ResearchMode.IMPROVE_EXISTING_FACTOR: ("factor_comparison_report", "formal_report"),
        ResearchMode.PORTFOLIO_GAP_SEARCH: ("portfolio_gap_report", "formal_report"),
        ResearchMode.DIAGNOSE_FACTOR_PORTFOLIO: ("diagnostic_report", "formal_report"),
        ResearchMode.EVENT_MARKET_TOPIC: ("event_study_report", "formal_report"),
        ResearchMode.NEW_DATA_SOURCE_EVALUATION: ("dataset_assessment", "formal_report"),
        ResearchMode.PAPER_STRATEGY_REPLICATION: ("replication_report", "formal_report"),
        ResearchMode.PRELAUNCH_REVIEW: ("prelaunch_review", "formal_report"),
        ResearchMode.MONITOR_DECAY_RETIREMENT: ("monitoring_recommendation", "formal_report"),
    }
    return mapping.get(mode, ("formal_report",))


__all__ = ["IntentResolution", "ResearchIntentV2", "SCHEMA_VERSION"]
