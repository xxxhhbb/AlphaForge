"""Static-only compilers into FactorIR v2.

Legacy source is parsed with :mod:`ast` and inspected.  It is never compiled,
imported, evaluated, or passed to a Python execution surface.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Mapping

from ..factor_ir_v2.models import (
    CompilationFailure,
    CompilationResult,
    FactorIRCompileError,
    FactorIRFailureCode,
    FactorIRNode,
    FactorIRProgram,
    IRValueKind,
    ProgramIdentity,
    canonical_hash,
)
from ..factor_ir_v2.policies import (
    ADAPTER_OPERATORS,
    ALIASES,
    ARGUMENT_COUNTS,
    COMPILER_HASH,
    NATIVE_OPERATORS,
    NATIVE_TO_SAFE_ADAPTER,
    POSTPROCESS_ALIASES,
    WINDOW_ARGUMENT,
    adapter_policy,
    operator_policy,
    postprocess_step,
)
from ..qlib_expression.models import (
    BinaryNode as QBinaryNode,
    CallNode as QCallNode,
    FieldNode as QFieldNode,
    NumberNode as QNumberNode,
    QlibExpressionPolicy,
    QlibFieldRole,
    StringNode as QStringNode,
    UnaryNode as QUnaryNode,
)
from ..qlib_expression.parser import parse_qlib_expression


MAX_SOURCE_LENGTH = 256_000
MAX_NODES = 512
MAX_DEPTH = 64
MAX_WINDOW = 2_520
_FORBIDDEN_FIELD_TOKENS = ("label", "sealed", "future_return", "forward_return", "target_return")

_BIN_OPS = {
    ast.Add: "Add", ast.Sub: "Sub", ast.Mult: "Mul", ast.Div: "Div", ast.Pow: "Power",
}
_CMP_OPS = {ast.Gt: "Gt", ast.GtE: "Ge", ast.Lt: "Lt", ast.LtE: "Le", ast.Eq: "Eq", ast.NotEq: "Ne"}
_QBIN_OPS = {"+": "Add", "-": "Sub", "*": "Mul", "/": "Div", "**": "Power", ">": "Gt", ">=": "Ge", "<": "Lt", "<=": "Le", "==": "Eq", "!=": "Ne", "&": "And", "|": "Or"}


def _fail(code: FactorIRFailureCode, message: str, **details: object) -> None:
    raise FactorIRCompileError(code, message, **details)


def _number(value: object) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _fail(FactorIRFailureCode.INVALID_ARGUMENT, "only finite numeric constants are accepted")
    try:
        number = Decimal(str(value))
    except InvalidOperation as exc:
        raise FactorIRCompileError(FactorIRFailureCode.INVALID_ARGUMENT, "invalid numeric constant") from exc
    if not number.is_finite():
        _fail(FactorIRFailureCode.INVALID_ARGUMENT, "numeric constants must be finite")
    if number == 0:
        return "0"
    rendered = format(number.normalize(), "f")
    return rendered.rstrip("0").rstrip(".") if "." in rendered else rendered


def _positive_window(value: object, operator: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        _fail(FactorIRFailureCode.DYNAMIC_WINDOW, f"{operator} window must be a literal integer")
    if value < 0 and operator == "Ref":
        _fail(FactorIRFailureCode.FUTURE_REFERENCE, "negative Ref reads future data")
    if not 1 <= value <= MAX_WINDOW:
        _fail(FactorIRFailureCode.INVALID_ARGUMENT, f"{operator} window must be from 1 to {MAX_WINDOW}")
    return value


@dataclass(frozen=True)
class _Value:
    node_id: str
    lookback: int


class _Builder:
    def __init__(self, *, allowed_fields: Iterable[str], source_hash: str, source_kind: str) -> None:
        field_values = tuple(allowed_fields)
        if any(not isinstance(item, str) or not item for item in field_values):
            _fail(FactorIRFailureCode.INVALID_ARGUMENT, "approved fields must be non-empty strings")
        forbidden = tuple(sorted(item for item in field_values if any(token in item.casefold() for token in _FORBIDDEN_FIELD_TOKENS)))
        if forbidden:
            _fail(FactorIRFailureCode.FORBIDDEN_FIELD, "privileged fields cannot enter the approved whitelist", fields=forbidden)
        self.allowed_fields = frozenset(field_values)
        self.source_hash = source_hash
        self.source_kind = source_kind
        self.nodes: list[FactorIRNode] = []
        self.fields: set[str] = set()
        self.operators: set[str] = set()
        self.adapters: set[str] = set()
        self.postprocess: list[Any] = []
        self.locals: dict[str, _Value] = {}

    def emit(self, kind: IRValueKind, operator: str, inputs: tuple[_Value, ...] = (), **attributes: str | int | float | bool) -> _Value:
        if len(self.nodes) >= MAX_NODES:
            _fail(FactorIRFailureCode.SIZE_LIMIT_EXCEEDED, "FactorIR node limit exceeded")
        node_id = f"n{len(self.nodes):04d}"
        self.nodes.append(FactorIRNode(node_id, kind, operator, tuple(item.node_id for item in inputs), attributes))
        return _Value(node_id, max((item.lookback for item in inputs), default=0))

    def field(self, name: str) -> _Value:
        if any(token in name.casefold() for token in _FORBIDDEN_FIELD_TOKENS):
            _fail(FactorIRFailureCode.FORBIDDEN_FIELD, f"field {name!r} is privileged", field=name)
        if name not in self.allowed_fields:
            _fail(FactorIRFailureCode.UNKNOWN_FIELD, f"field {name!r} is not in the approved whitelist", field=name)
        self.fields.add(name)
        return self.emit(IRValueKind.FIELD, "Field", field=name)

    def constant(self, value: object) -> _Value:
        return self.emit(IRValueKind.CONSTANT, "Constant", value=_number(value))

    def call(self, operator: str, arguments: tuple[_Value, ...], raw_window: object | None = None) -> _Value:
        expected = ARGUMENT_COUNTS.get(operator)
        if operator not in NATIVE_OPERATORS | ADAPTER_OPERATORS or expected is None:
            _fail(FactorIRFailureCode.UNKNOWN_CALL, f"call {operator!r} is not registered")
        if len(arguments) != expected:
            _fail(FactorIRFailureCode.INVALID_ARGUMENT, f"{operator} requires exactly {expected} arguments")
        lookback = max((item.lookback for item in arguments), default=0)
        attributes: dict[str, str | int | float | bool] = {}
        if operator in WINDOW_ARGUMENT:
            window = _positive_window(raw_window, operator)
            attributes["window"] = window
            lookback += window if operator in {"Ref", "Delta"} else window - 1
        kind = IRValueKind.CONDITIONAL if operator == "SafeIf" else IRValueKind.CALL
        result = self.emit(kind, operator, arguments, **attributes)
        self.operators.add(operator)
        if operator in ADAPTER_OPERATORS:
            self.adapters.add(operator)
        return _Value(result.node_id, lookback)

    def build(self, output: _Value) -> FactorIRProgram:
        if not self.fields:
            _fail(FactorIRFailureCode.INVALID_ARGUMENT, "a factor must reference at least one approved field")
        op_policies = tuple(operator_policy(name) for name in sorted(self.operators))
        adapter_policies = tuple(adapter_policy(name) for name in sorted(self.adapters))
        post_plan = tuple(self.postprocess)
        op_hash = canonical_hash([item.to_canonical() for item in op_policies])
        adapter_hash = canonical_hash([item.to_canonical() for item in adapter_policies])
        post_hash = canonical_hash([item.to_canonical() for item in post_plan])
        material = {
            "schema_version": "veriquant.factor-ir/v2", "source_kind": self.source_kind,
            "nodes": [node.to_canonical() for node in self.nodes], "output_node": output.node_id,
            "required_fields": sorted(self.fields), "lookback": output.lookback,
            "operator_policies": [item.to_canonical() for item in op_policies],
            "semantic_adapter_policies": [item.to_canonical() for item in adapter_policies],
            "postprocess_plan": [item.to_canonical() for item in post_plan],
            "source_hash": self.source_hash, "compiler_hash": COMPILER_HASH,
        }
        identity = ProgramIdentity(canonical_hash(material), self.source_hash, COMPILER_HASH, op_hash, adapter_hash, post_hash)
        return FactorIRProgram(tuple(self.nodes), output.node_id, tuple(sorted(self.fields)), output.lookback, op_policies, adapter_policies, post_plan, self.source_kind, identity)


class _PythonCompiler:
    def __init__(self, builder: _Builder) -> None:
        self.builder = builder

    def module(self, tree: ast.Module) -> _Value:
        output: _Value | None = None
        for statement in tree.body:
            if isinstance(statement, ast.Assign):
                if len(statement.targets) != 1 or not isinstance(statement.targets[0], ast.Name):
                    _fail(FactorIRFailureCode.FORBIDDEN_STATEMENT, "assignment target must be one local name")
                name = statement.targets[0].id
                if name in self.builder.locals or name in self.builder.allowed_fields:
                    _fail(FactorIRFailureCode.SSA_REASSIGNMENT, f"local {name!r} is assigned more than once")
                self.builder.locals[name] = self.expr(statement.value, 1)
                output = self.builder.locals[name]
            elif isinstance(statement, ast.Expr):
                output = self.expr(statement.value, 1)
            else:
                _fail(FactorIRFailureCode.FORBIDDEN_STATEMENT, f"statement {type(statement).__name__} is prohibited")
        if output is None:
            _fail(FactorIRFailureCode.EMPTY_SOURCE, "source does not contain a factor expression")
        return output

    def expr(self, node: ast.AST, depth: int) -> _Value:
        if depth > MAX_DEPTH:
            _fail(FactorIRFailureCode.DEPTH_LIMIT_EXCEEDED, "source expression depth exceeded")
        if isinstance(node, ast.Name):
            if node.id in self.builder.locals:
                return self.builder.locals[node.id]
            if node.id in self.builder.allowed_fields:
                return self.builder.field(node.id)
            _fail(FactorIRFailureCode.UNKNOWN_NAME, f"name {node.id!r} is not a field or prior SSA local")
        if isinstance(node, ast.Constant):
            return self.builder.constant(node.value)
        if isinstance(node, ast.Subscript):
            if not isinstance(node.value, ast.Name) or node.value.id not in {"panel", "data", "features"}:
                _fail(FactorIRFailureCode.FORBIDDEN_NODE, "only approved feature mapping subscripts are accepted")
            if not isinstance(node.slice, ast.Constant) or not isinstance(node.slice.value, str):
                _fail(FactorIRFailureCode.FORBIDDEN_NODE, "feature subscript must be a literal string")
            return self.builder.field(node.slice.value)
        if isinstance(node, ast.UnaryOp):
            if isinstance(node.op, ast.UAdd):
                return self.expr(node.operand, depth + 1)
            if isinstance(node.op, ast.USub):
                if isinstance(node.operand, ast.Constant):
                    return self.builder.constant(-node.operand.value)
                return self.builder.call("Mul", (self.builder.constant(-1), self.expr(node.operand, depth + 1)))
            if isinstance(node.op, ast.Not):
                return self.builder.call("Not", (self.expr(node.operand, depth + 1),))
            _fail(FactorIRFailureCode.FORBIDDEN_NODE, "unary operator is not registered")
        if isinstance(node, ast.BinOp):
            operator = _BIN_OPS.get(type(node.op))
            if operator is None:
                _fail(FactorIRFailureCode.FORBIDDEN_NODE, "binary operator is not registered")
            return self.builder.call(operator, (self.expr(node.left, depth + 1), self.expr(node.right, depth + 1)))
        if isinstance(node, ast.Compare):
            if len(node.ops) != 1 or len(node.comparators) != 1:
                _fail(FactorIRFailureCode.FORBIDDEN_NODE, "comparison chains are prohibited")
            operator = _CMP_OPS.get(type(node.ops[0]))
            if operator is None:
                _fail(FactorIRFailureCode.FORBIDDEN_NODE, "comparison operator is not registered")
            return self.builder.call(operator, (self.expr(node.left, depth + 1), self.expr(node.comparators[0], depth + 1)))
        if isinstance(node, ast.BoolOp):
            operator = "And" if isinstance(node.op, ast.And) else "Or" if isinstance(node.op, ast.Or) else ""
            if not operator or len(node.values) < 2:
                _fail(FactorIRFailureCode.FORBIDDEN_NODE, "boolean expression is invalid")
            value = self.expr(node.values[0], depth + 1)
            for item in node.values[1:]:
                value = self.builder.call(operator, (value, self.expr(item, depth + 1)))
            return value
        if isinstance(node, ast.IfExp):
            return self.builder.call("SafeIf", tuple(self.expr(item, depth + 1) for item in (node.test, node.body, node.orelse)))
        if isinstance(node, ast.Call):
            return self.call(node, depth)
        if isinstance(node, (ast.Attribute, ast.Lambda, ast.ListComp, ast.DictComp, ast.GeneratorExp)):
            _fail(FactorIRFailureCode.FORBIDDEN_NODE, f"node {type(node).__name__} is prohibited")
        _fail(FactorIRFailureCode.FORBIDDEN_NODE, f"expression node {type(node).__name__} is not supported")

    def call(self, node: ast.Call, depth: int) -> _Value:
        if node.keywords or not isinstance(node.func, ast.Name):
            _fail(FactorIRFailureCode.UNKNOWN_CALL, "only fixed, positional, bare-name calls are accepted")
        raw_name = node.func.id
        if raw_name in POSTPROCESS_ALIASES:
            if not node.args:
                _fail(FactorIRFailureCode.INVALID_ARGUMENT, "postprocess requires a formula argument")
            base = self.expr(node.args[0], depth + 1)
            params: dict[str, str | int | float | bool] = {}
            for index, item in enumerate(node.args[1:], 1):
                if not isinstance(item, ast.Constant) or isinstance(item.value, (bytes, complex)) or item.value is None:
                    _fail(FactorIRFailureCode.INVALID_ARGUMENT, "postprocess parameters must be scalar literals")
                params[f"arg{index}"] = item.value
            self.builder.postprocess.append(postprocess_step(raw_name, params))
            return base
        operator = ALIASES.get(raw_name, raw_name)
        expected = ARGUMENT_COUNTS.get(operator)
        if expected is None:
            _fail(FactorIRFailureCode.UNKNOWN_CALL, f"call {raw_name!r} is not allowlisted")
        if len(node.args) != expected:
            _fail(FactorIRFailureCode.INVALID_ARGUMENT, f"{raw_name} requires exactly {expected} arguments")
        raw_window: object | None = None
        if operator in WINDOW_ARGUMENT:
            index = WINDOW_ARGUMENT[operator]
            window_node = node.args[index]
            if isinstance(window_node, ast.UnaryOp) and isinstance(window_node.op, ast.USub) and isinstance(window_node.operand, ast.Constant):
                raw_window = -window_node.operand.value
            elif isinstance(window_node, ast.Constant):
                raw_window = window_node.value
            else:
                _fail(FactorIRFailureCode.DYNAMIC_WINDOW, f"{raw_name} window must be literal")
        arguments = tuple(self.expr(item, depth + 1) for item in node.args)
        return self.builder.call(operator, arguments, raw_window)


def _qlib_number(node: Any) -> int | float:
    if isinstance(node, QNumberNode):
        value = Decimal(node.canonical_value)
    elif isinstance(node, QUnaryNode) and isinstance(node.operand, QNumberNode) and node.operator in {"+", "-"}:
        value = Decimal(node.operand.canonical_value) * (-1 if node.operator == "-" else 1)
    else:
        _fail(FactorIRFailureCode.DYNAMIC_WINDOW, "window must be a literal integer")
    return int(value) if value == value.to_integral_value() else float(value)


def _compile_qlib_node(builder: _Builder, node: Any, depth: int = 1) -> _Value:
    if depth > MAX_DEPTH:
        _fail(FactorIRFailureCode.DEPTH_LIMIT_EXCEEDED, "expression depth exceeded")
    if isinstance(node, QFieldNode):
        return builder.field(node.name)
    if isinstance(node, QNumberNode):
        return builder.constant(float(node.canonical_value) if "." in node.canonical_value else int(node.canonical_value))
    if isinstance(node, QStringNode):
        _fail(FactorIRFailureCode.INVALID_ARGUMENT, "string literals are not factor values")
    if isinstance(node, QUnaryNode):
        value = _qlib_number(node)
        return builder.constant(value)
    if isinstance(node, QBinaryNode):
        return builder.call(_QBIN_OPS[node.operator], (_compile_qlib_node(builder, node.left, depth + 1), _compile_qlib_node(builder, node.right, depth + 1)))
    if isinstance(node, QCallNode):
        if node.name in POSTPROCESS_ALIASES:
            if not node.arguments:
                _fail(FactorIRFailureCode.INVALID_ARGUMENT, "postprocess requires a formula argument")
            base = _compile_qlib_node(builder, node.arguments[0], depth + 1)
            builder.postprocess.append(postprocess_step(node.name))
            return base
        operator = ALIASES.get(node.name, node.name)
        operator = NATIVE_TO_SAFE_ADAPTER.get(operator, operator)
        raw_window = _qlib_number(node.arguments[WINDOW_ARGUMENT[operator]]) if operator in WINDOW_ARGUMENT and len(node.arguments) > WINDOW_ARGUMENT[operator] else None
        arguments = tuple(_compile_qlib_node(builder, item, depth + 1) for item in node.arguments)
        return builder.call(operator, arguments, raw_window)
    _fail(FactorIRFailureCode.FORBIDDEN_NODE, "unknown Qlib AST node")


def _source_hash(source: str) -> str:
    if not isinstance(source, str) or not source.strip():
        _fail(FactorIRFailureCode.EMPTY_SOURCE, "formula source is empty")
    if len(source) > MAX_SOURCE_LENGTH:
        _fail(FactorIRFailureCode.SOURCE_TOO_LARGE, "formula source exceeds the static limit")
    return canonical_hash({"utf8_source": source})


def compile_legacy_python_ast(source: str, *, allowed_fields: Iterable[str]) -> FactorIRProgram:
    """Inspect a closed Python subset and return inert IR; never execute source."""

    source_hash = _source_hash(source)
    try:
        tree = ast.parse(source, mode="exec")
    except (SyntaxError, ValueError) as exc:
        raise FactorIRCompileError(FactorIRFailureCode.INVALID_SYNTAX, "legacy source is not valid syntax", line=getattr(exc, "lineno", None)) from exc
    builder = _Builder(allowed_fields=allowed_fields, source_hash=source_hash, source_kind="LEGACY_PYTHON_AST")
    return builder.build(_PythonCompiler(builder).module(tree))


def compile_qlib_safe_expression(expression: str, *, allowed_fields: Iterable[str]) -> FactorIRProgram:
    """Parse the repository's inert Qlib grammar, then close it into FactorIR."""

    source_hash = _source_hash(expression)
    fields = tuple(sorted(set(allowed_fields)))
    policy = QlibExpressionPolicy(field_roles={name: QlibFieldRole.FEATURE for name in fields}, max_nodes=MAX_NODES, max_depth=MAX_DEPTH)
    try:
        root = parse_qlib_expression(expression, policy)
    except Exception as exc:
        code = FactorIRFailureCode.INVALID_SYNTAX
        name = getattr(getattr(exc, "code", None), "value", "")
        if name in {"FORBIDDEN_SYNTAX", "INVALID_TOKEN"}:
            code = FactorIRFailureCode.FORBIDDEN_NODE
        raise FactorIRCompileError(code, str(exc), upstream_code=name) from exc
    builder = _Builder(allowed_fields=fields, source_hash=source_hash, source_kind="QLIB_EXPRESSION")
    return builder.build(_compile_qlib_node(builder, root))


