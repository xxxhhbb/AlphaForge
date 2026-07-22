"""Small Pratt parser for the inert Qlib expression grammar."""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from .models import (
    BinaryNode,
    CallNode,
    ExpressionNode,
    FieldNode,
    NumberNode,
    QlibExpressionError,
    QlibExpressionFailureCode,
    QlibExpressionPolicy,
    StringNode,
    UnaryNode,
)


_FORBIDDEN_NAMES = frozenset(
    {"eval", "exec", "compile", "open", "import", "lambda", "globals", "locals", "getattr", "setattr"}
)
_BINARY_PRECEDENCE = {"|": 1, "&": 2, "==": 3, "!=": 3, ">": 3, ">=": 3, "<": 3, "<=": 3, "+": 4, "-": 4, "*": 5, "/": 5, "**": 6}


@dataclass(frozen=True, slots=True)
class _Token:
    kind: str
    value: str
    position: int


def _error(code: QlibExpressionFailureCode, message: str, position: int) -> QlibExpressionError:
    return QlibExpressionError(code, message, position=position)


def _normalize_number(raw: str, position: int) -> str:
    try:
        value = Decimal(raw)
    except InvalidOperation as exc:
        raise _error(QlibExpressionFailureCode.INVALID_TOKEN, "invalid number", position) from exc
    if not value.is_finite():
        raise _error(QlibExpressionFailureCode.INVALID_TOKEN, "number must be finite", position)
    if value == 0:
        return "0"
    normalized = value.normalize()
    rendered = format(normalized, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered


def _scan(expression: str) -> tuple[_Token, ...]:
    tokens: list[_Token] = []
    index = 0
    length = len(expression)
    while index < length:
        char = expression[index]
        if char.isspace():
            index += 1
            continue
        if char == "$":
            start = index
            index += 1
            if index >= length or not (expression[index].isalpha() or expression[index] == "_"):
                raise _error(QlibExpressionFailureCode.INVALID_TOKEN, "field name expected after '$'", start)
            index += 1
            while index < length and (expression[index].isalnum() or expression[index] == "_"):
                index += 1
            tokens.append(_Token("FIELD", expression[start + 1 : index], start))
            continue
        if char.isalpha() or char == "_":
            start = index
            index += 1
            while index < length and (expression[index].isalnum() or expression[index] == "_"):
                index += 1
            name = expression[start:index]
            if "__" in name or name.lower() in _FORBIDDEN_NAMES:
                raise _error(QlibExpressionFailureCode.FORBIDDEN_SYNTAX, "forbidden identifier", start)
            tokens.append(_Token("IDENT", name, start))
            continue
        if char.isdigit() or (char == "." and index + 1 < length and expression[index + 1].isdigit()):
            start = index
            saw_digits = False
            while index < length and expression[index].isdigit():
                index += 1
                saw_digits = True
            if index < length and expression[index] == ".":
                index += 1
                while index < length and expression[index].isdigit():
                    index += 1
                    saw_digits = True
            if not saw_digits:
                raise _error(QlibExpressionFailureCode.INVALID_TOKEN, "invalid number", start)
            if index < length and expression[index] in "eE":
                index += 1
                if index < length and expression[index] in "+-":
                    index += 1
                exponent_start = index
                while index < length and expression[index].isdigit():
                    index += 1
                if exponent_start == index:
                    raise _error(QlibExpressionFailureCode.INVALID_TOKEN, "invalid exponent", start)
            tokens.append(_Token("NUMBER", expression[start:index], start))
            continue
        if char in "'\"":
            quote = char
            start = index
            index += 1
            value: list[str] = []
            while index < length and expression[index] != quote:
                current = expression[index]
                if current == "\\":
                    raise _error(QlibExpressionFailureCode.FORBIDDEN_SYNTAX, "string escapes are prohibited", index)
                if ord(current) < 32:
                    raise _error(QlibExpressionFailureCode.INVALID_TOKEN, "control character in string", index)
                value.append(current)
                index += 1
            if index >= length:
                raise _error(QlibExpressionFailureCode.INVALID_SYNTAX, "unterminated string", start)
            index += 1
            tokens.append(_Token("STRING", "".join(value), start))
            continue
        two = expression[index : index + 2]
        if two in {"**", ">=", "<=", "==", "!="}:
            tokens.append(_Token("OP", two, index))
            index += 2
            continue
        if char in "+-*/><&|":
            tokens.append(_Token("OP", char, index))
            index += 1
            continue
        if char in "(),":
            tokens.append(_Token(char, char, index))
            index += 1
            continue
        raise _error(QlibExpressionFailureCode.FORBIDDEN_SYNTAX, f"forbidden token {char!r}", index)
    tokens.append(_Token("EOF", "", length))
    return tuple(tokens)


class _Parser:
    def __init__(self, tokens: tuple[_Token, ...], policy: QlibExpressionPolicy) -> None:
        self.tokens = tokens
        self.policy = policy
        self.index = 0
        self.nodes = 0

    @property
    def current(self) -> _Token:
        return self.tokens[self.index]

    def _advance(self) -> _Token:
        token = self.current
        self.index += 1
        return token

    def _node(self, node: ExpressionNode, depth: int, position: int) -> ExpressionNode:
        self.nodes += 1
        if self.nodes > self.policy.max_nodes:
            raise _error(QlibExpressionFailureCode.SIZE_LIMIT_EXCEEDED, "expression node limit exceeded", position)
        if depth > self.policy.max_depth:
            raise _error(QlibExpressionFailureCode.DEPTH_LIMIT_EXCEEDED, "expression depth limit exceeded", position)
        return node

    def parse(self) -> ExpressionNode:
        root = self._expression(0, 1)
        if self.current.kind != "EOF":
            raise _error(QlibExpressionFailureCode.INVALID_SYNTAX, "unexpected trailing token", self.current.position)
        return root

    def _expression(self, minimum_precedence: int, depth: int) -> ExpressionNode:
        token = self._advance()
        if token.kind == "FIELD":
            left = self._node(FieldNode(token.value), depth, token.position)
        elif token.kind == "NUMBER":
            left = self._node(NumberNode(_normalize_number(token.value, token.position)), depth, token.position)
        elif token.kind == "STRING":
            left = self._node(StringNode(token.value), depth, token.position)
        elif token.kind == "OP" and token.value in {"+", "-"}:
            operand = self._expression(6, depth + 1)
            left = self._node(UnaryNode(token.value, operand), depth, token.position)
        elif token.kind == "(":
            left = self._expression(0, depth + 1)
            if self.current.kind != ")":
                raise _error(QlibExpressionFailureCode.INVALID_SYNTAX, "missing closing parenthesis", self.current.position)
            self._advance()
        elif token.kind == "IDENT":
            if self.current.kind != "(":
                raise _error(QlibExpressionFailureCode.FORBIDDEN_SYNTAX, "bare names are prohibited", token.position)
            self._advance()
            arguments: list[ExpressionNode] = []
            if self.current.kind != ")":
                while True:
                    if len(arguments) >= self.policy.max_arguments:
                        raise _error(QlibExpressionFailureCode.SIZE_LIMIT_EXCEEDED, "argument limit exceeded", token.position)
                    arguments.append(self._expression(0, depth + 1))
                    if self.current.kind != ",":
                        break
                    self._advance()
                    if self.current.kind == ")":
                        raise _error(QlibExpressionFailureCode.INVALID_SYNTAX, "trailing comma is prohibited", self.current.position)
            if self.current.kind != ")":
                raise _error(QlibExpressionFailureCode.INVALID_SYNTAX, "missing closing parenthesis", self.current.position)
            self._advance()
            left = self._node(CallNode(token.value, tuple(arguments)), depth, token.position)
        else:
            raise _error(QlibExpressionFailureCode.INVALID_SYNTAX, "expression operand expected", token.position)

        while self.current.kind == "OP":
            operator = self.current.value
            precedence = _BINARY_PRECEDENCE.get(operator)
            if precedence is None or precedence < minimum_precedence:
                break
            operator_token = self._advance()
            next_minimum = precedence if operator == "**" else precedence + 1
            right = self._expression(next_minimum, depth + 1)
            left = self._node(BinaryNode(operator, left, right), depth, operator_token.position)
        return left


def parse_qlib_expression(expression: str, policy: QlibExpressionPolicy) -> ExpressionNode:
    """Parse the restricted grammar without invoking Python or Qlib parsers."""

    if not isinstance(expression, str) or not expression.strip():
        raise QlibExpressionError(QlibExpressionFailureCode.EMPTY_EXPRESSION, "expression is empty")
    if len(expression) > policy.max_expression_length:
        raise QlibExpressionError(QlibExpressionFailureCode.EXPRESSION_TOO_LONG, "expression length limit exceeded")
    return _Parser(_scan(expression), policy).parse()


def canonical_string_literal(value: str) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
