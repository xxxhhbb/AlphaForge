"""Deterministic, PIT-bound postprocessing of non-authoritative factor scores."""

from __future__ import annotations

import math
from datetime import datetime
from statistics import fmean, pstdev

from ..contracts import FailureCode
from ..contracts.base import fail, require_aware_datetime
from ..data import MarketSnapshot, canonical_content_hash
from ..formula_execution import FactorScoreArtifact, FactorScorePoint
from .models import (
    POSTPROCESS_RUNTIME_HASH,
    PostprocessedProgramIdentity,
    ScorePostprocessOperator,
    ScorePostprocessPolicy,
    ScorePostprocessProgram,
    derive_postprocessed_program_identity,
)


ScoreKey = tuple[datetime, str]
ScoreSeries = dict[ScoreKey, float | None]


class ScorePostprocessService:
    """Apply an explicit postprocess IR without reading labels or sealed data."""

    def derive_identity(
        self,
        *,
        upstream: FactorScoreArtifact,
        program: ScorePostprocessProgram,
        policy: ScorePostprocessPolicy,
    ) -> PostprocessedProgramIdentity:
        self._require_types(upstream=upstream, program=program, policy=policy)
        return derive_postprocessed_program_identity(
            upstream_score_hash=upstream.artifact_hash,
            upstream_program_hash=upstream.program_hash,
            program=program,
            policy=policy,
        )

    def apply(
        self,
        *,
        upstream: FactorScoreArtifact,
        program: ScorePostprocessProgram,
        policy: ScorePostprocessPolicy,
        snapshot: MarketSnapshot,
    ) -> FactorScoreArtifact:
        self._require_types(upstream=upstream, program=program, policy=policy)
        if not isinstance(snapshot, MarketSnapshot):
            fail(FailureCode.INVALID_VALUE, "postprocess requires a typed MarketSnapshot")
        if len(program.steps) > policy.max_steps:
            fail(FailureCode.PROVIDER_BUDGET_EXCEEDED, "postprocess program exceeds policy step budget")
        unauthorized = set(program.required_fields) - set(policy.allowed_fields)
        if unauthorized:
            fail(
                FailureCode.UNAUTHORIZED_CAPABILITY,
                "postprocess program requests fields outside its policy",
                fields=tuple(sorted(unauthorized)),
            )
        missing = set(program.required_fields) - set(snapshot.fields)
        if missing:
            fail(FailureCode.UNKNOWN_FIELD, "snapshot lacks postprocess fields", fields=tuple(sorted(missing)))
        if snapshot.artifact_hash != upstream.snapshot_hash:
            fail(FailureCode.INVALID_HASH, "postprocess snapshot does not match upstream score artifact")
        universe_hash = canonical_content_hash(snapshot.historical_universe)
        if universe_hash != upstream.universe_hash:
            fail(FailureCode.INVALID_HASH, "postprocess universe does not match upstream score artifact")

        keys = _validate_snapshot_and_score_keys(snapshot=snapshot, upstream=upstream)
        rows = {(row.event_time, row.symbol): row for row in snapshot.rows}
        series: ScoreSeries = {key: point.score for key, point in zip(keys, upstream.values)}
        for step in program.steps:
            by_time = _group_by_time(keys)
            if step.operator is ScorePostprocessOperator.RANK_CROSS_SECTION:
                series = _rank_cross_section(series, by_time)
            elif step.operator is ScorePostprocessOperator.ZSCORE_CROSS_SECTION:
                series = _zscore_cross_section(series, by_time)
            elif step.operator is ScorePostprocessOperator.WINSORIZE:
                assert step.lower is not None and step.upper is not None
                series = _winsorize(series, by_time, step.lower, step.upper)
            elif step.operator in {
                ScorePostprocessOperator.NEUTRALIZE_BY_GROUP,
                ScorePostprocessOperator.NEUTRALIZE_BY_INDUSTRY,
            }:
                assert step.field is not None
                series = _neutralize_by_group(series, by_time, rows, step.field)
            elif step.operator is ScorePostprocessOperator.NEUTRALIZE_BY_MARKET_CAP:
                assert step.field is not None
                series = _neutralize_by_market_cap(
                    series,
                    by_time,
                    rows,
                    step.field,
                    policy.min_market_cap_observations,
                )
            else:  # pragma: no cover - exhaustive enum guard
                fail(FailureCode.INVALID_VALUE, "postprocess operator has no implementation")

        identity = self.derive_identity(upstream=upstream, program=program, policy=policy)
        return FactorScoreArtifact(
            program_hash=identity.derived_program_hash,
            snapshot_hash=upstream.snapshot_hash,
            universe_hash=upstream.universe_hash,
            runtime_hash=POSTPROCESS_RUNTIME_HASH,
            policy_hash=policy.artifact_hash,
            seed=upstream.seed,
            values=tuple(
                FactorScorePoint(event_time=event_time, symbol=symbol, score=series[(event_time, symbol)])
                for event_time, symbol in keys
            ),
        )

    @staticmethod
    def _require_types(
        *,
        upstream: FactorScoreArtifact,
        program: ScorePostprocessProgram,
        policy: ScorePostprocessPolicy,
    ) -> None:
        if not isinstance(upstream, FactorScoreArtifact):
            fail(FailureCode.INVALID_VALUE, "upstream must be FactorScoreArtifact")
        if not isinstance(program, ScorePostprocessProgram):
            fail(FailureCode.INVALID_VALUE, "program must be ScorePostprocessProgram")
        if not isinstance(policy, ScorePostprocessPolicy):
            fail(FailureCode.INVALID_VALUE, "policy must be ScorePostprocessPolicy")


