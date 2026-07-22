from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from veriquant.agent_research_sandbox.contracts import (
    DatasetAssessmentRow,
    DatasetAssessmentTable,
    DiagnosticRow,
    DiagnosticTable,
    EventRow,
    EventTable,
    ExperimentCodeIdentity,
    ExperimentDataIdentity,
    ExperimentReviewDecision,
    MechanicalReviewStatus,
    RegressionRow,
    RegressionTable,
    RegimeComparisonRow,
    RegimeComparisonTable,
    ResearchOutputClass,
    SandboxAmendmentIdentity,
    ScoreRow,
    ScoreTable,
)
from veriquant.agent_research_sandbox.evaluator import (
    EvidenceAdmissionFailureCode,
    EvidenceAdmissionStatus,
    SandboxEvidenceEvaluator,
)
from veriquant.agent_research_sandbox.process_runner import (
    ProcessIsolationLevel,
    ProcessRunStatus,
    ResearchProcessReceipt,
    canonical_hash,
)
from veriquant.data import LabelRow, MarketSnapshot, SnapshotRow
from veriquant.evidence import CostModel


UTC = timezone.utc


def _hash(value: str) -> str:
    return canonical_hash({"value": value})


def _fixtures():
    dates = tuple(datetime(2025, 1, day, tzinfo=UTC) for day in (2, 3, 6))
    fields = ("volume",)
    rows = tuple(
        SnapshotRow(event_time=day, symbol=symbol, values={"volume": 1000.0 + index})
        for index, (day, symbol) in enumerate(
            (item for day in dates for item in ((day, "000001.SZ"), (day, "600000.SH")))
        )
    )
    snapshot = MarketSnapshot(
        dataset_id="ashare-frozen",
        decision_time=dates[-1],
        start_time=dates[0],
        fields=fields,
        rows=rows,
        historical_universe={
            day.isoformat(): ("000001.SZ", "600000.SH") for day in dates
        },
        coverage_by_field={"volume": 1.0},
    )
    amendment = SandboxAmendmentIdentity(
        amendment_id="sandbox-amendment",
        amendment_version="v1",
        constitution_hash=_hash("constitution"),
        policy_hash=_hash("amendment-policy"),
        authorized_at=dates[0],
    )
    review_policy_hash = _hash("review-policy")
    code = ExperimentCodeIdentity(
        source_hash=_hash("source"),
        python_runtime_hash=_hash("python"),
        dependency_manifest_hash=_hash("dependencies"),
        review_policy_hash=review_policy_hash,
        amendment_hash=amendment.artifact_hash,
    )
    data = ExperimentDataIdentity(
        dataset_hash=_hash("dataset"),
        snapshot_hash=snapshot.artifact_hash,
        universe_hash=_hash("universe"),
        schema_hash=_hash("schema"),
        availability_policy_hash=_hash("availability"),
        decision_start=dates[0],
        decision_end=dates[-1],
    )
    scores = []
    labels = []
    for index, source in enumerate(rows):
        scores.append(
            ScoreRow(
                symbol=source.symbol,
                decision_time=source.event_time,
                source_available_at=source.event_time,
                score=float(index % 2),
                label_horizon_sessions=5,
            )
        )
        labels.append(
            LabelRow(
                event_time=source.event_time,
                symbol=source.symbol,
                value=(-1.0 if index % 2 == 0 else 1.0) * 0.01,
                available_at=source.event_time + timedelta(days=7),
            )
        )
    table = ScoreTable(
        experiment_id="experiment-1",
        code_identity_hash=code.artifact_hash,
        data_identity_hash=data.artifact_hash,
        rows=tuple(sorted(scores, key=lambda item: item.artifact_hash)),
    )
    review = ExperimentReviewDecision(
        experiment_id=table.experiment_id,
        source_hash=code.source_hash,
        policy_hash=review_policy_hash,
        amendment_hash=amendment.artifact_hash,
        status=MechanicalReviewStatus.ACCEPTED,
        findings=(),
        reviewer_suggestions=(),
        revision_number=0,
        may_submit_once_more=False,
    )
    output = table.to_dict()
    input_hash = _hash("runner-input")
    receipt = ResearchProcessReceipt(
        run_id="run-1",
        experiment_id=table.experiment_id,
        status=ProcessRunStatus.COMPLETED_AUXILIARY,
        isolation_level=ProcessIsolationLevel.OS_SANDBOXED,
        input_hash=input_hash,
        code_hash=code.source_hash,
        output_hash=canonical_hash(
            {"input_hash": input_hash, "output_kind": ScoreTable.__name__, "output": output}
        ),
        output_kind=ScoreTable.__name__,
        output=output,
        failure_code=None,
        diagnostic=None,
        amendment_hash=amendment.artifact_hash,
        official_evidence_eligible=True,
    )
    return {
        "dates": dates,
        "snapshot": snapshot,
        "amendment": amendment,
        "code_identity": code,
        "data_identity": data,
        "review_decision": review,
        "table": table,
        "runner_receipt": receipt,
        "labels": tuple(labels),
        "split_hash": _hash("split"),
        "cost_model": CostModel(spread_bps=2.0, linear_impact_bps=1.0),
        "seed": 7,
    }


