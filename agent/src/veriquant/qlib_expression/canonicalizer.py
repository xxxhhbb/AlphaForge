"""Stable rendering and identity helpers for parsed Qlib expressions."""

from __future__ import annotations

from .models import BinaryNode, CallNode, ExpressionNode, FieldNode, NumberNode, StringNode, UnaryNode
from .parser import canonical_string_literal


def canonicalize_expression(node: ExpressionNode) -> str:
    if isinstance(node, FieldNode):
        return f"${node.name}"
    if isinstance(node, NumberNode):
        return node.canonical_value
    if isinstance(node, StringNode):
        return canonical_string_literal(node.value)
    if isinstance(node, UnaryNode):
        return f"({node.operator}{canonicalize_expression(node.operand)})"
    if isinstance(node, BinaryNode):
        return f"({canonicalize_expression(node.left)}{node.operator}{canonicalize_expression(node.right)})"
    if isinstance(node, CallNode):
        arguments = ",".join(canonicalize_expression(item) for item in node.arguments)
        return f"{node.name}({arguments})"
    raise TypeError(f"unsupported expression node: {type(node).__name__}")
