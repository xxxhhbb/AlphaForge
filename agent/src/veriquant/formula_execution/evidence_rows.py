"""Backend-neutral conversion from certified scores to Evidence input rows."""

from __future__ import annotations

import math
from typing import Mapping

from ..contracts import FailureCode
from ..contracts.base import fail
from ..data import MarketSnapshot
from ..evidence import EvaluationRow
from .models import FactorScoreArtifact


def score_key(event_time, symbol: str) -> str:
    return f"{event_time.isoformat()}|{symbol}"


def evaluation_rows_from_scores(
    *,
    scores: FactorScoreArtifact,
    snapshot: MarketSnapshot,
    labels: Mapping[str, float],
    market_regimes: Mapping[str, str] | None = None,
) -> tuple[EvaluationRow, ...]:
    """Bind score points to labels/capacity fields without knowing the backend."""

    if not isinstance(scores, FactorScoreArtifact) or not isinstance(snapshot, MarketSnapshot):
        fail(FailureCode.INVALID_VALUE, "typed scores and MarketSnapshot are required")
    if scores.snapshot_hash != snapshot.artifact_hash:
        fail(FailureCode.INVALID_HASH, "score artifact does not bind the frozen snapshot")
    if "volume" not in snapshot.fields:
        fail(FailureCode.INVALID_VALUE, "volume is required for cost/capacity-aware evidence")
    row_by_key = {
        score_key(row.event_time, row.symbol): row
        for row in snapshot.rows
    }
    regimes = market_regimes or {}
    rows: list[EvaluationRow] = []
    missing_labels = 0
    missing_snapshot_rows = 0
    missing_or_bad_volume = 0
    for value in scores.values:
        if value.score is None:
            continue
        key = score_key(value.event_time, value.symbol)
        source = row_by_key.get(key)
        if source is None:
            missing_snapshot_rows += 1
            continue
        if key not in labels:
            missing_labels += 1
            continue
        label = labels[key]
        if isinstance(label, bool) or not isinstance(label, (int, float)) or not math.isfinite(float(label)):
            fail(FailureCode.INVALID_VALUE, "labels must be finite numeric values")
        volume = source.values.get("volume")
        if (
            isinstance(volume, bool)
            or not isinstance(volume, (int, float))
            or not math.isfinite(float(volume))
            or float(volume) <= 0.0
        ):
            missing_or_bad_volume += 1
            continue
        rows.append(
            EvaluationRow(
                event_time=value.event_time,
                symbol=value.symbol,
                score=float(value.score),
                label=float(label),
                market_regime=regimes.get(key, "all"),
                volume=float(volume),
            )
        )
    if missing_snapshot_rows:
        fail(
            FailureCode.INVALID_HASH,
            "score artifact contains keys absent from its frozen snapshot",
            missing=missing_snapshot_rows,
        )
    if missing_labels:
        fail(
            FailureCode.MISSING_REQUIRED_FIELD,
            "labels are missing for non-null factor rows",
            missing=missing_labels,
        )
    if missing_or_bad_volume:
        fail(
            FailureCode.INVALID_VALUE,
            "volume is missing or non-positive for evaluated rows",
            missing=missing_or_bad_volume,
        )
    if not rows:
        fail(FailureCode.INVALID_VALUE, "candidate produced no evaluable rows")
    return tuple(sorted(rows, key=lambda row: (row.event_time, row.symbol)))