def _validate_snapshot_and_score_keys(
    *,
    snapshot: MarketSnapshot,
    upstream: FactorScoreArtifact,
) -> tuple[ScoreKey, ...]:
    row_keys = tuple((row.event_time, row.symbol) for row in snapshot.rows)
    if len(row_keys) != len(set(row_keys)):
        fail(FailureCode.INVALID_VALUE, "snapshot contains duplicate event_time/symbol keys")
    historical: list[ScoreKey] = []
    for time_text, members in snapshot.historical_universe.items():
        try:
            event_time = datetime.fromisoformat(time_text)
        except (TypeError, ValueError) as exc:
            fail(FailureCode.INVALID_TIMESTAMP, "historical universe has an invalid session key")
            raise AssertionError from exc  # pragma: no cover
        require_aware_datetime(event_time, "historical_universe.session")
        if members != tuple(sorted(set(members))):
            fail(FailureCode.INVALID_VALUE, "historical universe members must be sorted and unique")
        historical.extend((event_time, symbol) for symbol in members)
    expected = tuple(sorted(historical))
    if tuple(sorted(row_keys)) != expected:
        fail(FailureCode.INVALID_HASH, "snapshot rows do not exactly match historical universe membership")
    score_keys = tuple((point.event_time, point.symbol) for point in upstream.values)
    if score_keys != expected:
        fail(FailureCode.INCOMPLETE_RESULT, "score artifact does not exactly cover historical universe")
    return expected


def _group_by_time(keys: tuple[ScoreKey, ...]) -> dict[datetime, tuple[ScoreKey, ...]]:
    grouped: dict[datetime, list[ScoreKey]] = {}
    for key in keys:
        grouped.setdefault(key[0], []).append(key)
    return {time: tuple(values) for time, values in sorted(grouped.items())}


def _rank_cross_section(series: ScoreSeries, grouped: dict[datetime, tuple[ScoreKey, ...]]) -> ScoreSeries:
    output: ScoreSeries = {}
    for keys in grouped.values():
        valid = sorted(
            ((series[key], key) for key in keys if series[key] is not None),
            key=lambda item: (item[0], item[1][1]),
        )
        ranks: dict[ScoreKey, float] = {}
        cursor = 0
        while cursor < len(valid):
            end = cursor + 1
            while end < len(valid) and valid[end][0] == valid[cursor][0]:
                end += 1
            average_rank = (cursor + end - 1) / 2.0
            percentile = 0.5 if len(valid) == 1 else average_rank / (len(valid) - 1)
            for _, key in valid[cursor:end]:
                ranks[key] = percentile
            cursor = end
        output.update({key: ranks.get(key) for key in keys})
    return output


