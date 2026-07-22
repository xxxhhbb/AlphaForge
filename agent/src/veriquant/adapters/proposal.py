"""Draft-only adapter for QuantaAlpha-shaped hypothesis and FactorTask payloads."""

from __future__ import annotations

import ast
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import Any, Mapping, Sequence

from ..contracts import (
    CanonicalContract,
    Component,
    ContractValidationError,
    FactorSpecDraft,
    FailureCode,
    Hypothesis,
    ResearchState,
)
from ..contracts.base import (
    require_aware_datetime,
    require_hash,
    require_nonempty_tuple,
    require_text,
)
from ..data import FieldRole
from ..factors import CompiledFactorDAG, FACTOR_DAG_SCHEMA_VERSION, parse_compile_factor_dag


_NORMALIZE_KEY = re.compile(r"[^a-z0-9]+")
_FORBIDDEN_KEYS = frozenset(
    {
        "code",
        "sourcecode",
        "python",
        "pythoncode",
        "factorpy",
        "factorpath",
        "implementation",
        "script",
        "officialmetric",
        "officialmetrics",
        "promotion",
        "promotiondecision",
        "portfolio",
        "portfolioplan",
        "targetweights",
        "risk",
        "riskapproval",
        "order",
        "orders",
        "orderplan",
        "sealed",
        "sealedref",
        "sealedtest",
        "sealedreference",
        "sealedreferences",
        "sealedmetrics",
        "testmetrics",
    }
)
_CODE_MARKERS = (
    "factor.py",
    "```python",
    "import ",
    " from ",
    "exec(",
    "eval(",
    "lambda ",
    "def ",
    "subprocess",
    "os.system",
    "__import__",
)
_HYPOTHESIS_FIELDS = frozenset(
    {
        "hypothesis_id",
        "statement",
        "hypothesis",
        "economic_mechanism",
        "reason",
        "expected_sign",
        "expected_direction",
        "falsification_tests",
        "falsification",
        "observations",
        "justification",
    }
)
_FACTOR_FIELDS = frozenset(
    {
        "draft_id",
        "factor_name",
        "name",
        "factor_description",
        "description",
        "factor_formulation",
        "formulation",
        "expression",
        "factor_expression",
        "variables",
        "required_fields",
        "lookback_bars",
        "neutralization",
    }
)


def _fail(code: FailureCode, message: str, **details: Any) -> None:
    raise ContractValidationError(code, message, details=details)


def _normalized_key(value: str) -> str:
    return _NORMALIZE_KEY.sub("", value.lower())


def _reject_forbidden(value: Any, path: str = "payload") -> None:
    if callable(value):
        _fail(
            FailureCode.FORBIDDEN_PROPOSAL_PAYLOAD,
            "Python callables are prohibited in proposal payloads",
            path=path,
        )
    if isinstance(value, Mapping):
        for raw_key, child in value.items():
            if not isinstance(raw_key, str):
                _fail(FailureCode.UNKNOWN_FIELD, "proposal keys must be strings", path=path)
            normalized = _normalized_key(raw_key)
            if normalized in _FORBIDDEN_KEYS:
                _fail(
                    FailureCode.FORBIDDEN_PROPOSAL_PAYLOAD,
                    "proposal contains a forbidden authority or executable field",
                    field=raw_key,
                    path=f"{path}.{raw_key}",
                )
            _reject_forbidden(child, f"{path}.{raw_key}")
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            _reject_forbidden(child, f"{path}[{index}]")
    elif isinstance(value, str):
        padded = f" {value.lower()} "
        marker = next((item for item in _CODE_MARKERS if item in padded), None)
        if marker is not None:
            _fail(
                FailureCode.FORBIDDEN_PROPOSAL_PAYLOAD,
                "proposal contains generated Python or factor.py content",
                path=path,
                marker=marker.strip(),
            )
    elif value is not None and not isinstance(value, (int, float, bool)):
        _fail(
            FailureCode.FORBIDDEN_PROPOSAL_PAYLOAD,
            "proposal contains an unsupported executable-capable value",
            path=path,
            value_type=type(value).__name__,
        )


