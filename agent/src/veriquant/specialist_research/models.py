"""Versioned contracts for one bounded AlphaForge specialist researcher.

The contracts deliberately separate broad LLM research from deterministic
FactorDAG preflight and all later evidence/promotion authorities.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from types import MappingProxyType
from typing import Any, ClassVar, Mapping

from ..contracts import CanonicalContract, FailureCode
from ..contracts.base import (
    fail,
    freeze_mapping,
    require_aware_datetime,
    require_hash,
    require_identifier,
    require_text,
)


SCHEMA_VERSION = "alphaforge.specialist-research/v1"
_EXECUTABLE = re.compile(
    r"(?i)(?:```|\b(?:import|exec|eval|lambda|open|compile|subprocess)\b|"
    r"\bos\.|\bsys\.|factor\.py|__|;)",
)
_SEALED = re.compile(r"(?i)\b(?:sealed|holdout|test[_ -]?rank|test[_ -]?metric)\b")


def _unique_texts(values: tuple[str, ...], name: str, *, required: bool = True) -> None:
    if not isinstance(values, tuple) or (required and not values):
        fail(FailureCode.MISSING_REQUIRED_FIELD, f"{name} must be a non-empty tuple")
    if len(values) != len(set(values)):
        fail(FailureCode.INVALID_VALUE, f"{name} must be unique")
    for value in values:
        require_text(value, name)


class SpecialistStage(str, Enum):
    CREATED = "CREATED"
    SCOPING = "SCOPING"
    PLANNING = "PLANNING"
    INVESTIGATING = "INVESTIGATING"
    ENUMERATING = "ENUMERATING"
    GENERATING = "GENERATING"
    SELF_CRITIQUING = "SELF_CRITIQUING"
    PREFLIGHT = "PREFLIGHT"
    SUBMITTING = "SUBMITTING"
    READY_TO_SUBMIT = "READY_TO_SUBMIT"
    AWAITING_EVIDENCE = "AWAITING_EVIDENCE"
    AWAITING_FEEDBACK = "AWAITING_FEEDBACK"
    REVISING = "REVISING"
    COMPLETED = "COMPLETED"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"


class SpecialistResultKind(str, Enum):
    """What useful research material survived the bounded DRAFT loop.

    This is deliberately not an Evidence or Promotion verdict.
    """

    EVALUATED_RESEARCH_RESULT = "EVALUATED_RESEARCH_RESULT"
    # Legacy value remains readable so existing content-addressed U4/U5
    # artifacts retain their original hash. New outcomes never emit it.
    DRAFT_CANDIDATES = "DRAFT_CANDIDATES"
    PARTIAL_RESEARCH_RESULT = "PARTIAL_RESEARCH_RESULT"
    NEGATIVE_RESEARCH_RESULT = "NEGATIVE_RESEARCH_RESULT"
    DATA_OR_CAPABILITY_GAP_REPORT = "DATA_OR_CAPABILITY_GAP_REPORT"
    SYSTEM_FAILED = "SYSTEM_FAILED"


TERMINAL_STAGES = frozenset(
    {
        SpecialistStage.COMPLETED,
        SpecialistStage.BLOCKED,
        SpecialistStage.FAILED,
        SpecialistStage.CANCELLED,
        SpecialistStage.BUDGET_EXHAUSTED,
    }
)


TRANSITIONS: Mapping[SpecialistStage, frozenset[SpecialistStage]] = MappingProxyType(
    {
        SpecialistStage.CREATED: frozenset({SpecialistStage.SCOPING, SpecialistStage.CANCELLED}),
        SpecialistStage.SCOPING: frozenset({SpecialistStage.PLANNING, SpecialistStage.BLOCKED, SpecialistStage.CANCELLED}),
        SpecialistStage.PLANNING: frozenset({SpecialistStage.INVESTIGATING, SpecialistStage.BLOCKED, SpecialistStage.BUDGET_EXHAUSTED, SpecialistStage.FAILED, SpecialistStage.CANCELLED}),
        SpecialistStage.INVESTIGATING: frozenset({SpecialistStage.ENUMERATING, SpecialistStage.GENERATING, SpecialistStage.BLOCKED, SpecialistStage.BUDGET_EXHAUSTED, SpecialistStage.FAILED, SpecialistStage.CANCELLED}),
        SpecialistStage.ENUMERATING: frozenset({SpecialistStage.GENERATING, SpecialistStage.SELF_CRITIQUING, SpecialistStage.PREFLIGHT, SpecialistStage.BLOCKED, SpecialistStage.BUDGET_EXHAUSTED, SpecialistStage.FAILED, SpecialistStage.CANCELLED}),
        SpecialistStage.GENERATING: frozenset({SpecialistStage.SELF_CRITIQUING, SpecialistStage.BLOCKED, SpecialistStage.BUDGET_EXHAUSTED, SpecialistStage.FAILED, SpecialistStage.CANCELLED}),
        SpecialistStage.SELF_CRITIQUING: frozenset({SpecialistStage.PREFLIGHT, SpecialistStage.BLOCKED, SpecialistStage.BUDGET_EXHAUSTED, SpecialistStage.FAILED, SpecialistStage.CANCELLED}),
        SpecialistStage.PREFLIGHT: frozenset({SpecialistStage.SUBMITTING, SpecialistStage.READY_TO_SUBMIT, SpecialistStage.REVISING, SpecialistStage.BLOCKED, SpecialistStage.BUDGET_EXHAUSTED, SpecialistStage.FAILED, SpecialistStage.CANCELLED}),
        SpecialistStage.SUBMITTING: frozenset({SpecialistStage.AWAITING_EVIDENCE, SpecialistStage.COMPLETED, SpecialistStage.BLOCKED, SpecialistStage.FAILED, SpecialistStage.CANCELLED}),
        SpecialistStage.READY_TO_SUBMIT: frozenset({SpecialistStage.AWAITING_FEEDBACK, SpecialistStage.COMPLETED, SpecialistStage.CANCELLED}),
        SpecialistStage.AWAITING_EVIDENCE: frozenset({SpecialistStage.REVISING, SpecialistStage.COMPLETED, SpecialistStage.BLOCKED, SpecialistStage.FAILED, SpecialistStage.CANCELLED}),
        SpecialistStage.AWAITING_FEEDBACK: frozenset({SpecialistStage.REVISING, SpecialistStage.COMPLETED, SpecialistStage.CANCELLED}),
        SpecialistStage.REVISING: frozenset({SpecialistStage.SELF_CRITIQUING, SpecialistStage.PREFLIGHT, SpecialistStage.BUDGET_EXHAUSTED, SpecialistStage.BLOCKED, SpecialistStage.FAILED, SpecialistStage.CANCELLED}),
        SpecialistStage.COMPLETED: frozenset(),
        SpecialistStage.BLOCKED: frozenset(),
        SpecialistStage.FAILED: frozenset(),
        SpecialistStage.CANCELLED: frozenset(),
        SpecialistStage.BUDGET_EXHAUSTED: frozenset(),
    }
)


def require_transition(current: SpecialistStage, target: SpecialistStage) -> None:
    if target not in TRANSITIONS[current]:
        fail(
            FailureCode.INVALID_STATE_TRANSITION,
            f"illegal specialist transition: {current.value} -> {target.value}",
        )


class TurnKind(str, Enum):
    PLAN = "PLAN"
    TOOL_CALL = "TOOL_CALL"
    CANDIDATES = "CANDIDATES"
    SELF_CRITIQUE = "SELF_CRITIQUE"
    REVISED_CANDIDATES = "REVISED_CANDIDATES"


class PreflightStatus(str, Enum):
    ACCEPTED = "ACCEPTED"
    REVISE = "REVISE"
    CAPABILITY_REQUEST = "CAPABILITY_REQUEST"


class CritiqueAction(str, Enum):
    KEEP = "KEEP"
    REVISE = "REVISE"
    DROP = "DROP"


@dataclass(frozen=True)
class SpecialistResearchBudget(CanonicalContract):
    max_planning_rounds: int = 2
    max_iterations: int = 12
    max_tool_calls: int = 20
    max_tokens: int = 45_000
    max_input_tokens: int = 30_000
    max_output_tokens: int = 15_000
    max_ideas: int = 30
    max_candidate_families: int = 12
    max_combinations: int = 48
    max_submitted_candidates: int = 10
    max_revisions: int = 2
    timeout_seconds: float = 600.0
    family_trial_budget: int = 10
    SCHEMA_VERSION: ClassVar[str] = SCHEMA_VERSION

    def __post_init__(self) -> None:
        limits = {
            "max_planning_rounds": (1, 3),
            "max_iterations": (1, 50),
            "max_tool_calls": (0, 100),
            "max_tokens": (1, 700_000),
            "max_input_tokens": (1, 500_000),
            "max_output_tokens": (1, 200_000),
            "max_ideas": (1, 100),
            "max_candidate_families": (1, 64),
            "max_combinations": (1, 512),
            "max_submitted_candidates": (1, 64),
            "max_revisions": (0, 3),
            "family_trial_budget": (1, 256),
        }
        for name, (low, high) in limits.items():
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
                fail(FailureCode.PROVIDER_BUDGET_EXCEEDED, f"{name} must be in [{low}, {high}]")
        if not 0 < float(self.timeout_seconds) <= 3600:
            fail(FailureCode.PROVIDER_BUDGET_EXCEEDED, "timeout_seconds must be in (0, 3600]")
        if self.max_submitted_candidates > self.max_combinations:
            fail(FailureCode.PROVIDER_BUDGET_EXCEEDED, "submitted candidates cannot exceed combination budget")
        if self.max_submitted_candidates > self.family_trial_budget:
            fail(FailureCode.PROVIDER_BUDGET_EXCEEDED, "submitted candidates cannot exceed family trial budget")

    @classmethod
    def for_depth(cls, depth: str) -> "SpecialistResearchBudget":
        """Return the frozen milestone-A budget envelope for a research depth."""

        normalized = str(depth).strip().upper()
        presets = {
            "QUICK_EXPLORATION": dict(
                max_ideas=8,
                max_candidate_families=2,
                max_combinations=8,
                max_submitted_candidates=4,
                max_revisions=0,
                family_trial_budget=4,
            ),
            "STANDARD_RESEARCH": dict(
                max_ideas=16,
                max_candidate_families=3,
                max_combinations=24,
                max_submitted_candidates=8,
                max_revisions=1,
                family_trial_budget=8,
            ),
            "STRICT_RESEARCH": dict(
                max_ideas=24,
                max_candidate_families=4,
                max_combinations=48,
                max_submitted_candidates=12,
                max_revisions=1,
                family_trial_budget=12,
            ),
        }
        aliases = {
            "QUICK": "QUICK_EXPLORATION",
            "STANDARD": "STANDARD_RESEARCH",
            "STANDARD_VALIDATION": "STANDARD_RESEARCH",
            "STRICT": "STRICT_RESEARCH",
            "STRICT_VALIDATION": "STRICT_RESEARCH",
        }
        normalized = aliases.get(normalized, normalized)
        if normalized not in presets:
            fail(FailureCode.INVALID_VALUE, "unknown specialist research depth", depth=normalized)
        return cls(**presets[normalized])


@dataclass(frozen=True)
class SpecialistResearchRequest(CanonicalContract):
    run_id: str
    campaign_hash: str
    specialist_id: str
    question: str
    universe: str
    label_horizon_sessions: int
    decision_time: datetime
    available_fields: tuple[str, ...]
    budget: SpecialistResearchBudget = field(default_factory=SpecialistResearchBudget)
    research_depth: str = "STANDARD_RESEARCH"
    requested_lookback_sessions: int | None = None
    seed: int = 7
    SCHEMA_VERSION: ClassVar[str] = SCHEMA_VERSION

    def __post_init__(self) -> None:
        require_identifier(self.run_id, "run_id")
        require_hash(self.campaign_hash, "campaign_hash")
        require_identifier(self.specialist_id, "specialist_id")
        require_text(self.question, "question")
        require_identifier(self.universe, "universe")
        if isinstance(self.label_horizon_sessions, bool) or not isinstance(self.label_horizon_sessions, int) or self.label_horizon_sessions <= 0:
            fail(FailureCode.INVALID_VALUE, "label_horizon_sessions must be positive")
        require_aware_datetime(self.decision_time, "decision_time")
        _unique_texts(self.available_fields, "available_fields")
        for value in self.available_fields:
            require_identifier(value, "available_fields")
        if self.research_depth not in {
            "QUICK_EXPLORATION", "STANDARD_RESEARCH", "STANDARD_VALIDATION",
            "STRICT_RESEARCH", "STRICT_VALIDATION",
        }:
            fail(FailureCode.INVALID_VALUE, "research_depth is invalid")
        if self.requested_lookback_sessions is not None and (
            isinstance(self.requested_lookback_sessions, bool)
            or not isinstance(self.requested_lookback_sessions, int)
            or self.requested_lookback_sessions <= 0
        ):
            fail(FailureCode.INVALID_VALUE, "requested_lookback_sessions must be positive")
        if isinstance(self.seed, bool) or not isinstance(self.seed, int) or self.seed < 0:
            fail(FailureCode.INVALID_VALUE, "seed must be a non-negative integer")

    @property
    def official_evidence_allowed(self) -> bool:
        return self.research_depth != "QUICK_EXPLORATION"


@dataclass(frozen=True)
class ResearchDirectionIdea(CanonicalContract):
    direction_id: str
    mechanism: str
    differentiation: str
    required_fields: tuple[str, ...]
    falsification: str
    SCHEMA_VERSION: ClassVar[str] = SCHEMA_VERSION

    def __post_init__(self) -> None:
        require_identifier(self.direction_id, "direction_id")
        require_text(self.mechanism, "mechanism")
        require_text(self.differentiation, "differentiation")
        _unique_texts(self.required_fields, "required_fields", required=False)
        for value in self.required_fields:
            require_identifier(value, "required_fields")
        require_text(self.falsification, "falsification")


@dataclass(frozen=True)
class SpecialistAgentSpec(CanonicalContract):
    """Frozen authority and research surface for one specialist."""

    specialist_id: str
    display_name: str
    mission: str
    candidate_families: tuple[str, ...]
    allowed_tools: tuple[str, ...]
    protocol_rules: tuple[str, ...]
    SCHEMA_VERSION: ClassVar[str] = SCHEMA_VERSION

    def __post_init__(self) -> None:
        require_identifier(self.specialist_id, "specialist_id")
        require_text(self.display_name, "display_name")
        require_text(self.mission, "mission")
        for name in ("candidate_families", "allowed_tools", "protocol_rules"):
            _unique_texts(getattr(self, name), name)


@dataclass(frozen=True)
class ResearchMechanismIdea(CanonicalContract):
    mechanism_id: str
    family_id: str
    mechanism: str
    observable_proxy: str
    predicted_direction: str
    required_fields: tuple[str, ...]
    falsification: str
    SCHEMA_VERSION: ClassVar[str] = SCHEMA_VERSION

    def __post_init__(self) -> None:
        require_identifier(self.mechanism_id, "mechanism_id")
        require_identifier(self.family_id, "family_id")
        for name in ("mechanism", "observable_proxy", "predicted_direction", "falsification"):
            require_text(getattr(self, name), name)
        _unique_texts(self.required_fields, "required_fields", required=False)
        for value in self.required_fields:
            require_identifier(value, "required_fields")


@dataclass(frozen=True)
class CombinationAxis(CanonicalContract):
    axis_id: str
    values: tuple[str, ...]
    rationale: str
    SCHEMA_VERSION: ClassVar[str] = SCHEMA_VERSION

    def __post_init__(self) -> None:
        require_identifier(self.axis_id, "axis_id")
        _unique_texts(self.values, "values")
        require_text(self.rationale, "rationale")


@dataclass(frozen=True)
class CandidateFamilyPlan(CanonicalContract):
    family_id: str
    mechanism_ids: tuple[str, ...]
    combination_axes: tuple[CombinationAxis, ...]
    max_combinations: int
    SCHEMA_VERSION: ClassVar[str] = SCHEMA_VERSION

    def __post_init__(self) -> None:
        require_identifier(self.family_id, "family_id")
        _unique_texts(self.mechanism_ids, "mechanism_ids")
        axis_ids = tuple(item.axis_id for item in self.combination_axes)
        if len(axis_ids) != len(set(axis_ids)):
            fail(FailureCode.INVALID_VALUE, "combination axes must be unique")
        if isinstance(self.max_combinations, bool) or not isinstance(self.max_combinations, int) or self.max_combinations <= 0:
            fail(FailureCode.INVALID_VALUE, "max_combinations must be positive")
        if not axis_ids and self.max_combinations != 1:
            fail(
                FailureCode.INVALID_VALUE,
                "an atomic family without combination axes must set max_combinations=1",
            )


@dataclass(frozen=True)
class InvestigationRequest(CanonicalContract):
    request_id: str
    tool_name: str
    arguments: Mapping[str, Any]
    research_question: str
    SCHEMA_VERSION: ClassVar[str] = SCHEMA_VERSION

    def __post_init__(self) -> None:
        require_identifier(self.request_id, "request_id")
        require_identifier(self.tool_name, "tool_name")
        require_text(self.research_question, "research_question")
        object.__setattr__(self, "arguments", freeze_mapping(self.arguments, "arguments"))


@dataclass(frozen=True)
class CandidateCombination(CanonicalContract):
    combination_id: str
    family_id: str
    axis_values: Mapping[str, str]
    expression: str
    required_fields: tuple[str, ...]
    SCHEMA_VERSION: ClassVar[str] = SCHEMA_VERSION

    def __post_init__(self) -> None:
        require_identifier(self.combination_id, "combination_id")
        require_identifier(self.family_id, "family_id")
        require_text(self.expression, "expression")
        if _EXECUTABLE.search(self.expression):
            fail(FailureCode.FORBIDDEN_PROPOSAL_PAYLOAD, "candidate combination contains executable content")
        object.__setattr__(self, "axis_values", freeze_mapping(self.axis_values, "axis_values"))
        _unique_texts(self.required_fields, "required_fields")
        for value in self.required_fields:
            require_identifier(value, "required_fields")


@dataclass(frozen=True)
class ResearchExplorationPlan(CanonicalContract):
    specialist_id: str
    scope_summary: str
    directions: tuple[ResearchDirectionIdea, ...]
    investigation_questions: tuple[str, ...]
    stop_conditions: tuple[str, ...]
    mechanism_ideas: tuple[ResearchMechanismIdea, ...] = ()
    candidate_family_plans: tuple[CandidateFamilyPlan, ...] = ()
    SCHEMA_VERSION: ClassVar[str] = SCHEMA_VERSION

    def __post_init__(self) -> None:
        require_identifier(self.specialist_id, "specialist_id")
        require_text(self.scope_summary, "scope_summary")
        if len(self.directions) < 2:
            fail(FailureCode.INCOMPLETE_RESULT, "research plan must cover at least two distinct directions")
        ids = tuple(item.direction_id for item in self.directions)
        if len(ids) != len(set(ids)):
            fail(FailureCode.INVALID_VALUE, "research directions must be unique")
        _unique_texts(self.investigation_questions, "investigation_questions")
        _unique_texts(self.stop_conditions, "stop_conditions")
        mechanism_ids = tuple(item.mechanism_id for item in self.mechanism_ideas)
        if len(mechanism_ids) != len(set(mechanism_ids)):
            fail(FailureCode.INVALID_VALUE, "mechanism ideas must be unique")
        family_ids = tuple(item.family_id for item in self.candidate_family_plans)
        if len(family_ids) != len(set(family_ids)):
            fail(FailureCode.INVALID_VALUE, "candidate family plans must be unique")


@dataclass(frozen=True)
class ToolCallDraft(CanonicalContract):
    tool_name: str
    arguments: Mapping[str, Any]
    research_question: str
    SCHEMA_VERSION: ClassVar[str] = SCHEMA_VERSION

    def __post_init__(self) -> None:
        require_identifier(self.tool_name, "tool_name")
        require_text(self.research_question, "research_question")
        object.__setattr__(self, "arguments", freeze_mapping(self.arguments, "arguments"))


@dataclass(frozen=True)
class InvestigationFinding(CanonicalContract):
    tool_name: str
    research_question: str
    arguments_hash: str
    result_hash: str
    result_summary: Mapping[str, Any]
    SCHEMA_VERSION: ClassVar[str] = SCHEMA_VERSION

    def __post_init__(self) -> None:
        require_identifier(self.tool_name, "tool_name")
        require_text(self.research_question, "research_question")
        require_hash(self.arguments_hash, "arguments_hash")
        require_hash(self.result_hash, "result_hash")
        object.__setattr__(self, "result_summary", freeze_mapping(self.result_summary, "result_summary"))


@dataclass(frozen=True)
class SpecialistCandidateDraft(CanonicalContract):
    candidate_id: str
    family_id: str
    hypothesis: str
    economic_mechanism: str
    expected_direction: str
    forecast_horizon_sessions: int
    lookback_sessions: int
    required_fields: tuple[str, ...]
    expression: str
    availability_assumptions: tuple[str, ...]
    neutralization: tuple[str, ...]
    falsification_tests: tuple[str, ...]
    cost_risks: tuple[str, ...]
    exposure_risks: tuple[str, ...]
    novelty_explanation: str
    parent_candidate_id: str | None = None
    revision_number: int = 0
    SCHEMA_VERSION: ClassVar[str] = SCHEMA_VERSION

    def __post_init__(self) -> None:
        require_identifier(self.candidate_id, "candidate_id")
        require_identifier(self.family_id, "family_id")
        for name in ("hypothesis", "economic_mechanism", "expected_direction", "expression", "novelty_explanation"):
            require_text(getattr(self, name), name)
        if _EXECUTABLE.search(self.expression):
            fail(FailureCode.FORBIDDEN_PROPOSAL_PAYLOAD, "candidate expression contains executable content")
        if _SEALED.search(self.canonical_json_without_hash_material()):
            fail(FailureCode.UNAUTHORIZED_CAPABILITY, "candidate references sealed material")
        for name in ("forecast_horizon_sessions", "lookback_sessions"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                fail(FailureCode.INVALID_VALUE, f"{name} must be positive")
        _unique_texts(self.required_fields, "required_fields")
        for value in self.required_fields:
            require_identifier(value, "required_fields")
        _unique_texts(self.availability_assumptions, "availability_assumptions")
        _unique_texts(self.neutralization, "neutralization", required=False)
        _unique_texts(self.falsification_tests, "falsification_tests")
        _unique_texts(self.cost_risks, "cost_risks")
        _unique_texts(self.exposure_risks, "exposure_risks")
        if self.parent_candidate_id is not None:
            require_identifier(self.parent_candidate_id, "parent_candidate_id")
        if isinstance(self.revision_number, bool) or not isinstance(self.revision_number, int) or self.revision_number < 0:
            fail(FailureCode.INVALID_VALUE, "revision_number must be non-negative")
        if self.revision_number == 0 and self.parent_candidate_id is not None:
            fail(FailureCode.INVALID_STATE, "original candidate cannot declare a parent")
        if self.revision_number > 0 and self.parent_candidate_id is None:
            fail(FailureCode.MISSING_REQUIRED_FIELD, "revised candidate requires parent_candidate_id")

    def canonical_json_without_hash_material(self) -> str:
        return " ".join(
            (
                self.hypothesis,
                self.economic_mechanism,
                self.expression,
                self.novelty_explanation,
                *self.availability_assumptions,
                *self.falsification_tests,
                *self.cost_risks,
                *self.exposure_risks,
            )
        )


@dataclass(frozen=True)
class CandidateCritique(CanonicalContract):
    candidate_id: str
    action: CritiqueAction
    weaknesses: tuple[str, ...]
    rationale: str
    SCHEMA_VERSION: ClassVar[str] = SCHEMA_VERSION

    def __post_init__(self) -> None:
        require_identifier(self.candidate_id, "candidate_id")
        if not isinstance(self.action, CritiqueAction):
            fail(FailureCode.INVALID_VALUE, "critique action is invalid")
        _unique_texts(self.weaknesses, "weaknesses")
        require_text(self.rationale, "rationale")


@dataclass(frozen=True)
class FactorLanguageCapabilityRequest(CanonicalContract):
    request_id: str
    request_type: str
    requested_name: str
    economic_reason: str
    deterministic_semantics: str
    lookahead_risk: str
    example_expression: str
    SCHEMA_VERSION: ClassVar[str] = SCHEMA_VERSION

    def __post_init__(self) -> None:
        require_identifier(self.request_id, "request_id")
        require_identifier(self.request_type, "request_type")
        require_identifier(self.requested_name, "requested_name")
        for name in ("economic_reason", "deterministic_semantics", "lookahead_risk", "example_expression"):
            require_text(getattr(self, name), name)
        if _EXECUTABLE.search(self.example_expression):
            fail(FailureCode.FORBIDDEN_PROPOSAL_PAYLOAD, "capability request contains executable content")


@dataclass(frozen=True)
class CandidatePreflightResult(CanonicalContract):
    candidate_id: str
    draft_hash: str
    status: PreflightStatus
    normalized_expression: str | None = None
    factor_dag_hash: str | None = None
    formula_program_hash: str | None = None
    formula_program_kind: str = "LEGACY_FACTOR_DAG"
    formula_manifest_hash: str | None = None
    failure_code: str | None = None
    message: str = ""
    SCHEMA_VERSION: ClassVar[str] = SCHEMA_VERSION

    def __post_init__(self) -> None:
        require_identifier(self.candidate_id, "candidate_id")
        require_hash(self.draft_hash, "draft_hash")
        if not isinstance(self.status, PreflightStatus):
            fail(FailureCode.INVALID_VALUE, "preflight status is invalid")
        if self.status is PreflightStatus.ACCEPTED:
            if not self.normalized_expression or not self.effective_program_hash or self.failure_code is not None:
                fail(FailureCode.INVALID_STATE, "accepted preflight requires a certified formula program and no failure")
            require_hash(self.effective_program_hash, "formula program hash")
            if self.formula_program_kind not in {"LEGACY_FACTOR_DAG", "CERTIFIED_QLIB_EXPRESSION"}:
                fail(FailureCode.INVALID_VALUE, "unsupported formula program kind")
            if self.formula_program_kind == "CERTIFIED_QLIB_EXPRESSION":
                if self.factor_dag_hash is not None or self.formula_program_hash is None:
                    fail(FailureCode.INVALID_STATE, "Qlib program cannot masquerade as FactorDAG")
                if self.formula_manifest_hash is None:
                    fail(FailureCode.MISSING_REQUIRED_FIELD, "Qlib preflight requires operator manifest hash")
                require_hash(self.formula_manifest_hash, "formula_manifest_hash")
            elif self.factor_dag_hash is None or self.formula_program_hash is not None:
                fail(FailureCode.INVALID_STATE, "legacy preflight requires only factor_dag_hash")
        elif not self.failure_code:
            fail(FailureCode.MISSING_REQUIRED_FIELD, "rejected preflight requires failure_code")

    @property
    def effective_program_hash(self) -> str | None:
        """Backend-neutral identity without claiming a Qlib program is a DAG."""

        return self.formula_program_hash or self.factor_dag_hash

    def to_dict(self) -> dict[str, object]:
        payload = super().to_dict()
        if self.formula_program_kind == "LEGACY_FACTOR_DAG":
            payload.pop("formula_program_hash", None)
            payload.pop("formula_program_kind", None)
            payload.pop("formula_manifest_hash", None)
        return payload


@dataclass(frozen=True)
class RedactedMachineFeedback(CanonicalContract):
    candidate_id: str
    compile_status: str
    pit_status: str
    visible_failure_codes: tuple[str, ...]
    cost_warning: str | None
    stability_warning: str | None
    incrementality_warning: str | None
    revision_allowed: bool
    SCHEMA_VERSION: ClassVar[str] = SCHEMA_VERSION

    def __post_init__(self) -> None:
        require_identifier(self.candidate_id, "candidate_id")
        require_identifier(self.compile_status, "compile_status")
        require_identifier(self.pit_status, "pit_status")
        _unique_texts(self.visible_failure_codes, "visible_failure_codes", required=False)
        for value in self.visible_failure_codes:
            require_identifier(value, "visible_failure_codes")
        for name in ("cost_warning", "stability_warning", "incrementality_warning"):
            value = getattr(self, name)
            if value is not None:
                require_text(value, name)
                if _SEALED.search(value):
                    fail(FailureCode.UNAUTHORIZED_CAPABILITY, "feedback contains sealed material")
        if not isinstance(self.revision_allowed, bool):
            fail(FailureCode.INVALID_VALUE, "revision_allowed must be boolean")


@dataclass(frozen=True)
class RedactedRevisionRequest(CanonicalContract):
    request_hash: str
    specialist_id: str
    feedback: tuple[RedactedMachineFeedback, ...]
    revision_number: int
    SCHEMA_VERSION: ClassVar[str] = SCHEMA_VERSION

    def __post_init__(self) -> None:
        require_hash(self.request_hash, "request_hash")
        require_identifier(self.specialist_id, "specialist_id")
        if not self.feedback or len({item.candidate_id for item in self.feedback}) != len(self.feedback):
            fail(FailureCode.INVALID_VALUE, "revision feedback must be unique and non-empty")
        if isinstance(self.revision_number, bool) or not isinstance(self.revision_number, int) or self.revision_number <= 0:
            fail(FailureCode.INVALID_VALUE, "revision_number must be positive")


@dataclass(frozen=True)
class SpecialistDraftIssue(CanonicalContract):
    """Sanitized record for one quarantined provider item."""

    item_kind: str
    item_index: int
    failure_code: str
    message: str
    SCHEMA_VERSION: ClassVar[str] = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.item_kind not in {
            "plan",
            "candidate",
            "candidate_expansion",
            "self_critique",
            "capability_request",
            "tool_call",
        }:
            fail(FailureCode.INVALID_VALUE, "draft issue item_kind is invalid")
        if isinstance(self.item_index, bool) or not isinstance(self.item_index, int) or self.item_index < 0:
            fail(FailureCode.INVALID_VALUE, "draft issue item_index must be non-negative")
        require_identifier(self.failure_code, "failure_code")
        require_text(self.message, "message")
        if _SEALED.search(self.message):
            fail(FailureCode.UNAUTHORIZED_CAPABILITY, "draft issue message contains sealed material")


@dataclass(frozen=True)
class SpecialistTurn(CanonicalContract):
    kind: TurnKind
    plan: ResearchExplorationPlan | None = None
    tool_call: ToolCallDraft | None = None
    candidates: tuple[SpecialistCandidateDraft, ...] = ()
    critiques: tuple[CandidateCritique, ...] = ()
    capability_requests: tuple[FactorLanguageCapabilityRequest, ...] = ()
    draft_issues: tuple[SpecialistDraftIssue, ...] = ()
    input_tokens: int = 0
    output_tokens: int = 0
    SCHEMA_VERSION: ClassVar[str] = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if not isinstance(self.kind, TurnKind):
            fail(FailureCode.INVALID_VALUE, "turn kind is invalid")
        for name in ("input_tokens", "output_tokens"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                fail(FailureCode.INVALID_VALUE, f"{name} must be non-negative")
        populated = {
            "plan": self.plan is not None,
            "tool_call": self.tool_call is not None,
            "candidates": bool(self.candidates),
            "critiques": bool(self.critiques),
        }
        expected = {
            TurnKind.PLAN: "plan",
            TurnKind.TOOL_CALL: "tool_call",
            TurnKind.CANDIDATES: "candidates",
            TurnKind.REVISED_CANDIDATES: "candidates",
            TurnKind.SELF_CRITIQUE: "critiques",
        }[self.kind]
        if not populated[expected] or sum(populated.values()) != 1:
            fail(FailureCode.PROVIDER_SCHEMA_REJECTED, "turn payload does not match turn kind")


@dataclass(frozen=True)
class SpecialistUsage(CanonicalContract):
    iterations: int
    tool_calls: int
    input_tokens: int
    output_tokens: int
    revisions: int
    SCHEMA_VERSION: ClassVar[str] = SCHEMA_VERSION

    def __post_init__(self) -> None:
        for name in ("iterations", "tool_calls", "input_tokens", "output_tokens", "revisions"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                fail(FailureCode.INVALID_VALUE, f"{name} must be non-negative")


@dataclass(frozen=True)
class SpecialistAgentCheckpoint(CanonicalContract):
    request_hash: str
    specialist_id: str
    current_stage: SpecialistStage
    plan: ResearchExplorationPlan | None
    findings: tuple[InvestigationFinding, ...]
    candidates: tuple[SpecialistCandidateDraft, ...]
    active_candidates: tuple[SpecialistCandidateDraft, ...]
    preflight_results: tuple[CandidatePreflightResult, ...]
    capability_requests: tuple[FactorLanguageCapabilityRequest, ...]
    usage: SpecialistUsage
    stage_history: tuple[SpecialistStage, ...]
    draft_issues: tuple[SpecialistDraftIssue, ...] = ()
    SCHEMA_VERSION: ClassVar[str] = SCHEMA_VERSION

    def __post_init__(self) -> None:
        require_hash(self.request_hash, "request_hash")
        require_identifier(self.specialist_id, "specialist_id")
        if self.current_stage in TERMINAL_STAGES:
            fail(FailureCode.INVALID_STATE, "checkpoint cannot claim a terminal stage")
        if not self.stage_history or self.stage_history[0] is not SpecialistStage.CREATED or self.stage_history[-1] is not self.current_stage:
            fail(FailureCode.INVALID_STATE, "checkpoint history must end at current_stage")
        for current, target in zip(self.stage_history, self.stage_history[1:]):
            require_transition(current, target)
        ids = tuple(item.candidate_id for item in self.candidates)
        if len(ids) != len(set(ids)):
            fail(FailureCode.INVALID_VALUE, "checkpoint candidates must be unique")
        if any(item.candidate_id not in set(ids) for item in self.active_candidates):
            fail(FailureCode.INVALID_STATE, "active candidates must belong to checkpoint candidates")


@dataclass(frozen=True)
class SpecialistAgentOutcome(CanonicalContract):
    request_hash: str
    specialist_id: str
    status: SpecialistStage
    plan: ResearchExplorationPlan | None
    findings: tuple[InvestigationFinding, ...]
    candidates: tuple[SpecialistCandidateDraft, ...]
    preflight_results: tuple[CandidatePreflightResult, ...]
    capability_requests: tuple[FactorLanguageCapabilityRequest, ...]
    usage: SpecialistUsage
    stage_history: tuple[SpecialistStage, ...]
    result_kind: SpecialistResultKind = SpecialistResultKind.SYSTEM_FAILED
    draft_issues: tuple[SpecialistDraftIssue, ...] = ()
    failure_code: str | None = None
    message: str = ""
    SCHEMA_VERSION: ClassVar[str] = SCHEMA_VERSION

    def __post_init__(self) -> None:
        require_hash(self.request_hash, "request_hash")
        require_identifier(self.specialist_id, "specialist_id")
        if self.status not in TERMINAL_STAGES:
            fail(FailureCode.INVALID_STATE, "outcome status must be terminal")
        if not isinstance(self.result_kind, SpecialistResultKind):
            fail(FailureCode.INVALID_VALUE, "specialist result_kind is invalid")
        if not self.stage_history or self.stage_history[0] is not SpecialistStage.CREATED or self.stage_history[-1] is not self.status:
            fail(FailureCode.INVALID_STATE, "stage_history must span CREATED to the terminal status")
        for current, target in zip(self.stage_history, self.stage_history[1:]):
            require_transition(current, target)
        ids = tuple(item.candidate_id for item in self.candidates)
        if len(ids) != len(set(ids)):
            fail(FailureCode.INVALID_VALUE, "outcome candidates must be unique")
        if self.status is SpecialistStage.COMPLETED:
            if self.plan is None or not self.candidates or not self.preflight_results or self.failure_code is not None:
                fail(FailureCode.INCOMPLETE_RESULT, "completed outcome lacks substantive DRAFT artifacts")
            accepted = {item.candidate_id for item in self.preflight_results if item.status is PreflightStatus.ACCEPTED}
            if not accepted:
                fail(FailureCode.INCOMPLETE_RESULT, "completed outcome requires an accepted FactorDAG draft")
        elif not self.failure_code:
            fail(FailureCode.MISSING_REQUIRED_FIELD, "non-completed outcome requires failure_code")

    @classmethod
    def from_payload(
        cls,
        payload: Mapping[str, Any],
        *,
        component: Any | None = None,
    ) -> "SpecialistAgentOutcome":
        raw = dict(payload)
        return super().from_payload(raw, component=component)


# Public milestone name; retained alias keeps existing callers source-compatible.
SpecialistAgentBudget = SpecialistResearchBudget
