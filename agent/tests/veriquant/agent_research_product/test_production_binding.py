from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from veriquant.agent_research_product import (
    ExperimentExecutionSpec,
    FrozenFeatureObservation,
    ProductionEvidenceContext,
    ProductionExperimentBinding,
)
from veriquant.agent_research_sandbox.contracts import (
    ExperimentReviewPolicy,
    SandboxAmendmentIdentity,
    ScoreRow,
    ScoreTable,
)
from veriquant.agent_research_sandbox.docker_runner import (
    DockerResearchRequest,
    DockerTrainingObservation,
)
from veriquant.agent_research_sandbox.evaluator import EvidenceAdmissionStatus, SandboxEvidenceEvaluator
from veriquant.agent_research_sandbox.process_runner import (
    AMENDMENT_VERSION,
    ProcessIsolationLevel,
    ProcessRunStatus,
    ResearchProcessReceipt,
    ResearchProcessRequest,
    canonical_hash,
)
from veriquant.agent_research_sandbox.review import ExperimentReviewGate
from veriquant.data import LabelRow, MarketSnapshot, SnapshotRow
from veriquant.evidence import CostModel
from veriquant.research_agent_runtime import (
    ResearchAgentOutcome,
    ResearchAgentStatus,
)
from veriquant.research_run_controller import ResearchRunLifecycle, ResearchRunManifest


UTC = timezone.utc
T0 = datetime(2026, 1, 5, 8, tzinfo=UTC)
T1 = T0 + timedelta(days=1)
SOURCE = "def run_research(rows, seed):\n    return {'rows': []}\n"


def h(char: str) -> str:
    return "sha256:" + char * 64


def source_hash(source: str = SOURCE) -> str:
    return "sha256:" + hashlib.sha256(source.encode("utf-8")).hexdigest()


def snapshot() -> MarketSnapshot:
    rows = tuple(
        SnapshotRow(
            event_time=when,
            symbol=symbol,
            values={"close": close, "volume": volume},
        )
        for when, symbol, close, volume in (
            (T0, "000001.SZ", 10.0, 1000.0),
            (T0, "000002.SZ", 11.0, 1100.0),
            (T1, "000001.SZ", 10.5, 1200.0),
            (T1, "000002.SZ", 10.8, 1300.0),
        )
    )
    return MarketSnapshot(
        dataset_id="approved_proxy_panel",
        decision_time=T1,
        start_time=T0,
        fields=("close", "volume"),
        rows=rows,
        historical_universe={
            T0.isoformat(): ("000001.SZ", "000002.SZ"),
            T1.isoformat(): ("000001.SZ", "000002.SZ"),
        },
        coverage_by_field={"close": 1.0, "volume": 1.0},
    )


def observations(value: MarketSnapshot) -> tuple[FrozenFeatureObservation, ...]:
    return tuple(
        FrozenFeatureObservation(
            symbol=row.symbol,
            decision_time=row.event_time,
            source_available_at=row.event_time,
            features=dict(row.values),
        )
        for row in value.rows
    )


def labels() -> tuple[LabelRow, ...]:
    return tuple(
        LabelRow(
            event_time=when,
            symbol=symbol,
            value=value,
            available_at=when + timedelta(days=20),
        )
        for when, symbol, value in (
            (T0, "000001.SZ", 0.03),
            (T0, "000002.SZ", -0.01),
            (T1, "000001.SZ", 0.02),
            (T1, "000002.SZ", -0.02),
        )
    )


def outcome() -> ResearchAgentOutcome:
    return ResearchAgentOutcome(
        task_id="task_market",
        task_hash=h("1"),
        specialist_id="market_structure",
        status=ResearchAgentStatus.COMPLETED,
        plan_hash=h("2"),
        investigation_receipts=(),
        draft_artifacts=(),
        provider_calls=1,
        tool_calls=2,
    )


def make_binding(*, training: bool = False):
    frozen = snapshot()
    policy = ExperimentReviewPolicy()
    amendment = SandboxAmendmentIdentity(
        amendment_id="research_sandbox_amendment",
        amendment_version=AMENDMENT_VERSION,
        constitution_hash=h("3"),
        policy_hash=h("4"),
        authorized_at=T0,
    )
    training_rows = ()
    cutoff = None
    if training:
        training_rows = (
            DockerTrainingObservation(
                symbol="000001.SZ",
                decision_time=(T0 - timedelta(days=40)).isoformat(),
                source_available_at=(T0 - timedelta(days=40)).isoformat(),
                outcome_available_at=(T0 - timedelta(days=10)).isoformat(),
                target_value=0.01,
                features={"close": 9.0, "volume": 900.0},
            ),
        )
        cutoff = (T0 - timedelta(days=5)).isoformat()
    context = ProductionEvidenceContext(
        snapshot=frozen,
        labels=labels(),
        dataset_hash=h("5"),
        availability_policy_hash=h("6"),
        split_hash=h("7"),
        cost_model=CostModel(spread_bps=2.0, linear_impact_bps=1.0),
        seed=7,
    )
    binding = ProductionExperimentBinding(
        amendment=amendment,
        policy=policy,
        python_runtime_hash=h("8"),
        dependency_manifest_hash=h("9"),
        evidence_context=context,
        execution_specs={
            "market_structure": ExperimentExecutionSpec(
                output_kind="ScoreTable",
                feature_rows=observations(frozen),
                training_rows=training_rows,
                training_cutoff=cutoff,
            )
        },
    )
    run_manifest = ResearchRunManifest(
        run_id="run_production_binding",
        campaign_hash=h("a"),
        intent_hash=h("b"),
        data_plan_hash=h("c"),
        route_plan_hash=h("d"),
        policy_hash=policy.artifact_hash,
        code_hash=h("e"),
        selected_group_ids=("market_structure",),
        candidate_request_hashes=(),
        created_at=T0,
        provider_id="configured_provider",
        model_id="configured_model",
        data_snapshot_hash=frozen.artifact_hash,
        current_stage=ResearchRunLifecycle.RESEARCHING,
        remaining_candidate_slots=4,
    )
    return binding, run_manifest