def _stable_id(prefix: str, value: Any) -> str:
    digest = hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    return f"{prefix}-{digest[:20]}"


def _only_fields(payload: Mapping[str, Any], allowed: frozenset[str], path: str) -> None:
    unknown = set(payload) - allowed
    if unknown:
        _fail(
            FailureCode.UNKNOWN_FIELD,
            f"{path} contains unknown fields",
            path=path,
            fields=tuple(sorted(unknown)),
        )


def _first_text(payload: Mapping[str, Any], names: Sequence[str], field: str) -> str:
    for name in names:
        value = payload.get(name)
        if isinstance(value, str) and value.strip():
            return value.strip()
    _fail(FailureCode.MISSING_REQUIRED_FIELD, f"{field} is required", field=field)


def _string_tuple(value: Any, field: str, *, nonempty: bool = True) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or any(
        not isinstance(item, str) or not item.strip() for item in value
    ):
        _fail(FailureCode.INVALID_VALUE, f"{field} must be an array of non-empty strings")
    result = tuple(item.strip() for item in value)
    if nonempty and not result:
        _fail(FailureCode.INVALID_VALUE, f"{field} must not be empty")
    return result


@dataclass(frozen=True, kw_only=True)
class ProposalContext(CanonicalContract):
    direction_hash: str
    universe: tuple[str, ...]
    horizon: str
    decision_time: datetime
    availability_assumptions: tuple[str, ...]
    field_roles: Mapping[str, FieldRole]

    def __post_init__(self) -> None:
        require_hash(self.direction_hash, "direction_hash")
        require_nonempty_tuple(self.universe, "universe")
        if any(not isinstance(item, str) or not item for item in self.universe):
            _fail(FailureCode.INVALID_VALUE, "universe values must be non-empty strings")
        require_text(self.horizon, "horizon")
        require_aware_datetime(self.decision_time, "decision_time")
        require_nonempty_tuple(self.availability_assumptions, "availability_assumptions")
        if not isinstance(self.field_roles, Mapping) or not self.field_roles:
            _fail(FailureCode.INVALID_VALUE, "field_roles must be a non-empty mapping")
        if any(
            not isinstance(name, str) or not name or not isinstance(role, FieldRole)
            for name, role in self.field_roles.items()
        ):
            _fail(FailureCode.INVALID_VALUE, "field_roles contains an invalid field or role")
        object.__setattr__(
            self,
            "field_roles",
            MappingProxyType(dict(sorted(self.field_roles.items()))),
        )


@dataclass(frozen=True, kw_only=True)
class DraftProposal(CanonicalContract):
    hypothesis: Hypothesis
    factor_draft: FactorSpecDraft
    factor_dag: CompiledFactorDAG
    state: ResearchState = ResearchState.DRAFT
    produced_by: Component = Component.QUANTAALPHA

    def __post_init__(self) -> None:
        if not isinstance(self.hypothesis, Hypothesis):
            _fail(FailureCode.INVALID_VALUE, "hypothesis must be a Hypothesis contract")
        if not isinstance(self.factor_draft, FactorSpecDraft):
            _fail(FailureCode.INVALID_VALUE, "factor_draft must be a FactorSpecDraft contract")
        if not isinstance(self.factor_dag, CompiledFactorDAG):
            _fail(FailureCode.INVALID_FACTOR_DAG, "factor_dag must be a compiled restricted DAG")
        if self.hypothesis.state is not ResearchState.DRAFT:
            _fail(FailureCode.INVALID_STATE, "adapter hypothesis must remain DRAFT")
        if self.factor_draft.state is not ResearchState.DRAFT or self.state is not ResearchState.DRAFT:
            _fail(FailureCode.INVALID_STATE, "adapter outputs must remain DRAFT")
        if self.factor_draft.hypothesis_hash != self.hypothesis.artifact_hash:
            _fail(FailureCode.INVALID_VALUE, "factor draft does not bind the adapted hypothesis")
        if self.factor_draft.proposed_expression != self.factor_dag.canonical_json():
            _fail(FailureCode.INVALID_FACTOR_DAG, "factor draft must store only canonical FactorDAG")
        if self.produced_by is not Component.QUANTAALPHA:
            _fail(FailureCode.INVALID_ARTIFACT_PRODUCER, "proposal adapter is QuantaAlpha-only")


