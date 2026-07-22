"""Build deterministic registered-factor controls from the frozen V3 snapshot."""

from __future__ import annotations

from typing import Mapping

from ..data import FieldRole
from ..evidence import EvaluationRow, RegisteredFactor
from ..evidence.service import row_key
from ..factors import execute_factor_dag, parse_compile_factor_dag
from ..research_validation_pipeline_v2 import FrozenValidationData


_BASELINES: Mapping[str, Mapping[str, object]] = {
    "registered_momentum_20d": {
        "schema_version": "veriquant.factor-dag/v1",
        "dag_id": "registered-momentum-20d-v3",
        "nodes": [
            {"id": "close", "op": "field", "field": "close"},
            {"id": "m20", "op": "pct_change", "inputs": ["close"], "periods": 20},
            {"id": "rank", "op": "rank_cross_section", "inputs": ["m20"]},
        ],
        "output_node": "rank",
    },
    "registered_size_zscore": {
        "schema_version": "veriquant.factor-dag/v1",
        "dag_id": "registered-size-zscore-v3",
        "nodes": [
            {"id": "market_cap", "op": "field", "field": "market_cap"},
            {"id": "z", "op": "zscore_cross_section", "inputs": ["market_cap"]},
        ],
        "output_node": "z",
    },
}


def build_registered_baselines(
    data: FrozenValidationData,
    *,
    seed: int = 7,
    minimum_coverage: float = 0.8,
) -> tuple[RegisteredFactor, ...]:
    """Return only baselines supported by the same frozen, PIT-safe snapshot."""

    fields = set(data.snapshot.fields)
    coverage = data.snapshot.coverage_by_field
    results: list[RegisteredFactor] = []
    for factor_id, payload in sorted(_BASELINES.items()):
        required = {
            str(node["field"])
            for node in payload["nodes"]  # type: ignore[index]
            if isinstance(node, dict) and node.get("op") == "field"
        }
        if not required <= fields or any(float(coverage.get(field, 0.0)) < minimum_coverage for field in required):
            continue
        roles = {field: FieldRole.FEATURE for field in required}
        dag = parse_compile_factor_dag(payload, field_roles=roles)
        executed = execute_factor_dag(dag, data.snapshot, seed=seed, config={"role": "REGISTERED_BASELINE"})
        scores = {
            row_key(EvaluationRow(
                event_time=item.event_time,
                symbol=item.symbol,
                score=float(item.value),
                label=0.0,
                market_regime="REGISTERED_BASELINE",
                volume=1.0,
            )): float(item.value)
            for item in executed.values
            if item.value is not None
        }
        if scores:
            results.append(RegisteredFactor(factor_id=factor_id, factor_hash=dag.artifact_hash, scores=scores))
    return tuple(results)


__all__ = ["build_registered_baselines"]