def accepted_request(binding, run_manifest):
    request = binding.review_request(
        manifest=run_manifest, outcome=outcome(), source_text=SOURCE
    )
    decision = ExperimentReviewGate().review(request)
    assert decision.status.value == "ACCEPTED"
    return request, decision


def completed_receipt(request, table, process):
    base = process.process_request if isinstance(process, DockerResearchRequest) else process
    output = table.to_dict()
    return ResearchProcessReceipt(
        run_id=base.run_id,
        experiment_id=base.experiment_id,
        status=ProcessRunStatus.COMPLETED_AUXILIARY,
        isolation_level=ProcessIsolationLevel.OS_SANDBOXED,
        input_hash=process.input_hash,
        code_hash=base.code_hash,
        output_hash=canonical_hash(
            {"input_hash": process.input_hash, "output_kind": "ScoreTable", "output": output}
        ),
        output_kind="ScoreTable",
        output=output,
        failure_code=None,
        diagnostic=None,
        amendment_hash=base.amendment_hash,
        official_evidence_eligible=True,
    )


def test_production_binding_builds_typed_review_process_and_real_evidence_arguments():
    binding, run_manifest = make_binding()
    request, decision = accepted_request(binding, run_manifest)
    process = binding.process_request(
        manifest=run_manifest,
        review_request=request,
        review_decision=decision,
        source_text=SOURCE,
    )
    assert isinstance(process, ResearchProcessRequest)
    assert process.data_identity_hash == binding.data_identity.artifact_hash
    assert all("label" not in row and "target_value" not in row for row in process.feature_rows)

    score_table = ScoreTable(
        experiment_id=request.experiment_id,
        code_identity_hash=request.code_identity.artifact_hash,
        data_identity_hash=request.data_identity.artifact_hash,
        rows=tuple(
            sorted(
                (
                    ScoreRow(
                        symbol=row.symbol,
                        decision_time=row.decision_time,
                        source_available_at=row.source_available_at,
                        score=float(index % 2),
                        label_horizon_sessions=20,
                    )
                    for index, row in enumerate(observations(binding.evidence_context.snapshot))
                ),
                key=lambda row: row.artifact_hash,
            )
        ),
    )
    receipt = completed_receipt(request, score_table, process)
    parsed = binding.parse_table(review_request=request, receipt=receipt)
    arguments = binding.evidence_arguments(
        manifest=run_manifest,
        review_request=request,
        review_decision=decision,
        receipt=receipt,
        table=parsed,
    )
    assert arguments["snapshot"].artifact_hash == run_manifest.data_snapshot_hash
    assert arguments["split_hash"] == h("7")
    assert arguments["cost_model"].artifact_hash == CostModel(
        spread_bps=2.0, linear_impact_bps=1.0
    ).artifact_hash
    result = SandboxEvidenceEvaluator().evaluate(**dict(arguments))
    assert result.status is EvidenceAdmissionStatus.OFFICIAL_EVIDENCE
    assert result.experiment_identity is not None
    assert result.experiment_identity.seed == 7


def test_supervised_spec_builds_docker_request_and_keeps_prediction_rows_label_free():
    binding, run_manifest = make_binding(training=True)
    request, decision = accepted_request(binding, run_manifest)
    execution = binding.process_request(
        manifest=run_manifest,
        review_request=request,
        review_decision=decision,
        source_text=SOURCE,
    )
    assert isinstance(execution, DockerResearchRequest)
    assert execution.entrypoint_version == "TRAIN_PREDICT_V2"
    assert execution.training_rows[0].target_value == 0.01
    assert all("target_value" not in row for row in execution.process_request.feature_rows)


