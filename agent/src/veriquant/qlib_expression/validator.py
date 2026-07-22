"""Fail-closed semantic validation for parsed Qlib expressions."""

from __future__ import annotations

import math
from decimal import Decimal

from .canonicalizer import canonicalize_expression
from .models import (
    BinaryNode,
    CallNode,
    CertifiedQlibExpression,
    ExpressionNode,
    FieldNode,
    NumberNode,
    QlibExpressionDraft,
    QlibExpressionError,
    QlibExpressionFailureCode,
    QlibExpressionPolicy,
    QlibFieldRole,
    QlibOperatorTrust,
    StringNode,
    UnaryNode,
    stable_hash,
)
from .operator_manifest import QlibOperatorManifest, STATIC_QLIB_OPERATOR_MANIFEST
from .parser import parse_qlib_expression


_BINARY_OPERATOR_NAMES = {
    "+": "Add", "-": "Sub", "*": "Mul", "/": "Div", "**": "Power", ">": "Gt", ">=": "Ge",
    "<": "Lt", "<=": "Le", "==": "Eq", "!=": "Ne", "&": "And", "|": "Or",
}
_INTEGER_WINDOW_OPERATORS = frozenset(
    {"Max", "Min", "Sum", "Mean", "Std", "Var", "Skew", "Kurt", "Med", "Mad", "Slope", "Rsquare", "Resi", "Rank", "Count", "WMA", "Delta", "IdxMax", "IdxMin"}
)
_PAIR_ROLLING = frozenset({"Corr", "Cov"})
_ELEMENT_OPERATORS = frozenset({"Abs", "Sign", "Log", "Not"})


def _fail(code: QlibExpressionFailureCode, message: str, **details: object) -> None:
    raise QlibExpressionError(code, message, details=details)


def _decimal_literal(node: ExpressionNode) -> Decimal | None:
    if isinstance(node, NumberNode):
        return Decimal(node.canonical_value)
    if isinstance(node, UnaryNode) and isinstance(node.operand, NumberNode):
        value = Decimal(node.operand.canonical_value)
        return -value if node.operator == "-" else value
    return None


def _bounded_integer(node: ExpressionNode, *, maximum: int, name: str, allow_zero: bool = False) -> int:
    value = _decimal_literal(node)
    minimum = 0 if allow_zero else 1
    if value is None or value != value.to_integral_value() or not minimum <= value <= maximum:
        _fail(QlibExpressionFailureCode.INVALID_WINDOW, f"{name} must be an integer from {minimum} to {maximum}")
    return int(value)


def _contains_expression(node: ExpressionNode) -> bool:
    return not isinstance(node, (NumberNode, StringNode)) and not (
        isinstance(node, UnaryNode) and isinstance(node.operand, NumberNode)
    )


def _ensure_expression(node: ExpressionNode, operator: str, index: int) -> None:
    if not _contains_expression(node):
        _fail(QlibExpressionFailureCode.INVALID_ARGUMENT, f"{operator} argument {index} must be an expression")


