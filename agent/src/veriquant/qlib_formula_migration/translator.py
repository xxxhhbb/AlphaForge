"""Translate a deliberately small Python factor AST into inert Qlib syntax.

This module never imports a factor source file and never evaluates Python.  It
recognizes only literal assignments and an allowlisted expression subset.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from decimal import Decimal
from typing import Mapping

from .models import FormulaMigrationError, FormulaMigrationFailureCode


_EPSILON = "0.000000000001"
_CALL_MAP: Mapping[str, str] = {
    "ts_mean": "Mean",
    "ts_std": "Std",
    "ts_corr": "Corr",
    "ts_cov": "Cov",
    "ts_max": "Max",
    "ts_min": "Min",
    "ts_argmax": "IdxMax",
    "ts_argmin": "IdxMin",
    "ts_rank": "Rank",
    "delta": "Delta",
    "decay_linear": "WMA",
}
_NUMPY_CALL_MAP: Mapping[str, str] = {
    "log": "Log",
    "abs": "Abs",
    "sign": "Sign",
    "sqrt": "Power",
}
_PARITY_OPERATORS = frozenset(
    {"Cov", "IdxMax", "IdxMin", "Quantile", "Rank", "Rsquare", "Std", "WMA"}
)
_CROSS_SECTIONAL = frozenset({"rank", "zscore", "scale"})


def _number(value: object) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise FormulaMigrationError(
            FormulaMigrationFailureCode.INVALID_ARGUMENT,
            "only finite numeric literals are permitted",
        )
    decimal = Decimal(str(value))
    if not decimal.is_finite():
        raise FormulaMigrationError(
            FormulaMigrationFailureCode.INVALID_ARGUMENT,
            "numeric literal must be finite",
        )
    if decimal == 0:
        return "0"
    return format(decimal.normalize(), "f").rstrip("0").rstrip(".") if "." in format(decimal.normalize(), "f") else format(decimal.normalize(), "f")


def _safe_div(left: str, right: str) -> str:
    adjusted = f"(({right})+{_EPSILON}*Sign({right}))"
    return f"If(Abs({right})>0,({left})/{adjusted},0/0)"


@dataclass(frozen=True, slots=True)
class StaticTranslation:
    expression: str
    postprocess_steps: tuple[str, ...] = ()
    parity_operators: tuple[str, ...] = ()


class StaticComputeTranslator:
    """Restricted symbolic interpreter over a single ``compute`` function."""

    def __init__(self, *, maximum_statements: int = 96) -> None:
        self.maximum_statements = maximum_statements
        self._environment: dict[str, str] = {}

    def translate(self, tree: ast.Module) -> StaticTranslation:
        functions = [
            node
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "compute"
        ]
        if not functions:
            raise FormulaMigrationError(
                FormulaMigrationFailureCode.COMPUTE_MISSING,
                "compute function is absent",
            )
        if len(functions) != 1 or isinstance(functions[0], ast.AsyncFunctionDef):
            raise FormulaMigrationError(
                FormulaMigrationFailureCode.MULTIPLE_COMPUTE,
                "exactly one synchronous compute function is required",
            )
        function = functions[0]
        if len(function.body) > self.maximum_statements:
            raise FormulaMigrationError(
                FormulaMigrationFailureCode.UNSUPPORTED_STATEMENT,
                "compute function exceeds the static statement limit",
            )
        self._environment = {}
        return_node: ast.expr | None = None
        for statement in function.body:
            if isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Constant) and isinstance(statement.value.value, str):
                continue
            if isinstance(statement, ast.Assign):
                if len(statement.targets) != 1 or not isinstance(statement.targets[0], ast.Name):
                    self._unsupported_statement(statement)
                self._environment[statement.targets[0].id] = self._expr(statement.value)
                continue
            if isinstance(statement, ast.AnnAssign) and isinstance(statement.target, ast.Name) and statement.value is not None:
                self._environment[statement.target.id] = self._expr(statement.value)
                continue
            if isinstance(statement, ast.Return):
                if statement.value is None or return_node is not None:
                    self._unsupported_statement(statement)
                return_node = statement.value
                continue
            self._unsupported_statement(statement)
        if return_node is None:
            raise FormulaMigrationError(
                FormulaMigrationFailureCode.COMPUTE_MISSING,
                "compute function has no static return expression",
            )
        postprocess: tuple[str, ...] = ()
        if isinstance(return_node, ast.Call):
            call_name = self._call_name(return_node.func)
            if call_name in _CROSS_SECTIONAL:
                if len(return_node.args) != 1 or return_node.keywords:
                    raise FormulaMigrationError(
                        FormulaMigrationFailureCode.INVALID_ARGUMENT,
                        "cross-sectional postprocess requires one argument",
                    )
                postprocess = ({"rank": "cross_sectional_rank", "zscore": "cross_sectional_zscore", "scale": "cross_sectional_scale"}[call_name],)
                expression = self._expr(return_node.args[0])
            else:
                expression = self._expr(return_node)
        else:
            expression = self._expr(return_node)
        operators = tuple(
            sorted(operator for operator in _PARITY_OPERATORS if f"{operator}(" in expression)
        )
        return StaticTranslation(
            expression=expression,
            postprocess_steps=postprocess,
            parity_operators=operators,
        )

    @staticmethod
    def _unsupported_statement(node: ast.AST) -> None:
        raise FormulaMigrationError(
            FormulaMigrationFailureCode.UNSUPPORTED_STATEMENT,
            f"statement {type(node).__name__} is outside the static subset",
        )

    def _expr(self, node: ast.AST) -> str:
        if isinstance(node, ast.Name):
            if node.id in self._environment:
                return self._environment[node.id]
            raise FormulaMigrationError(
                FormulaMigrationFailureCode.UNKNOWN_NAME,
                f"name {node.id!r} is not a translated local",
            )
        if isinstance(node, ast.Constant):
            return _number(node.value)
        if isinstance(node, ast.Subscript):
            return self._subscript(node)
        if isinstance(node, ast.UnaryOp):
            operation = {ast.USub: "-", ast.UAdd: "+"}.get(type(node.op))
            if operation is None:
                raise FormulaMigrationError(
                    FormulaMigrationFailureCode.UNSUPPORTED_NODE,
                    "unary operation is outside the static subset",
                )
            return f"{operation}({self._expr(node.operand)})"
        if isinstance(node, ast.BinOp):
            operation = {
                ast.Add: "+",
                ast.Sub: "-",
                ast.Mult: "*",
                ast.Div: "/",
                ast.Pow: "**",
            }.get(type(node.op))
            if operation is None:
                raise FormulaMigrationError(
                    FormulaMigrationFailureCode.UNSUPPORTED_NODE,
                    "binary operation is outside the static subset",
                )
            return f"({self._expr(node.left)}){operation}({self._expr(node.right)})"
        if isinstance(node, ast.Compare):
            if len(node.ops) != 1 or len(node.comparators) != 1:
                raise FormulaMigrationError(
                    FormulaMigrationFailureCode.UNSUPPORTED_NODE,
                    "comparison chaining is prohibited",
                )
            operation = {
                ast.Gt: ">",
                ast.GtE: ">=",
                ast.Lt: "<",
                ast.LtE: "<=",
                ast.Eq: "==",
                ast.NotEq: "!=",
            }.get(type(node.ops[0]))
            if operation is None:
                raise FormulaMigrationError(
                    FormulaMigrationFailureCode.UNSUPPORTED_NODE,
                    "comparison operator is unsupported",
                )
            return f"({self._expr(node.left)}){operation}({self._expr(node.comparators[0])})"
        if isinstance(node, ast.IfExp):
            return f"If({self._expr(node.test)},{self._expr(node.body)},{self._expr(node.orelse)})"
        if isinstance(node, ast.Call):
            return self._call(node)
        raise FormulaMigrationError(
            FormulaMigrationFailureCode.UNSUPPORTED_NODE,
            f"expression node {type(node).__name__} is outside the static subset",
        )

    def _subscript(self, node: ast.Subscript) -> str:
        if not isinstance(node.value, ast.Name) or node.value.id != "panel":
            raise FormulaMigrationError(
                FormulaMigrationFailureCode.DYNAMIC_SUBSCRIPT,
                "only panel[<literal field>] subscripts are permitted",
            )
        if not isinstance(node.slice, ast.Constant) or not isinstance(node.slice.value, str):
            raise FormulaMigrationError(
                FormulaMigrationFailureCode.DYNAMIC_SUBSCRIPT,
                "panel field must be a literal string",
            )
        field = node.slice.value
        if not field or not field.replace("_", "a").isalnum():
            raise FormulaMigrationError(
                FormulaMigrationFailureCode.FIELD_NAME_UNSUPPORTED,
                "field cannot be represented in certified Qlib syntax",
            )
        return "$" + field

    @staticmethod
    def _call_name(node: ast.AST) -> str:
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            return f"{node.value.id}.{node.attr}"
        return ""

    def _call(self, node: ast.Call) -> str:
        if node.keywords:
            raise FormulaMigrationError(
                FormulaMigrationFailureCode.INVALID_ARGUMENT,
                "keyword arguments are not accepted by the static translator",
            )
        name = self._call_name(node.func)
        if name in _CROSS_SECTIONAL:
            raise FormulaMigrationError(
                FormulaMigrationFailureCode.CROSS_SECTIONAL_INTERMEDIATE,
                "cross-sectional operation may only be a terminal postprocess",
            )
        if name in _CALL_MAP:
            args = tuple(self._expr(arg) for arg in node.args)
            if name in {"delta", "ts_mean", "ts_std", "ts_corr", "ts_cov", "ts_max", "ts_min", "ts_argmax", "ts_argmin", "ts_rank", "decay_linear"}:
                self._validate_positive_window(node.args[-1] if node.args else None)
            return f"{_CALL_MAP[name]}({','.join(args)})"
        if name == "safe_div":
            if len(node.args) != 2:
                self._invalid_call(name)
            return _safe_div(self._expr(node.args[0]), self._expr(node.args[1]))
        if name == "signed_power":
            if len(node.args) != 2:
                self._invalid_call(name)
            base, power = self._expr(node.args[0]), self._expr(node.args[1])
            return f"Sign({base})*Power(Abs({base}),{power})"
        if name in {"np.log", "numpy.log", "np.abs", "numpy.abs", "np.sign", "numpy.sign"}:
            if len(node.args) != 1:
                self._invalid_call(name)
            operator = _NUMPY_CALL_MAP[name.split(".")[-1]]
            return f"{operator}({self._expr(node.args[0])})"
        if name in {"np.sqrt", "numpy.sqrt"}:
            if len(node.args) != 1:
                self._invalid_call(name)
            return f"Power({self._expr(node.args[0])},0.5)"
        if isinstance(node.func, ast.Attribute):
            attr = node.func.attr
            receiver = node.func.value
            if attr in {"pct_change", "shift", "diff"}:
                if len(node.args) > 1:
                    self._invalid_call(attr)
                period_node = node.args[0] if node.args else ast.Constant(value=1)
                period = self._positive_integer(period_node)
                base = self._expr(receiver)
                if attr == "pct_change":
                    return f"({base})/(Ref({base},{period}))-1"
                if attr == "shift":
                    return f"Ref({base},{period})"
                return f"Delta({base},{period})"
            if attr == "astype":
                if len(node.args) != 1:
                    self._invalid_call(attr)
                return self._expr(receiver)
            if attr == "abs" and not node.args:
                return f"Abs({self._expr(receiver)})"
            if attr == "where" and len(node.args) == 2:
                return f"If({self._expr(node.args[0])},{self._expr(receiver)},{self._expr(node.args[1])})"
        raise FormulaMigrationError(
            FormulaMigrationFailureCode.UNKNOWN_CALL,
            f"call {name or type(node.func).__name__!r} is not allowlisted",
        )

    @staticmethod
    def _invalid_call(name: str) -> None:
        raise FormulaMigrationError(
            FormulaMigrationFailureCode.INVALID_ARGUMENT,
            f"call {name!r} has invalid arguments",
        )

    def _validate_positive_window(self, node: ast.AST | None) -> None:
        self._positive_integer(node)

    @staticmethod
    def _positive_integer(node: ast.AST | None) -> int:
        if (
            isinstance(node, ast.UnaryOp)
            and isinstance(node.op, ast.USub)
            and isinstance(node.operand, ast.Constant)
            and isinstance(node.operand.value, int)
            and not isinstance(node.operand.value, bool)
        ):
            raise FormulaMigrationError(
                FormulaMigrationFailureCode.FUTURE_REFERENCE,
                "negative lag/ref is a future reference",
            )
        if not isinstance(node, ast.Constant) or isinstance(node.value, bool) or not isinstance(node.value, int):
            raise FormulaMigrationError(
                FormulaMigrationFailureCode.INVALID_ARGUMENT,
                "window/ref period must be a literal positive integer",
            )
        if node.value < 0:
            raise FormulaMigrationError(
                FormulaMigrationFailureCode.FUTURE_REFERENCE,
                "negative lag/ref is a future reference",
            )
        if node.value == 0:
            raise FormulaMigrationError(
                FormulaMigrationFailureCode.INVALID_ARGUMENT,
                "zero window/ref is not accepted",
            )
        return node.value


__all__ = ["StaticComputeTranslator", "StaticTranslation"]