class _ExpressionBuilder:
    def __init__(self, *, field_roles: Mapping[str, FieldRole]) -> None:
        self.field_roles = field_roles
        self.nodes: list[dict[str, Any]] = []
        self._cache: dict[str, str] = {}

    def _node(self, payload: dict[str, Any]) -> str:
        signature = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        existing = self._cache.get(signature)
        if existing is not None:
            return existing
        node_id = f"n{len(self.nodes):03d}"
        self.nodes.append({"id": node_id, **payload})
        self._cache[signature] = node_id
        return node_id

    @staticmethod
    def _number(node: ast.AST, field: str) -> int | float:
        if not isinstance(node, ast.Constant) or isinstance(node.value, bool) or not isinstance(
            node.value, (int, float)
        ):
            _fail(FailureCode.INVALID_FACTOR_DAG, f"{field} must be a numeric literal")
        return node.value

    @staticmethod
    def _positive_int(node: ast.AST, field: str) -> int:
        value = _ExpressionBuilder._number(node, field)
        if not isinstance(value, int) or value < 1:
            _fail(FailureCode.INVALID_FACTOR_DAG, f"{field} must be a positive integer")
        return value

    def build(self, node: ast.AST) -> str:
        if isinstance(node, ast.Name):
            if node.id not in self.field_roles:
                _fail(FailureCode.UNKNOWN_FIELD, f"unknown factor field: {node.id}")
            return self._node({"op": "field", "field": node.id})
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
            _fail(
                FailureCode.INVALID_FACTOR_DAG,
                "factor expression permits only allowlisted function calls and field names",
            )
        if node.keywords:
            _fail(FailureCode.INVALID_FACTOR_DAG, "factor expression keywords are prohibited")
        name = node.func.id
        args = list(node.args)
        if name == "field":
            if len(args) != 1 or not isinstance(args[0], ast.Constant) or not isinstance(
                args[0].value, str
            ):
                _fail(FailureCode.INVALID_FACTOR_DAG, "field requires one string literal")
            field_name = args[0].value
            if field_name not in self.field_roles:
                _fail(FailureCode.UNKNOWN_FIELD, f"unknown factor field: {field_name}")
            return self._node({"op": "field", "field": field_name})
        if name in {"lag", "pct_change"}:
            if len(args) != 2:
                _fail(FailureCode.INVALID_FACTOR_DAG, f"{name} requires value and periods")
            return self._node(
                {
                    "op": name,
                    "inputs": [self.build(args[0])],
                    "periods": self._positive_int(args[1], f"{name}.periods"),
                }
            )
        if name in {"rolling_mean", "rolling_std"}:
            if len(args) != 2:
                _fail(FailureCode.INVALID_FACTOR_DAG, f"{name} requires value and window")
            return self._node(
                {
                    "op": name,
                    "inputs": [self.build(args[0])],
                    "window": self._positive_int(args[1], f"{name}.window"),
                }
            )
        if name in {"subtract", "add", "multiply"}:
            if len(args) != 2:
                _fail(FailureCode.INVALID_FACTOR_DAG, f"{name} requires two factor inputs")
            return self._node({"op": name, "inputs": [self.build(args[0]), self.build(args[1])]})
        if name == "divide_safe":
            if len(args) not in {2, 3}:
                _fail(FailureCode.INVALID_FACTOR_DAG, "divide_safe requires two inputs and optional epsilon")
            payload: dict[str, Any] = {
                "op": name,
                "inputs": [self.build(args[0]), self.build(args[1])],
                "epsilon": self._number(args[2], "divide_safe.epsilon") if len(args) == 3 else 1e-12,
            }
            return self._node(payload)
        if name == "winsorize":
            if len(args) not in {1, 3}:
                _fail(FailureCode.INVALID_FACTOR_DAG, "winsorize requires one input or lower/upper")
            payload = {"op": name, "inputs": [self.build(args[0])], "lower": 0.05, "upper": 0.95}
            if len(args) == 3:
                payload["lower"] = self._number(args[1], "winsorize.lower")
                payload["upper"] = self._number(args[2], "winsorize.upper")
            return self._node(payload)
        if name in {"rank_cross_section", "zscore_cross_section"}:
            if len(args) != 1:
                _fail(FailureCode.INVALID_FACTOR_DAG, f"{name} requires one input")
            return self._node({"op": name, "inputs": [self.build(args[0])]})
        if name == "neutralize_by_group":
            if len(args) != 2:
                _fail(FailureCode.INVALID_FACTOR_DAG, "neutralize_by_group requires value and group")
            return self._node({"op": name, "inputs": [self.build(args[0]), self.build(args[1])]})
        _fail(FailureCode.INVALID_FACTOR_DAG, f"unknown factor expression operator: {name}")


