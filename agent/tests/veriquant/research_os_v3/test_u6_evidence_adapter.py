from __future__ import annotations

from datetime import datetime, timedelta, timezone
from dataclasses import replace

import pytest

from veriquant.candidate_translation import CandidateDraftEnvelope, SafeCandidateTranslator
from veriquant.contracts import ContractValidationError, FailureCode
from veriquant.data import MarketSnapshot, SnapshotRow, canonical_content_hash
from veriquant.evidence.ledger import TrialLedger
from veriquant.evidence.models import CostModel, WalkForwardSpec
from veriquant.research_os_v3.evidence_adapter import (
    EvidenceAdapterFailureCode,
    EvidenceAdapterStatus,
    U4EvidenceAdapterRequest,
    U4OfficialEvidenceAdapter,
)
from veriquant.research_os_v3.multi_models import (
    MultiSpecialistBranchResult,
    MultiSpecialistBranchStatus,
    MultiSpecialistCandidateRef,
    MultiSpecialistRunResult,
    MultiSpecialistRunStatus,
)
from veriquant.research_validation_pipeline_v2 import FrozenValidationData
from veriquant.specialist_research import (
    CandidatePreflightResult,
    PreflightStatus,
    ResearchDirectionIdea,
    ResearchExplorationPlan,
    SpecialistAgentOutcome,
    SpecialistCandidateDraft,
    SpecialistResultKind,
    SpecialistStage,
    SpecialistUsage,
)


H = "sha256:" + "a" * 64


def _snapshot():
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    symbols = ("AAA", "BBB", "CCC", "DDD")
    rows = []
    labels = {}
    for day in range(12):
        when = start + timedelta(days=day)
        for index, symbol in enumerate(symbols):
            rows.append(SnapshotRow(
                event_time=when,
                symbol=symbol,
                values={"close": 10.0 + day * (index + 1) * 0.1, "volume": 1000.0 + day + index},
            ))
            labels[f"{when.isoformat()}|{symbol}"] = (index - 1.5) * 0.001 + day * 0.00001
    snapshot = MarketSnapshot(
        dataset_id="fixture.u6",
        decision_time=start + timedelta(days=12),
        start_time=start,
        fields=("close", "volume"),
        rows=tuple(rows),
        historical_universe={(start + timedelta(days=day)).isoformat(): symbols for day in range(12)},
        coverage_by_field={"close": 1.0, "volume": 1.0},
    )
    return snapshot, labels


def _candidate(candidate_id: str, expression: str) -> SpecialistCandidateDraft:
    return SpecialistCandidateDraft(
        candidate_id=candidate_id,
        family_id="momentum-family",
        hypothesis="短期价格变化可能包含横截面信息。",
        economic_mechanism="投资者反应速度存在差异。",
        expected_direction="positive",
        forecast_horizon_sessions=1,
        lookback_sessions=3,
        required_fields=("close",),
        expression=expression,
        availability_assumptions=("event_time<=available_at<=decision_time",),
        neutralization=(),
        falsification_tests=("成本后收益为负则拒绝",),
        cost_risks=("换手可能侵蚀收益",),
        exposure_risks=("可能含规模暴露",),
        novelty_explanation="与已登记基线进行机器比较。",
    )


