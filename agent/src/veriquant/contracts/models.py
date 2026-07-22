"""Immutable VeriQuant boundary contracts and validators.

These types carry data only.  They do not evaluate factors, compute evidence,
optimize portfolios, approve risk, send orders, execute fills, or implement a
ledger service.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, ClassVar, Mapping

from .base import (
    CanonicalContract,
    fail,
    freeze_mapping,
    require_aware_datetime,
    require_finite_number,
    require_hash,
    require_identifier,
    require_nonempty_tuple,
    require_text,
    require_time_order,
)
from .enums import Capability, Component, EvidenceClass, FailureCode, ResearchState
from .transitions import validate_transition


_PROPOSERS = frozenset(
    {Component.LLM_RESEARCHER, Component.RESEARCH_DESK, Component.QUANTAALPHA}
)
_AUXILIARY_PRODUCERS = frozenset(
    {
        Component.LLM_RESEARCHER,
        Component.RESEARCH_DESK,
        Component.QUANTAALPHA,
        Component.QLIB,
    }
)
_FACTOR_OPERATORS = frozenset(
    {
        "ADD", "SUBTRACT", "MULTIPLY", "DIVIDE", "NEGATE", "ABS",
        "LOG1P_ABS", "LAG", "DELTA", "ROLLING_MEAN", "ROLLING_STD",
        "ROLLING_MIN", "ROLLING_MAX", "ROLLING_SUM", "RANK_CROSS_SECTION",
        "ZSCORE_CROSS_SECTION", "CLIP",
    }
)


def _require_enum(value: Any, enum_type: type, field_name: str) -> None:
    if not isinstance(value, enum_type):
        fail(FailureCode.INVALID_VALUE, f"{field_name} must be {enum_type.__name__}", field=field_name)


def _validate_state(value: ResearchState, allowed: frozenset[ResearchState] | None = None) -> None:
    _require_enum(value, ResearchState, "state")
    if allowed is not None and value not in allowed:
        fail(
            FailureCode.INVALID_STATE,
            f"state {value.value} is not valid for this contract",
            state=value.value,
            allowed=tuple(sorted(item.value for item in allowed)),
        )


def _validate_string_tuple(value: tuple[str, ...], field_name: str, *, nonempty: bool = True) -> None:
    if not isinstance(value, tuple) or (nonempty and not value):
        fail(FailureCode.INVALID_VALUE, f"{field_name} must be a tuple", field=field_name)
    for item in value:
        require_text(item, field_name)


def _validate_hash_tuple(value: tuple[str, ...], field_name: str, *, nonempty: bool = True) -> None:
    _validate_string_tuple(value, field_name, nonempty=nonempty)
    for item in value:
        require_hash(item, field_name)


def _validate_context(obj: Any) -> None:
    _validate_string_tuple(obj.universe, "universe")
    require_text(obj.horizon, "horizon")
    require_aware_datetime(obj.decision_time, "decision_time")
    _validate_string_tuple(obj.availability_assumptions, "availability_assumptions")


def _set_mapping(obj: Any, field_name: str, *, numeric_values: bool = False) -> None:
    object.__setattr__(
        obj,
        field_name,
        freeze_mapping(getattr(obj, field_name), field_name, numeric_values=numeric_values),
    )


def _require_exact_component(value: Component, expected: Component, field_name: str) -> None:
    _require_enum(value, Component, field_name)
    if value is not expected:
        fail(
            FailureCode.INVALID_ARTIFACT_PRODUCER,
            f"{field_name} must be {expected.value}",
            field=field_name,
            expected=expected.value,
            actual=value.value,
        )


@dataclass(frozen=True, kw_only=True)
class DatasetCard(CanonicalContract):
    ALLOWED_PRODUCERS: ClassVar[frozenset[Component]] = frozenset({Component.DATA_SERVICE})

    dataset_id: str
    source: str
    universe: tuple[str, ...]
    horizon: str
    decision_time: datetime
    availability_assumptions: tuple[str, ...]
    fields: tuple[str, ...]
    event_time: datetime
    published_at: datetime
    available_at: datetime
    snapshot_hash: str
    record_count: int
    produced_by: Component = Component.DATA_SERVICE

    def __post_init__(self) -> None:
        require_identifier(self.dataset_id, "dataset_id")
        require_text(self.source, "source")
        _validate_context(self)
        _validate_string_tuple(self.fields, "fields")
        require_time_order(
            ("event_time", self.event_time),
            ("published_at", self.published_at),
            ("available_at", self.available_at),
            ("decision_time", self.decision_time),
        )
        require_hash(self.snapshot_hash, "snapshot_hash")
        if isinstance(self.record_count, bool) or not isinstance(self.record_count, int) or self.record_count < 0:
            fail(FailureCode.INVALID_VALUE, "record_count must be a non-negative integer")
        _require_exact_component(self.produced_by, Component.DATA_SERVICE, "produced_by")


@dataclass(frozen=True, kw_only=True)
class PointInTimeRecord(CanonicalContract):
    ALLOWED_PRODUCERS: ClassVar[frozenset[Component]] = frozenset({Component.DATA_SERVICE})

    record_id: str
    dataset_id: str
    entity_id: str
    field_name: str
    value: str | int | float | bool | None
    event_time: datetime
    published_at: datetime
    available_at: datetime
    source: str
    produced_by: Component = Component.DATA_SERVICE

    def __post_init__(self) -> None:
        require_identifier(self.record_id, "record_id")
        require_identifier(self.dataset_id, "dataset_id")
        require_identifier(self.entity_id, "entity_id")
        require_text(self.field_name, "field_name")
        require_text(self.source, "source")
        if isinstance(self.value, float):
            require_finite_number(self.value, "value")
        require_time_order(
            ("event_time", self.event_time),
            ("published_at", self.published_at),
            ("available_at", self.available_at),
        )
        _require_exact_component(self.produced_by, Component.DATA_SERVICE, "produced_by")


@dataclass(frozen=True, kw_only=True)
class ResearchQuestion(CanonicalContract):
    ALLOWED_PRODUCERS: ClassVar[frozenset[Component]] = _PROPOSERS

    question_id: str
    text: str
    universe: tuple[str, ...]
    horizon: str
    decision_time: datetime
    availability_assumptions: tuple[str, ...]
    state: ResearchState = ResearchState.DRAFT

    def __post_init__(self) -> None:
        require_identifier(self.question_id, "question_id")
        require_text(self.text, "text")
        _validate_context(self)
        _validate_state(self.state, frozenset({ResearchState.DRAFT, ResearchState.REVISE}))


@dataclass(frozen=True, kw_only=True)
class ResearchDirection(CanonicalContract):
    ALLOWED_PRODUCERS: ClassVar[frozenset[Component]] = _PROPOSERS

    direction_id: str
    question_hash: str
    thesis: str
    constraints: tuple[str, ...]
    universe: tuple[str, ...]
    horizon: str
    decision_time: datetime
    availability_assumptions: tuple[str, ...]
    state: ResearchState = ResearchState.DRAFT

    def __post_init__(self) -> None:
        require_identifier(self.direction_id, "direction_id")
        require_hash(self.question_hash, "question_hash")
        require_text(self.thesis, "thesis")
        _validate_string_tuple(self.constraints, "constraints", nonempty=False)
        _validate_context(self)
        _validate_state(self.state, frozenset({ResearchState.DRAFT, ResearchState.REVISE}))


@dataclass(frozen=True, kw_only=True)
class Hypothesis(CanonicalContract):
    ALLOWED_PRODUCERS: ClassVar[frozenset[Component]] = _PROPOSERS

    hypothesis_id: str
    direction_hash: str
    statement: str
    economic_mechanism: str
    expected_sign: str
    falsification_tests: tuple[str, ...]
    universe: tuple[str, ...]
    horizon: str
    decision_time: datetime
    availability_assumptions: tuple[str, ...]
    state: ResearchState = ResearchState.DRAFT

    def __post_init__(self) -> None:
        require_identifier(self.hypothesis_id, "hypothesis_id")
        require_hash(self.direction_hash, "direction_hash")
        require_text(self.statement, "statement")
        require_text(self.economic_mechanism, "economic_mechanism")
        if self.expected_sign not in {"POSITIVE", "NEGATIVE", "NON_MONOTONIC"}:
            fail(FailureCode.INVALID_VALUE, "expected_sign has an unsupported value")
        _validate_string_tuple(self.falsification_tests, "falsification_tests")
        _validate_context(self)
        _validate_state(self.state, frozenset({ResearchState.DRAFT, ResearchState.REVISE}))


@dataclass(frozen=True, kw_only=True)
class FactorSpecDraft(CanonicalContract):
    ALLOWED_PRODUCERS: ClassVar[frozenset[Component]] = _PROPOSERS

    draft_id: str
    hypothesis_hash: str
    name: str
    description: str
    proposed_expression: str
    required_fields: tuple[str, ...]
    lookback_bars: int
    neutralization: tuple[str, ...]
    universe: tuple[str, ...]
    horizon: str
    decision_time: datetime
    availability_assumptions: tuple[str, ...]
    state: ResearchState = ResearchState.DRAFT

    def __post_init__(self) -> None:
        require_identifier(self.draft_id, "draft_id")
        require_hash(self.hypothesis_hash, "hypothesis_hash")
        require_text(self.name, "name")
        require_text(self.description, "description")
        require_text(self.proposed_expression, "proposed_expression")
        _validate_string_tuple(self.required_fields, "required_fields")
        if isinstance(self.lookback_bars, bool) or not isinstance(self.lookback_bars, int) or self.lookback_bars < 1:
            fail(FailureCode.INVALID_VALUE, "lookback_bars must be a positive integer")
        _validate_string_tuple(self.neutralization, "neutralization", nonempty=False)
        _validate_context(self)
        _validate_state(self.state, frozenset({ResearchState.DRAFT, ResearchState.REVISE}))


@dataclass(frozen=True, kw_only=True)
class FactorSpec(CanonicalContract):
    # DATA_SERVICE is the existing deterministic boundary available in Wave 01.
    # The architecture document records a dedicated regulator service as future work.
    ALLOWED_PRODUCERS: ClassVar[frozenset[Component]] = frozenset({Component.DATA_SERVICE})

    factor_id: str
    draft_hash: str
    name: str
    description: str
    required_fields: tuple[str, ...]
    lookback_bars: int
    neutralization: tuple[str, ...]
    universe: tuple[str, ...]
    horizon: str
    decision_time: datetime
    availability_assumptions: tuple[str, ...]
    state: ResearchState = ResearchState.DATA_VALIDATED
    produced_by: Component = Component.DATA_SERVICE

    def __post_init__(self) -> None:
        require_identifier(self.factor_id, "factor_id")
        require_hash(self.draft_hash, "draft_hash")
        require_text(self.name, "name")
        require_text(self.description, "description")
        _validate_string_tuple(self.required_fields, "required_fields")
        if isinstance(self.lookback_bars, bool) or not isinstance(self.lookback_bars, int) or self.lookback_bars < 1:
            fail(FailureCode.INVALID_VALUE, "lookback_bars must be a positive integer")
        _validate_string_tuple(self.neutralization, "neutralization", nonempty=False)
        _validate_context(self)
        _validate_state(self.state, frozenset({ResearchState.DATA_VALIDATED, ResearchState.PIT_VALIDATED}))
        _require_exact_component(self.produced_by, Component.DATA_SERVICE, "produced_by")


@dataclass(frozen=True, kw_only=True)
class FactorNode:
    node_id: str
    operator: str
    inputs: tuple[str, ...]
    parameters: Mapping[str, str | int | float | bool] = field(default_factory=dict)

    def __post_init__(self) -> None:
        require_identifier(self.node_id, "node_id")
        if self.operator not in _FACTOR_OPERATORS:
            fail(FailureCode.INVALID_FACTOR_DAG, f"operator {self.operator!r} is not allowed")
        _validate_string_tuple(self.inputs, "inputs")
        _set_mapping(self, "parameters")


@dataclass(frozen=True, kw_only=True)
class FactorDAG(CanonicalContract):
    ALLOWED_PRODUCERS: ClassVar[frozenset[Component]] = frozenset({Component.DATA_SERVICE})

    dag_id: str
    factor_spec_hash: str
    nodes: tuple[FactorNode, ...]
    output_node: str
    required_fields: tuple[str, ...]
    universe: tuple[str, ...]
    horizon: str
    decision_time: datetime
    availability_assumptions: tuple[str, ...]
    state: ResearchState = ResearchState.PIT_VALIDATED
    produced_by: Component = Component.DATA_SERVICE

    def __post_init__(self) -> None:
        require_identifier(self.dag_id, "dag_id")
        require_hash(self.factor_spec_hash, "factor_spec_hash")
        require_nonempty_tuple(self.nodes, "nodes")
        if not all(isinstance(node, FactorNode) for node in self.nodes):
            fail(FailureCode.INVALID_FACTOR_DAG, "nodes must contain FactorNode values")
        _validate_string_tuple(self.required_fields, "required_fields")
        _validate_context(self)
        _validate_state(self.state, frozenset({ResearchState.PIT_VALIDATED}))
        _require_exact_component(self.produced_by, Component.DATA_SERVICE, "produced_by")
        by_id = {node.node_id: node for node in self.nodes}
        if len(by_id) != len(self.nodes):
            fail(FailureCode.INVALID_FACTOR_DAG, "node identifiers must be unique")
        if self.output_node not in by_id:
            fail(FailureCode.INVALID_FACTOR_DAG, "output_node does not exist")
        raw_fields = set(self.required_fields)
        dependencies: dict[str, tuple[str, ...]] = {}
        for node in self.nodes:
            unknown = [item for item in node.inputs if item not in by_id and item not in raw_fields]
            if unknown:
                fail(FailureCode.INVALID_FACTOR_DAG, "undeclared FactorDAG input", node=node.node_id)
            dependencies[node.node_id] = tuple(item for item in node.inputs if item in by_id)
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(node_id: str) -> None:
            if node_id in visiting:
                fail(FailureCode.INVALID_FACTOR_DAG, "FactorDAG contains a cycle", node=node_id)
            if node_id in visited:
                return
            visiting.add(node_id)
            for dependency in dependencies[node_id]:
                visit(dependency)
            visiting.remove(node_id)
            visited.add(node_id)

        for node_id in by_id:
            visit(node_id)


@dataclass(frozen=True, kw_only=True)
class ExperimentSpec(CanonicalContract):
    ALLOWED_PRODUCERS: ClassVar[frozenset[Component]] = frozenset({Component.EVIDENCE_SERVICE})

    experiment_id: str
    factor_dag_hash: str
    dataset_hash: str
    universe: tuple[str, ...]
    horizon: str
    decision_time: datetime
    availability_assumptions: tuple[str, ...]
    label_start: datetime
    label_end: datetime
    evaluation_protocol: str
    seed: int
    state: ResearchState = ResearchState.PIT_VALIDATED
    produced_by: Component = Component.EVIDENCE_SERVICE

    def __post_init__(self) -> None:
        require_identifier(self.experiment_id, "experiment_id")
        require_hash(self.factor_dag_hash, "factor_dag_hash")
        require_hash(self.dataset_hash, "dataset_hash")
        _validate_context(self)
        require_time_order(("decision_time", self.decision_time), ("label_start", self.label_start), ("label_end", self.label_end))
        require_text(self.evaluation_protocol, "evaluation_protocol")
        if isinstance(self.seed, bool) or not isinstance(self.seed, int) or self.seed < 0:
            fail(FailureCode.INVALID_VALUE, "seed must be a non-negative integer")
        _validate_state(self.state, frozenset({ResearchState.PIT_VALIDATED}))
        _require_exact_component(self.produced_by, Component.EVIDENCE_SERVICE, "produced_by")


@dataclass(frozen=True, kw_only=True)
class ExperimentIdentity(CanonicalContract):
    ALLOWED_PRODUCERS: ClassVar[frozenset[Component]] = frozenset({Component.EVIDENCE_SERVICE})

    experiment_id: str
    experiment_spec_hash: str
    factor_dag_hash: str
    dataset_hash: str
    code_hash: str
    config_hash: str
    seed: int
    state: ResearchState = ResearchState.PIT_VALIDATED
    produced_by: Component = Component.EVIDENCE_SERVICE

    def __post_init__(self) -> None:
        require_identifier(self.experiment_id, "experiment_id")
        for name in ("experiment_spec_hash", "factor_dag_hash", "dataset_hash", "code_hash", "config_hash"):
            require_hash(getattr(self, name), name)
        if isinstance(self.seed, bool) or not isinstance(self.seed, int) or self.seed < 0:
            fail(FailureCode.INVALID_VALUE, "seed must be a non-negative integer")
        _validate_state(self.state, frozenset({ResearchState.PIT_VALIDATED}))
        _require_exact_component(self.produced_by, Component.EVIDENCE_SERVICE, "produced_by")


@dataclass(frozen=True, kw_only=True)
class CandidateFamily(CanonicalContract):
    family_id: str
    name: str
    semantic_key: str
    budget_limit: int
    budget_consumed: int
    member_hashes: tuple[str, ...]
    state: ResearchState

    def __post_init__(self) -> None:
        require_identifier(self.family_id, "family_id")
        require_text(self.name, "name")
        require_text(self.semantic_key, "semantic_key")
        if any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in (self.budget_limit, self.budget_consumed)):
            fail(FailureCode.INVALID_VALUE, "family budgets must be non-negative integers")
        if self.budget_consumed > self.budget_limit:
            fail(FailureCode.INVALID_VALUE, "budget_consumed cannot exceed budget_limit")
        _validate_hash_tuple(self.member_hashes, "member_hashes", nonempty=False)
        _validate_state(self.state)


@dataclass(frozen=True, kw_only=True)
class EvolutionLineage(CanonicalContract):
    """Versioned DRAFT-generation lineage; it carries no evaluation verdict."""

    SCHEMA_VERSION: ClassVar[str] = "1.1.0"

    lineage_id: str
    candidate_id: str
    candidate_hash: str
    parent_ids: tuple[str, ...]
    operator: str
    generation: int
    prompt_hash: str
    novelty: float
    budget_reservation_id: str
    trial_id: str
    seed: int
    outcome: str
    failure_code: FailureCode | None
    created_at: datetime
    state: ResearchState

    def __post_init__(self) -> None:
        require_identifier(self.lineage_id, "lineage_id")
        require_identifier(self.candidate_id, "candidate_id")
        require_hash(self.candidate_hash, "candidate_hash")
        if not isinstance(self.parent_ids, tuple):
            fail(FailureCode.INVALID_VALUE, "parent_ids must be a tuple")
        for parent_id in self.parent_ids:
            require_identifier(parent_id, "parent_ids")
        if len(set(self.parent_ids)) != len(self.parent_ids):
            fail(FailureCode.INVALID_VALUE, "parent_ids must be unique")
        if self.operator not in {"ORIGINAL", "MUTATION", "CROSSOVER"}:
            fail(FailureCode.INVALID_VALUE, "unknown evolution operator")
        required_parents = {"ORIGINAL": 0, "MUTATION": 1, "CROSSOVER": 2}[self.operator]
        if len(self.parent_ids) != required_parents:
            fail(
                FailureCode.INVALID_VALUE,
                f"{self.operator} requires {required_parents} parent ids",
            )
        if isinstance(self.generation, bool) or not isinstance(self.generation, int) or self.generation < 0:
            fail(FailureCode.INVALID_VALUE, "generation must be a non-negative integer")
        require_hash(self.prompt_hash, "prompt_hash")
        require_finite_number(self.novelty, "novelty")
        if not 0.0 <= float(self.novelty) <= 1.0:
            fail(FailureCode.INVALID_VALUE, "novelty must be between zero and one")
        require_identifier(self.budget_reservation_id, "budget_reservation_id")
        require_identifier(self.trial_id, "trial_id")
        if isinstance(self.seed, bool) or not isinstance(self.seed, int) or self.seed < 0:
            fail(FailureCode.INVALID_VALUE, "seed must be a non-negative integer")
        allowed_outcomes = {
            "COMMITTED_DRAFT",
            "GENERATION_FAILED",
            "PARSE_FAILED",
            "PIT_FAILED",
            "EVALUATION_FAILED",
            "TIMEOUT",
            "CANCELLED",
        }
        if self.outcome not in allowed_outcomes:
            fail(FailureCode.INVALID_VALUE, "unknown evolution lineage outcome")
        if (self.outcome == "COMMITTED_DRAFT") != (self.failure_code is None):
            fail(FailureCode.INVALID_VALUE, "only committed drafts omit a failure code")
        require_aware_datetime(self.created_at, "created_at")
        expected_state = (
            ResearchState.DRAFT if self.outcome == "COMMITTED_DRAFT" else ResearchState.BLOCKED
        )