def _zscore_cross_section(series: ScoreSeries, grouped: dict[datetime, tuple[ScoreKey, ...]]) -> ScoreSeries:
    output: ScoreSeries = {}
    for keys in grouped.values():
        valid = [series[key] for key in keys if series[key] is not None]
        assert all(value is not None for value in valid)
        deviation = pstdev(valid) if len(valid) >= 2 else 0.0
        mean = fmean(valid) if valid else 0.0
        for key in keys:
            value = series[key]
            output[key] = None if value is None or deviation == 0.0 else (value - mean) / deviation
    return output


def _winsorize(
    series: ScoreSeries,
    grouped: dict[datetime, tuple[ScoreKey, ...]],
    lower_probability: float,
    upper_probability: float,
) -> ScoreSeries:
    output: ScoreSeries = {}
    for keys in grouped.values():
        valid = sorted(series[key] for key in keys if series[key] is not None)
        if not valid:
            output.update({key: None for key in keys})
            continue
        lower = _quantile(valid, lower_probability)
        upper = _quantile(valid, upper_probability)
        for key in keys:
            value = series[key]
            output[key] = None if value is None else min(max(value, lower), upper)
    return output


def _neutralize_by_group(
    series: ScoreSeries,
    grouped: dict[datetime, tuple[ScoreKey, ...]],
    rows: dict[ScoreKey, object],
    field: str,
) -> ScoreSeries:
    output: ScoreSeries = {}
    for keys in grouped.values():
        categories: dict[ScoreKey, str | None] = {}
        values_by_category: dict[str, list[float]] = {}
        for key in keys:
            raw = rows[key].values[field]  # type: ignore[attr-defined]
            category = _category(raw, field)
            categories[key] = category
            value = series[key]
            if category is not None and value is not None:
                values_by_category.setdefault(category, []).append(value)
        means = {category: fmean(values) for category, values in values_by_category.items()}
        for key in keys:
            value = series[key]
            category = categories[key]
            output[key] = None if value is None or category is None else value - means[category]
    return output


def _neutralize_by_market_cap(
    series: ScoreSeries,
    grouped: dict[datetime, tuple[ScoreKey, ...]],
    rows: dict[ScoreKey, object],
    field: str,
    minimum: int,
) -> ScoreSeries:
    output: ScoreSeries = {}
    for keys in grouped.values():
        exposures: dict[ScoreKey, float | None] = {
            key: _positive_numeric(rows[key].values[field], field)  # type: ignore[attr-defined]
            for key in keys
        }
        valid = [
            (key, math.log(exposures[key]), series[key])
            for key in keys
            if exposures[key] is not None and series[key] is not None
        ]
        if len(valid) < minimum:
            output.update({key: None for key in keys})
            continue
        xs = [item[1] for item in valid]
        ys = [item[2] for item in valid]
        x_mean = fmean(xs)
        y_mean = fmean(ys)
        denominator = sum((x - x_mean) ** 2 for x in xs)
        if denominator == 0.0:
            output.update({key: None for key in keys})
            continue
        beta = sum((x - x_mean) * (y - y_mean) for x, y in zip(xs, ys)) / denominator
        alpha = y_mean - beta * x_mean
        residuals = {key: y - (alpha + beta * x) for key, x, y in valid}
        output.update({key: residuals.get(key) for key in keys})
    return output


def _category(value: object, field: str) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        fail(FailureCode.INVALID_VALUE, "group field must contain string/integer categories", field=field)
    text = str(value).strip()
    if not text:
        fail(FailureCode.INVALID_VALUE, "group field contains an empty category", field=field)
    return text


def _positive_numeric(value: object, field: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        fail(FailureCode.INVALID_VALUE, "market-cap field must be numeric", field=field)
    converted = float(value)
    if not math.isfinite(converted):
        fail(FailureCode.NON_FINITE_NUMBER, "market-cap field must be finite", field=field)
    if converted <= 0.0:
        fail(FailureCode.INVALID_VALUE, "market-cap field must be positive", field=field)
    return converted


def _quantile(sorted_values: list[float], probability: float) -> float:
    if len(sorted_values) == 1:
        return sorted_values[0]
    position = probability * (len(sorted_values) - 1)
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return sorted_values[lower]
    weight = position - lower
    return sorted_values[lower] * (1.0 - weight) + sorted_values[upper] * weight


__all__ = ["ScorePostprocessService"]