def _outcome():
    valid = _candidate("candidate-1", "rank_cross_section(pct_change(close, 1))")
    invalid = _candidate("candidate-2", "ts_argmax(close, 1)")
    translator = SafeCandidateTranslator()
    accepted = translator.translate(CandidateDraftEnvelope(
        campaign_hash=H,
        group_id="market_structure",
        candidate_id=valid.candidate_id,
        expression=valid.expression,
        requested_lookback_sessions=1,
        declared_lookback_sessions=valid.lookback_sessions,
        allowed_fields=("close", "volume"),
        source_draft_hash=valid.artifact_hash,
    ))
    rejected = translator.translate(CandidateDraftEnvelope(
        campaign_hash=H,
        group_id="market_structure",
        candidate_id=invalid.candidate_id,
        expression=invalid.expression,
        requested_lookback_sessions=1,
        declared_lookback_sessions=invalid.lookback_sessions,
        allowed_fields=("close", "volume"),
        source_draft_hash=invalid.artifact_hash,
    ))
    plan = ResearchExplorationPlan(
        specialist_id="market_structure",
        scope_summary="研究价格行为的可证伪候选。",
        directions=(
            ResearchDirectionIdea(
                direction_id="direction-1", mechanism="反应不足", differentiation="短窗口",
                required_fields=("close",), falsification="成本后为负",
            ),
            ResearchDirectionIdea(
                direction_id="direction-2", mechanism="横截面排序", differentiation="稳健排序",
                required_fields=("close",), falsification="跨期不稳定",
            ),
        ),
        investigation_questions=("数据覆盖是否充分",),
        stop_conditions=("预算耗尽",),
    )
    stages = (
        SpecialistStage.CREATED, SpecialistStage.SCOPING, SpecialistStage.PLANNING,
        SpecialistStage.INVESTIGATING, SpecialistStage.GENERATING,
        SpecialistStage.SELF_CRITIQUING, SpecialistStage.PREFLIGHT,
        SpecialistStage.SUBMITTING, SpecialistStage.COMPLETED,
    )
    return SpecialistAgentOutcome(
        request_hash=H,
        specialist_id="market_structure",
        status=SpecialistStage.COMPLETED,
        plan=plan,
        findings=(),
        candidates=(valid, invalid),
        preflight_results=(
            CandidatePreflightResult(
                candidate_id=valid.candidate_id, draft_hash=valid.artifact_hash,
                status=PreflightStatus.ACCEPTED,
                normalized_expression=accepted.normalized_expression,
                factor_dag_hash=accepted.factor_dag_hash,
            ),
            CandidatePreflightResult(
                candidate_id=invalid.candidate_id, draft_hash=invalid.artifact_hash,
                status=PreflightStatus.CAPABILITY_REQUEST,
                failure_code=rejected.failure_code or "INVALID_FACTOR_DAG",
                message=rejected.message,
            ),
        ),
        capability_requests=(),
        usage=SpecialistUsage(iterations=4, tool_calls=0, input_tokens=100, output_tokens=100, revisions=0),
        stage_history=stages,
        result_kind=SpecialistResultKind.DRAFT_CANDIDATES,
    )


def _run(outcome: SpecialistAgentOutcome) -> MultiSpecialistRunResult:
    preflights = {item.candidate_id: item for item in outcome.preflight_results}
    refs = tuple(sorted((
        MultiSpecialistCandidateRef(
            specialist_id=outcome.specialist_id,
            candidate_id=item.candidate_id,
            candidate_hash=item.artifact_hash,
            preflight_status=preflights[item.candidate_id].status.value,
            factor_dag_hash=preflights[item.candidate_id].factor_dag_hash,
            failure_code=preflights[item.candidate_id].failure_code,
        ) for item in outcome.candidates
    ), key=lambda item: item.candidate_id))
    branch = MultiSpecialistBranchResult(
        specialist_id=outcome.specialist_id,
        branch_identity_hash=canonical_content_hash({"branch": outcome.specialist_id}),
        status=MultiSpecialistBranchStatus.COMPLETED_DRAFT,
        context_hash=canonical_content_hash({"context": 1}),
        outcome_hash=outcome.artifact_hash,
        candidate_refs=refs,
        typed_failures=(),
        missing_capabilities=(),
        provider_calls=4,
    )
    return MultiSpecialistRunResult(
        run_id="u6-fixture-run",
        manifest_hash=canonical_content_hash({"manifest": 1}),
        campaign_hash=H,
        route_hash=canonical_content_hash({"route": 1}),
        context_hash=branch.context_hash,
        status=MultiSpecialistRunStatus.COMPLETED,
        branches=(branch,),
        candidate_refs=refs,
        committed_branch_hashes=(branch.artifact_hash,),
        candidate_count=2,
        accepted_preflight_count=1,
        typed_failures=(),
    )


