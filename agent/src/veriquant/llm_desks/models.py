"""Typed contracts for bounded LLM Research Desks."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping

from ..contracts import CanonicalContract, ContractValidationError, FailureCode
from ..contracts.base import (
    freeze_mapping,
    require_aware_datetime,
    require_finite_number,
    require_hash,
    require_identifier,
    require_nonempty_tuple,
    require_text,
)
from .safety import assert_no_forbidden_content, canonical_hash, fail


class DeskId(str, Enum):
    FUNDAMENTAL = "fundamental"
    EXPECTATIONS_EVENTS = "expectations_events"
    MARKET_STRUCTURE = "market_structure"
    ALTERNATIVE_DATA = "alternative_data"
    MACRO_REGIME = "macro_regime"
    META_ALPHA = "meta_alpha"


class LLMDeskStatus(str, Enum):
    DRAFT = "DRAFT"
    INCOMPLETE = "INCOMPLETE"
    BLOCKED = "BLOCKED"


class LLMDeskStage(str, Enum):
    HYPOTHESIS = "hypothesis"
    FACTOR_DRAFT = "factor_draft"
    DETERMINISTIC_COMPILE_PIT = "deterministic_compile_pit"
    OFFICIAL_EVALUATE = "official_evaluate"
    REDACTED_FEEDBACK = "redacted_feedback"


FIVE_STAGE_SEQUENCE: tuple[LLMDeskStage, ...] = (
    LLMDeskStage.HYPOTHESIS,
    LLMDeskStage.FACTOR_DRAFT,
    LLMDeskStage.DETERMINISTIC_COMPILE_PIT,
    LLMDeskStage.OFFICIAL_EVALUATE,
    LLMDeskStage.REDACTED_FEEDBACK,
)


class ExpectedSign(str, Enum):
    POSITIVE = "POSITIVE"
    NEGATIVE = "NEGATIVE"
    NON_MONOTONIC = "NON_MONOTONIC"


def coerce_desk_id(value: DeskId | str) -> DeskId:
    try:
        return value if isinstance(value, DeskId) else DeskId(value)
    except ValueError as exc:
        raise ContractValidationError(
            FailureCode.INVALID_IDENTIFIER,
            "unknown Research Desk id",
            details={"desk_id": str(value)},
        ) from exc


def coerce_status(value: LLMDeskStatus | str) -> LLMDeskStatus:
    try:
        return value if isinstance(value, LLMDeskStatus) else LLMDeskStatus(value)
    except ValueError as exc:
        raise ContractValidationError(
            FailureCode.INVALID_STATE,
            "LLM Desk output status must be DRAFT, INCOMPLETE, or BLOCKED",
            details={"status": str(value)},
        ) from exc


@dataclass(frozen=True, kw_only=True)
class ProviderBudget(CanonicalContract):
    """Per-desk model and hard provider limits."""

    model: str
    max_tokens: int = 2048
    max_calls: int = 6
    max_cost: float = 1.0
    timeout_seconds: float = 5.0
    max_retries: int = 1

    def __post_init__(self) -> None:
        require_text(self.model, "model")
        for name in ("max_tokens", "max_calls"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                fail(FailureCode.INVALID_VALUE, f"{name} must be a positive integer")
        if isinstance(self.max_retries, bool) or not isinstance(self.max_retries, int) or self.max_retries < 0:
            fail(FailureCode.INVALID_VALUE, "max_retries must be a non-negative integer")
        for name in ("max_cost", "timeout_seconds"):
            require_finite_number(getattr(self, name), name)
        if self.max_cost < 0:
            fail(FailureCode.INVALID_VALUE, "max_cost must be non-negative")
        if not 0.0 < float(self.timeout_seconds) <= 300.0:
            fail(FailureCode.INVALID_VALUE, "timeout_seconds must be in (0, 300]")


@dataclass(frozen=True, kw_only=True)
class ProviderTraceEvent(CanonicalContract):
    desk_id: DeskId
    stage: LLMDeskStage
    prompt_hash: str
    model: str
    parameters: Mapping[str, Any]
    tokens_in: int
    tokens_out: int
    cost: float
    tool_calls: tuple[str, ...]
    output_hash: str | None = None
    failure_code: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "desk_id", coerce_desk_id(self.desk_id))
        if not isinstance(self.stage, LLMDeskStage):
            object.__setattr__(self, "stage", LLMDeskStage(self.stage))
        require_hash(self.prompt_hash, "prompt_hash")
        require_text(self.model, "model")
        assert_no_forbidden_content(dict(self.parameters), where="trace.parameters")
        object.__setattr__(self, "parameters", MappingProxyType(dict(self.parameters)))
        for name in ("tokens_in", "tokens_out"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                fail(FailureCode.INVALID_VALUE, f"{name} must be a non-negative integer")
        require_finite_number(self.cost, "cost")
        if self.cost < 0:
            fail(FailureCode.INVALID_VALUE, "cost must be non-negative")
        for item in self.tool_calls:
            require_identifier(item, "tool_call")
        if self.output_hash is not None:
            require_hash(self.output_hash, "output_hash")
        if self.failure_code is not None:
            require_identifier(self.failure_code, "failure_code")


@dataclass(frozen=True, kw_only=True)
class ProviderTrace(CanonicalContract):
    desk_id: DeskId
    events: tuple[ProviderTraceEvent, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "desk_id", coerce_desk_id(self.desk_id))
        if any(event.desk_id is not self.desk_id for event in self.events):
            fail(FailureCode.INVALID_VALUE, "provider trace contains another desk id")


class ProviderSession:
    """Mutable per-desk accounting; no session is shared across desks."""

    def __init__(self, *, desk_id: DeskId | str, budget: ProviderBudget) -> None:
        self.desk_id = coerce_desk_id(desk_id)
        self.budget = budget
        self.calls_used = 0
        self.tokens_used = 0
        self.cost_used = 0.0
        self._events: list[ProviderTraceEvent] = []
        self._pending_tools: list[str] = []

    @property
    def remaining_tokens(self) -> int:
        return max(0, self.budget.max_tokens - self.tokens_used)

    def note_tool_call(self, name: str) -> None:
        require_identifier(name, "tool_call")
        self._pending_tools.append(name)

    def reserve_call(self, stage: LLMDeskStage) -> None:
        if self.calls_used >= self.budget.max_calls:
            raise ContractValidationError(
                FailureCode.PROVIDER_BUDGET_EXCEEDED,
                "provider call budget exhausted",
                details={"desk_id": self.desk_id.value, "stage": stage.value},
            )
        self.calls_used += 1

    def record_success(
        self,
        *,
        stage: LLMDeskStage,
        prompt_material: Mapping[str, Any],
        output: Any,
        tokens_in: int,
        tokens_out: int,
        cost: float,
        parameters: Mapping[str, Any],
    ) -> None:
        self._record(
            stage=stage,
            prompt_material=prompt_material,
            output_hash=canonical_hash(output),
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cost=cost,
            failure_code=None,
            parameters=parameters,
        )

    def record_failure(
        self,
        *,
        stage: LLMDeskStage,
        prompt_material: Mapping[str, Any],
        output_hash: str | None,
        failure_code: FailureCode,
        parameters: Mapping[str, Any],
    ) -> None:
        self._record(
            stage=stage,
            prompt_material=prompt_material,
            output_hash=output_hash,
            tokens_in=0,
            tokens_out=0,
            cost=0.0,
            failure_code=failure_code,
            parameters=parameters,
        )

    def _record(
        self,
        *,
        stage: LLMDeskStage,
        prompt_material: Mapping[str, Any],
        output_hash: str | None,
        tokens_in: int,
        tokens_out: int,
        cost: float,
        failure_code: FailureCode | None,
        parameters: Mapping[str, Any],
    ) -> None:
        if self.tokens_used + tokens_in + tokens_out > self.budget.max_tokens:
            failure_code = FailureCode.PROVIDER_BUDGET_EXCEEDED
        if self.cost_used + float(cost) > self.budget.max_cost:
            failure_code = FailureCode.PROVIDER_BUDGET_EXCEEDED
        if failure_code is None:
            self.tokens_used += tokens_in + tokens_out
            self.cost_used += float(cost)
        event = ProviderTraceEvent(
            desk_id=self.desk_id,
            stage=stage,
            prompt_hash=canonical_hash(prompt_material),
            model=self.budget.model,
            parameters=dict(parameters),
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cost=float(cost),
            tool_calls=tuple(self._pending_tools),
            output_hash=output_hash,
            failure_code=failure_code.value if failure_code is not None else None,
        )
        self._pending_tools.clear()
        self._events.append(event)
        if failure_code is not None and (tokens_in > 0 or tokens_out > 0 or cost > 0.0):
            raise ContractValidationError(
                failure_code,
                "provider call failed closed",
                details={"desk_id": self.desk_id.value, "stage": stage.value},
            )

    def snapshot(self) -> ProviderTrace:
        return ProviderTrace(desk_id=self.desk_id, events=tuple(self._events))


@dataclass(frozen=True, kw_only=True)
class DeskLLMConfig(CanonicalContract):
    desk_id: DeskId
    role: str
    context: tuple[str, ...]
    model: str
    budget: ProviderBudget
    tool_names: tuple[str, ...]
    artifact_namespace: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "desk_id", coerce_desk_id(self.desk_id))
        require_text(self.role, "role")
        require_nonempty_tuple(self.context, "context")
        require_text(self.model, "model")
        if self.model != self.budget.model:
            fail(FailureCode.INVALID_VALUE, "desk model must equal provider budget model")
        require_nonempty_tuple(self.tool_names, "tool_names")
        if len(set(self.tool_names)) != len(self.tool_names):
            fail(FailureCode.INVALID_VALUE, "desk tool names must be unique")
        require_identifier(self.artifact_namespace, "artifact_namespace")
        if self.artifact_namespace != self.desk_id.value:
            fail(FailureCode.INVALID_VALUE, "artifact namespace must equal desk id")


def build_default_desk_configs() -> tuple[DeskLLMConfig, ...]:
    definitions = (
        (
            DeskId.FUNDAMENTAL,
            "Fundamental point-in-time research proposer",
            ("Use released accounting fields.", "Return only inert drafts."),
            ("fundamental.read_context", "fundamental.read_field_catalog"),
        ),
        (
            DeskId.EXPECTATIONS_EVENTS,
            "Expectations and event proxy research proposer",
            ("Use visible event summaries.", "Return only inert drafts."),
            ("expectations_events.read_context", "expectations_events.read_field_catalog"),
        ),
        (
            DeskId.MARKET_STRUCTURE,
            "Market structure research proposer",
            ("Use visible liquidity summaries.", "Return only inert drafts."),
            ("market_structure.read_context", "market_structure.read_field_catalog"),
        ),
        (
            DeskId.ALTERNATIVE_DATA,
            "Alternative data proxy research proposer",
            ("Use sanitized offline proxy summaries.", "Return only inert drafts."),
            ("alternative_data.read_context", "alternative_data.read_field_catalog"),
        ),
        (
            DeskId.MACRO_REGIME,
            "Macro regime research proposer",
            ("Use released macro regime summaries.", "Return only inert drafts."),
            ("macro_regime.read_context", "macro_regime.read_field_catalog"),
        ),
        (
            DeskId.META_ALPHA,
            "Meta alpha research proposer",
            ("Use redacted registered-factor summaries.", "Return only inert drafts."),
            ("meta_alpha.read_context", "meta_alpha.read_field_catalog"),
        ),
    )
    return tuple(
        DeskLLMConfig(
            desk_id=desk_id,
            role=role,
            context=context,
            model=f"deterministic-{desk_id.value}",
            budget=ProviderBudget(model=f"deterministic-{desk_id.value}"),
            tool_names=tool_names,
            artifact_namespace=desk_id.value,
        )
        for desk_id, role, context, tool_names in definitions
    )


@dataclass(frozen=True, kw_only=True)
class ResearchQuestionView(CanonicalContract):
    question_id: str
    text: str
    universe: tuple[str, ...]
    horizon: str
    decision_time: datetime
    availability_assumptions: tuple[str, ...]

    def __post_init__(self) -> None:
        require_identifier(self.question_id, "question_id")
        require_text(self.text, "text")
        require_nonempty_tuple(self.universe, "universe")
        if any(not isinstance(item, str) or not item for item in self.universe):
            fail(FailureCode.INVALID_VALUE, "universe entries must be non-empty strings")
        require_text(self.horizon, "horizon")
        require_aware_datetime(self.decision_time, "decision_time")
        require_nonempty_tuple(self.availability_assumptions, "availability_assumptions")


@dataclass(frozen=True, kw_only=True)
class HypothesisFill(CanonicalContract):
    state: LLMDeskStatus
    desk_id: DeskId
    statement: str
    economic_mechanism: str
    expected_sign: ExpectedSign
    universe: tuple[str, ...]
    horizon: str
    decision_time: datetime
    availability_assumptions: tuple[str, ...]
    falsification_tests: tuple[str, ...]
    react_context_hashes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "state", coerce_status(self.state))
        object.__setattr__(self, "desk_id", coerce_desk_id(self.desk_id))
        if not isinstance(self.expected_sign, ExpectedSign):
            try:
                object.__setattr__(self, "expected_sign", ExpectedSign(self.expected_sign))
            except ValueError as exc:
                raise ContractValidationError(
                    FailureCode.PROVIDER_SCHEMA_REJECTED,
                    "expected_sign has an invalid value",
                    details={"expected_sign": str(self.expected_sign)},
                ) from exc
        for field in ("statement", "economic_mechanism", "horizon"):
            require_text(getattr(self, field), field)
        assert_no_forbidden_content(
            {"statement": self.statement, "economic_mechanism": self.economic_mechanism},
            where="hypothesis",
        )
        require_nonempty_tuple(self.universe, "universe")
        require_aware_datetime(self.decision_time, "decision_time")
        require_nonempty_tuple(self.availability_assumptions, "availability_assumptions")
        require_nonempty_tuple(self.falsification_tests, "falsification_tests")
        for item in (*self.falsification_tests, *self.react_context_hashes):
            if item in self.react_context_hashes:
                require_hash(item, "react_context_hash")
            else:
                assert_no_forbidden_content(item, where="falsification_tests")


@dataclass(frozen=True, kw_only=True)
class FactorDraftFill(CanonicalContract):
    state: LLMDeskStatus
    desk_id: DeskId
    name: str
    description: str
    expression: str
    variables: tuple[str, ...]
    lookback_bars: int
    neutralization: tuple[str, ...] = ()
    source_hypothesis_hash: str | None = None
    react_context_hashes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "state", coerce_status(self.state))
        object.__setattr__(self, "desk_id", coerce_desk_id(self.desk_id))
        for field in ("name", "description", "expression"):
            require_text(getattr(self, field), field)
        require_identifier(self.name, "factor_name")
        assert_no_forbidden_content(
            {"description": self.description, "expression": self.expression},
            where="factor_draft",
        )
        require_nonempty_tuple(self.variables, "variables")
        for variable in (*self.variables, *self.neutralization):
            require_identifier(variable, "factor_variable")
            lowered = variable.lower()
            if any(token in lowered for token in ("future", "label", "return_5d")):
                fail(FailureCode.UNAUTHORIZED_CAPABILITY, "factor draft references a label or future field")
        if isinstance(self.lookback_bars, bool) or not isinstance(self.lookback_bars, int) or self.lookback_bars < 1:
            fail(FailureCode.PROVIDER_SCHEMA_REJECTED, "lookback_bars must be a positive integer")
        if self.source_hypothesis_hash is not None:
            require_hash(self.source_hypothesis_hash, "source_hypothesis_hash")
        for item in self.react_context_hashes:
            require_hash(item, "react_context_hash")


@dataclass(frozen=True, kw_only=True)
class RedactedEvidenceSummary(CanonicalContract):
    evidence_hash: str
    visible_metrics: Mapping[str, float]
    allowed_notes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        require_hash(self.evidence_hash, "evidence_hash")
        for key in self.visible_metrics:
            assert_no_forbidden_content({key: self.visible_metrics[key]}, where="visible_metrics")
        object.__setattr__(
            self,
            "visible_metrics",
            freeze_mapping(self.visible_metrics, "visible_metrics", numeric_values=True),
        )
        for note in self.allowed_notes:
            assert_no_forbidden_content(note, where="allowed_notes")


@dataclass(frozen=True, kw_only=True)
class FeedbackExplanation(CanonicalContract):
    state: LLMDeskStatus
    desk_id: DeskId
    summary_hash: str
    explanation: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "state", coerce_status(self.state))
        object.__setattr__(self, "desk_id", coerce_desk_id(self.desk_id))
        require_hash(self.summary_hash, "summary_hash")
        require_text(self.explanation, "explanation")
        assert_no_forbidden_content(self.explanation, where="feedback_explanation")


@dataclass(frozen=True, kw_only=True)
class CompilePITArtifact(CanonicalContract):
    compiled_factor_hash: str
    pit_snapshot_hash: str
    status: str = "PIT_VALIDATED"

    def __post_init__(self) -> None:
        require_hash(self.compiled_factor_hash, "compiled_factor_hash")
        require_hash(self.pit_snapshot_hash, "pit_snapshot_hash")
        if self.status != "PIT_VALIDATED":
            fail(FailureCode.INVALID_STATE, "compile/PIT artifact must be PIT_VALIDATED")


@dataclass(frozen=True, kw_only=True)
class OfficialEvaluationView(CanonicalContract):
    evidence_hash: str
    redacted_summary: RedactedEvidenceSummary
    produced_by: str = "deterministic_evidence_service"

    def __post_init__(self) -> None:
        require_hash(self.evidence_hash, "evidence_hash")
        if self.redacted_summary.evidence_hash != self.evidence_hash:
            fail(FailureCode.INVALID_HASH, "redacted summary must bind official evidence hash")
        if self.produced_by != "deterministic_evidence_service":
            fail(FailureCode.INVALID_ARTIFACT_PRODUCER, "only deterministic service may produce this view")
