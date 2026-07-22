from __future__ import annotations

from dataclasses import replace

import pytest

from veriquant.factor_ir_v2 import FactorIRCompileError, FactorIRFailureCode
from veriquant.static_formula_compiler import (
    compile_factor_spec_draft,
    compile_legacy_python_ast,
    compile_qlib_safe_expression,
    try_compile_legacy_python_ast,
)


FIELDS = ("close", "high", "industry", "low", "open", "size", "volume")


def test_qlib_native_and_safe_adapters_have_complete_identity() -> None:
    program = compile_qlib_safe_expression(
        "SafeIf($close>$open,SafeWMA($close,5),SafeIdxMin($volume,3))",
        allowed_fields=FIELDS,
    )
    assert program.required_fields == ("close", "open", "volume")
    assert program.lookback == 4
    assert {item.adapter for item in program.semantic_adapter_policies} == {"SafeIf", "SafeIdxMin", "SafeWMA"}
    assert all(item.policy_hash.startswith("sha256:") for item in program.semantic_adapter_policies)
    assert program.identity.program_hash.startswith("sha256:")
    assert program.identity.source_hash.startswith("sha256:")
    assert program.identity.compiler_hash.startswith("sha256:")
    assert program.program_hash == program.identity.program_hash
    assert program.source_hash == program.identity.source_hash
    assert program.compiler_hash == program.identity.compiler_hash


def test_qlib_replay_and_whitespace_canonical_nodes() -> None:
    left = compile_qlib_safe_expression("Mean(Ref($close,2),5)", allowed_fields=FIELDS)
    right = compile_qlib_safe_expression("Mean(Ref($close,2),5)", allowed_fields=tuple(reversed(FIELDS)))
    assert left.to_canonical() == right.to_canonical()
    assert left.lookback == 6


def test_source_hash_is_bound_even_when_semantics_match() -> None:
    left = compile_qlib_safe_expression("Mean($close,5)", allowed_fields=FIELDS)
    right = compile_qlib_safe_expression("Mean( $close , 5 )", allowed_fields=FIELDS)
    assert [node.to_canonical() for node in left.nodes] == [node.to_canonical() for node in right.nodes]
    assert left.identity.source_hash != right.identity.source_hash
    assert left.identity.program_hash != right.identity.program_hash


def test_legacy_ssa_arithmetic_boolean_ternary_and_postprocess() -> None:
    source = """
momentum = close / ref(close, 5) - 1
liquid = volume > ts_mean(volume, 10)
signal = momentum if liquid and close > open else -momentum
zscore(signal)
"""
    program = compile_legacy_python_ast(source, allowed_fields=FIELDS)
    assert program.required_fields == ("close", "open", "volume")
    assert program.lookback == 9
    assert [step.operation for step in program.postprocess_plan] == ["zscore_cross_section"]
    assert "SafeIf" in {item.adapter for item in program.semantic_adapter_policies}


def test_legacy_panel_literal_field_is_read_statically() -> None:
    program = compile_legacy_python_ast("x = panel['close']\nts_mean(x, 3)", allowed_fields=FIELDS)
    assert program.required_fields == ("close",)
    assert program.lookback == 2


@pytest.mark.parametrize(
    ("source", "code"),
    [
        ("import os\nclose", FactorIRFailureCode.FORBIDDEN_STATEMENT),
        ("for x in close:\n    x", FactorIRFailureCode.FORBIDDEN_STATEMENT),
        ("def compute():\n    return close", FactorIRFailureCode.FORBIDDEN_STATEMENT),
        ("obj.method(close)", FactorIRFailureCode.UNKNOWN_CALL),
        ("unknown(close)", FactorIRFailureCode.UNKNOWN_CALL),
        ("missing_name + close", FactorIRFailureCode.UNKNOWN_NAME),
        ("x = close\nx = open\nx", FactorIRFailureCode.SSA_REASSIGNMENT),
        ("ts_mean(close, window)", FactorIRFailureCode.DYNAMIC_WINDOW),
        ("ref(close, -1)", FactorIRFailureCode.FUTURE_REFERENCE),
        ("lambda x: x", FactorIRFailureCode.FORBIDDEN_NODE),
    ],
)
def test_legacy_forbidden_surfaces_are_typed(source: str, code: FactorIRFailureCode) -> None:
    with pytest.raises(FactorIRCompileError) as caught:
        compile_legacy_python_ast(source, allowed_fields=FIELDS)
    assert caught.value.code is code


