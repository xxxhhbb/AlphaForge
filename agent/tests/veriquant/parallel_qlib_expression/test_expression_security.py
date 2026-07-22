from __future__ import annotations

import pytest

from src.veriquant.qlib_expression.api import (
    PINNED_OPERATOR_NAMES,
    STATIC_QLIB_OPERATOR_MANIFEST,
    QlibExpressionError,
    QlibExpressionFailureCode,
    QlibExpressionPolicy,
    QlibFieldRole,
    QlibOperatorManifest,
    QlibOperatorSpec,
    QlibOperatorTrust,
    QlibRuntimeStatus,
    certify_qlib_expression,
    probe_qlib_runtime,
)


def policy(**overrides: object) -> QlibExpressionPolicy:
    values: dict[str, object] = {
        "field_roles": {
            "open": QlibFieldRole.FEATURE,
            "close": QlibFieldRole.FEATURE,
            "volume": QlibFieldRole.FEATURE,
            "future_return_20d": QlibFieldRole.LABEL,
            "sealed_rank": QlibFieldRole.SEALED,
        },
        "max_window": 252,
        "max_ref_periods": 252,
    }
    values.update(overrides)
    return QlibExpressionPolicy(**values)  # type: ignore[arg-type]


def failure(expression: str, expected: QlibExpressionFailureCode) -> None:
    with pytest.raises(QlibExpressionError) as caught:
        certify_qlib_expression(expression, policy=policy())
    assert caught.value.code is expected


def test_momentum_expression_is_canonical_and_has_dependency_plan() -> None:
    certified = certify_qlib_expression(
        "($close / Ref($close, 50)) - 1",
        policy=policy(),
    )
    assert certified.canonical_expression == "(($close/Ref($close,50))-1)"
    assert certified.required_fields == ("close",)
    assert certified.operators == ("Div", "Ref", "Sub")
    assert certified.maximum_lookback == 50


def test_whitespace_parentheses_and_decimal_spelling_have_same_hash() -> None:
    first = certify_qlib_expression("Mean($close, 20) / 1.0", policy=policy())
    second = certify_qlib_expression("(((Mean( $close , 20.000 )))/(1))", policy=policy())
    assert first.canonical_expression == second.canonical_expression
    assert first.expression_hash == second.expression_hash


@pytest.mark.parametrize(
    "expression",
    (
        "__import__('os').system('whoami')",
        "Abs.__call__($close)",
        "$close[0]",
        "Abs(x=$close)",
        "lambda($close)",
        "open('secret')",
        "[x for x in $close]",
        "{$close: 1}",
        "Abs($close);Ref($close,-1)",
    ),
)
def test_python_and_dynamic_attribute_surfaces_fail_closed(expression: str) -> None:
    with pytest.raises(QlibExpressionError) as caught:
        certify_qlib_expression(expression, policy=policy())
    assert caught.value.code in {
        QlibExpressionFailureCode.FORBIDDEN_SYNTAX,
        QlibExpressionFailureCode.INVALID_SYNTAX,
    }


def test_negative_ref_is_future_reference() -> None:
    failure("Ref($close, -1)", QlibExpressionFailureCode.FUTURE_REFERENCE)


def test_zero_ref_is_not_a_current_value_shortcut() -> None:
    failure("Ref($close, 0)", QlibExpressionFailureCode.INVALID_WINDOW)


@pytest.mark.parametrize("field", ("future_return_20d", "sealed_rank"))
def test_label_and_sealed_fields_are_rejected(field: str) -> None:
    failure(f"Mean(${field}, 20)", QlibExpressionFailureCode.FORBIDDEN_FIELD)


def test_unknown_field_and_operator_are_typed() -> None:
    failure("Mean($vwap, 20)", QlibExpressionFailureCode.UNKNOWN_FIELD)
    failure("CustomOperator($close, 20)", QlibExpressionFailureCode.UNKNOWN_OPERATOR)


def test_injected_custom_operator_cannot_extend_the_pinned_manifest() -> None:
    injected = QlibOperatorSpec("CustomOperator", 1, QlibOperatorTrust.TRUSTED)
    with pytest.raises(QlibExpressionError) as caught:
        QlibOperatorManifest(STATIC_QLIB_OPERATOR_MANIFEST.operators + (injected,))
    assert caught.value.code is QlibExpressionFailureCode.MANIFEST_INVALID


