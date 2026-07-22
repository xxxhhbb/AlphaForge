"""Restricted, deterministic FactorDAG parser, compiler, and interpreter."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from statistics import fmean, pstdev
from types import MappingProxyType
from typing import Any, Iterable, Mapping

from ..contracts import CanonicalContract, ContractValidationError, FailureCode
from ..contracts.base import require_aware_datetime, require_hash, require_identifier
from ..data import FieldRole, MarketSnapshot, canonical_content_hash


FACTOR_DAG_SCHEMA_VERSION = "veriquant.factor-dag/v1"
MAX_DAG_NODES = 64
MAX_WINDOW = 252


class FactorOperator(str, Enum):
    FIELD = "field"
    LAG = "lag"
    ROLLING_MEAN = "rolling_mean"
    ROLLING_STD = "rolling_std"
    PCT_CHANGE = "pct_change"
    SUBTRACT = "subtract"
    ADD = "add"
    MULTIPLY = "multiply"
    DIVIDE_SAFE = "divide_safe"
    WINSORIZE = "winsorize"
    RANK_CROSS_SECTION = "rank_cross_section"
    ZSCORE_CROSS_SECTION = "zscore_cross_section"
    NEUTRALIZE_BY_GROUP = "neutralize_by_group"


def _raise(code: FailureCode, message: str, **details: Any) -> None:
    raise ContractValidationError(code, message, details=details)


def _reject_executable_values(value: Any, path: str = "payload") -> None:
    if callable(value):
        _raise(FailureCode.INVALID_FACTOR_DAG, "Python callables are prohibited", path=path)
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                _raise(FailureCode.INVALID_FACTOR_DAG, "FactorDAG keys must be strings", path=path)
            _reject_executable_values(item, f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _reject_executable_values(item, f"{path}[{index}]")
    elif value is not None and not isinstance(value, (str, int, float, bool)):
        _raise(
            FailureCode.INVALID_FACTOR_DAG,
            f"unsupported FactorDAG value type: {type(value).__name__}",
            path=path,
        )


@dataclass(frozen=True, kw_only=True)
class CompiledNode:
    node_id: str
    operator: FactorOperator
    inputs: tuple[str, ...]
    parameters: Mapping[str, str | int | float]

    def __post_init__(self) -> None:
        require_identifier(self.node_id, "node_id")
        if not isinstance(self.operator, FactorOperator):
            _raise(FailureCode.INVALID_FACTOR_DAG, "compiled node has invalid operator")
        object.__setattr__(self, "parameters", MappingProxyType(dict(self.parameters)))


@dataclass(frozen=True, kw_only=True)
class CompiledFactorDAG(CanonicalContract):
    dag_id: str
    schema_version_id: str
    nodes: tuple[CompiledNode, ...]
    output_node: str
    required_fields: tuple[str, ...]

    def __post_init__(self) -> None:
        require_identifier(self.dag_id, "dag_id")
        if self.schema_version_id != FACTOR_DAG_SCHEMA_VERSION:
            _raise(FailureCode.INVALID_SCHEMA_VERSION, "compiled FactorDAG schema mismatch")
        if not self.nodes or len(self.nodes) > MAX_DAG_NODES:
            _raise(FailureCode.INVALID_FACTOR_DAG, "compiled FactorDAG size is invalid")
        if self.output_node not in {node.node_id for node in self.nodes}:
            _raise(FailureCode.INVALID_FACTOR_DAG, "compiled output node is missing")


def _expect_keys(node: Mapping[str, Any], allowed: set[str], node_id: str) -> None:
    unknown = set(node) - allowed
    if unknown:
        _raise(
            FailureCode.UNKNOWN_FIELD,
            "FactorDAG node contains unknown fields",
            node=node_id,
            fields=tuple(sorted(unknown)),
        )


def _inputs(node: Mapping[str, Any], node_id: str, count: int) -> tuple[str, ...]:
    raw = node.get("inputs")
    if not isinstance(raw, list) or len(raw) != count or any(not isinstance(item, str) for item in raw):
        _raise(
            FailureCode.INVALID_FACTOR_DAG,
            f"{node_id} requires exactly {count} string inputs",
        )
    return tuple(raw)


def _positive_window(node: Mapping[str, Any], node_id: str, name: str) -> int:
    value = node.get(name)
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= MAX_WINDOW:
        _raise(
            FailureCode.INVALID_FACTOR_DAG,
            f"{node_id}.{name} must be an integer from 1 to {MAX_WINDOW}",
        )
    return value


def parse_compile_factor_dag(
    payload: Mapping[str, Any], *, field_roles: Mapping[str, FieldRole]
) -> CompiledFactorDAG:
    """Strictly parse an inert mapping into a topologically ordered program."""

    if not isinstance(payload, Mapping):
        _raise(FailureCode.INVALID_FACTOR_DAG, "FactorDAG payload must be a mapping")
    _reject_executable_values(payload)
    root_allowed = {"schema_version", "dag_id", "nodes", "output_node"}
    root_unknown = set(payload) - root_allowed
    if root_unknown:
        _raise(
            FailureCode.UNKNOWN_FIELD,
            "FactorDAG contains unknown root fields",
            fields=tuple(sorted(root_unknown)),
        )
    if payload.get("schema_version") != FACTOR_DAG_SCHEMA_VERSION:
        _raise(FailureCode.INVALID_SCHEMA_VERSION, "unsupported FactorDAG schema version")
    dag_id = payload.get("dag_id")
    if not isinstance(dag_id, str):
        _raise(FailureCode.INVALID_IDENTIFIER, "dag_id must be a string")
    require_identifier(dag_id, "dag_id")
    raw_nodes = payload.get("nodes")
    if not isinstance(raw_nodes, list) or not raw_nodes or len(raw_nodes) > MAX_DAG_NODES:
        _raise(FailureCode.INVALID_FACTOR_DAG, "nodes must be a non-empty size-bounded list")
    output_node = payload.get("output_node")
    if not isinstance(output_node, str):
        _raise(FailureCode.INVALID_FACTOR_DAG, "output_node must be a string")

    parsed: dict[str, CompiledNode] = {}
    declaration_order: list[str] = []
    required_fields: set[str] = set()
    for index, raw_node in enumerate(raw_nodes):
        if not isinstance(raw_node, Mapping):
            _raise(FailureCode.INVALID_FACTOR_DAG, "each FactorDAG node must be a mapping")
        node_id = raw_node.get("id")
        if not isinstance(node_id, str):
            _raise(FailureCode.INVALID_IDENTIFIER, f"node {index} has no valid id")
        require_identifier(node_id, "node_id")
        if node_id in parsed:
            _raise(FailureCode.INVALID_FACTOR_DAG, "duplicate node id", node=node_id)
        raw_operator = raw_node.get("op")
        try:
            operator = FactorOperator(raw_operator)
        except (TypeError, ValueError) as exc:
            raise ContractValidationError(
                FailureCode.INVALID_FACTOR_DAG,
                f"unknown FactorDAG operator: {raw_operator!r}",
                details={"node": node_id, "operator": raw_operator},
            ) from exc

        parameters: dict[str, str | int | float] = {}
        if operator is FactorOperator.FIELD:
            _expect_keys(raw_node, {"id", "op", "field"}, node_id)
            field_name = raw_node.get("field")
            if not isinstance(field_name, str) or field_name not in field_roles:
                _raise(FailureCode.UNKNOWN_FIELD, f"unknown FactorDAG field: {field_name}")
            if field_roles[field_name] is FieldRole.LABEL:
                _raise(
                    FailureCode.UNAUTHORIZED_CAPABILITY,
                    f"label field is prohibited in FactorDAG features: {field_name}",
                )
            inputs: tuple[str, ...] = ()
            parameters["field"] = field_name
            required_fields.add(field_name)
        elif operator in {FactorOperator.LAG, FactorOperator.PCT_CHANGE}:
            parameter_name = "periods"
            _expect_keys(raw_node, {"id", "op", "inputs", parameter_name}, node_id)
            inputs = _inputs(raw_node, node_id, 1)
            parameters[parameter_name] = _positive_window(raw_node, node_id, parameter_name)
        elif operator in {FactorOperator.ROLLING_MEAN, FactorOperator.ROLLING_STD}:
            _expect_keys(raw_node, {"id", "op", "inputs", "window"}, node_id)
            inputs = _inputs(raw_node, node_id, 1)
            window = _positive_window(raw_node, node_id, "window")
            if operator is FactorOperator.ROLLING_STD and window < 2:
                _raise(FailureCode.INVALID_FACTOR_DAG, "rolling_std window must be at least 2")
            parameters["window"] = window
        elif operator in {
            FactorOperator.SUBTRACT,
            FactorOperator.ADD,
            FactorOperator.MULTIPLY,
        }:
            _expect_keys(raw_node, {"id", "op", "inputs"}, node_id)
            inputs = _inputs(raw_node, node_id, 2)
        elif operator is FactorOperator.DIVIDE_SAFE:
            _expect_keys(raw_node, {"id", "op", "inputs", "epsilon"}, node_id)
            inputs = _inputs(raw_node, node_id, 2)
            epsilon = raw_node.get("epsilon", 1e-12)
            if isinstance(epsilon, bool) or not isinstance(epsilon, (int, float)) or not math.isfinite(epsilon) or epsilon <= 0:
                _raise(FailureCode.INVALID_FACTOR_DAG, "divide_safe epsilon must be finite and positive")
            parameters["epsilon"] = float(epsilon)
        elif operator is FactorOperator.WINSORIZE:
            _expect_keys(raw_node, {"id", "op", "inputs", "lower", "upper"}, node_id)
            inputs = _inputs(raw_node, node_id, 1)
            lower = raw_node.get("lower", 0.05)
            upper = raw_node.get("upper", 0.95)
            if (
                isinstance(lower, bool)
                or isinstance(upper, bool)
                or not isinstance(lower, (int, float))
                or not isinstance(upper, (int, float))
                or not (0.0 <= float(lower) < float(upper) <= 1.0)
            ):
                _raise(FailureCode.INVALID_FACTOR_DAG, "winsorize quantiles are invalid")
            parameters.update(lower=float(lower), upper=float(upper))
        elif operator in {
            FactorOperator.RANK_CROSS_SECTION,
            FactorOperator.ZSCORE_CROSS_SECTION,
        }:
            _expect_keys(raw_node, {"id", "op", "inputs"}, node_id)
            inputs = _inputs(raw_node, node_id, 1)
        elif operator is FactorOperator.NEUTRALIZE_BY_GROUP:
            _expect_keys(raw_node, {"id", "op", "inputs"}, node_id)
            inputs = _inputs(raw_node, node_id, 2)
        else:  # pragma: no cover - exhaustive enum guard
            _raise(FailureCode.INVALID_FACTOR_DAG, "operator has no compiler implementation")
        parsed[node_id] = CompiledNode(
            node_id=node_id,
            operator=operator,
            inputs=inputs,
            parameters=parameters,
        )
        declaration_order.append(node_id)

    if output_node not in parsed:
        _raise(FailureCode.INVALID_FACTOR_DAG, "output_node does not identify a node")
    for node in parsed.values():
        missing = [item for item in node.inputs if item not in parsed]
        if missing:
            _raise(
                FailureCode.INVALID_FACTOR_DAG,
                "FactorDAG references missing nodes",
                node=node.node_id,
                inputs=tuple(missing),
            )

    visiting: set[str] = set()
    visited: set[str] = set()
    ordered: list[CompiledNode] = []

    def visit(node_id: str) -> None:
        if node_id in visiting:
            _raise(FailureCode.INVALID_FACTOR_DAG, "FactorDAG contains a cycle", node=node_id)
        if node_id in visited:
            return
        visiting.add(node_id)
        for dependency in parsed[node_id].inputs:
            visit(dependency)
        visiting.remove(node_id)
        visited.add(node_id)
        ordered.append(parsed[node_id])

    for node_id in declaration_order:
        visit(node_id)
    return CompiledFactorDAG(
        dag_id=dag_id,
        schema_version_id=FACTOR_DAG_SCHEMA_VERSION,
        nodes=tuple(ordered),
        output_node=output_node,
        required_fields=tuple(sorted(required_fields)),
    )


@dataclass(frozen=True, kw_only=True)
class FactorValue:
    event_time: datetime
    symbol: str
    value: float | None

    def __post_init__(self) -> None:
        require_aware_datetime(self.event_time, "event_time")
        require_identifier(self.symbol, "symbol")
        if self.value is not None and not math.isfinite(self.value):
            _raise(FailureCode.NON_FINITE_NUMBER, "factor values must be finite or missing")


@dataclass(frozen=True, kw_only=True)
class FactorResult(CanonicalContract):
    dag_hash: str
    snapshot_hash: str
    config_hash: str
    seed: int
    values: tuple[FactorValue, ...]

    def __post_init__(self) -> None:
        require_hash(self.dag_hash, "dag_hash")
        require_hash(self.snapshot_hash, "snapshot_hash")
        require_hash(self.config_hash, "config_hash")
        if isinstance(self.seed, bool) or not isinstance(self.seed, int) or self.seed < 0:
            _raise(FailureCode.INVALID_VALUE, "seed must be a non-negative integer")


SeriesKey = tuple[datetime, str]
Series = dict[SeriesKey, Any]


def _number(value: Any, *, node_id: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _raise(FailureCode.INVALID_FACTOR_DAG, "numeric operator received non-numeric data", node=node_id)
    converted = float(value)
    if not math.isfinite(converted):
        _raise(FailureCode.NON_FINITE_NUMBER, "numeric operator received non-finite data", node=node_id)
    return converted


def _group_keys_by_symbol(keys: Iterable[SeriesKey]) -> dict[str, list[SeriesKey]]:
    grouped: dict[str, list[SeriesKey]] = {}
    for key in keys:
        grouped.setdefault(key[1], []).append(key)
    for values in grouped.values():
        values.sort(key=lambda item: item[0])
    return grouped


def _group_keys_by_time(keys: Iterable[SeriesKey]) -> dict[datetime, list[SeriesKey]]:
    grouped: dict[datetime, list[SeriesKey]] = {}
    for key in keys:
        grouped.setdefault(key[0], []).append(key)
    for values in grouped.values():
        values.sort(key=lambda item: item[1])
    return grouped


def _quantile(sorted_values: list[float], probability: float) -> float:
    if len(sorted_values) == 1:
        return sorted_values[0]
    position = probability * (len(sorted_values) - 1)
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return sorted_values[lower]
    weight = position - lower
    return sorted_values[lower] * (1.0 - weight) + sorted_values[upper] * weight


def execute_factor_dag(
    dag: CompiledFactorDAG,
    snapshot: MarketSnapshot,
    *,
    seed: int = 0,
    config: Mapping[str, Any] | None = None,
) -> FactorResult:
    """Interpret a compiled DAG; no generated source or callable is executed."""

    if not isinstance(dag, CompiledFactorDAG) or not isinstance(snapshot, MarketSnapshot):
        _raise(FailureCode.INVALID_FACTOR_DAG, "executor requires compiled DAG and MarketSnapshot")
    missing_fields = set(dag.required_fields) - set(snapshot.fields)
    if missing_fields:
        _raise(
            FailureCode.UNKNOWN_FIELD,
            "snapshot lacks FactorDAG fields",
            fields=tuple(sorted(missing_fields)),
        )
    if isinstance(seed, bool) or not isinstance(seed, int) or seed < 0:
        _raise(FailureCode.INVALID_VALUE, "seed must be a non-negative integer")
    execution_config = dict(config or {})
    _reject_executable_values(execution_config, "config")
    keys = [(row.event_time, row.symbol) for row in snapshot.rows]
    row_values = {(row.event_time, row.symbol): row.values for row in snapshot.rows}
    results: dict[str, Series] = {}

    for node in dag.nodes:
        op = node.operator
        if op is FactorOperator.FIELD:
            field_name = str(node.parameters["field"])
            series = {key: row_values[key].get(field_name) for key in keys}
        elif op is FactorOperator.LAG:
            source = results[node.inputs[0]]
            periods = int(node.parameters["periods"])
            series = {}
            for symbol_keys in _group_keys_by_symbol(keys).values():
                for index, key in enumerate(symbol_keys):
                    series[key] = source[symbol_keys[index - periods]] if index >= periods else None
        elif op in {FactorOperator.ROLLING_MEAN, FactorOperator.ROLLING_STD}:
            source = results[node.inputs[0]]
            window = int(node.parameters["window"])
            series = {}
            for symbol_keys in _group_keys_by_symbol(keys).values():
                for index, key in enumerate(symbol_keys):
                    if index + 1 < window:
                        series[key] = None
                        continue
                    values = [
                        _number(source[item], node_id=node.node_id)
                        for item in symbol_keys[index - window + 1 : index + 1]
                    ]
                    if any(value is None for value in values):
                        series[key] = None
                    else:
                        numeric = [value for value in values if value is not None]
                        series[key] = fmean(numeric) if op is FactorOperator.ROLLING_MEAN else pstdev(numeric)
        elif op is FactorOperator.PCT_CHANGE:
            source = results[node.inputs[0]]
            periods = int(node.parameters["periods"])
            series = {}
            for symbol_keys in _group_keys_by_symbol(keys).values():
                for index, key in enumerate(symbol_keys):
                    if index < periods:
                        series[key] = None
                        continue
                    current = _number(source[key], node_id=node.node_id)
                    previous = _number(source[symbol_keys[index - periods]], node_id=node.node_id)
                    series[key] = (
                        None
                        if current is None or previous is None or previous == 0.0
                        else current / previous - 1.0
                    )
        elif op in {
            FactorOperator.SUBTRACT,
            FactorOperator.ADD,
            FactorOperator.MULTIPLY,
            FactorOperator.DIVIDE_SAFE,
        }:
            left = results[node.inputs[0]]
            right = results[node.inputs[1]]
            series = {}
            for key in keys:
                left_value = _number(left[key], node_id=node.node_id)
                right_value = _number(right[key], node_id=node.node_id)
                if left_value is None or right_value is None:
                    series[key] = None
                elif op is FactorOperator.SUBTRACT:
                    series[key] = left_value - right_value
                elif op is FactorOperator.ADD:
                    series[key] = left_value + right_value
                elif op is FactorOperator.MULTIPLY:
                    series[key] = left_value * right_value
                else:
                    epsilon = float(node.parameters["epsilon"])
                    series[key] = None if abs(right_value) <= epsilon else left_value / right_value
        elif op is FactorOperator.WINSORIZE:
            source = results[node.inputs[0]]
            series = {}
            for time_keys in _group_keys_by_time(keys).values():
                numeric_by_key = {
                    key: _number(source[key], node_id=node.node_id) for key in time_keys
                }
                valid = sorted(value for value in numeric_by_key.values() if value is not None)
                if not valid:
                    series.update({key: None for key in time_keys})
                    continue
                lower = _quantile(valid, float(node.parameters["lower"]))
                upper = _quantile(valid, float(node.parameters["upper"]))
                for key, value in numeric_by_key.items():
                    series[key] = None if value is None else min(max(value, lower), upper)
        elif op is FactorOperator.RANK_CROSS_SECTION:
            source = results[node.inputs[0]]
            series = {}
            for time_keys in _group_keys_by_time(keys).values():
                numeric_by_key = {
                    key: _number(source[key], node_id=node.node_id) for key in time_keys
                }
                valid_pairs = sorted(
                    ((value, key) for key, value in numeric_by_key.items() if value is not None),
                    key=lambda item: (item[0], item[1][1]),
                )
                ranks: dict[SeriesKey, float] = {}
                cursor = 0
                while cursor < len(valid_pairs):
                    end = cursor + 1
                    while end < len(valid_pairs) and valid_pairs[end][0] == valid_pairs[cursor][0]:
                        end += 1
                    average_rank = (cursor + end - 1) / 2.0
                    percentile = 0.5 if len(valid_pairs) == 1 else average_rank / (len(valid_pairs) - 1)
                    for _, key in valid_pairs[cursor:end]:
                        ranks[key] = percentile
                    cursor = end
                for key in time_keys:
                    series[key] = ranks.get(key)
        elif op is FactorOperator.ZSCORE_CROSS_SECTION:
            source = results[node.inputs[0]]
            series = {}
            for time_keys in _group_keys_by_time(keys).values():
                numeric_by_key = {
                    key: _number(source[key], node_id=node.node_id) for key in time_keys
                }
                valid = [value for value in numeric_by_key.values() if value is not None]
                deviation = pstdev(valid) if len(valid) >= 2 else 0.0
                mean = fmean(valid) if valid else 0.0
                for key, value in numeric_by_key.items():
                    series[key] = None if value is None or deviation == 0.0 else (value - mean) / deviation
        elif op is FactorOperator.NEUTRALIZE_BY_GROUP:
            source = results[node.inputs[0]]
            groups = results[node.inputs[1]]
            series = {}
            for time_keys in _group_keys_by_time(keys).values():
                grouped_values: dict[str, list[float]] = {}
                numeric_by_key: dict[SeriesKey, float | None] = {}
                group_by_key: dict[SeriesKey, str | None] = {}
                for key in time_keys:
                    value = _number(source[key], node_id=node.node_id)
                    group_value = groups[key]
                    group = None if group_value is None else str(group_value)
                    numeric_by_key[key] = value
                    group_by_key[key] = group
                    if value is not None and group is not None:
                        grouped_values.setdefault(group, []).append(value)
                means = {group: fmean(values) for group, values in grouped_values.items()}
                for key in time_keys:
                    value = numeric_by_key[key]
                    group = group_by_key[key]
                    series[key] = (
                        None if value is None or group is None or group not in means else value - means[group]
                    )
        else:  # pragma: no cover - exhaustive enum guard
            _raise(FailureCode.INVALID_FACTOR_DAG, "operator has no executor implementation")
        results[node.node_id] = series

    output = results[dag.output_node]
    factor_values = tuple(
        FactorValue(
            event_time=row.event_time,
            symbol=row.symbol,
            value=None
            if output[(row.event_time, row.symbol)] is None
            else float(output[(row.event_time, row.symbol)]),
        )
        for row in snapshot.rows
    )
    return FactorResult(
        dag_hash=dag.artifact_hash,
        snapshot_hash=snapshot.artifact_hash,
        config_hash=canonical_content_hash(execution_config),
        seed=seed,
        values=factor_values,
    )

