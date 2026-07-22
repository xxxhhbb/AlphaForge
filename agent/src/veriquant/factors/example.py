"""Fixed 20-session momentum vertical slice through a separate 5-day label."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from ..contracts import CanonicalContract
from ..contracts.base import require_aware_datetime, require_hash, require_identifier
from ..data import DatasetRegistry, LABEL_FIELD
from .dag import FactorResult, execute_factor_dag, parse_compile_factor_dag


def momentum_20_dag_payload() -> dict[str, object]:
    return {
        "schema_version": "veriquant.factor-dag/v1",
        "dag_id": "momentum-20-ranked",
        "nodes": [
            {"id": "close-field", "op": "field", "field": "close"},
            {
                "id": "momentum-20",
                "op": "pct_change",
                "inputs": ["close-field"],
                "periods": 20,
            },
            {
                "id": "momentum-winsorized",
                "op": "winsorize",
                "inputs": ["momentum-20"],
                "lower": 0.05,
                "upper": 0.95,
            },
            {
                "id": "momentum-ranked",
                "op": "rank_cross_section",
                "inputs": ["momentum-winsorized"],
            },
        ],
        "output_node": "momentum-ranked",
    }


@dataclass(frozen=True, kw_only=True)
class FactorLabelRow:
    event_time: datetime
    symbol: str
    factor_value: float
    future_return_5d: float

    def __post_init__(self) -> None:
        require_aware_datetime(self.event_time, "event_time")
        require_identifier(self.symbol, "symbol")


@dataclass(frozen=True, kw_only=True)
class FactorLabelRun(CanonicalContract):
    dataset_id: str
    factor_result_hash: str
    label_field: str
    evaluation_time: datetime
    rows: tuple[FactorLabelRow, ...]

    def __post_init__(self) -> None:
        require_identifier(self.dataset_id, "dataset_id")
        require_hash(self.factor_result_hash, "factor_result_hash")
        if self.label_field != LABEL_FIELD:
            raise ValueError("the fixed example requires future_return_5d")
        require_aware_datetime(self.evaluation_time, "evaluation_time")


def run_momentum_20_to_label(
    registry: DatasetRegistry,
    *,
    dataset_id: str,
    decision_time: datetime,
    evaluation_time: datetime | None = None,
    seed: int = 0,
) -> tuple[FactorResult, FactorLabelRun]:
    """Run a feature-only factor, then join labels after their availability gate."""

    dataset = registry.get(dataset_id)
    snapshot = registry.as_of_snapshot(
        dataset_id,
        decision_time=decision_time,
        fields=("close",),
        start_time=dataset.calendar[0],
    )
    compiled = parse_compile_factor_dag(
        momentum_20_dag_payload(), field_roles=dataset.field_roles
    )
    factor_result = execute_factor_dag(
        compiled,
        snapshot,
        seed=seed,
        config={"example": "momentum_20", "label": LABEL_FIELD},
    )
    label_cutoff = evaluation_time or dataset.card.decision_time
    labels = registry.labels(
        dataset_id,
        label_field=LABEL_FIELD,
        evaluation_time=label_cutoff,
    )
    label_by_key = {(row.event_time, row.symbol): row.value for row in labels}
    joined = tuple(
        FactorLabelRow(
            event_time=value.event_time,
            symbol=value.symbol,
            factor_value=value.value,
            future_return_5d=label_by_key[(value.event_time, value.symbol)],
        )
        for value in factor_result.values
        if value.value is not None and label_by_key.get((value.event_time, value.symbol)) is not None
    )
    return factor_result, FactorLabelRun(
        dataset_id=dataset_id,
        factor_result_hash=factor_result.artifact_hash,
        label_field=LABEL_FIELD,
        evaluation_time=label_cutoff,
        rows=joined,
    )