def compile_factor_spec_draft(draft: object, *, allowed_fields: Iterable[str] | None = None) -> FactorIRProgram:
    """Compile a FactorSpecDraft-like object without granting it authority."""
    from ..contracts.models import FactorSpecDraft

    if type(draft) is FactorSpecDraft:
        expression = draft.proposed_expression
        declared = draft.required_fields
        requested = draft.lookback_bars
    elif type(draft) is dict:
        unknown = set(draft) - {"proposed_expression", "required_fields", "lookback_bars"}
        if unknown:
            _fail(FactorIRFailureCode.DRAFT_METADATA_MISMATCH, "draft mapping contains unknown fields", fields=tuple(sorted(unknown)))
        expression = draft.get("proposed_expression")
        declared = tuple(draft.get("required_fields", ()))
        requested = draft.get("lookback_bars")
    else:
        _fail(FactorIRFailureCode.INVALID_ARGUMENT, "draft must be an exact FactorSpecDraft or plain dict")
    fields = tuple(allowed_fields) if allowed_fields is not None else declared
    if not isinstance(expression, str) or not fields:
        _fail(FactorIRFailureCode.INVALID_ARGUMENT, "draft requires proposed_expression and approved fields")
    try:
        program = compile_qlib_safe_expression(expression, allowed_fields=fields) if "$" in expression else compile_legacy_python_ast(expression, allowed_fields=fields)
    except FactorIRCompileError:
        raise
    if declared and not set(program.required_fields).issubset(declared):
        _fail(FactorIRFailureCode.DRAFT_METADATA_MISMATCH, "compiled fields exceed draft declarations")
    if isinstance(requested, int) and not isinstance(requested, bool) and program.lookback > requested:
        _fail(FactorIRFailureCode.DRAFT_METADATA_MISMATCH, "compiled lookback exceeds draft declaration", compiled=program.lookback, declared=requested)
    return program


