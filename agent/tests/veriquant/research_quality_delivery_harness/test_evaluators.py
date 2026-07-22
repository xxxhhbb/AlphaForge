from __future__ import annotations

import copy

import pytest

from veriquant.research_quality_delivery_harness.acceptance import _negative, _positive
from veriquant.research_quality_delivery_harness.evaluators import EvaluationRejected, OUTPUT_KINDS, OutputEvaluatorRegistry


@pytest.mark.parametrize("kind", OUTPUT_KINDS)
def test_each_output_evaluator_produces_content_addressed_official_evidence(kind):
    table, context = _positive(kind)
    registry = OutputEvaluatorRegistry()
    left = registry.evaluate(table=table, context=context, evidence_id=f"canary_{kind}")
    right = registry.evaluate(table=copy.deepcopy(table), context=copy.deepcopy(context), evidence_id=f"canary_{kind}")
    assert left.authority.value == "OFFICIAL_EVIDENCE"
    assert left.metrics
    assert set(left.metrics) == set(left.metric_hashes)
    assert left.artifact_hash == right.artifact_hash


@pytest.mark.parametrize("kind", OUTPUT_KINDS)
def test_each_output_evaluator_rejects_its_negative_fixture(kind):
    table, context = _negative(kind)
    with pytest.raises(EvaluationRejected) as captured:
        OutputEvaluatorRegistry().evaluate(table=table, context=context, evidence_id=f"negative_{kind}")
    assert captured.value.code
    assert captured.value.diagnostics


def test_regression_recomputes_t_and_rejects_mismatch():
    table, context = _positive("RegressionTable")
    table["rows"][0]["t_statistic"] = 4.0
    with pytest.raises(EvaluationRejected, match="t-statistic") as captured:
        OutputEvaluatorRegistry().evaluate(table=table, context=context, evidence_id="bad_t")
    assert captured.value.code == "T_STATISTIC_MISMATCH"


def test_dataset_assessment_cannot_invent_coverage():
    table, context = _positive("DatasetAssessmentTable")
    table["rows"][0]["coverage"] = 1.0
    with pytest.raises(EvaluationRejected) as captured:
        OutputEvaluatorRegistry().evaluate(table=table, context=context, evidence_id="invented_coverage")
    assert captured.value.code == "COVERAGE_MISMATCH"
