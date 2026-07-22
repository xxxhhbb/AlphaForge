"""Strict JSON wire format containing typed AST nodes, never expression text."""

from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal
from typing import Mapping

from ..qlib_expression.api import (
    CertifiedQlibExpression,
    QlibExpressionPolicy,
    QlibFieldRole,
    STATIC_QLIB_OPERATOR_MANIFEST,
    canonicalize_expression,
)
from ..qlib_expression.models import (
    BinaryNode,
    CallNode,
    ExpressionNode,
    FieldNode,
    NumberNode,
    StringNode,
    UnaryNode,
    stable_hash,
)
from ..qlib_expression.validator import _Validation
from ..qlib_operator_worker.api import (
    QlibFactorScoreArtifact,
    QlibFactorScoreRow,
    QlibInMemoryFeatureRow,
    QlibOperatorExecutionRequest,
    QlibOperatorWorkerFailure,
    QlibOperatorWorkerFailureCode,
    QlibRuntimeIdentity,
    inspect_certified_program,
)
from .models import QlibSubprocessFailure, QlibSubprocessFailureCode, SUBPROCESS_SCHEMA_VERSION


class WireFormatError(ValueError):
    pass


def _mapping(value: object, name: str) -> Mapping[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise WireFormatError(f"{name} must be an object")
    return value


def _exact(value: Mapping[str, object], keys: set[str], name: str) -> None:
    if set(value) != keys:
        raise WireFormatError(f"{name} has unknown or missing fields")


def node_to_wire(node: ExpressionNode) -> dict[str, object]:
    if isinstance(node, FieldNode):
        return {"type": "field", "name": node.name}
    if isinstance(node, NumberNode):
        return {"type": "number", "value": node.canonical_value}
    if isinstance(node, StringNode):
        return {"type": "string", "value": node.value}
    if isinstance(node, UnaryNode):
        return {"type": "unary", "operator": node.operator, "operand": node_to_wire(node.operand)}
    if isinstance(node, BinaryNode):
        return {
            "type": "binary",
            "operator": node.operator,
            "left": node_to_wire(node.left),
            "right": node_to_wire(node.right),
        }
    if isinstance(node, CallNode):
        return {"type": "call", "name": node.name, "arguments": [node_to_wire(item) for item in node.arguments]}
    raise WireFormatError("unsupported AST node")


def node_from_wire(
    value: object,
    *,
    depth: int = 0,
    max_depth: int = 64,
    max_arguments: int = 16,
) -> ExpressionNode:
    if depth > max_depth:
        raise WireFormatError("AST nesting exceeds subprocess protocol")
    item = _mapping(value, "AST node")
    kind = item.get("type")
    if kind == "field":
        _exact(item, {"type", "name"}, "field node")
        if not isinstance(item["name"], str):
            raise WireFormatError("field name must be a string")
        return FieldNode(item["name"])
    if kind == "number":
        _exact(item, {"type", "value"}, "number node")
        raw = item["value"]
        if not isinstance(raw, str):
            raise WireFormatError("number value must be canonical text")
        try:
            if not Decimal(raw).is_finite():
                raise WireFormatError("number must be finite")
        except Exception as exc:
            if isinstance(exc, WireFormatError):
                raise
            raise WireFormatError("invalid number literal") from exc
        return NumberNode(raw)
    if kind == "string":
        _exact(item, {"type", "value"}, "string node")
        if not isinstance(item["value"], str) or len(item["value"]) > 256:
            raise WireFormatError("string node is invalid")
        return StringNode(item["value"])
    if kind == "unary":
        _exact(item, {"type", "operator", "operand"}, "unary node")
        if item["operator"] not in {"+", "-"}:
            raise WireFormatError("unknown unary operator")
        return UnaryNode(
            str(item["operator"]),
            node_from_wire(item["operand"], depth=depth + 1, max_depth=max_depth, max_arguments=max_arguments),
        )
    if kind == "binary":
        _exact(item, {"type", "operator", "left", "right"}, "binary node")
        if item["operator"] not in {"+", "-", "*", "/", "**", ">", ">=", "<", "<=", "==", "!=", "&", "|"}:
            raise WireFormatError("unknown binary operator")
        return BinaryNode(
            str(item["operator"]),
            node_from_wire(item["left"], depth=depth + 1, max_depth=max_depth, max_arguments=max_arguments),
            node_from_wire(item["right"], depth=depth + 1, max_depth=max_depth, max_arguments=max_arguments),
        )
    if kind == "call":
        _exact(item, {"type", "name", "arguments"}, "call node")
        name = item["name"]
        arguments = item["arguments"]
        if not isinstance(name, str) or not isinstance(arguments, list) or len(arguments) > max_arguments:
            raise WireFormatError("call node is invalid")
        return CallNode(
            name,
            tuple(
                node_from_wire(argument, depth=depth + 1, max_depth=max_depth, max_arguments=max_arguments)
                for argument in arguments
            ),
        )
    raise WireFormatError("unknown AST node type")


def request_to_wire(request: QlibOperatorExecutionRequest, policy: QlibExpressionPolicy) -> dict[str, object]:
    if type(request) is not QlibOperatorExecutionRequest or type(policy) is not QlibExpressionPolicy:
        raise WireFormatError("typed request and policy are required")
    if request.program.policy_hash != policy.policy_hash:
        raise WireFormatError("policy does not bind certified program")
    return {
        "schema_version": SUBPROCESS_SCHEMA_VERSION,
        "expected_input_hash": request.input_hash,
        "program": {
            "schema_version": request.program.schema_version,
            "expression_hash": request.program.expression_hash,
            "manifest_hash": request.program.manifest_hash,
            "policy_hash": request.program.policy_hash,
            "required_fields": list(request.program.required_fields),
            "operators": list(request.program.operators),
            "maximum_lookback": request.program.maximum_lookback,
            "node_count": request.program.node_count,
            "root": node_to_wire(request.program.root),
        },
        "policy": policy.to_canonical(),
        "snapshot_hash": request.snapshot_hash,
        "universe_hash": request.universe_hash,
        "output_symbols": list(request.output_symbols),
        "rows": [row.to_canonical() for row in request.rows],
    }


def _policy_from_wire(value: object) -> QlibExpressionPolicy:
    item = _mapping(value, "policy")
    expected = {
        "schema_version", "field_roles", "allowed_operators", "approved_instruments", "max_expression_length",
        "max_nodes", "max_depth", "max_arguments", "max_window", "max_ref_periods", "allow_unbounded_windows",
    }
    _exact(item, expected, "policy")
    roles = _mapping(item["field_roles"], "field_roles")
    return QlibExpressionPolicy(
        schema_version=item["schema_version"],  # type: ignore[arg-type]
        field_roles={str(key): QlibFieldRole(value) for key, value in roles.items()},
        allowed_operators=tuple(item["allowed_operators"]),  # type: ignore[arg-type]
        approved_instruments=tuple(item["approved_instruments"]),  # type: ignore[arg-type]
        max_expression_length=item["max_expression_length"],  # type: ignore[arg-type]
        max_nodes=item["max_nodes"],  # type: ignore[arg-type]
        max_depth=item["max_depth"],  # type: ignore[arg-type]
        max_arguments=item["max_arguments"],  # type: ignore[arg-type]
        max_window=item["max_window"],  # type: ignore[arg-type]
        max_ref_periods=item["max_ref_periods"],  # type: ignore[arg-type]
        allow_unbounded_windows=item["allow_unbounded_windows"],  # type: ignore[arg-type]
    )


def request_from_wire(value: object) -> tuple[QlibOperatorExecutionRequest, QlibExpressionPolicy]:
    item = _mapping(value, "request")
    _exact(
        item,
        {"schema_version", "expected_input_hash", "program", "policy", "snapshot_hash", "universe_hash", "output_symbols", "rows"},
        "request",
    )
    if item["schema_version"] != SUBPROCESS_SCHEMA_VERSION:
        raise WireFormatError("subprocess schema mismatch")
    program_wire = _mapping(item["program"], "program")
    _exact(
        program_wire,
        {"schema_version", "expression_hash", "manifest_hash", "policy_hash", "required_fields", "operators", "maximum_lookback", "node_count", "root"},
        "program",
    )
    policy = _policy_from_wire(item["policy"])
    root = node_from_wire(
        program_wire["root"],
        max_depth=policy.max_depth,
        max_arguments=policy.max_arguments,
    )
    canonical = canonicalize_expression(root)
    program = CertifiedQlibExpression(
        schema_version=program_wire["schema_version"],  # type: ignore[arg-type]
        canonical_expression=canonical,
        expression_hash=program_wire["expression_hash"],  # type: ignore[arg-type]
        manifest_hash=program_wire["manifest_hash"],  # type: ignore[arg-type]
        policy_hash=program_wire["policy_hash"],  # type: ignore[arg-type]
        required_fields=tuple(program_wire["required_fields"]),  # type: ignore[arg-type]
        operators=tuple(program_wire["operators"]),  # type: ignore[arg-type]
        maximum_lookback=program_wire["maximum_lookback"],  # type: ignore[arg-type]
        node_count=program_wire["node_count"],  # type: ignore[arg-type]
        root=root,
    )
    if program.manifest_hash != STATIC_QLIB_OPERATOR_MANIFEST.manifest_hash:
        raise WireFormatError("operator manifest is not pinned")
    if program.policy_hash != policy.policy_hash:
        raise WireFormatError("policy hash mismatch")
    identity = {
        "schema_version": program.schema_version,
        "canonical_expression": canonical,
        "manifest_hash": program.manifest_hash,
        "policy_hash": program.policy_hash,
    }
    if stable_hash(identity) != program.expression_hash:
        raise WireFormatError("canonical program hash mismatch")
    inspect_certified_program(program)
    validation = _Validation(policy, STATIC_QLIB_OPERATOR_MANIFEST)
    maximum_lookback = validation.visit(root)
    if (
        tuple(sorted(validation.fields)) != program.required_fields
        or tuple(sorted(validation.operators)) != program.operators
        or validation.node_count != program.node_count
        or maximum_lookback != program.maximum_lookback
    ):
        raise WireFormatError("structured program metadata failed policy revalidation")
    if validation.node_count > policy.max_nodes:
        raise WireFormatError("structured program exceeds policy node limit")

    rows_wire = item["rows"]
    if not isinstance(rows_wire, list):
        raise WireFormatError("rows must be an array")
    rows = []
    for raw_row in rows_wire:
        row = _mapping(raw_row, "row")
        _exact(row, {"event_time", "symbol", "values"}, "row")
        if not isinstance(row["event_time"], str) or not isinstance(row["symbol"], str):
            raise WireFormatError("row identity is invalid")
        rows.append(
            QlibInMemoryFeatureRow(
                event_time=datetime.fromisoformat(row["event_time"]),
                symbol=row["symbol"],
                values=_mapping(row["values"], "row values"),  # type: ignore[arg-type]
            )
        )
    if not isinstance(item["output_symbols"], list) or not all(isinstance(symbol, str) for symbol in item["output_symbols"]):
        raise WireFormatError("output_symbols must be an array of strings")
    request = QlibOperatorExecutionRequest(
        program=program,
        snapshot_hash=item["snapshot_hash"],  # type: ignore[arg-type]
        universe_hash=item["universe_hash"],  # type: ignore[arg-type]
        output_symbols=tuple(item["output_symbols"]),
        rows=tuple(rows),
    )
    if request.input_hash != item["expected_input_hash"]:
        raise WireFormatError("request input hash mismatch")
    return request, policy


def artifact_to_wire(artifact: QlibFactorScoreArtifact) -> dict[str, object]:
    return {
        "kind": "artifact",
        "artifact": {
            "program_hash": artifact.program_hash,
            "snapshot_hash": artifact.snapshot_hash,
            "universe_hash": artifact.universe_hash,
            "input_hash": artifact.input_hash,
            "runtime_hash": artifact.runtime_hash,
            "output_hash": artifact.output_hash,
            "missing_scores": artifact.missing_scores,
            "artifact_class": artifact.artifact_class,
            "rows": [row.to_canonical() for row in artifact.rows],
        },
    }


def worker_failure_to_wire(failure: QlibOperatorWorkerFailure) -> dict[str, object]:
    return {
        "kind": "worker_failure",
        "failure": {
            "code": failure.code.value,
            "message": failure.message,
            "program_hash": failure.program_hash,
            "input_hash": failure.input_hash,
            "retryable": failure.retryable,
        },
    }


def subprocess_failure_to_wire(failure: QlibSubprocessFailure) -> dict[str, object]:
    return {
        "kind": "protocol_failure",
        "failure": {
            "code": failure.code.value,
            "message": failure.message,
            "program_hash": failure.program_hash,
            "input_hash": failure.input_hash,
            "retryable": failure.retryable,
        },
    }


def result_from_wire(value: object, *, expected_program_hash: str, expected_input_hash: str) -> QlibFactorScoreArtifact | QlibSubprocessFailure:
    item = _mapping(value, "worker result")
    kind = item.get("kind")
    if kind == "artifact":
        _exact(item, {"kind", "artifact"}, "artifact result")
        raw = _mapping(item["artifact"], "artifact")
        _exact(raw, {"program_hash", "snapshot_hash", "universe_hash", "input_hash", "runtime_hash", "output_hash", "missing_scores", "artifact_class", "rows"}, "artifact")
        raw_rows = raw["rows"]
        if not isinstance(raw_rows, list):
            raise WireFormatError("artifact rows must be an array")
        rows = []
        for value_row in raw_rows:
            row = _mapping(value_row, "score row")
            _exact(row, {"event_time", "symbol", "score"}, "score row")
            rows.append(QlibFactorScoreRow(datetime.fromisoformat(row["event_time"]), row["symbol"], row["score"]))  # type: ignore[arg-type]
        artifact = QlibFactorScoreArtifact(
            program_hash=raw["program_hash"], snapshot_hash=raw["snapshot_hash"], universe_hash=raw["universe_hash"],  # type: ignore[arg-type]
            input_hash=raw["input_hash"], runtime_hash=raw["runtime_hash"], output_hash=raw["output_hash"],  # type: ignore[arg-type]
            rows=tuple(rows), missing_scores=raw["missing_scores"], artifact_class=raw["artifact_class"],  # type: ignore[arg-type]
        )
        if artifact.program_hash != expected_program_hash or artifact.input_hash != expected_input_hash:
            raise WireFormatError("artifact does not bind expected request")
        if artifact.runtime_hash != QlibRuntimeIdentity.expected().runtime_hash:
            raise WireFormatError("artifact does not bind pinned Qlib runtime")
        return artifact
    if kind in {"worker_failure", "protocol_failure"}:
        _exact(item, {"kind", "failure"}, "failure result")
        raw = _mapping(item["failure"], "failure")
        _exact(raw, {"code", "message", "program_hash", "input_hash", "retryable"}, "failure")
        if raw["program_hash"] != expected_program_hash or raw["input_hash"] != expected_input_hash:
            raise WireFormatError("failure does not bind expected request")
        worker_code = None
        if kind == "worker_failure":
            code = QlibSubprocessFailureCode.WORKER_EXECUTION_FAILED
            worker_code = QlibOperatorWorkerFailureCode(raw["code"])
            message = f"{worker_code.value}: {raw['message']}"
        else:
            code = QlibSubprocessFailureCode(raw["code"])
            message = str(raw["message"])
        return QlibSubprocessFailure(
            code,
            message,
            expected_program_hash,
            expected_input_hash,
            bool(raw["retryable"]),
            worker_code,
        )
    raise WireFormatError("unknown worker result kind")


def dumps(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def loads(value: bytes) -> object:
    return json.loads(value.decode("utf-8"))


__all__ = [
    "WireFormatError", "artifact_to_wire", "dumps", "loads", "node_from_wire", "node_to_wire",
    "request_from_wire", "request_to_wire", "result_from_wire", "subprocess_failure_to_wire", "worker_failure_to_wire",
]