def _evaluate(values, **overrides):
    arguments = {key: value for key, value in values.items() if key != "dates"}
    arguments.update(overrides)
    return SandboxEvidenceEvaluator().evaluate(**arguments)


def test_score_table_is_independently_joined_and_evaluated() -> None:
    values = _fixtures()
    result = _evaluate(values)
    assert result.status is EvidenceAdmissionStatus.OFFICIAL_EVIDENCE
    assert result.official_evidence is not None
    assert result.official_evidence.partition == "VALIDATION"
    assert result.evaluation_row_count == 6
    assert result.official_evidence.metrics.observation_count == 6
    # No label-bearing rows are exposed on the admission result.
    assert not hasattr(result, "evaluation_rows")
    assert not hasattr(result, "labels")


def test_market_event_availability_and_research_decision_clocks_are_distinct() -> None:
    values = _fixtures()
    source_snapshot = values["snapshot"]
    shifted_rows = tuple(
        replace(row, event_time=row.event_time + timedelta(hours=15))
        for row in source_snapshot.rows
    )
    snapshot = replace(
        source_snapshot,
        start_time=shifted_rows[0].event_time,
        decision_time=shifted_rows[-1].event_time + timedelta(hours=2),
        rows=shifted_rows,
        historical_universe={
            (day + timedelta(hours=15)).isoformat(): members
            for day, members in (
                (datetime.fromisoformat(raw), members)
                for raw, members in source_snapshot.historical_universe.items()
            )
        },
    )
    data_identity = replace(
        values["data_identity"],
        snapshot_hash=snapshot.artifact_hash,
        decision_start=shifted_rows[0].event_time + timedelta(hours=2),
        decision_end=shifted_rows[-1].event_time + timedelta(hours=2),
    )
    scores = tuple(
        ScoreRow(
            symbol=row.symbol,
            decision_time=row.event_time + timedelta(hours=2),
            source_available_at=row.event_time + timedelta(hours=1),
            score=float(index % 2),
            label_horizon_sessions=5,
        )
        for index, row in enumerate(shifted_rows)
    )
    table = ScoreTable(
        experiment_id=values["table"].experiment_id,
        code_identity_hash=values["code_identity"].artifact_hash,
        data_identity_hash=data_identity.artifact_hash,
        rows=tuple(sorted(scores, key=lambda item: item.artifact_hash)),
    )
    labels = tuple(
        replace(
            label,
            event_time=label.event_time + timedelta(hours=15),
            available_at=label.available_at + timedelta(hours=15),
        )
        for label in values["labels"]
    )
    receipt = values["runner_receipt"]
    output = table.to_dict()
    receipt = replace(
        receipt,
        output=output,
        output_hash=canonical_hash(
            {"input_hash": receipt.input_hash, "output_kind": receipt.output_kind, "output": output}
        ),
    )
    result = _evaluate(
        values,
        snapshot=snapshot,
        data_identity=data_identity,
        table=table,
        labels=labels,
        runner_receipt=receipt,
    )
    assert result.status is EvidenceAdmissionStatus.OFFICIAL_EVIDENCE
    assert result.evaluation_row_count == len(scores)


def test_process_fenced_receipt_is_typed_blocked() -> None:
    values = _fixtures()
    receipt = values["runner_receipt"]
    fenced = replace(
        receipt,
        isolation_level=ProcessIsolationLevel.PROCESS_FENCED,
        official_evidence_eligible=False,
    )
    result = _evaluate(values, runner_receipt=fenced)
    assert result.status is EvidenceAdmissionStatus.BLOCKED
    assert result.failure_code is EvidenceAdmissionFailureCode.RUNNER_NOT_OS_SANDBOXED
    assert result.official_evidence is None