def _request(*, outcomes=None, approver_id="evidence.court.visible"):
    outcome = _outcome()
    snapshot, labels = _snapshot()
    frozen = FrozenValidationData(
        snapshot=snapshot,
        labels=labels,
        qualification_hash=canonical_content_hash({"qualified": True}),
        source_label="DETERMINISTIC_OFFLINE_FIXTURE",
    )
    return U4EvidenceAdapterRequest(
        run=_run(outcome),
        outcomes={outcome.specialist_id: outcome} if outcomes is None else outcomes,
        frozen_data=frozen,
        walk_forward=WalkForwardSpec(train_periods=3, validation_periods=2, sealed_periods=1, step_periods=2),
        cost_model=CostModel(spread_bps=2.0, linear_impact_bps=3.0),
        requested_lookback_sessions=1,
        family_budget=2,
        seed=7,
        approver_id=approver_id,
    )


def test_every_u4_candidate_gets_terminal_accounting_and_only_service_produces_official(tmp_path):
    result = U4OfficialEvidenceAdapter(ledger_path=tmp_path / "trial-ledger.sqlite3").run(_request())

    assert len(result.terminals) == 2
    assert result.terminals[0].status is EvidenceAdapterStatus.OFFICIAL_VISIBLE_EVALUATED
    assert result.terminals[0].visible_metrics is not None
    assert result.terminals[0].visible_metrics.evidence_class == "OFFICIAL_EVIDENCE"
    assert result.terminals[1].status is EvidenceAdapterStatus.TRANSLATION_REJECTED
    assert result.terminals[1].visible_metrics is None
    assert all(item.budget_consumed for item in result.terminals)
    assert result.official_evidence_hash is not None
    assert result.promotion_hash is None
    replay = TrialLedger(tmp_path / "trial-ledger.sqlite3").replay()
    assert sum(item.consumed for item in replay.families.values()) == 2


def test_replay_is_idempotent_and_has_same_result_hash(tmp_path):
    adapter = U4OfficialEvidenceAdapter(ledger_path=tmp_path / "trial-ledger.sqlite3")
    request = _request()

    first = adapter.run(request)
    second = adapter.run(request)

    assert first.artifact_hash == second.artifact_hash
    assert first.official_evidence_hash == second.official_evidence_hash
    replay = TrialLedger(tmp_path / "trial-ledger.sqlite3").replay()
    assert sum(item.consumed for item in replay.families.values()) == 2


def test_trusted_product_mode_never_falls_back_to_legacy_factor_dag(tmp_path):
    request = replace(_request(), trusted_product_mode=True)
    result = U4OfficialEvidenceAdapter(
        ledger_path=tmp_path / "trusted-product-ledger.sqlite3"
    ).run(request)

    assert result.official_evidence_hash is None
    assert all(item.status is EvidenceAdapterStatus.TRANSLATION_REJECTED for item in result.terminals)
    assert all(item.budget_consumed for item in result.terminals)
    assert all("DRAFT_REVIEW_REJECTED" in item.failure_codes for item in result.terminals)
    replay = TrialLedger(tmp_path / "trusted-product-ledger.sqlite3").replay()
    assert sum(item.consumed for item in replay.families.values()) == 2


def test_missing_committed_outcome_is_typed_terminal_and_consumes_budget(tmp_path):
    result = U4OfficialEvidenceAdapter(ledger_path=tmp_path / "trial-ledger.sqlite3").run(_request(outcomes={}))

    assert len(result.terminals) == 2
    assert all(item.status is EvidenceAdapterStatus.TRANSLATION_REJECTED for item in result.terminals)
    assert all(EvidenceAdapterFailureCode.OUTCOME_MISSING.value in item.failure_codes for item in result.terminals)
    assert all(item.budget_consumed for item in result.terminals)
    assert result.official_evidence_hash is None


def test_same_specialist_proposer_cannot_approve(tmp_path):
    adapter = U4OfficialEvidenceAdapter(ledger_path=tmp_path / "trial-ledger.sqlite3")
    with pytest.raises(ContractValidationError) as exc:
        adapter.run(_request(approver_id="research-specialist.market_structure"))
    assert exc.value.code is FailureCode.SELF_APPROVAL


def test_visible_result_contains_no_hidden_partition_or_promotion_authority(tmp_path):
    result = U4OfficialEvidenceAdapter(ledger_path=tmp_path / "trial-ledger.sqlite3").run(_request())
    rendered = result.canonical_json().lower()
    assert "sealed" not in rendered
    assert "promote_challenger" not in rendered
    assert '"promotion_hash":null' in rendered
