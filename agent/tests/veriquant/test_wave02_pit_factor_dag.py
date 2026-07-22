from __future__ import annotations

import math
from datetime import timedelta

import pytest

from src.veriquant.contracts import ContractValidationError, FailureCode
from src.veriquant.data import DatasetRegistry, LABEL_FIELD, build_synthetic_market_fixture
from src.veriquant.factors import (
    execute_factor_dag,
    momentum_20_dag_payload,
    parse_compile_factor_dag,
    run_momentum_20_to_label,
)


def registered_fixture(**kwargs):
    fixture = build_synthetic_market_fixture(**kwargs)
    registry = DatasetRegistry()
    card = fixture.register(registry)
    return fixture, registry, card


def close_pct_change_payload(periods: int = 20) -> dict[str, object]:
    return {
        "schema_version": "veriquant.factor-dag/v1",
        "dag_id": "close-change",
        "nodes": [
            {"id": "close-field", "op": "field", "field": "close"},
            {
                "id": "close-change",
                "op": "pct_change",
                "inputs": ["close-field"],
                "periods": periods,
            },
        ],
        "output_node": "close-change",
    }


def result_map(result):
    return {(row.event_time, row.symbol): row.value for row in result.values}


def test_same_frozen_input_config_and_seed_reproduce_hash_and_output() -> None:
    first, first_registry, first_card = registered_fixture(seed=17)
    second, second_registry, second_card = registered_fixture(seed=17)
    decision_time = first.calendar[47] + timedelta(hours=7)

    first_snapshot = first_registry.as_of_snapshot(
        first.dataset_id, decision_time=decision_time, fields=("close",)
    )
    second_snapshot = second_registry.as_of_snapshot(
        second.dataset_id, decision_time=decision_time, fields=("close",)
    )
    first_dag = parse_compile_factor_dag(
        momentum_20_dag_payload(), field_roles=first.field_roles
    )
    second_dag = parse_compile_factor_dag(
        momentum_20_dag_payload(), field_roles=second.field_roles
    )
    config = {"purpose": "reproducibility-test", "normalization": "rank"}
    first_result = execute_factor_dag(first_dag, first_snapshot, seed=23, config=config)
    second_result = execute_factor_dag(second_dag, second_snapshot, seed=23, config=config)

    assert first_card.snapshot_hash == second_card.snapshot_hash
    assert first_snapshot.artifact_hash == second_snapshot.artifact_hash
    assert first_dag.artifact_hash == second_dag.artifact_hash
    assert first_result.artifact_hash == second_result.artifact_hash
    assert first_result.values == second_result.values


def test_future_perturbation_does_not_change_past_snapshot_or_factor() -> None:
    baseline, baseline_registry, baseline_card = registered_fixture(seed=9)
    shocked, shocked_registry, shocked_card = registered_fixture(
        seed=9,
        future_price_shock_start=46,
        future_price_shock_multiplier=1.8,
    )
    decision_time = baseline.calendar[40] + timedelta(hours=7)
    base_snapshot = baseline_registry.as_of_snapshot(
        baseline.dataset_id, decision_time=decision_time, fields=("close",)
    )
    shock_snapshot = shocked_registry.as_of_snapshot(
        shocked.dataset_id, decision_time=decision_time, fields=("close",)
    )
    dag = parse_compile_factor_dag(
        momentum_20_dag_payload(), field_roles=baseline.field_roles
    )
    base_result = execute_factor_dag(dag, base_snapshot, seed=5, config={"fixed": True})
    shock_result = execute_factor_dag(dag, shock_snapshot, seed=5, config={"fixed": True})

    assert baseline_card.snapshot_hash != shocked_card.snapshot_hash
    assert base_snapshot.artifact_hash == shock_snapshot.artifact_hash
    assert base_result.artifact_hash == shock_result.artifact_hash
    assert base_result.values == shock_result.values


def test_available_at_lag_delays_signal_by_exact_fixture_sessions() -> None:
    immediate, immediate_registry, _ = registered_fixture(seed=11)
    delayed, delayed_registry, _ = registered_fixture(
        seed=11, market_availability_lag_steps=2
    )
    decision_time = immediate.calendar[52] + timedelta(hours=7)
    immediate_snapshot = immediate_registry.as_of_snapshot(
        immediate.dataset_id, decision_time=decision_time, fields=("close",)
    )
    delayed_snapshot = delayed_registry.as_of_snapshot(
        delayed.dataset_id, decision_time=decision_time, fields=("close",)
    )
    immediate_dag = parse_compile_factor_dag(
        close_pct_change_payload(), field_roles=immediate.field_roles
    )
    delayed_dag = parse_compile_factor_dag(
        close_pct_change_payload(), field_roles=delayed.field_roles
    )
    immediate_values = result_map(execute_factor_dag(immediate_dag, immediate_snapshot))
    delayed_values = result_map(execute_factor_dag(delayed_dag, delayed_snapshot))

    for index in range(24, 51):
        assert delayed_values[(delayed.calendar[index], "AAA")] == pytest.approx(
            immediate_values[(immediate.calendar[index - 2], "AAA")]
        )