def test_unknown_and_privileged_fields_fail_closed() -> None:
    with pytest.raises(FactorIRCompileError) as unknown:
        compile_qlib_safe_expression("$turnover", allowed_fields=FIELDS)
    assert unknown.value.code is FactorIRFailureCode.UNKNOWN_FIELD
    with pytest.raises(FactorIRCompileError) as forbidden:
        compile_qlib_safe_expression("$sealed_label", allowed_fields=FIELDS)
    assert forbidden.value.code is FactorIRFailureCode.FORBIDDEN_FIELD


@pytest.mark.parametrize(
    ("expression", "adapter"),
    [
        ("If($close>$open,$close,$open)", "SafeIf"),
        ("IdxMax($high,20)", "SafeIdxMax"),
        ("IdxMin($low,20)", "SafeIdxMin"),
        ("WMA($close,12)", "SafeWMA"),
    ],
)
def test_known_difference_native_names_are_always_safe_adapters(expression: str, adapter: str) -> None:
    program = compile_qlib_safe_expression(expression, allowed_fields=FIELDS)
    assert program.nodes[-1].operator == adapter
    assert program.semantic_adapter_policies[0].adapter == adapter


def test_postprocess_policy_changes_static_program_identity() -> None:
    base = compile_legacy_python_ast("close", allowed_fields=FIELDS)
    ranked = compile_legacy_python_ast("rank(close)", allowed_fields=FIELDS)
    assert base.identity.program_hash != ranked.identity.program_hash
    assert ranked.identity.postprocess_plan_hash != base.identity.postprocess_plan_hash


def test_factor_spec_draft_compiles_but_cannot_expand_declared_fields() -> None:
    draft = {"proposed_expression": "ts_mean(close, 5)", "required_fields": ("close",), "lookback_bars": 5}
    assert compile_factor_spec_draft(draft).required_fields == ("close",)
    with pytest.raises(FactorIRCompileError) as caught:
        compile_factor_spec_draft(
            {"proposed_expression": "close + open", "required_fields": ("close",), "lookback_bars": 5},
            allowed_fields=("close", "open"),
        )
    assert caught.value.code is FactorIRFailureCode.DRAFT_METADATA_MISMATCH


def test_draft_rejects_arbitrary_attribute_execution() -> None:
    class Hostile:
        def __getattr__(self, name: str) -> object:
            raise AssertionError(f"attribute executed: {name}")

    with pytest.raises(FactorIRCompileError) as caught:
        compile_factor_spec_draft(Hostile())
    assert caught.value.code is FactorIRFailureCode.INVALID_ARGUMENT


def test_privileged_field_is_rejected_even_when_caller_allowlists_it() -> None:
    with pytest.raises(FactorIRCompileError) as caught:
        compile_qlib_safe_expression("$sealed_label", allowed_fields=("sealed_label",))
    assert caught.value.code is FactorIRFailureCode.FORBIDDEN_FIELD


def test_policy_summary_hash_tampering_is_rejected() -> None:
    program = compile_qlib_safe_expression("SafeWMA($close,5)", allowed_fields=FIELDS)
    tampered_identity = replace(program.identity, semantic_adapter_policy_hash="sha256:" + "0" * 64)
    with pytest.raises(FactorIRCompileError) as caught:
        replace(program, identity=tampered_identity)
    assert caught.value.code is FactorIRFailureCode.POLICY_MISMATCH


def test_typed_result_never_masquerades_as_program() -> None:
    result = try_compile_legacy_python_ast("open('secret')", allowed_fields=FIELDS)
    assert result.ok is False
    assert result.program is None
    assert result.failure is not None
    assert result.failure.code is FactorIRFailureCode.UNKNOWN_CALL
    assert result.failure.source_hash.startswith("sha256:")


def test_no_callable_can_enter_ir_attributes() -> None:
    with pytest.raises(FactorIRCompileError):
        compile_legacy_python_ast("close.__class__", allowed_fields=FIELDS)