def compile_proposal_expression(
    expression: str | Mapping[str, Any], *, field_roles: Mapping[str, FieldRole]
) -> CompiledFactorDAG:
    """Convert an inert proposal expression into the internal FactorDAG only."""

    _reject_forbidden(expression, "factor_task.expression")
    if isinstance(expression, Mapping):
        return parse_compile_factor_dag(expression, field_roles=field_roles)
    if not isinstance(expression, str) or not expression.strip():
        _fail(FailureCode.INVALID_FACTOR_DAG, "factor expression must be a DSL string or FactorDAG mapping")
    if len(expression) > 4096:
        _fail(FailureCode.INVALID_FACTOR_DAG, "factor expression exceeds the static size limit")
    try:
        parsed = ast.parse(expression, mode="eval")
    except SyntaxError as exc:
        raise ContractValidationError(
            FailureCode.INVALID_FACTOR_DAG,
            "factor expression is not accepted by the restricted parser",
            details={"offset": exc.offset},
        ) from exc
    builder = _ExpressionBuilder(field_roles=field_roles)
    output = builder.build(parsed.body)
    dag_payload = {
        "schema_version": FACTOR_DAG_SCHEMA_VERSION,
        "dag_id": _stable_id("qa-dag", {"expression": expression, "fields": sorted(field_roles)}),
        "nodes": builder.nodes,
        "output_node": output,
    }
    return parse_compile_factor_dag(dag_payload, field_roles=field_roles)