def try_compile_legacy_python_ast(source: str, *, allowed_fields: Iterable[str]) -> CompilationResult:
    try:
        return CompilationResult(program=compile_legacy_python_ast(source, allowed_fields=allowed_fields))
    except FactorIRCompileError as exc:
        source_hash = canonical_hash({"utf8_source": source}) if isinstance(source, str) else canonical_hash({"invalid_source_type": type(source).__name__})
        return CompilationResult(failure=CompilationFailure(exc.code, str(exc), source_hash, COMPILER_HASH, exc.details))


def try_compile_qlib_safe_expression(expression: str, *, allowed_fields: Iterable[str]) -> CompilationResult:
    try:
        return CompilationResult(program=compile_qlib_safe_expression(expression, allowed_fields=allowed_fields))
    except FactorIRCompileError as exc:
        source_hash = canonical_hash({"utf8_source": expression}) if isinstance(expression, str) else canonical_hash({"invalid_source_type": type(expression).__name__})
        return CompilationResult(failure=CompilationFailure(exc.code, str(exc), source_hash, COMPILER_HASH, exc.details))


class StaticFormulaCompiler:
    """Small façade exposing only static compilation operations."""

    compiler_hash = COMPILER_HASH

    def compile_qlib(self, expression: str, *, allowed_fields: Iterable[str]) -> FactorIRProgram:
        return compile_qlib_safe_expression(expression, allowed_fields=allowed_fields)

    def compile_legacy(self, source: str, *, allowed_fields: Iterable[str]) -> FactorIRProgram:
        return compile_legacy_python_ast(source, allowed_fields=allowed_fields)

    def compile_draft(self, draft: object, *, allowed_fields: Iterable[str] | None = None) -> FactorIRProgram:
        return compile_factor_spec_draft(draft, allowed_fields=allowed_fields)
