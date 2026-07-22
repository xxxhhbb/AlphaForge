"""Deterministic walk-forward evidence calculations from frozen rows."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from statistics import fmean, pstdev
from typing import Iterable, Mapping, Sequence

from ..contracts import CanonicalContract, ContractValidationError, FailureCode
from ..data import canonical_content_hash
from .models import (
    CostModel,
    EvaluationRow,
    ExperimentIdentity,
    MetricSet,
    OfficialEvidence,
    RegisteredFactor,
    ResearchSummary,
    VISIBLE_SUMMARY_METRICS,
    WalkForwardSpec,
)


def _raise(code: FailureCode, message: str, **details) -> None:
    raise ContractValidationError(code, message, details=details)


def row_key(row: EvaluationRow) -> str:
    return f"{row.event_time.isoformat()}|{row.symbol}"


def _pearson(left: Sequence[float], right: Sequence[float]) -> float:
    if len(left) != len(right) or len(left) < 2:
        return 0.0
    left_mean = fmean(left)
    right_mean = fmean(right)
    left_centered = [value - left_mean for value in left]
    right_centered = [value - right_mean for value in right]
    denominator = math.sqrt(
        sum(value * value for value in left_centered)
        * sum(value * value for value in right_centered)
    )
    if denominator == 0.0:
        return 0.0
    return sum(a * b for a, b in zip(left_centered, right_centered)) / denominator


def _ranks(values: Sequence[float]) -> list[float]:
    indexed = sorted(enumerate(values), key=lambda item: (item[1], item[0]))
    result = [0.0] * len(values)
    cursor = 0
    while cursor < len(indexed):
        end = cursor + 1
        while end < len(indexed) and indexed[end][1] == indexed[cursor][1]:
            end += 1
        average = (cursor + end - 1) / 2.0
        for original_index, _ in indexed[cursor:end]:
            result[original_index] = average
        cursor = end
    return result


def _rank_ic(left: Sequence[float], right: Sequence[float]) -> float:
    return _pearson(_ranks(left), _ranks(right))


def _compound(returns: Iterable[float]) -> float:
    wealth = 1.0
    for value in returns:
        wealth *= 1.0 + value
    return wealth - 1.0


def _max_drawdown(returns: Sequence[float]) -> float:
    wealth = 1.0
    peak = 1.0
    drawdown = 0.0
    for value in returns:
        wealth *= 1.0 + value
        peak = max(peak, wealth)
        drawdown = min(drawdown, wealth / peak - 1.0)
    return abs(drawdown)


def _stability(values: Sequence[float], groups: int = 2) -> tuple[float, float]:
    if not values:
        return 0.0, 0.0
    chunk_size = max(1, math.ceil(len(values) / groups))
    means = [fmean(values[index : index + chunk_size]) for index in range(0, len(values), chunk_size)]
    return sum(value > 0.0 for value in means) / len(means), min(means)


def _daily_ic(rows: Sequence[EvaluationRow]) -> list[tuple[datetime, float]]:
    by_time: dict[datetime, list[EvaluationRow]] = {}
    for row in rows:
        by_time.setdefault(row.event_time, []).append(row)
    output: list[tuple[datetime, float]] = []
    for event_time in sorted(by_time):
        day = sorted(by_time[event_time], key=lambda item: item.symbol)
        output.append(
            (
                event_time,
                _pearson([item.score for item in day], [item.label for item in day]),
            )
        )
    return output


def _residualized_rows(
    rows: Sequence[EvaluationRow], registered: Sequence[RegisteredFactor]
) -> tuple[EvaluationRow, ...]:
    if not registered:
        return tuple(rows)
    keys = [row_key(row) for row in rows]
    residual = [row.score for row in rows]
    for factor in registered:
        available = [index for index, key in enumerate(keys) if key in factor.scores]
        if len(available) < 2:
            continue
        reg_values = [factor.scores[keys[index]] for index in available]
        reg_mean = fmean(reg_values)
        centered = [value - reg_mean for value in reg_values]
        denominator = sum(value * value for value in centered)
        if denominator == 0.0:
            continue
        residual_mean = fmean(residual[index] for index in available)
        beta = sum(
            (residual[index] - residual_mean) * centered[position]
            for position, index in enumerate(available)
        ) / denominator
        for position, index in enumerate(available):
            residual[index] -= beta * centered[position]
    return tuple(
        EvaluationRow(
            event_time=row.event_time,
            symbol=row.symbol,
            score=residual[index],
            label=row.label,
            market_regime=row.market_regime,
            volume=row.volume,
        )
        for index, row in enumerate(rows)
    )


@dataclass(frozen=True, kw_only=True)
class WalkForwardFold(CanonicalContract):
    fold_id: str
    train_rows: tuple[EvaluationRow, ...]
    validation_rows: tuple[EvaluationRow, ...]
    sealed_rows: tuple[EvaluationRow, ...]


@dataclass(frozen=True, kw_only=True)
class VisibleFoldEvidence(CanonicalContract):
    fold_id: str
    train: OfficialEvidence
    validation: OfficialEvidence


def build_walk_forward_folds(
    rows: Sequence[EvaluationRow], spec: WalkForwardSpec
) -> tuple[WalkForwardFold, ...]:
    ordered_rows = tuple(sorted(rows, key=lambda item: (item.event_time, item.symbol)))
    dates = sorted({row.event_time for row in ordered_rows})
    window = spec.train_periods + spec.validation_periods + spec.sealed_periods
    if len(dates) < window:
        _raise(
            FailureCode.INVALID_VALUE,
            "insufficient periods for walk-forward specification",
            available=len(dates),
            required=window,
        )
    folds: list[WalkForwardFold] = []
    for start in range(0, len(dates) - window + 1, spec.step_periods):
        train_dates = set(dates[start : start + spec.train_periods])
        validation_start = start + spec.train_periods
        validation_dates = set(
            dates[validation_start : validation_start + spec.validation_periods]
        )
        sealed_start = validation_start + spec.validation_periods
        sealed_dates = set(dates[sealed_start : sealed_start + spec.sealed_periods])
        folds.append(
            WalkForwardFold(
                fold_id=f"fold-{len(folds):03d}",
                train_rows=tuple(row for row in ordered_rows if row.event_time in train_dates),
                validation_rows=tuple(
                    row for row in ordered_rows if row.event_time in validation_dates
                ),
                sealed_rows=tuple(row for row in ordered_rows if row.event_time in sealed_dates),
            )
        )
    return tuple(folds)


class EvidenceService:
    """The sole deterministic producer of official metrics."""

    def evaluate(
        self,
        *,
        identity: ExperimentIdentity,
        rows: Sequence[EvaluationRow],
        partition: str,
        cost_model: CostModel,
        registered_factors: Sequence[RegisteredFactor] = (),
        annualization: int = 252,
        quantile_count: int = 2,
    ) -> OfficialEvidence:
        if identity.cost_hash != cost_model.artifact_hash:
            _raise(FailureCode.INVALID_HASH, "cost model does not match ExperimentIdentity")
        if partition not in {"TRAIN", "VALIDATION", "SEALED"}:
            _raise(FailureCode.INVALID_VALUE, "unknown evidence partition")
        ordered = tuple(sorted(rows, key=lambda item: (item.event_time, item.symbol)))
        if not ordered:
            _raise(FailureCode.INVALID_VALUE, "evidence rows cannot be empty")
        metrics = self._metrics(
            ordered,
            cost_model=cost_model,
            registered_factors=tuple(registered_factors),
            annualization=annualization,
            quantile_count=quantile_count,
        )
        rows_hash = canonical_content_hash(ordered)
        registered_hash = canonical_content_hash(tuple(registered_factors))
        return OfficialEvidence(
            identity_hash=identity.identity_hash,
            partition=partition,
            metrics=metrics,
            input_hashes=(
                identity.dataset_hash,
                identity.universe_hash,
                identity.factor_hash,
                identity.split_hash,
                identity.label_hash,
                identity.cost_hash,
                identity.code_hash,
                identity.availability_hash,
                rows_hash,
                registered_hash,
            ),
        )

    def evaluate_visible_walk_forward(
        self,
        *,
        identity: ExperimentIdentity,
        rows: Sequence[EvaluationRow],
        spec: WalkForwardSpec,
        cost_model: CostModel,
        registered_factors: Sequence[RegisteredFactor] = (),
    ) -> tuple[VisibleFoldEvidence, ...]:
        if identity.split_hash != spec.artifact_hash:
            _raise(FailureCode.INVALID_HASH, "walk-forward spec does not match identity")
        folds = build_walk_forward_folds(rows, spec)
        return tuple(
            VisibleFoldEvidence(
                fold_id=fold.fold_id,
                train=self.evaluate(
                    identity=identity,
                    rows=fold.train_rows,
                    partition="TRAIN",
                    cost_model=cost_model,
                    registered_factors=registered_factors,
                    annualization=spec.annualization,
                    quantile_count=spec.quantile_count,
                ),
                validation=self.evaluate(
                    identity=identity,
                    rows=fold.validation_rows,
                    partition="VALIDATION",
                    cost_model=cost_model,
                    registered_factors=registered_factors,
                    annualization=spec.annualization,
                    quantile_count=spec.quantile_count,
                ),
            )
            for fold in folds
        )

    def research_summary(
        self, identity: ExperimentIdentity, visible: Sequence[VisibleFoldEvidence]
    ) -> ResearchSummary:
        if not visible:
            _raise(FailureCode.INVALID_VALUE, "visible evidence cannot be empty")

        def average(partition: str) -> Mapping[str, float]:
            metric_maps = [getattr(item, partition).metrics.flat() for item in visible]
            return {
                key: fmean(mapping[key] for mapping in metric_maps)
                for key in sorted(VISIBLE_SUMMARY_METRICS)
            }

        return ResearchSummary(
            identity_hash=identity.identity_hash,
            train_metrics=average("train"),
            validation_metrics=average("validation"),
        )

    def _metrics(
        self,
        rows: Sequence[EvaluationRow],
        *,
        cost_model: CostModel,
        registered_factors: Sequence[RegisteredFactor],
        annualization: int,
        quantile_count: int,
    ) -> MetricSet:
        scores = [row.score for row in rows]
        labels = [row.label for row in rows]
        global_pearson = _pearson(scores, labels)
        global_rank = _rank_ic(scores, labels)
        daily_ic_pairs = _daily_ic(rows)
        daily_ics = [value for _, value in daily_ic_pairs]
        ic_mean = fmean(daily_ics) if daily_ics else 0.0
        ic_std = pstdev(daily_ics) if len(daily_ics) >= 2 else 0.0
        icir = 0.0 if ic_std == 0.0 else ic_mean / ic_std * math.sqrt(annualization)

        by_time: dict[datetime, list[EvaluationRow]] = {}
        for row in rows:
            by_time.setdefault(row.event_time, []).append(row)
        previous_weights: dict[str, float] = {}
        spreads: list[float] = []
        turnovers: list[float] = []
        gross_daily: list[float] = []
        net_daily: list[float] = []
        spread_costs: list[float] = []
        impact_costs: list[float] = []
        for event_time in sorted(by_time):
            day = sorted(by_time[event_time], key=lambda item: (item.score, item.symbol))
            bucket = max(1, len(day) // quantile_count)
            bottom = day[:bucket]
            top = day[-bucket:]
            weights = {row.symbol: 0.5 / len(top) for row in top}
            for row in bottom:
                weights[row.symbol] = weights.get(row.symbol, 0.0) - 0.5 / len(bottom)
            symbols = set(previous_weights) | set(weights)
            turnover = 0.5 * sum(
                abs(weights.get(symbol, 0.0) - previous_weights.get(symbol, 0.0))
                for symbol in symbols
            )
            label_by_symbol = {row.symbol: row.label for row in day}
            gross = sum(weight * label_by_symbol[symbol] for symbol, weight in weights.items())
            spread_cost = turnover * cost_model.spread_bps / 10_000.0
            impact_cost = turnover * cost_model.linear_impact_bps / 10_000.0
            spreads.append(fmean(row.label for row in top) - fmean(row.label for row in bottom))
            turnovers.append(turnover)
            gross_daily.append(gross)
            spread_costs.append(spread_cost)
            impact_costs.append(impact_cost)
            net_daily.append(gross - spread_cost - impact_cost)
            previous_weights = weights

        sub_positive, sub_min = _stability(daily_ics)
        regime_ics: list[float] = []
        for regime in sorted({row.market_regime for row in rows}):
            regime_rows = [row for row in rows if row.market_regime == regime]
            regime_ics.append(
                _pearson(
                    [row.score for row in regime_rows],
                    [row.label for row in regime_rows],
                )
            )
        regime_positive = (
            sum(value > 0.0 for value in regime_ics) / len(regime_ics) if regime_ics else 0.0
        )
        regime_min = min(regime_ics) if regime_ics else 0.0

        correlations: list[float] = []
        for factor in registered_factors:
            pairs = [
                (row.score, factor.scores[row_key(row)])
                for row in rows
                if row_key(row) in factor.scores
            ]
            correlations.append(
                abs(_pearson([pair[0] for pair in pairs], [pair[1] for pair in pairs]))
                if len(pairs) >= 2
                else 0.0
            )
        residual_rows = _residualized_rows(rows, registered_factors)
        incremental_daily = [value for _, value in _daily_ic(residual_rows)]
        incremental_ic = fmean(incremental_daily) if incremental_daily else 0.0

        return MetricSet(
            pearson_ic=global_pearson,
            rank_ic=global_rank,
            ic_mean=ic_mean,
            ic_std=ic_std,
            icir=icir,
            quantile_spread=fmean(spreads) if spreads else 0.0,
            turnover=fmean(turnovers) if turnovers else 0.0,
            gross_return=_compound(gross_daily),
            net_return=_compound(net_daily),
            spread_cost=sum(spread_costs),
            linear_impact_cost=sum(impact_costs),
            max_drawdown=_max_drawdown(net_daily),
            subperiod_positive_fraction=sub_positive,
            subperiod_min_ic=sub_min,
            regime_positive_fraction=regime_positive,
            regime_min_ic=regime_min,
            max_registered_abs_correlation=max(correlations, default=0.0),
            incremental_ic_mean=incremental_ic,
            observation_count=len(rows),
            period_count=len(by_time),
        )