def test_manifest_snapshot_or_policy_identity_mismatch_fails_closed():
    binding, run_manifest = make_binding()
    with pytest.raises(ValueError, match="MarketSnapshot"):
        binding.review_request(
            manifest=replace(run_manifest, data_snapshot_hash=h("f")),
            outcome=outcome(),
            source_text=SOURCE,
        )
    with pytest.raises(ValueError, match="review policy"):
        binding.review_request(
            manifest=replace(run_manifest, policy_hash=h("f")),
            outcome=outcome(),
            source_text=SOURCE,
        )


def test_feature_rows_must_be_exact_snapshot_rows_and_cannot_contain_labels():
    frozen = snapshot()
    with pytest.raises(ValueError, match="label"):
        FrozenFeatureObservation(
            symbol="000001.SZ",
            decision_time=T0,
            source_available_at=T0,
            features={"label": 0.1},
        )
    policy = ExperimentReviewPolicy()
    amendment = SandboxAmendmentIdentity(
        amendment_id="research_sandbox_amendment",
        amendment_version=AMENDMENT_VERSION,
        constitution_hash=h("1"),
        policy_hash=h("2"),
        authorized_at=T0,
    )
    context = ProductionEvidenceContext(
        snapshot=frozen,
        labels=labels(),
        dataset_hash=h("3"),
        availability_policy_hash=h("4"),
        split_hash=h("5"),
        cost_model=CostModel(spread_bps=1.0, linear_impact_bps=1.0),
        seed=7,
    )
    wrong = FrozenFeatureObservation(
        symbol="000001.SZ",
        decision_time=T0,
        source_available_at=T0,
        features={"close": 999.0, "volume": 1000.0},
    )
    with pytest.raises(ValueError, match="exact frozen snapshot"):
        ProductionExperimentBinding(
            amendment=amendment,
            policy=policy,
            python_runtime_hash=h("6"),
            dependency_manifest_hash=h("7"),
            evidence_context=context,
            execution_specs={
                "market_structure": ExperimentExecutionSpec(
                    output_kind="ScoreTable", feature_rows=(wrong,)
                )
            },
        )


def test_feature_rows_may_select_an_exact_nonempty_snapshot_subset_and_preserve_missing():
    frozen = snapshot()
    first = frozen.rows[0]
    enriched = replace(
        frozen,
        fields=("close", "volume", "nullable_metric"),
        rows=tuple(
            replace(row, values={**dict(row.values), "nullable_metric": None})
            for row in frozen.rows
        ),
        coverage_by_field={"close": 1.0, "volume": 1.0, "nullable_metric": 0.5},
    )
    policy = ExperimentReviewPolicy()
    amendment = SandboxAmendmentIdentity(
        amendment_id="research_sandbox_amendment",
        amendment_version=AMENDMENT_VERSION,
        constitution_hash=h("1"),
        policy_hash=h("2"),
        authorized_at=T0,
    )
    context = ProductionEvidenceContext(
        snapshot=enriched,
        labels=labels(),
        dataset_hash=h("3"),
        availability_policy_hash=h("4"),
        split_hash=h("5"),
        cost_model=CostModel(spread_bps=1.0, linear_impact_bps=1.0),
        seed=7,
    )
    exact_subset = FrozenFeatureObservation(
        symbol=first.symbol,
        decision_time=first.event_time,
        source_available_at=first.event_time,
        features={"close": first.values["close"], "nullable_metric": None},
    )
    ProductionExperimentBinding(
        amendment=amendment,
        policy=policy,
        python_runtime_hash=h("6"),
        dependency_manifest_hash=h("7"),
        evidence_context=context,
        execution_specs={
            "market_structure": ExperimentExecutionSpec(
                output_kind="ScoreTable", feature_rows=(exact_subset,)
            )
        },
    )

    extra = replace(exact_subset, features={"close": first.values["close"], "invented": 0.0})
    with pytest.raises(ValueError, match="exact frozen snapshot"):
        ProductionExperimentBinding(
            amendment=amendment,
            policy=policy,
            python_runtime_hash=h("6"),
            dependency_manifest_hash=h("7"),
            evidence_context=context,
            execution_specs={
                "market_structure": ExperimentExecutionSpec(
                    output_kind="ScoreTable", feature_rows=(extra,)
                )
            },
        )


def test_standard_table_parser_rejects_runner_owned_identity_tampering():
    binding, run_manifest = make_binding()
    request, decision = accepted_request(binding, run_manifest)
    table = ScoreTable(
        experiment_id=request.experiment_id,
        code_identity_hash=request.code_identity.artifact_hash,
        data_identity_hash=request.data_identity.artifact_hash,
        rows=(
            ScoreRow(
                symbol="000001.SZ",
                decision_time=T0,
                source_available_at=T0,
                score=1.0,
                label_horizon_sessions=20,
            ),
        ),
    )
    process = binding.process_request(
        manifest=run_manifest,
        review_request=request,
        review_decision=decision,
        source_text=SOURCE,
    )
    receipt = completed_receipt(request, table, process)
    tampered = dict(receipt.output)
    tampered["experiment_id"] = "other_experiment"
    with pytest.raises(Exception):
        binding.parse_table(
            review_request=request,
            receipt=replace(receipt, output=tampered),
        )
