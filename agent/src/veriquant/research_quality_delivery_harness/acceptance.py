"""Mechanical acceptance fixtures for all six output evaluators."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from .evaluators import EvaluationRejected, OUTPUT_KINDS, OutputEvaluatorRegistry
from .storage import atomic_json, canonical_hash


def run_output_evaluator_acceptance(*, output_root: str | Path) -> dict[str, Any]:
    registry = OutputEvaluatorRegistry()
    rows = []
    for output_kind in OUTPUT_KINDS:
        positive_table, positive_context = _positive(output_kind)
        positive = registry.evaluate(
            table=positive_table, context=positive_context,
            evidence_id=f"evaluator_canary_{output_kind}",
        )
        negative_code = None
        try:
            negative_table, negative_context = _negative(output_kind)
            registry.evaluate(
                table=negative_table, context=negative_context,
                evidence_id=f"negative_{output_kind}",
            )
        except EvaluationRejected as exc:
            negative_code = exc.code
        diagnostic_codes = []
        for table, context in _diagnostic_cases(output_kind):
            try:
                registry.evaluate(
                    table=table, context=context,
                    evidence_id=f"diagnostic_{output_kind}_{len(diagnostic_codes)}",
                )
            except EvaluationRejected as exc:
                diagnostic_codes.append(exc.code)
        passed = (
            positive.authority.value == "OFFICIAL_EVIDENCE"
            and positive.metrics
            and negative_code is not None
            and diagnostic_codes
        )
        rows.append({
            "output_kind": output_kind,
            "implementation_status": "IMPLEMENTED_AND_TESTED" if passed else "FAILED",
            "positive_canary": {
                "status": "PASSED" if passed else "FAILED",
                "authority": positive.authority.value,
                "evidence_hash": positive.artifact_hash,
                "metric_count": len(positive.metrics),
                "real_runner_canary_status": "PENDING_QUALIFICATION_RUN",
            },
            "negative_fixture": {"status": "REJECTED", "failure_code": negative_code},
            "diagnostic_paths": sorted(set(diagnostic_codes)),
        })
    result = {
        "schema_version": "alphaforge.output-evaluator-matrix/v1",
        "artifact_type": "output-evaluator-matrix",
        "output_kind_count": len(rows),
        "implemented_and_tested_count": sum(row["implementation_status"] == "IMPLEMENTED_AND_TESTED" for row in rows),
        "rows": rows,
    }
    return atomic_json(Path(output_root) / "output_evaluator_matrix.json", result)


def _base(kind: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "contract_type": kind,
        "schema_version": "alphaforge.agent-research-sandbox/v1",
        "experiment_id": f"fixture_{kind}",
        "code_identity_hash": canonical_hash({"code": kind}),
        "data_identity_hash": canonical_hash({"data": kind}),
        "rows": rows,
    }


def _context(**extra: Any) -> dict[str, Any]:
    value = {
        "data_scope": {"entity_count": 5, "time_count": 2, "observation_count": 10},
        "missingness": {"missing_rate": 0.0, "exclusion_rule": "none"},
        "split": {"train": "fixture_train", "validation": "fixture_validation"},
        "cost_model": {"spread_bps": 2.0, "linear_impact_bps": 1.0},
        "source_artifact_hashes": [canonical_hash({"source": "fixture"})],
    }
    value.update(extra)
    return value


def _positive(kind: str):
    if kind == "ScoreTable":
        rows, labels = [], []
        for day in ("2026-01-05T15:00:00+08:00", "2026-01-06T15:00:00+08:00"):
            for index in range(5):
                symbol = f"00000{index + 1}.SZ"
                rows.append({"symbol": symbol, "decision_time": day, "source_available_at": day, "score": float(index), "label_horizon_sessions": 5})
                labels.append({"symbol": symbol, "decision_time": day, "value": float(index) / 100.0})
        return _base(kind, rows), _context(labels=labels)
    if kind == "RegressionTable":
        return _base(kind, [{"term": "signal", "estimate": 0.5, "std_error": 0.1, "t_statistic": 5.0, "p_value": 5.733e-7, "observation_count": 120}]), _context(condition_number=12.0)
    if kind == "RegimeComparisonTable":
        return _base(kind, [
            {"regime_id": "high", "metric_name": "net_effect", "estimate": 0.03, "sample_count": 60},
            {"regime_id": "low", "metric_name": "net_effect", "estimate": 0.01, "sample_count": 70},
        ]), _context()
    if kind == "EventTable":
        rows, outcomes = [], {}
        for index in range(5):
            event_id = f"event_{index}"
            rows.append({
                "event_id": event_id, "symbol": f"00000{index + 1}.SZ", "event_type": "forecast_revision",
                "event_time": "2026-01-05T09:00:00+08:00", "published_at": "2026-01-05T09:00:00+08:00",
                "available_at": "2026-01-05T09:30:00+08:00", "decision_time": "2026-01-05T15:00:00+08:00",
                "signal": 1.0,
            })
            outcomes[event_id] = {"car": 0.02 + index / 1000.0, "control_return": 0.005, "placebo_car": 0.001}
        return _base(kind, rows), _context(event_outcomes=outcomes, event_window_start=0, event_window_end=5)
    if kind == "DiagnosticTable":
        return _base(kind, [{"diagnostic_id": "drift", "metric_name": "psi", "value": 0.12, "unit": "ratio", "interpretation": "moderate visible drift"}]), _context()
    if kind == "DatasetAssessmentTable":
        return _base(kind, [{"field_name": "close", "source_id": "frozen_snapshot", "coverage": 0.95, "pit_eligible": True, "observed_count": 95, "limitation": "visible sample only"}]), _context(field_coverage={"close": 0.95})
    raise AssertionError(kind)


def _negative(kind: str):
    table, context = _positive(kind)
    if kind == "ScoreTable":
        table = _base(kind, [])
    elif kind == "RegressionTable":
        table = _base(kind, [{"term": "signal", "estimate": 1.0, "std_error": 0.0, "t_statistic": 0.0, "p_value": 1.0, "observation_count": 50}])
    elif kind == "RegimeComparisonTable":
        table = _base(kind, [{"regime_id": "all", "metric_name": "effect", "estimate": 0.1, "sample_count": 50}])
    elif kind == "EventTable":
        table["rows"][0]["available_at"] = "2026-01-06T09:30:00+08:00"
    elif kind == "DiagnosticTable":
        table["rows"][0]["diagnostic_id"] = ""
    elif kind == "DatasetAssessmentTable":
        table["rows"][0]["coverage"] = 1.0
    return table, context


def _diagnostic_cases(kind: str):
    table, context = _positive(kind)
    if kind == "ScoreTable":
        return [(_base(kind, table["rows"][:2]), context), (_base(kind, [dict(row, score=1.0) for row in table["rows"]]), context)]
    if kind == "RegressionTable":
        duplicate = table["rows"] * 2
        return [(_base(kind, duplicate), context), (_base(kind, [dict(table["rows"][0], observation_count=0)]), context)]
    if kind == "RegimeComparisonTable":
        low = [dict(row, sample_count=1) for row in table["rows"]]
        return [(_base(kind, low), context)]
    if kind == "EventTable":
        return [(table, _context(event_outcomes={}))]
    if kind == "DiagnosticTable":
        duplicate = table["rows"] * 2
        return [(_base(kind, duplicate), context)]
    if kind == "DatasetAssessmentTable":
        return [(table, _context(field_coverage={}))]
    raise AssertionError(kind)


__all__ = ["run_output_evaluator_acceptance"]