def test_excluded_builtin_and_policy_disabled_operator_fail_closed() -> None:
    failure("TResample($close, '1D', 'mean')", QlibExpressionFailureCode.OPERATOR_NOT_TRUSTED)
    restricted = policy(allowed_operators=("Mean",))
    with pytest.raises(QlibExpressionError) as caught:
        certify_qlib_expression("Std($close, 20)", policy=restricted)
    assert caught.value.code is QlibExpressionFailureCode.OPERATOR_NOT_TRUSTED


def test_window_quantile_and_argument_contracts() -> None:
    failure("Mean($close, 253)", QlibExpressionFailureCode.INVALID_WINDOW)
    failure("Mean($close, 20.5)", QlibExpressionFailureCode.INVALID_WINDOW)
    failure("Quantile($close, 20, 1.1)", QlibExpressionFailureCode.INVALID_ARGUMENT)
    failure("Corr($close, $volume)", QlibExpressionFailureCode.INVALID_ARGUMENT_COUNT)


def test_condition_pair_rolling_and_composite_operators_are_supported() -> None:
    certified = certify_qlib_expression(
        "If(Corr($close,$volume,20)>0,Sign(Power(Abs($close),2)),0)",
        policy=policy(),
    )
    assert certified.required_fields == ("close", "volume")
    assert set(certified.operators) == {"Abs", "Corr", "Gt", "If", "Power", "Sign"}
    assert certified.maximum_lookback == 19


def test_instrument_change_needs_an_explicit_approved_benchmark() -> None:
    with pytest.raises(QlibExpressionError) as caught:
        certify_qlib_expression("ChangeInstrument('SH000300',$close)", policy=policy())
    assert caught.value.code is QlibExpressionFailureCode.INVALID_ARGUMENT

    approved = certify_qlib_expression(
        "Corr($close,ChangeInstrument('SH000300',$close),60)",
        policy=policy(approved_instruments=("SH000300",)),
    )
    assert approved.maximum_lookback == 59


def test_pinned_manifest_classifies_the_entire_qlib_097_opslist() -> None:
    assert len(PINNED_OPERATOR_NAMES) == 52
    assert set(STATIC_QLIB_OPERATOR_MANIFEST.by_name) == set(PINNED_OPERATOR_NAMES)
    assert not any(item.trust.value == "UNREVIEWED" for item in STATIC_QLIB_OPERATOR_MANIFEST.operators)
    for name in ("Rolling", "Feature", "PFeature", "TResample", "P", "PRef"):
        assert STATIC_QLIB_OPERATOR_MANIFEST.by_name[name].trust.value == "EXCLUDED"
    assert STATIC_QLIB_OPERATOR_MANIFEST.manifest_hash.startswith("sha256:")


def test_runtime_probe_is_typed_and_never_installs() -> None:
    unavailable = probe_qlib_runtime(module_finder=lambda _: None)
    assert unavailable.status is QlibRuntimeStatus.UNAVAILABLE
    assert unavailable.failure_code is QlibExpressionFailureCode.QLIB_RUNTIME_UNAVAILABLE
    assert "no installation or download" in unavailable.message

    mismatch = probe_qlib_runtime(module_finder=lambda _: object(), version_reader=lambda _: "0.9.8")
    assert mismatch.status is QlibRuntimeStatus.VERSION_MISMATCH
    assert mismatch.failure_code is QlibExpressionFailureCode.QLIB_RUNTIME_VERSION_MISMATCH

    available = probe_qlib_runtime(module_finder=lambda _: object(), version_reader=lambda _: "0.9.7")
    assert available.status is QlibRuntimeStatus.AVAILABLE
    assert available.failure_code is None


def test_depth_node_and_expression_length_limits_are_enforced() -> None:
    with pytest.raises(QlibExpressionError) as caught:
        certify_qlib_expression("Abs(Abs(Abs($close)))", policy=policy(max_depth=2))
    assert caught.value.code is QlibExpressionFailureCode.DEPTH_LIMIT_EXCEEDED

    with pytest.raises(QlibExpressionError) as caught:
        certify_qlib_expression("$close+$open+$volume", policy=policy(max_nodes=3))
    assert caught.value.code is QlibExpressionFailureCode.SIZE_LIMIT_EXCEEDED

    with pytest.raises(QlibExpressionError) as caught:
        certify_qlib_expression("Mean($close,20)", policy=policy(max_expression_length=8))
    assert caught.value.code is QlibExpressionFailureCode.EXPRESSION_TOO_LONG