class QuantaAlphaProposalAdapter:
    """Map static AlphaAgentHypothesis/FactorTask payloads to DRAFT artifacts."""

    def __init__(self, *, max_candidates: int = 32) -> None:
        if isinstance(max_candidates, bool) or not isinstance(max_candidates, int) or max_candidates < 1:
            _fail(FailureCode.INVALID_VALUE, "max_candidates must be a positive integer")
        self.max_candidates = max_candidates

    def adapt(self, payload: Mapping[str, Any], *, context: ProposalContext) -> DraftProposal:
        if not isinstance(payload, Mapping):
            _fail(FailureCode.INVALID_VALUE, "proposal payload must be a mapping")
        _reject_forbidden(payload)
        _only_fields(payload, frozenset({"hypothesis", "factor_task"}), "payload")
        hypothesis_payload = payload.get("hypothesis")
        factor_payload = payload.get("factor_task")
        if not isinstance(hypothesis_payload, Mapping):
            _fail(FailureCode.MISSING_REQUIRED_FIELD, "hypothesis object is required")
        if not isinstance(factor_payload, Mapping):
            _fail(FailureCode.MISSING_REQUIRED_FIELD, "factor_task object is required")
        _only_fields(hypothesis_payload, _HYPOTHESIS_FIELDS, "hypothesis")
        _only_fields(factor_payload, _FACTOR_FIELDS, "factor_task")

        statement = _first_text(hypothesis_payload, ("statement", "hypothesis"), "statement")
        mechanism = _first_text(
            hypothesis_payload,
            ("economic_mechanism", "reason", "justification"),
            "economic_mechanism",
        )
        raw_sign = _first_text(
            hypothesis_payload, ("expected_sign", "expected_direction"), "expected_sign"
        )
        sign = raw_sign.strip().upper().replace("-", "_").replace(" ", "_")
        sign = {"UP": "POSITIVE", "DOWN": "NEGATIVE", "NONMONOTONIC": "NON_MONOTONIC"}.get(
            sign, sign
        )
        falsification = hypothesis_payload.get(
            "falsification_tests", hypothesis_payload.get("falsification")
        )
        falsification_tests = _string_tuple(falsification, "falsification_tests")
        hypothesis_id = hypothesis_payload.get("hypothesis_id") or _stable_id(
            "qa-hyp",
            {
                "direction": context.direction_hash,
                "statement": statement,
                "mechanism": mechanism,
                "sign": sign,
            },
        )
        hypothesis = Hypothesis(
            hypothesis_id=hypothesis_id,
            direction_hash=context.direction_hash,
            statement=statement,
            economic_mechanism=mechanism,
            expected_sign=sign,
            falsification_tests=falsification_tests,
            universe=context.universe,
            horizon=context.horizon,
            decision_time=context.decision_time,
            availability_assumptions=context.availability_assumptions,
            state=ResearchState.DRAFT,
        )

        expression = factor_payload.get("expression", factor_payload.get("factor_expression"))
        if expression is None:
            _fail(FailureCode.MISSING_REQUIRED_FIELD, "factor expression is required")
        compiled = compile_proposal_expression(expression, field_roles=context.field_roles)
        declared_fields = factor_payload.get("required_fields", factor_payload.get("variables"))
        if declared_fields is not None:
            if isinstance(declared_fields, Mapping):
                declared = tuple(str(item) for item in declared_fields)
            else:
                declared = _string_tuple(declared_fields, "required_fields")
            if set(declared) != set(compiled.required_fields):
                _fail(
                    FailureCode.INVALID_VALUE,
                    "declared variables must exactly match FactorDAG fields",
                    declared=tuple(sorted(declared)),
                    compiled=compiled.required_fields,
                )
        inferred_lookback = max(
            (
                int(value)
                for node in compiled.nodes
                for key, value in node.parameters.items()
                if key in {"periods", "window"}
            ),
            default=1,
        )
        requested_lookback = factor_payload.get("lookback_bars", inferred_lookback)
        if (
            isinstance(requested_lookback, bool)
            or not isinstance(requested_lookback, int)
            or requested_lookback < inferred_lookback
        ):
            _fail(
                FailureCode.INVALID_VALUE,
                "lookback_bars must be an integer covering every DAG window",
                inferred=inferred_lookback,
            )
        neutralization = _string_tuple(
            factor_payload.get("neutralization", ()), "neutralization", nonempty=False
        )
        name = _first_text(factor_payload, ("factor_name", "name"), "factor name")
        description = _first_text(
            factor_payload,
            ("factor_description", "description", "factor_formulation", "formulation"),
            "factor description",
        )
        draft_id = factor_payload.get("draft_id") or _stable_id(
            "qa-draft",
            {"hypothesis": hypothesis.artifact_hash, "dag": compiled.artifact_hash, "name": name},
        )
        factor_draft = FactorSpecDraft(
            draft_id=draft_id,
            hypothesis_hash=hypothesis.artifact_hash,
            name=name,
            description=description,
            proposed_expression=compiled.canonical_json(),
            required_fields=compiled.required_fields,
            lookback_bars=requested_lookback,
            neutralization=neutralization,
            universe=context.universe,
            horizon=context.horizon,
            decision_time=context.decision_time,
            availability_assumptions=context.availability_assumptions,
            state=ResearchState.DRAFT,
        )
        return DraftProposal(
            hypothesis=hypothesis,
            factor_draft=factor_draft,
            factor_dag=compiled,
        )

    def adapt_candidates(
        self, payloads: Sequence[Mapping[str, Any]], *, context: ProposalContext
    ) -> tuple[DraftProposal, ...]:
        if not isinstance(payloads, (list, tuple)) or not payloads:
            _fail(FailureCode.INVALID_VALUE, "payloads must be a non-empty static sequence")
        if len(payloads) > self.max_candidates:
            _fail(FailureCode.INVALID_VALUE, "static candidate batch exceeds its fixed bound")
        return tuple(self.adapt(payload, context=context) for payload in payloads)
