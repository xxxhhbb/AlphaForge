"""Deterministic, output-specific Evidence Snapshot v2 evaluators.

The evaluators do not trust provider prose or authority fields.  They accept a
canonical standard table, independently derive/validate machine metrics, and
either return OFFICIAL_EVIDENCE or a non-empty typed diagnostic.
"""

from __future__ import annotations

from collections import defaultdict
import math
from statistics import mean, pstdev
from typing import Any, Callable, Mapping, Sequence

from .contracts import EvidenceAuthority, EvidenceSnapshotV2, ResearchVerdict
from .storage import canonical_hash, normalize_json


EVALUATOR_VERSION = "alphaforge.output-evaluators/v1"
OUTPUT_KINDS = (
    "ScoreTable", "RegressionTable", "RegimeComparisonTable", "EventTable",
    "DiagnosticTable", "DatasetAssessmentTable",
)


class EvaluationRejected(ValueError):
    def __init__(self, code: str, message: str, diagnostics: Sequence[Mapping[str, Any]] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.diagnostics = tuple(diagnostics or ({"code": code, "message": message},))


class OutputEvaluatorRegistry:
    """Dispatch exactly one deterministic evaluator per standard table type."""

    def __init__(self) -> None:
        self._evaluators: dict[str, Callable[[Mapping[str, Any], Mapping[str, Any]], tuple[dict[str, Any], ResearchVerdict, dict[str, Any]]]] = {
            "ScoreTable": _score,
            "RegressionTable": _regression,
            "RegimeComparisonTable": _regime,
            "EventTable": _event,
            "DiagnosticTable": _diagnostic,
            "DatasetAssessmentTable": _dataset_assessment,
        }

    def evaluate(
        self, *, table: Mapping[str, Any], context: Mapping[str, Any], evidence_id: str,
    ) -> EvidenceSnapshotV2:
        output_kind = str(table.get("contract_type") or table.get("output_kind") or "")
        evaluator = self._evaluators.get(output_kind)
        if evaluator is None:
            raise EvaluationRejected("OUTPUT_KIND_UNSUPPORTED", "standard output type is not registered")
        rows = table.get("rows")
        if not isinstance(rows, list) or not rows:
            raise EvaluationRejected("EMPTY_STANDARD_TABLE", "standard output rows must be non-empty")
        _finite_tree(table)
        metrics, verdict, sections = evaluator(table, context)
        normalized_metrics = normalize_json(metrics)
        if not normalized_metrics:
            raise EvaluationRejected("EMPTY_MACHINE_METRICS", "evaluator produced no machine metrics")
        table_hash = canonical_hash(table)
        context_hash = canonical_hash(_public_context(context))
        metric_hashes = {
            name: canonical_hash({
                "evidence_id": evidence_id,
                "output_kind": output_kind,
                "table_hash": table_hash,
                "context_hash": context_hash,
                "metric_name": name,
                "metric_value": value,
                "evaluator_version": EVALUATOR_VERSION,
            })
            for name, value in sorted(normalized_metrics.items())
        }
        sources = tuple(sorted(set(
            [table_hash, context_hash]
            + [str(item) for item in context.get("source_artifact_hashes", ())]
        )))
        if any(not item.startswith("sha256:") for item in sources):
            raise EvaluationRejected("SOURCE_IDENTITY_INVALID", "Evidence source lacks a content hash")
        return EvidenceSnapshotV2(
            evidence_id=evidence_id,
            output_kind=output_kind,
            authority=EvidenceAuthority.OFFICIAL_EVIDENCE,
            verdict=verdict,
            data_scope=dict(context.get("data_scope") or {}),
            missingness=dict(context.get("missingness") or {}),
            split=dict(context.get("split") or {}),
            cost_model=dict(context.get("cost_model") or {}),
            metrics=normalized_metrics,
            metric_hashes=metric_hashes,
            subperiod_results=tuple(sections.get("subperiod_results", ())),
            regime_results=tuple(sections.get("regime_results", ())),
            layer_results=tuple(sections.get("layer_results", ())),
            robustness=tuple(sections.get("robustness", ())),
            falsification=tuple(sections.get("falsification", ())),
            diagnostics=tuple(sections.get("diagnostics", ())),
            source_artifact_hashes=sources,
            evaluator_version=EVALUATOR_VERSION,
        )


def _score(table: Mapping[str, Any], context: Mapping[str, Any]):
    rows = table["rows"]
    labels = context.get("labels")
    if not isinstance(labels, list) or not labels:
        raise EvaluationRejected("LABELS_UNAVAILABLE", "ScoreTable evaluation requires frozen visible labels")
    label_map = {}
    for item in labels:
        key = (str(item.get("symbol")), str(item.get("decision_time")))
        if key in label_map:
            raise EvaluationRejected("DUPLICATE_LABEL", "frozen labels contain duplicate keys")
        label_map[key] = _number(item.get("value"), "label")
    grouped: dict[str, list[tuple[float, float]]] = defaultdict(list)
    missing = 0
    for row in rows:
        score = _number(row.get("score"), "score")
        key = (str(row.get("symbol")), str(row.get("decision_time")))
        if key not in label_map:
            missing += 1
            continue
        grouped[key[1]].append((score, label_map[key]))
    pairs = [pair for values in grouped.values() for pair in values]
    if len(pairs) < 8:
        raise EvaluationRejected("SAMPLE_INSUFFICIENT", "ScoreTable has fewer than eight joined observations")
    daily_ic, daily_rank_ic, spreads = [], [], []
    for values in grouped.values():
        if len(values) < 2:
            continue
        x, y = zip(*values)
        if _variance(x) == 0 or _variance(y) == 0:
            continue
        daily_ic.append(_corr(x, y))
        daily_rank_ic.append(_corr(_ranks(x), _ranks(y)))
        ordered = sorted(values)
        width = max(1, len(ordered) // 3)
        spreads.append(mean(value for _, value in ordered[-width:]) - mean(value for _, value in ordered[:width]))
    if not daily_ic:
        raise EvaluationRejected("ZERO_VARIANCE", "ScoreTable has no evaluable cross-section")
    ic_mean = mean(daily_ic)
    ic_std = pstdev(daily_ic) if len(daily_ic) > 1 else 0.0
    rank_mean = mean(daily_rank_ic) if daily_rank_ic else 0.0
    spread = mean(spreads) if spreads else 0.0
    spread_bps = float((context.get("cost_model") or {}).get("spread_bps", 0.0))
    impact_bps = float((context.get("cost_model") or {}).get("linear_impact_bps", 0.0))
    turnover = _turnover(rows)
    net_spread = spread - turnover * (spread_bps + impact_bps) / 10_000.0
    icir = ic_mean / ic_std if ic_std > 0 else 0.0
    metrics = {
        "observation_count": len(pairs), "decision_count": len(grouped),
        "label_missing_count": missing, "pearson_ic_mean": ic_mean,
        "rank_ic_mean": rank_mean, "ic_std": ic_std, "icir": icir,
        "quantile_spread_gross": spread, "turnover": turnover,
        "quantile_spread_net": net_spread,
    }
    if icir > 0.3 and net_spread > 0:
        verdict = ResearchVerdict.SUPPORTED
    elif icir > 0 and net_spread >= 0:
        verdict = ResearchVerdict.CONDITIONALLY_SUPPORTED
    elif icir < 0 or net_spread < 0:
        verdict = ResearchVerdict.REJECTED
    else:
        verdict = ResearchVerdict.INSUFFICIENT_EVIDENCE
    return metrics, verdict, {
        "robustness": ({"check": "cost_after_spread_and_linear_impact", "passed": net_spread >= 0},),
        "diagnostics": ({"code": "JOIN_COVERAGE", "joined": len(pairs), "missing": missing},),
    }


def _regression(table: Mapping[str, Any], context: Mapping[str, Any]):
    rows = table["rows"]
    terms = [str(row.get("term") or "") for row in rows]
    if not all(terms) or len(set(terms)) != len(terms):
        raise EvaluationRejected("COLLINEAR_OR_DUPLICATE_TERMS", "regression terms must be unique")
    metrics: dict[str, Any] = {"term_count": len(rows)}
    significant = 0
    minimum_n = None
    diagnostics = []
    for row in rows:
        term = str(row["term"])
        estimate = _number(row.get("estimate"), "estimate")
        standard_error = _number(row.get("std_error"), "std_error")
        sample_count = _positive_int(row.get("observation_count"), "observation_count")
        if standard_error <= 0:
            raise EvaluationRejected("ZERO_STANDARD_ERROR", "regression standard error must be positive")
        calculated_t = estimate / standard_error
        calculated_p = math.erfc(abs(calculated_t) / math.sqrt(2.0))
        reported_t = _number(row.get("t_statistic"), "t_statistic")
        reported_p = _number(row.get("p_value"), "p_value")
        if abs(reported_t - calculated_t) > max(1e-6, abs(calculated_t) * 1e-4):
            raise EvaluationRejected("T_STATISTIC_MISMATCH", "reported t-statistic differs from deterministic recomputation")
        if not 0 <= reported_p <= 1:
            raise EvaluationRejected("P_VALUE_INVALID", "p-value lies outside [0,1]")
        metrics[f"coefficient.{term}"] = estimate
        metrics[f"standard_error.{term}"] = standard_error
        metrics[f"t_statistic.{term}"] = calculated_t
        metrics[f"p_value_normal.{term}"] = calculated_p
        metrics[f"sample_count.{term}"] = sample_count
        significant += int(calculated_p < 0.05)
        minimum_n = sample_count if minimum_n is None else min(minimum_n, sample_count)
        diagnostics.append({"term": term, "reported_p": reported_p, "recomputed_p": calculated_p})
    condition_number = context.get("condition_number")
    if condition_number is not None:
        condition_number = _number(condition_number, "condition_number")
        metrics["condition_number"] = condition_number
        if condition_number > 1e8:
            raise EvaluationRejected("DESIGN_MATRIX_SINGULAR", "regression condition number exceeds policy")
    metrics["significant_term_count"] = significant
    metrics["minimum_sample_count"] = minimum_n
    if minimum_n is not None and minimum_n < 20:
        verdict = ResearchVerdict.INSUFFICIENT_EVIDENCE
    elif significant:
        verdict = ResearchVerdict.SUPPORTED
    else:
        verdict = ResearchVerdict.REJECTED
    return metrics, verdict, {"diagnostics": tuple(diagnostics)}


def _regime(table: Mapping[str, Any], context: Mapping[str, Any]):
    del context
    rows = table["rows"]
    by_metric: dict[str, list[tuple[str, float, int]]] = defaultdict(list)
    for row in rows:
        by_metric[str(row.get("metric_name"))].append((
            str(row.get("regime_id")), _number(row.get("estimate"), "estimate"),
            _positive_int(row.get("sample_count"), "sample_count"),
        ))
    if not by_metric or any(len(values) < 2 for values in by_metric.values()):
        raise EvaluationRejected("REGIME_COUNT_INSUFFICIENT", "each regime metric requires at least two states")
    metrics: dict[str, Any] = {}
    results, stable = [], True
    for metric_name, values in sorted(by_metric.items()):
        estimates = [item[1] for item in values]
        counts = [item[2] for item in values]
        if min(counts) < 5:
            raise EvaluationRejected("REGIME_SAMPLE_INSUFFICIENT", "regime state has fewer than five samples")
        difference = max(estimates) - min(estimates)
        sign_consistency = all(value >= 0 for value in estimates) or all(value <= 0 for value in estimates)
        stable = stable and sign_consistency
        metrics[f"difference.{metric_name}"] = difference
        metrics[f"minimum_sample_count.{metric_name}"] = min(counts)
        metrics[f"sign_stability.{metric_name}"] = 1.0 if sign_consistency else 0.0
        for regime_id, estimate, count in values:
            metrics[f"effect.{metric_name}.{regime_id}"] = estimate
            results.append({"metric": metric_name, "regime": regime_id, "effect": estimate, "sample_count": count})
    verdict = ResearchVerdict.SUPPORTED if stable else ResearchVerdict.CONDITIONALLY_SUPPORTED
    return metrics, verdict, {"regime_results": tuple(results)}


def _event(table: Mapping[str, Any], context: Mapping[str, Any]):
    rows = table["rows"]
    outcomes = context.get("event_outcomes")
    if not isinstance(outcomes, Mapping) or not outcomes:
        raise EvaluationRejected("EVENT_OUTCOMES_UNAVAILABLE", "event evaluator requires frozen event outcomes")
    cars, adjusted, placebo = [], [], []
    for row in rows:
        event_id = str(row.get("event_id"))
        outcome = outcomes.get(event_id)
        if not isinstance(outcome, Mapping):
            continue
        published = str(row.get("published_at") or "")
        available = str(row.get("available_at") or "")
        decision = str(row.get("decision_time") or "")
        if not published or not available or not decision or not (published <= available <= decision):
            raise EvaluationRejected("EVENT_CLOCK_VIOLATION", "event clocks violate publication/availability/decision order")
        car = _number(outcome.get("car"), "car")
        control = _number(outcome.get("control_return"), "control_return")
        placebo_value = _number(outcome.get("placebo_car"), "placebo_car")
        cars.append(car)
        adjusted.append(car - control)
        placebo.append(placebo_value)
    if len(cars) < 5:
        raise EvaluationRejected("EVENT_SAMPLE_INSUFFICIENT", "fewer than five frozen events were joined")
    metrics = {
        "event_count": len(cars), "mean_car": mean(cars),
        "mean_control_adjusted_car": mean(adjusted),
        "mean_placebo_car": mean(placebo),
        "event_window_start": int(context.get("event_window_start", 0)),
        "event_window_end": int(context.get("event_window_end", 5)),
    }
    if abs(mean(adjusted)) > abs(mean(placebo)) and mean(adjusted) != 0:
        verdict = ResearchVerdict.SUPPORTED
    elif mean(adjusted) == 0:
        verdict = ResearchVerdict.REJECTED
    else:
        verdict = ResearchVerdict.CONDITIONALLY_SUPPORTED
    return metrics, verdict, {
        "falsification": ({"check": "placebo_window", "placebo_mean_car": mean(placebo)},),
        "robustness": ({"check": "matched_control", "adjusted_mean_car": mean(adjusted)},),
    }


def _diagnostic(table: Mapping[str, Any], context: Mapping[str, Any]):
    del context
    rows = table["rows"]
    metrics = {}
    diagnostics = []
    for row in rows:
        diagnostic_id = str(row.get("diagnostic_id") or "")
        metric_name = str(row.get("metric_name") or "")
        if not diagnostic_id or not metric_name:
            raise EvaluationRejected("DIAGNOSTIC_IDENTITY_MISSING", "diagnostic rows need stable identities")
        key = f"{diagnostic_id}.{metric_name}"
        if key in metrics:
            raise EvaluationRejected("DUPLICATE_DIAGNOSTIC", "diagnostic identities are duplicated")
        metrics[key] = _number(row.get("value"), "diagnostic value")
        diagnostics.append({
            "diagnostic_id": diagnostic_id, "metric_name": metric_name,
            "unit": str(row.get("unit") or ""), "interpretation": str(row.get("interpretation") or ""),
        })
    return metrics, ResearchVerdict.CONDITIONALLY_SUPPORTED, {"diagnostics": tuple(diagnostics)}


def _dataset_assessment(table: Mapping[str, Any], context: Mapping[str, Any]):
    expected = context.get("field_coverage")
    if not isinstance(expected, Mapping) or not expected:
        raise EvaluationRejected("FIELD_COVERAGE_UNAVAILABLE", "dataset evaluator requires independently measured coverage")
    metrics, diagnostics = {}, []
    seen = set()
    for row in table["rows"]:
        field = str(row.get("field_name") or "")
        if not field or field in seen:
            raise EvaluationRejected("FIELD_IDENTITY_INVALID", "dataset assessment fields must be unique")
        seen.add(field)
        if field not in expected:
            raise EvaluationRejected("FIELD_NOT_IN_FROZEN_SCHEMA", "assessment references an unregistered field")
        claimed = _number(row.get("coverage"), "coverage")
        measured = _number(expected[field], "measured coverage")
        if abs(claimed - measured) > 1e-12:
            raise EvaluationRejected("COVERAGE_MISMATCH", "claimed coverage differs from frozen measurement")
        pit_eligible = row.get("pit_eligible") is True
        metrics[f"coverage.{field}"] = measured
        metrics[f"pit_eligible.{field}"] = 1.0 if pit_eligible else 0.0
        metrics[f"observed_count.{field}"] = _nonnegative_int(row.get("observed_count"), "observed_count")
        diagnostics.append({"field": field, "limitation": str(row.get("limitation") or "")})
    minimum = min(metrics[key] for key in metrics if key.startswith("coverage."))
    pit_all = all(value == 1.0 for key, value in metrics.items() if key.startswith("pit_eligible."))
    metrics["minimum_coverage"] = minimum
    verdict = (
        ResearchVerdict.SUPPORTED if minimum >= 0.8 and pit_all
        else ResearchVerdict.CONDITIONALLY_SUPPORTED if minimum > 0 and pit_all
        else ResearchVerdict.DATA_UNAVAILABLE
    )
    return metrics, verdict, {"diagnostics": tuple(diagnostics)}


def _public_context(context: Mapping[str, Any]) -> dict[str, Any]:
    return normalize_json({key: value for key, value in context.items() if key not in {"sealed", "secrets"}})


def _number(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise EvaluationRejected("NONFINITE_VALUE", f"{name} must be finite")
    return float(value)


def _positive_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise EvaluationRejected("SAMPLE_COUNT_INVALID", f"{name} must be positive")
    return value


def _nonnegative_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise EvaluationRejected("OBSERVED_COUNT_INVALID", f"{name} must be non-negative")
    return value


def _finite_tree(value: Any) -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise EvaluationRejected("NONFINITE_VALUE", "NaN/Inf is forbidden in a standard table")
    if isinstance(value, Mapping):
        for item in value.values():
            _finite_tree(item)
    elif isinstance(value, list):
        for item in value:
            _finite_tree(item)


def _variance(values: Sequence[float]) -> float:
    center = mean(values)
    return mean((value - center) ** 2 for value in values)


def _corr(left: Sequence[float], right: Sequence[float]) -> float:
    left_mean, right_mean = mean(left), mean(right)
    numerator = sum((x - left_mean) * (y - right_mean) for x, y in zip(left, right))
    denominator = math.sqrt(sum((x - left_mean) ** 2 for x in left) * sum((y - right_mean) ** 2 for y in right))
    return numerator / denominator if denominator else 0.0


def _ranks(values: Sequence[float]) -> list[float]:
    order = sorted(range(len(values)), key=lambda index: (values[index], index))
    result = [0.0] * len(values)
    cursor = 0
    while cursor < len(order):
        end = cursor + 1
        while end < len(order) and values[order[end]] == values[order[cursor]]:
            end += 1
        rank = (cursor + end - 1) / 2.0
        for index in order[cursor:end]:
            result[index] = rank
        cursor = end
    return result


def _turnover(rows: Sequence[Mapping[str, Any]]) -> float:
    grouped: dict[str, dict[str, float]] = defaultdict(dict)
    for row in rows:
        grouped[str(row.get("decision_time"))][str(row.get("symbol"))] = _number(row.get("score"), "score")
    dates = sorted(grouped)
    if len(dates) < 2:
        return 0.0
    changes = []
    for previous, current in zip(dates, dates[1:]):
        symbols = set(grouped[previous]) | set(grouped[current])
        changes.append(mean(abs(grouped[current].get(symbol, 0.0) - grouped[previous].get(symbol, 0.0)) for symbol in symbols))
    return mean(changes)


__all__ = [
    "EVALUATOR_VERSION", "EvaluationRejected", "OUTPUT_KINDS", "OutputEvaluatorRegistry",
]