def test_historical_universe_prevents_future_member_and_survivor_leakage() -> None:
    fixture, registry, _ = registered_fixture()
    decision_time = fixture.calendar[50] + timedelta(hours=7)
    snapshot = registry.as_of_snapshot(
        fixture.dataset_id, decision_time=decision_time, fields=("close",)
    )
    early = snapshot.historical_universe[fixture.calendar[10].isoformat()]
    after_entry = snapshot.historical_universe[fixture.calendar[25].isoformat()]
    after_exit = snapshot.historical_universe[fixture.calendar[45].isoformat()]

    assert "CCC" not in early
    assert "BBB" in early
    assert "CCC" in after_entry
    assert "BBB" not in after_exit
    assert registry.universe_snapshot(
        fixture.dataset_id, fixture.calendar[10] + timedelta(hours=7)
    ) == ("AAA", "BBB", "DDD")


def test_financial_revision_and_coverage_are_bitemporal() -> None:
    fixture, registry, _ = registered_fixture()
    initial = registry.as_of_snapshot(
        fixture.dataset_id,
        decision_time=fixture.calendar[10] + timedelta(hours=7),
        fields=("book_value", "market_cap", "macro_rate"),
    )
    revised = registry.as_of_snapshot(
        fixture.dataset_id,
        decision_time=fixture.calendar[18] + timedelta(hours=7),
        fields=("book_value", "market_cap", "macro_rate"),
    )
    late = registry.as_of_snapshot(
        fixture.dataset_id,
        decision_time=fixture.calendar[40] + timedelta(hours=7),
        fields=("book_value", "market_cap", "macro_rate"),
    )

    initial_aaa = [
        row.values["book_value"]
        for row in initial.rows
        if row.event_time == fixture.calendar[10] and row.symbol == "AAA"
    ][0]
    revised_aaa = [
        row.values["book_value"]
        for row in revised.rows
        if row.event_time == fixture.calendar[18] and row.symbol == "AAA"
    ][0]
    assert initial_aaa == 24.0
    assert revised_aaa == 24.8
    assert late.coverage_by_field["book_value"] > initial.coverage_by_field["book_value"]
    assert any(row.values["macro_rate"] is None for row in late.rows)


def test_label_is_separate_and_never_accepted_as_feature() -> None:
    fixture, registry, _ = registered_fixture()
    decision_time = fixture.calendar[40] + timedelta(hours=7)

    with pytest.raises(ContractValidationError) as snapshot_error:
        registry.as_of_snapshot(
            fixture.dataset_id,
            decision_time=decision_time,
            fields=(LABEL_FIELD,),
        )
    assert snapshot_error.value.code is FailureCode.UNAUTHORIZED_CAPABILITY

    label_dag = {
        "schema_version": "veriquant.factor-dag/v1",
        "dag_id": "forbidden-label",
        "nodes": [{"id": "label", "op": "field", "field": LABEL_FIELD}],
        "output_node": "label",
    }
    with pytest.raises(ContractValidationError) as dag_error:
        parse_compile_factor_dag(label_dag, field_roles=fixture.field_roles)
    assert dag_error.value.code is FailureCode.UNAUTHORIZED_CAPABILITY


@pytest.mark.parametrize(
    ("payload", "code"),
    [
        (
            {
                "schema_version": "veriquant.factor-dag/v1",
                "dag_id": "unknown-field",
                "nodes": [{"id": "bad", "op": "field", "field": "future_price"}],
                "output_node": "bad",
            },
            FailureCode.UNKNOWN_FIELD,
        ),
        (
            {
                "schema_version": "veriquant.factor-dag/v1",
                "dag_id": "unknown-operator",
                "nodes": [{"id": "bad", "op": "python", "field": "close"}],
                "output_node": "bad",
            },
            FailureCode.INVALID_FACTOR_DAG,
        ),
        (close_pct_change_payload(periods=0), FailureCode.INVALID_FACTOR_DAG),
        (close_pct_change_payload(periods=-1), FailureCode.INVALID_FACTOR_DAG),
        (close_pct_change_payload(periods=253), FailureCode.INVALID_FACTOR_DAG),
    ],
)
def test_unknown_fields_operators_illegal_windows_and_future_lag_fail_closed(
    payload: dict[str, object], code: FailureCode
) -> None:
    fixture = build_synthetic_market_fixture()
    with pytest.raises(ContractValidationError) as caught:
        parse_compile_factor_dag(payload, field_roles=fixture.field_roles)
    assert caught.value.code is code


