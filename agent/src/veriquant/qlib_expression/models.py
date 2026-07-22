"""Typed, inert contracts for certified Qlib expressions.

These objects describe syntax and policy only.  They are runtime-independent
and never execute an expression.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Mapping, TypeAlias


QLIB_EXPRESSION_SCHEMA_VERSION = "veriquant.qlib-expression/v1"
QLIB_POLICY_SCHEMA_VERSION = "veriquant.qlib-expression-policy/v1"


class QlibExpressionFailureCode(str, Enum):
    EMPTY_EXPRESSION = "EMPTY_EXPRESSION"
    EXPRESSION_TOO_LONG = "EXPRESSION_TOO_LONG"
    INVALID_TOKEN = "INVALID_TOKEN"
    INVALID_SYNTAX = "INVALID_SYNTAX"
    FORBIDDEN_SYNTAX = "FORBIDDEN_SYNTAX"
    UNKNOWN_FIELD = "UNKNOWN_FIELD"
    FORBIDDEN_FIELD = "FORBIDDEN_FIELD"
    UNKNOWN_OPERATOR = "UNKNOWN_OPERATOR"
    OPERATOR_NOT_TRUSTED = "OPERATOR_NOT_TRUSTED"
    INVALID_ARGUMENT_COUNT = "INVALID_ARGUMENT_COUNT"
    INVALID_ARGUMENT = "INVALID_ARGUMENT"
    INVALID_WINDOW = "INVALID_WINDOW"
    FUTURE_REFERENCE = "FUTURE_REFERENCE"
    SIZE_LIMIT_EXCEEDED = "SIZE_LIMIT_EXCEEDED"
    DEPTH_LIMIT_EXCEEDED = "DEPTH_LIMIT_EXCEEDED"
    QLIB_RUNTIME_UNAVAILABLE = "QLIB_RUNTIME_UNAVAILABLE"
    QLIB_RUNTIME_VERSION_MISMATCH = "QLIB_RUNTIME_VERSION_MISMATCH"
    MANIFEST_INVALID = "MANIFEST_INVALID"


class QlibFieldRole(str, Enum):
    FEATURE = "FEATURE"
    METADATA = "METADATA"
    LABEL = "LABEL"
    SEALED = "SEALED"


class QlibOperatorTrust(str, Enum):
    TRUSTED = "TRUSTED"
    TRUSTED_WITH_CONSTRAINTS = "TRUSTED_WITH_CONSTRAINTS"
    EXCLUDED = "EXCLUDED"


class QlibRuntimeStatus(str, Enum):
    AVAILABLE = "AVAILABLE"
    UNAVAILABLE = "UNAVAILABLE"
    VERSION_MISMATCH = "VERSION_MISMATCH"


class QlibExpressionError(ValueError):
    """Fail-closed expression error with a stable machine code."""

    def __init__(
        self,
        code: QlibExpressionFailureCode,
        message: str,
        *,
        position: int | None = None,
        details: Mapping[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.position = position
        self.details = MappingProxyType(dict(details or {}))


@dataclass(frozen=True, slots=True)
class FieldNode:
    name: str


@dataclass(frozen=True, slots=True)
class NumberNode:
    canonical_value: str


@dataclass(frozen=True, slots=True)
class StringNode:
    value: str


@dataclass(frozen=True, slots=True)
class UnaryNode:
    operator: str
    operand: "ExpressionNode"


@dataclass(frozen=True, slots=True)
class BinaryNode:
    operator: str
    left: "ExpressionNode"
    right: "ExpressionNode"


@dataclass(frozen=True, slots=True)
class CallNode:
    name: str
    arguments: tuple["ExpressionNode", ...]


ExpressionNode: TypeAlias = FieldNode | NumberNode | StringNode | UnaryNode | BinaryNode | CallNode


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def stable_hash(value: object) -> str:
    return "sha256:" + hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class QlibExpressionDraft:
    expression: str
    schema_version: str = QLIB_EXPRESSION_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != QLIB_EXPRESSION_SCHEMA_VERSION:
            raise QlibExpressionError(
                QlibExpressionFailureCode.INVALID_SYNTAX,
                "unsupported Qlib expression schema version",
            )
        if not isinstance(self.expression, str) or not self.expression.strip():
            raise QlibExpressionError(QlibExpressionFailureCode.EMPTY_EXPRESSION, "expression is empty")


@dataclass(frozen=True, slots=True)
class QlibExpressionPolicy:
    """Immutable field/operator/window limits applied before certification."""

    field_roles: Mapping[str, QlibFieldRole]
    allowed_operators: tuple[str, ...] = ()
    approved_instruments: tuple[str, ...] = ()
    max_expression_length: int = 8_192
    max_nodes: int = 128
    max_depth: int = 32
    max_arguments: int = 8
    max_window: int = 2_520
    max_ref_periods: int = 2_520
    allow_unbounded_windows: bool = False
    schema_version: str = QLIB_POLICY_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != QLIB_POLICY_SCHEMA_VERSION:
            raise QlibExpressionError(QlibExpressionFailureCode.MANIFEST_INVALID, "policy schema mismatch")
        normalized_roles: dict[str, QlibFieldRole] = {}
        for name, role in self.field_roles.items():
            if not isinstance(name, str) or not name or not name.replace("_", "a").isalnum():
                raise QlibExpressionError(QlibExpressionFailureCode.MANIFEST_INVALID, "invalid policy field")
            try:
                normalized_roles[name] = role if isinstance(role, QlibFieldRole) else QlibFieldRole(role)
            except (TypeError, ValueError) as exc:
                raise QlibExpressionError(
                    QlibExpressionFailureCode.MANIFEST_INVALID, "invalid field role"
                ) from exc
        for name in ("max_expression_length", "max_nodes", "max_depth", "max_arguments", "max_window", "max_ref_periods"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise QlibExpressionError(QlibExpressionFailureCode.MANIFEST_INVALID, f"{name} must be positive")
        if len(set(self.allowed_operators)) != len(self.allowed_operators):
            raise QlibExpressionError(QlibExpressionFailureCode.MANIFEST_INVALID, "duplicate allowed operator")
        if len(set(self.approved_instruments)) != len(self.approved_instruments):
            raise QlibExpressionError(QlibExpressionFailureCode.MANIFEST_INVALID, "duplicate approved instrument")
        object.__setattr__(self, "field_roles", MappingProxyType(dict(sorted(normalized_roles.items()))))
        object.__setattr__(self, "allowed_operators", tuple(sorted(self.allowed_operators)))
        object.__setattr__(self, "approved_instruments", tuple(sorted(self.approved_instruments)))

    def to_canonical(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "field_roles": {key: value.value for key, value in self.field_roles.items()},
            "allowed_operators": list(self.allowed_operators),
            "approved_instruments": list(self.approved_instruments),
            "max_expression_length": self.max_expression_length,
            "max_nodes": self.max_nodes,
            "max_depth": self.max_depth,
            "max_arguments": self.max_arguments,
            "max_window": self.max_window,
            "max_ref_periods": self.max_ref_periods,
            "allow_unbounded_windows": self.allow_unbounded_windows,
        }

    @property
    def policy_hash(self) -> str:
        return stable_hash(self.to_canonical())


@dataclass(frozen=True, slots=True)
class QlibExpressionDiagnostic:
    code: QlibExpressionFailureCode
    message: str
    position: int | None = None
    details: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "details", MappingProxyType(dict(self.details)))


@dataclass(frozen=True, slots=True)
class CertifiedQlibExpression:
    canonical_expression: str
    expression_hash: str
    manifest_hash: str
    policy_hash: str
    required_fields: tuple[str, ...]
    operators: tuple[str, ...]
    maximum_lookback: int
    node_count: int
    root: ExpressionNode = field(repr=False, compare=False)
    schema_version: str = QLIB_EXPRESSION_SCHEMA_VERSION

    def __post_init__(self) -> None:
        for name in ("expression_hash", "manifest_hash", "policy_hash"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.startswith("sha256:") or len(value) != 71:
                raise QlibExpressionError(QlibExpressionFailureCode.MANIFEST_INVALID, f"invalid {name}")
        if self.maximum_lookback < 0 or self.node_count < 1:
            raise QlibExpressionError(QlibExpressionFailureCode.MANIFEST_INVALID, "invalid certified expression metadata")


@dataclass(frozen=True, slots=True)
class QlibRuntimeAvailability:
    status: QlibRuntimeStatus
    expected_version: str
    observed_version: str | None
    failure_code: QlibExpressionFailureCode | None
    message: str

    def __post_init__(self) -> None:
        if self.status is QlibRuntimeStatus.AVAILABLE and self.failure_code is not None:
            raise QlibExpressionError(QlibExpressionFailureCode.MANIFEST_INVALID, "available runtime has failure")
        if self.status is not QlibRuntimeStatus.AVAILABLE and self.failure_code is None:
            raise QlibExpressionError(QlibExpressionFailureCode.MANIFEST_INVALID, "unavailable runtime lacks failure")
