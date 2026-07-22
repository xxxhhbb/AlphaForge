"""Structured AST to fixed Qlib 0.9.7 class compiler."""

from __future__ import annotations

from decimal import Decimal

from ..qlib_expression.models import (
    BinaryNode,
    CallNode,
    ExpressionNode,
    FieldNode,
    NumberNode,
    StringNode,
    UnaryNode,
)

from .models import QlibOperatorWorkerFailureCode, QlibOperatorWorkerError
from .runtime import PinnedQlibBindings


_BINARY_OPERATOR_NAMES = {
    "+": "Add", "-": "Sub", "*": "Mul", "/": "Div", "**": "Power", ">": "Gt", ">=": "Ge",
    "<": "Lt", "<=": "Le", "==": "Eq", "!=": "Ne", "&": "And", "|": "Or",
}


def _fail(code: QlibOperatorWorkerFailureCode, message: str) -> None:
    raise QlibOperatorWorkerError(code, message)


def _number(node: NumberNode) -> int | float:
    value = Decimal(node.canonical_value)
    if value == value.to_integral_value():
        return int(value)
    return float(value)


class StructuredQlibCompiler:
    """Compiler with no text input and no caller-provided operator registry."""

    def __init__(
        self,
        *,
        bindings: PinnedQlibBindings,
        values_by_field: dict[str, dict[str, tuple[float, ...]]],
        namespace: str,
    ) -> None:
        self._bindings = bindings
        self._values_by_field = values_by_field
        self._namespace = namespace
        self._leaves: dict[str, object] = {}

    def compile(self, root: ExpressionNode) -> object:
        return self._compile(root)

    def _leaf(self, field: str) -> object:
        if field not in self._values_by_field:
            _fail(QlibOperatorWorkerFailureCode.DATA_SCHEMA_MISMATCH, "certified field lacks in-memory values")
        if field not in self._leaves:
            self._leaves[field] = self._bindings.leaf_factory(
                field,
                self._values_by_field[field],
                self._namespace,
            )
        return self._leaves[field]

    def _operator(self, name: str) -> type:
        operator = self._bindings.operator_classes.get(name)
        if operator is None:
            _fail(QlibOperatorWorkerFailureCode.EXCLUDED_OPERATOR, "operator has no fixed Qlib class mapping")
        return operator

    def _compile(self, node: ExpressionNode) -> object:
        if isinstance(node, FieldNode):
            return self._leaf(node.name)
        if isinstance(node, NumberNode):
            return _number(node)
        if isinstance(node, UnaryNode):
            if not isinstance(node.operand, NumberNode):
                _fail(QlibOperatorWorkerFailureCode.UNSUPPORTED_NODE, "only certified numeric unary nodes are supported")
            value = _number(node.operand)
            return -value if node.operator == "-" else value
        if isinstance(node, BinaryNode):
            name = _BINARY_OPERATOR_NAMES.get(node.operator)
            if name is None:
                _fail(QlibOperatorWorkerFailureCode.UNSUPPORTED_NODE, "unknown binary node")
            return self._operator(name)(self._compile(node.left), self._compile(node.right))
        if isinstance(node, StringNode):
            _fail(QlibOperatorWorkerFailureCode.UNSUPPORTED_NODE, "standalone string nodes are prohibited")
        if not isinstance(node, CallNode):
            _fail(QlibOperatorWorkerFailureCode.UNSUPPORTED_NODE, "unknown certified node type")
        if node.name == "ChangeInstrument":
            instrument, feature = node.arguments
            if not isinstance(instrument, StringNode):
                _fail(QlibOperatorWorkerFailureCode.UNSUPPORTED_NODE, "ChangeInstrument target must be certified string")
            return self._operator(node.name)(instrument.value, self._compile(feature))
        if node.name == "Mask":
            feature, instrument = node.arguments
            if not isinstance(instrument, StringNode):
                _fail(QlibOperatorWorkerFailureCode.UNSUPPORTED_NODE, "Mask target must be certified string")
            return self._operator(node.name)(self._compile(feature), instrument.value)
        arguments = tuple(self._compile(argument) for argument in node.arguments)
        if any(isinstance(argument, str) for argument in arguments):
            _fail(QlibOperatorWorkerFailureCode.UNSUPPORTED_NODE, "string arguments require a constrained instrument operator")
        return self._operator(node.name)(*arguments)