@pytest.mark.parametrize(
    "table_factory",
    [
        lambda values: EventTable(
            experiment_id="experiment-1",
            code_identity_hash=values["code_identity"].artifact_hash,
            data_identity_hash=values["data_identity"].artifact_hash,
            rows=(
                EventRow(
                    event_id="event-1",
                    symbol="000001.SZ",
                    event_type="announcement",
                    event_time=values["dates"][0],
                    published_at=values["dates"][0],
                    available_at=values["dates"][0],
                    decision_time=values["dates"][0],
                    signal=1.0,
                ),
            ),
        ),
        lambda values: RegressionTable(
            experiment_id="experiment-1",
            code_identity_hash=values["code_identity"].artifact_hash,
            data_identity_hash=values["data_identity"].artifact_hash,
            specification_id="spec-1",
            rows=(
                RegressionRow(
                    term="signal",
                    estimate=0.1,
                    std_error=0.05,
                    t_statistic=2.0,
                    p_value=0.05,
                    observation_count=6,
                ),
            ),
        ),
        lambda values: RegimeComparisonTable(
            experiment_id="experiment-1",
            code_identity_hash=values["code_identity"].artifact_hash,
            data_identity_hash=values["data_identity"].artifact_hash,
            rows=(
                RegimeComparisonRow(
                    regime_id="all",
                    metric_name="rank_ic",
                    estimate=0.1,
                    sample_count=6,
                ),
            ),
        ),
        lambda values: DiagnosticTable(
            experiment_id="experiment-1",
            code_identity_hash=values["code_identity"].artifact_hash,
            data_identity_hash=values["data_identity"].artifact_hash,
            rows=(
                DiagnosticRow(
                    diagnostic_id="coverage",
                    metric_name="coverage",
                    value=1.0,
                    unit="ratio",
                    interpretation="complete fixture coverage",
                ),
            ),
        ),
        lambda values: DatasetAssessmentTable(
            experiment_id="experiment-1",
            code_identity_hash=values["code_identity"].artifact_hash,
            data_identity_hash=values["data_identity"].artifact_hash,
            rows=(
                DatasetAssessmentRow(
                    field_name="volume",
                    source_id="frozen-snapshot",
                    coverage=1.0,
                    pit_eligible=True,
                    observed_count=6,
                    limitation="fixture only",
                ),
            ),
        ),
    ],
)
def test_non_score_tables_can_only_be_auxiliary(table_factory) -> None:
    values = _fixtures()
    result = _evaluate(values, table=table_factory(values))
    assert result.status is EvidenceAdmissionStatus.AUXILIARY_ONLY
    assert result.failure_code is EvidenceAdmissionFailureCode.NON_SCORE_TABLE_AUXILIARY_ONLY
    assert result.auxiliary_output_class is ResearchOutputClass.AUXILIARY_RESEARCH_EVIDENCE
    assert result.official_evidence is None


def test_future_label_perturbation_does_not_change_past_evidence() -> None:
    values = _fixtures()
    original = _evaluate(values)
    unrelated = LabelRow(
        event_time=values["dates"][-1] + timedelta(days=30),
        symbol="000001.SZ",
        value=999.0,
        available_at=values["dates"][-1] + timedelta(days=40),
    )
    perturbed = _evaluate(values, labels=values["labels"] + (unrelated,))
    assert original.official_evidence == perturbed.official_evidence
    assert original.artifact_hash == perturbed.artifact_hash


def test_delayed_source_availability_is_blocked() -> None:
    values = _fixtures()
    rows = list(values["table"].rows)
    object.__setattr__(rows[0], "source_available_at", rows[0].decision_time + timedelta(seconds=1))
    table = ScoreTable(
        experiment_id=values["table"].experiment_id,
        code_identity_hash=values["table"].code_identity_hash,
        data_identity_hash=values["table"].data_identity_hash,
        rows=tuple(sorted(rows, key=lambda item: item.artifact_hash)),
    )
    receipt = values["runner_receipt"]
    output = table.to_dict()
    rebound = replace(
        receipt,
        output=output,
        output_hash=canonical_hash(
            {"input_hash": receipt.input_hash, "output_kind": receipt.output_kind, "output": output}
        ),
    )
    result = _evaluate(values, table=table, runner_receipt=rebound)
    assert result.status is EvidenceAdmissionStatus.BLOCKED
    assert result.failure_code is EvidenceAdmissionFailureCode.AVAILABILITY_VIOLATION


def test_runner_output_tamper_is_blocked() -> None:
    values = _fixtures()
    receipt = values["runner_receipt"]
    altered_output = {**receipt.output, "experiment_id": "different-experiment"}
    altered = replace(
        receipt,
        output=altered_output,
        output_hash=canonical_hash(
            {
                "input_hash": receipt.input_hash,
                "output_kind": receipt.output_kind,
                "output": altered_output,
            }
        ),
    )
    result = _evaluate(values, runner_receipt=altered)
    assert result.status is EvidenceAdmissionStatus.BLOCKED
    assert result.failure_code is EvidenceAdmissionFailureCode.RUNNER_OUTPUT_TAMPERED


def test_same_frozen_inputs_have_stable_identity_evidence_and_result_hash() -> None:
    first = _evaluate(_fixtures())
    second = _evaluate(_fixtures())
    assert first.experiment_identity == second.experiment_identity
    assert first.official_evidence == second.official_evidence
    assert first.artifact_hash == second.artifact_hash