class _Validation:
    def __init__(self, policy: QlibExpressionPolicy, manifest: QlibOperatorManifest) -> None:
        self.policy = policy
        self.manifest = manifest
        self.specs = manifest.by_name
        self.fields: set[str] = set()
        self.operators: set[str] = set()
        self.node_count = 0

    def _operator(self, name: str, arguments: int) -> None:
        spec = self.specs.get(name)
        if spec is None:
            _fail(QlibExpressionFailureCode.UNKNOWN_OPERATOR, f"unknown Qlib operator: {name}", operator=name)
        if spec.trust is QlibOperatorTrust.EXCLUDED:
            _fail(QlibExpressionFailureCode.OPERATOR_NOT_TRUSTED, f"Qlib operator is excluded: {name}", operator=name)
        if self.policy.allowed_operators and name not in self.policy.allowed_operators:
            _fail(QlibExpressionFailureCode.OPERATOR_NOT_TRUSTED, f"operator is not enabled by policy: {name}", operator=name)
        if arguments != spec.argument_count:
            _fail(
                QlibExpressionFailureCode.INVALID_ARGUMENT_COUNT,
                f"{name} requires exactly {spec.argument_count} arguments",
                operator=name,
            )
        self.operators.add(name)

    def visit(self, node: ExpressionNode) -> int:
        self.node_count += 1
        if isinstance(node, FieldNode):
            role = self.policy.field_roles.get(node.name)
            if role is None:
                _fail(QlibExpressionFailureCode.UNKNOWN_FIELD, f"unknown Qlib field: ${node.name}", field=node.name)
            if role in {QlibFieldRole.LABEL, QlibFieldRole.SEALED}:
                _fail(QlibExpressionFailureCode.FORBIDDEN_FIELD, f"forbidden Qlib field: ${node.name}", field=node.name, role=role.value)
            self.fields.add(node.name)
            return 0
        if isinstance(node, NumberNode):
            return 0
        if isinstance(node, StringNode):
            _fail(QlibExpressionFailureCode.INVALID_ARGUMENT, "string literals are only permitted for approved instrument operators")
        if isinstance(node, UnaryNode):
            if not isinstance(node.operand, NumberNode):
                _fail(QlibExpressionFailureCode.OPERATOR_NOT_TRUSTED, "unary feature operators are not in the pinned OpsList")
            return 0
        if isinstance(node, BinaryNode):
            name = _BINARY_OPERATOR_NAMES[node.operator]
            self._operator(name, 2)
            left = self.visit(node.left)
            right = self.visit(node.right)
            return max(left, right)
        if not isinstance(node, CallNode):
            _fail(QlibExpressionFailureCode.INVALID_ARGUMENT, "unknown expression node")
        self._operator(node.name, len(node.arguments))

        if node.name == "Ref":
            _ensure_expression(node.arguments[0], node.name, 0)
            periods = _decimal_literal(node.arguments[1])
            if periods is not None and periods < 0:
                _fail(QlibExpressionFailureCode.FUTURE_REFERENCE, "negative Ref reads future data", periods=str(periods))
            n = _bounded_integer(
                node.arguments[1], maximum=self.policy.max_ref_periods, name="Ref periods", allow_zero=self.policy.allow_unbounded_windows
            )
            if n == 0 and not self.policy.allow_unbounded_windows:
                _fail(QlibExpressionFailureCode.INVALID_WINDOW, "Ref zero has full-history semantics in pinned Qlib")
            return self.visit(node.arguments[0]) + n

        if node.name in _INTEGER_WINDOW_OPERATORS:
            _ensure_expression(node.arguments[0], node.name, 0)
            window = _bounded_integer(node.arguments[1], maximum=self.policy.max_window, name=f"{node.name} window")
            child = self.visit(node.arguments[0])
            return child + (window if node.name == "Delta" else window - 1)

        if node.name == "EMA":
            _ensure_expression(node.arguments[0], node.name, 0)
            raw = _decimal_literal(node.arguments[1])
            if raw is None or raw <= 0:
                _fail(QlibExpressionFailureCode.INVALID_WINDOW, "EMA window/alpha must be positive")
            if raw < 1:
                effective = math.ceil(math.log(1e-6) / math.log(1 - float(raw)))
                if effective > self.policy.max_window:
                    _fail(QlibExpressionFailureCode.INVALID_WINDOW, "EMA alpha exceeds effective lookback policy")
                window = effective
            else:
                window = _bounded_integer(node.arguments[1], maximum=self.policy.max_window, name="EMA window")
            return self.visit(node.arguments[0]) + window - 1

        if node.name == "Quantile":
            _ensure_expression(node.arguments[0], node.name, 0)
            window = _bounded_integer(node.arguments[1], maximum=self.policy.max_window, name="Quantile window")
            quantile = _decimal_literal(node.arguments[2])
            if quantile is None or not Decimal("0") <= quantile <= Decimal("1"):
                _fail(QlibExpressionFailureCode.INVALID_ARGUMENT, "Quantile qscore must be from 0 to 1")
            return self.visit(node.arguments[0]) + window - 1

        if node.name in _PAIR_ROLLING:
            _ensure_expression(node.arguments[0], node.name, 0)
            _ensure_expression(node.arguments[1], node.name, 1)
            window = _bounded_integer(node.arguments[2], maximum=self.policy.max_window, name=f"{node.name} window")
            left = self.visit(node.arguments[0])
            right = self.visit(node.arguments[1])
            return max(left, right) + window - 1

        if node.name in {"ChangeInstrument", "Mask"}:
            instrument_index = 0 if node.name == "ChangeInstrument" else 1
            expression_index = 1 if node.name == "ChangeInstrument" else 0
            instrument = node.arguments[instrument_index]
            if not isinstance(instrument, StringNode) or instrument.value not in self.policy.approved_instruments:
                _fail(QlibExpressionFailureCode.INVALID_ARGUMENT, f"{node.name} requires an explicitly approved instrument")
            _ensure_expression(node.arguments[expression_index], node.name, expression_index)
            return self.visit(node.arguments[expression_index])

        if node.name in _ELEMENT_OPERATORS:
            _ensure_expression(node.arguments[0], node.name, 0)
        if node.name == "If":
            _ensure_expression(node.arguments[0], node.name, 0)
        maximum = 0
        for argument in node.arguments:
            if isinstance(argument, StringNode):
                _fail(QlibExpressionFailureCode.INVALID_ARGUMENT, f"string argument is not valid for {node.name}")
            maximum = max(maximum, self.visit(argument))
        return maximum


def certify_qlib_expression(
    draft: str | QlibExpressionDraft,
    *,
    policy: QlibExpressionPolicy,
    manifest: QlibOperatorManifest = STATIC_QLIB_OPERATOR_MANIFEST,
) -> CertifiedQlibExpression:
    """Parse and certify an inert expression; no Qlib runtime is called."""

    expression = draft.expression if isinstance(draft, QlibExpressionDraft) else draft
    root = parse_qlib_expression(expression, policy)
    validation = _Validation(policy, manifest)
    maximum_lookback = validation.visit(root)
    if not validation.fields:
        _fail(QlibExpressionFailureCode.INVALID_ARGUMENT, "a factor expression must reference at least one approved field")
    canonical = canonicalize_expression(root)
    identity = {
        "schema_version": QlibExpressionDraft(expression=expression).schema_version,
        "canonical_expression": canonical,
        "manifest_hash": manifest.manifest_hash,
        "policy_hash": policy.policy_hash,
    }
    return CertifiedQlibExpression(
        canonical_expression=canonical,
        expression_hash=stable_hash(identity),
        manifest_hash=manifest.manifest_hash,
        policy_hash=policy.policy_hash,
        required_fields=tuple(sorted(validation.fields)),
        operators=tuple(sorted(validation.operators)),
        maximum_lookback=maximum_lookback,
        node_count=validation.node_count,
        root=root,
    )
