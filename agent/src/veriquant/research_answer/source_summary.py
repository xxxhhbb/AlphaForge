"""Deterministic, non-executing summaries of reviewed research source."""

from __future__ import annotations

import ast
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ReviewedSourceSummary:
    input_fields: tuple[str, ...]
    implementation_summary: str
    additive_fields: tuple[str, ...] = ()
    design_warnings: tuple[str, ...] = ()


def summarize_reviewed_source(source: str, *, output_kind: str) -> ReviewedSourceSummary | None:
    """Inspect syntax only; never import or execute generated source."""

    try:
        tree = ast.parse(source, mode="exec")
    except (SyntaxError, TypeError):
        return None
    ordered_fields: list[str] = []
    score_expressions: list[ast.expr] = []
    component_names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _call_name(node.func) == "get_feature" and len(node.args) >= 2:
            value = node.args[1]
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                ordered_fields.append(value.value)
        if isinstance(node, ast.Assign):
            names = [target.id for target in node.targets if isinstance(target, ast.Name)]
            if "feature_fields" in names:
                ordered_fields.extend(_string_sequence(node.value))
            if "components" in names:
                component_names.extend(_string_sequence(node.value))
            if "score" in names:
                score_expressions.append(node.value)
        if isinstance(node, ast.AugAssign) and isinstance(node.target, ast.Name) and node.target.id == "score":
            score_expressions.append(node.value)
    reserved = {
        "decision_time", "features", "label_horizon_sessions", "source_available_at",
        "symbol", "target_value",
    }
    display_fields = tuple(dict.fromkeys(
        item for item in ordered_fields if item not in reserved
    ))
    fields = tuple(sorted(set(display_fields)))
    if not fields and not score_expressions:
        return None
    pieces = []
    if fields:
        # Preserve source order in prose so indexed expressions such as
        # z_scores[0] remain correctly mapped.  The typed field tuple stays
        # stable-sorted for canonical serialization.
        pieces.append("输入字段（按源码顺序）：" + "、".join(display_fields))
    if component_names:
        pieces.append("组合分量顺序：" + "、".join(component_names))
    if score_expressions:
        pieces.append(
            "具名评分公式："
            + "；".join(_render_expression(item, display_fields) for item in score_expressions[-3:])
        )
    if output_kind == "ScoreTable":
        pieces.append("分数越高表示研究假设中的未来收益排序越高")
    else:
        pieces.append("该输出是研究分析表，不是可直接用于股票排序的 Alpha 分数")
    additive_fields = tuple(sorted({
        display_fields[index]
        for expression in score_expressions
        if output_kind == "ScoreTable"
        for index in _additive_z_score_indexes(expression, len(display_fields))
        if 0 <= index < len(display_fields)
    }))
    return ReviewedSourceSummary(
        input_fields=fields,
        implementation_summary="；".join(pieces) + "。",
        additive_fields=additive_fields,
    )


def _call_name(value: ast.expr) -> str:
    if isinstance(value, ast.Name):
        return value.id
    if isinstance(value, ast.Attribute):
        return value.attr
    return ""


def _string_sequence(value: ast.expr) -> list[str]:
    if not isinstance(value, (ast.List, ast.Tuple)):
        return []
    return [
        item.value for item in value.elts
        if isinstance(item, ast.Constant) and isinstance(item.value, str)
    ]


def _render_expression(value: ast.expr, fields: tuple[str, ...], parent_precedence: int = 0) -> str:
    """Render indexed feature vectors as customer-readable named formulas."""

    if isinstance(value, ast.Subscript) and isinstance(value.value, ast.Name):
        if value.value.id == "z_scores":
            index = _constant_index(value.slice)
            if index is not None and 0 <= index < len(fields):
                return f"Z({fields[index]})"
    if isinstance(value, ast.BinOp):
        operators = {
            ast.Add: ("+", 1), ast.Sub: ("-", 1),
            ast.Mult: ("×", 2), ast.Div: ("÷", 2),
        }
        symbol, precedence = operators.get(type(value.op), (ast.unparse(value.op), 1))
        left = _render_expression(value.left, fields, precedence)
        right = _render_expression(value.right, fields, precedence + (1 if isinstance(value.op, (ast.Sub, ast.Div)) else 0))
        rendered = f"{left} {symbol} {right}"
        return f"({rendered})" if precedence < parent_precedence else rendered
    if isinstance(value, ast.UnaryOp):
        symbol = "-" if isinstance(value.op, ast.USub) else "+"
        return symbol + _render_expression(value.operand, fields, 3)
    if isinstance(value, ast.Constant):
        return str(value.value)
    return ast.unparse(value)


def _is_additive_expression(value: ast.expr) -> bool:
    if isinstance(value, ast.BinOp) and isinstance(value.op, (ast.Add, ast.Sub)):
        return _is_additive_expression(value.left) and _is_additive_expression(value.right)
    if isinstance(value, ast.BinOp) and isinstance(value.op, ast.Div):
        return _is_additive_expression(value.left) and isinstance(value.right, ast.Constant)
    if isinstance(value, ast.UnaryOp):
        return _is_additive_expression(value.operand)
    return isinstance(value, (ast.Subscript, ast.Name, ast.Constant))


def _additive_z_score_indexes(value: ast.expr, field_count: int) -> set[int]:
    """Return only z-score components entering through an additive path.

    Multiplication/interaction intentionally stops traversal.  Common generated
    forms such as ``sum(z_scores) / len(z_scores)`` are still recognized.
    """

    if isinstance(value, ast.Subscript) and isinstance(value.value, ast.Name):
        if value.value.id == "z_scores":
            index = _constant_index(value.slice)
            return set() if index is None else {index}
    if isinstance(value, ast.BinOp) and isinstance(value.op, (ast.Add, ast.Sub)):
        return (
            _additive_z_score_indexes(value.left, field_count)
            | _additive_z_score_indexes(value.right, field_count)
        )
    if isinstance(value, ast.BinOp) and isinstance(value.op, ast.Div):
        if isinstance(value.right, ast.Constant) or (
            isinstance(value.right, ast.Call) and _call_name(value.right.func) == "len"
        ):
            return _additive_z_score_indexes(value.left, field_count)
    if isinstance(value, ast.UnaryOp):
        return _additive_z_score_indexes(value.operand, field_count)
    if (
        isinstance(value, ast.Call)
        and _call_name(value.func) == "sum"
        and value.args
        and isinstance(value.args[0], ast.Name)
        and value.args[0].id == "z_scores"
    ):
        return set(range(field_count))
    return set()


def _z_score_indexes(value: ast.AST) -> set[int]:
    indexes = set()
    for node in ast.walk(value):
        if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name) and node.value.id == "z_scores":
            index = _constant_index(node.slice)
            if index is not None:
                indexes.add(index)
    return indexes


def _constant_index(value: ast.expr) -> int | None:
    return value.value if isinstance(value, ast.Constant) and isinstance(value.value, int) else None


__all__ = ["ReviewedSourceSummary", "summarize_reviewed_source"]
