from __future__ import annotations

import pytest

from src.veriquant.contracts import ContractValidationError, FailureCode
from src.veriquant.data import build_synthetic_market_fixture
from src.veriquant.factors import parse_compile_factor_dag


def test_negative_lag_is_rejected_as_future_lookahead() -> None:
    fixture = build_synthetic_market_fixture()
    payload = {
        "schema_version": "veriquant.factor-dag/v1",
        "dag_id": "forbidden-future-lag",
        "nodes": [
            {"id": "close", "op": "field", "field": "close"},
            {"id": "future", "op": "lag", "inputs": ["close"], "periods": -1},
        ],
        "output_node": "future",
    }

    with pytest.raises(ContractValidationError) as caught:
        parse_compile_factor_dag(payload, field_roles=fixture.field_roles)

    assert caught.value.code is FailureCode.INVALID_FACTOR_DAG