def test_callable_and_import_shaped_payloads_are_inert_and_rejected() -> None:
    fixture = build_synthetic_market_fixture()
    callable_payload = close_pct_change_payload()
    callable_payload["callback"] = lambda value: value
    with pytest.raises(ContractValidationError) as callable_error:
        parse_compile_factor_dag(callable_payload, field_roles=fixture.field_roles)
    assert callable_error.value.code is FailureCode.INVALID_FACTOR_DAG

    import_payload = close_pct_change_payload()
    import_payload["import"] = "os"
    with pytest.raises(ContractValidationError) as import_error:
        parse_compile_factor_dag(import_payload, field_roles=fixture.field_roles)
    assert import_error.value.code is FailureCode.UNKNOWN_FIELD


def test_divide_safe_maps_zero_denominator_to_missing_not_infinity() -> None:
    fixture, registry, _ = registered_fixture()
    decision_time = fixture.calendar[10] + timedelta(hours=7)
    snapshot = registry.as_of_snapshot(
        fixture.dataset_id, decision_time=decision_time, fields=("close",)
    )
    payload = {
        "schema_version": "veriquant.factor-dag/v1",
        "dag_id": "safe-zero-division",
        "nodes": [
            {"id": "close", "op": "field", "field": "close"},
            {"id": "zero", "op": "subtract", "inputs": ["close", "close"]},
            {
                "id": "ratio",
                "op": "divide_safe",
                "inputs": ["close", "zero"],
                "epsilon": 1e-12,
            },
        ],
        "output_node": "ratio",
    }
    dag = parse_compile_factor_dag(payload, field_roles=fixture.field_roles)
    result = execute_factor_dag(dag, snapshot)

    assert result.values
    assert all(item.value is None for item in result.values)
    assert not any(item.value is not None and math.isinf(item.value) for item in result.values)


def test_every_whitelisted_operator_compiles_and_executes() -> None:
    fixture, registry, _ = registered_fixture()
    decision_time = fixture.calendar[35] + timedelta(hours=7)
    snapshot = registry.as_of_snapshot(
        fixture.dataset_id,
        decision_time=decision_time,
        fields=("close", "volume", "industry"),
    )
    payload = {
        "schema_version": "veriquant.factor-dag/v1",
        "dag_id": "operator-coverage",
        "nodes": [
            {"id": "close", "op": "field", "field": "close"},
            {"id": "volume", "op": "field", "field": "volume"},
            {"id": "industry", "op": "field", "field": "industry"},
            {"id": "lagged", "op": "lag", "inputs": ["close"], "periods": 1},
            {"id": "mean", "op": "rolling_mean", "inputs": ["close"], "window": 3},
            {"id": "std", "op": "rolling_std", "inputs": ["close"], "window": 3},
            {"id": "change", "op": "pct_change", "inputs": ["close"], "periods": 2},
            {"id": "sub", "op": "subtract", "inputs": ["close", "lagged"]},
            {"id": "add", "op": "add", "inputs": ["mean", "std"]},
            {"id": "mul", "op": "multiply", "inputs": ["sub", "add"]},
            {"id": "div", "op": "divide_safe", "inputs": ["mul", "volume"]},
            {"id": "winsor", "op": "winsorize", "inputs": ["change"]},
            {"id": "rank", "op": "rank_cross_section", "inputs": ["winsor"]},
            {"id": "zscore", "op": "zscore_cross_section", "inputs": ["rank"]},
            {
                "id": "neutral",
                "op": "neutralize_by_group",
                "inputs": ["zscore", "industry"],
            },
        ],
        "output_node": "neutral",
    }
    dag = parse_compile_factor_dag(payload, field_roles=fixture.field_roles)
    result = execute_factor_dag(dag, snapshot, config={"all_operators": True})

    assert result.values
    assert len(dag.nodes) == 15
    assert any(item.value is not None for item in result.values)


def test_fixed_momentum_factor_runs_through_separate_future_5d_label() -> None:
    fixture, registry, card = registered_fixture(seed=31)
    decision_time = fixture.calendar[50] + timedelta(hours=7)
    factor_result, joined = run_momentum_20_to_label(
        registry,
        dataset_id=fixture.dataset_id,
        decision_time=decision_time,
        evaluation_time=card.decision_time,
        seed=31,
    )

    assert factor_result.values
    assert joined.rows
    assert joined.factor_result_hash == factor_result.artifact_hash
    assert joined.label_field == LABEL_FIELD
    assert all(math.isfinite(row.future_return_5d) for row in joined.rows)

