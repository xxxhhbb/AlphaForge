from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from veriquant.candidate_tournament import TournamentStatus
from veriquant.contracts import ContractValidationError
from veriquant.data import MarketSnapshot, SnapshotRow, canonical_content_hash
from veriquant.evidence.ledger import TrialLedger
from veriquant.evidence.models import CostModel, WalkForwardSpec
from veriquant.research_group_runtime import (
    GroupCandidateDraft,
    GroupProposalPayload,
    GroupRuntimeBranchResult,
    GroupRuntimeResult,
)
from veriquant.research_group_runtime.runtime import GroupBranchStatus
from veriquant.research_os import AnswerLevel, ResearchGroupSubmission, ValidationDisposition
from veriquant.research_validation_pipeline_v2 import (
    CandidateTerminalStatus,
    FrozenValidationData,
    ResearchValidationPipelineRequest,
    ResearchValidationPipelineV2,
    VisibleMetricProjector,
)


H = "sha256:" + "a" * 64


def _snapshot(*, omit_label: bool = False):
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
                values={"close": 10.0 + day * (index + 1) * 0.1, "volume": 1000.0 + 5 * day + index},
            ))
            labels[f"{when.isoformat()}|{symbol}"] = (index - 1.5) * 0.001 + day * 0.00001
    if omit_label:
        labels.pop(f"{(start + timedelta(days=6)).isoformat()}|AAA")
    snapshot = MarketSnapshot(
        dataset_id="fixture.research-v2",
        decision_time=start + timedelta(days=12),
        start_time=start,
        fields=("close", "volume"),
        rows=tuple(rows),
        historical_universe={
            (start + timedelta(days=day)).isoformat(): symbols for day in range(12)
        },
        coverage_by_field={"close": 1.0, "volume": 1.0},
    )
    return snapshot, labels


def _branch(group: str, expressions: tuple[str, ...]) -> GroupRuntimeBranchResult:
    candidates = tuple(
        GroupCandidateDraft(
            candidate_id=f"{group}-c{index}",
            expression=expression,
            variables=("close",),
            lookback_sessions=1,
            rationale="可证伪候选，仅为研究草案。",
        )
        for index, expression in enumerate(expressions, start=1)
    )
    proposal = GroupProposalPayload(
        group_id=group,
        hypothesis="短窗口价格信息可能含有横截面差异。",
        economic_mechanism="行为反应差异，需要机器证据反证。",
        falsification_tests=("成本后为负则拒绝",),
        candidates=candidates,
    )
    submission = ResearchGroupSubmission(
        campaign_hash=H,
        group_id=group,
        hypothesis_hash=proposal.artifact_hash,
        candidate_draft_hashes=tuple(item.artifact_hash for item in candidates),
        answer_level=AnswerLevel.CONCEPTUAL,
    )
    return GroupRuntimeBranchResult(
        group_id=group,
        status=GroupBranchStatus.COMPLETED,
        proposal=proposal,
        submission=submission,
    )


def _request(*, omit_label: bool = False, invalid: bool = True):
    snapshot, labels = _snapshot(omit_label=omit_label)
    expressions = ("rank_cross_section(pct_change(close, 1))",)
    if invalid:
        expressions += ("ts_argmax(close, 1)",)
    runtime = GroupRuntimeResult(branches=(
        _branch("market_structure", expressions),
        _branch("meta_alpha", ("zscore_cross_section(pct_change(close, 1))",)),
    ))
    data = FrozenValidationData(
        snapshot=snapshot,
        labels=labels,
        qualification_hash=canonical_content_hash({"qualified": True}),
        source_label="DETERMINISTIC_OFFLINE_FIXTURE",
    )
    return ResearchValidationPipelineRequest(
        campaign_hash=H,
        runtime=runtime,
        frozen_data=data,
        requested_lookback_sessions=1,
        family_budget_per_candidate=1,
        seed=7,
    )


def _pipeline(tmp_path: Path):
    return ResearchValidationPipelineV2(
        ledger_path=tmp_path / "trials.sqlite3",
        walk_forward=WalkForwardSpec(train_periods=3, validation_periods=2, sealed_periods=1, step_periods=2),
        cost_model=CostModel(spread_bps=2.0, linear_impact_bps=3.0),
    )


def test_every_submitted_candidate_is_terminal_and_budget_accounted(tmp_path: Path):
    request = _request()
    result = _pipeline(tmp_path).run(request)
    assert len(result.terminal_records) == 3
    assert not result.tournament.unvalidated_candidate_hashes
    replay = TrialLedger(tmp_path / "trials.sqlite3").replay()
    assert sum(family.consumed for family in replay.families.values()) == 3
    assert sum(family.terminal_trials for family in replay.families.values()) == 3


def test_unknown_operator_is_typed_terminal_rejection_and_consumes(tmp_path: Path):
    result = _pipeline(tmp_path).run(_request())
    rejected = next(item for item in result.terminal_records if item.translation.status.value != "ACCEPTED")
    assert rejected.status is CandidateTerminalStatus.TRANSLATION_REJECTED
    assert "DRAFT_TRANSLATION_REJECTED" in rejected.failure_codes
    assert rejected.validation.result_ref.budget_consumed is True


def test_visible_pipeline_never_creates_challenger(tmp_path: Path):
    result = _pipeline(tmp_path).run(_request(invalid=False))
    assert all(item.disposition is not ValidationDisposition.CHALLENGER for item in result.terminal_records)


def test_all_reject_or_revise_still_has_best_observed_without_fake_winner(tmp_path: Path):
    result = _pipeline(tmp_path).run(_request(invalid=False))
    assert result.best_observed is not None
    if result.tournament.status is TournamentStatus.COMPLETED_NO_WINNER:
        assert result.tournament.winner_candidate_hash is None
        assert result.best_observed.authority == "VISIBLE_RESEARCH_CLUE_ONLY"
        assert result.best_observed.eligible_for_selection is False


def test_missing_pit_label_is_terminal_and_consumes_for_every_candidate(tmp_path: Path):
    result = _pipeline(tmp_path).run(_request(omit_label=True, invalid=False))
    assert all(item.status is CandidateTerminalStatus.PIT_REJECTED for item in result.terminal_records)
    replay = TrialLedger(tmp_path / "trials.sqlite3").replay()
    assert sum(family.consumed for family in replay.families.values()) == 2


def test_replay_does_not_double_consume_and_result_hash_is_stable(tmp_path: Path):
    pipeline = _pipeline(tmp_path)
    request = _request(invalid=False)
    first = pipeline.run(request)
    second = pipeline.run(request)
    assert first.reproducibility_hash == second.reproducibility_hash
    replay = TrialLedger(tmp_path / "trials.sqlite3").replay()
    assert sum(family.consumed for family in replay.families.values()) == 2


def test_rankic_is_not_a_projector_input():
    assert all("rank_ic" not in name for name in VisibleMetricProjector.REQUIRED)


def test_official_tushare_masquerade_is_rejected():
    snapshot, labels = _snapshot()
    with pytest.raises(ContractValidationError):
        FrozenValidationData(
            snapshot=snapshot,
            labels=labels,
            qualification_hash=H,
            source_label="OFFICIAL_TUSHARE",
        )


def test_failed_group_does_not_erase_independent_completed_candidates(tmp_path: Path):
    request = _request(invalid=False)
    failed = GroupRuntimeBranchResult(
        group_id="macro_regime",
        status=GroupBranchStatus.FAILED,
        proposal=None,
        submission=None,
        failure_code="PROVIDER_TIMEOUT",
    )
    runtime = GroupRuntimeResult(branches=request.runtime.branches + (failed,))
    amended = ResearchValidationPipelineRequest(
        campaign_hash=request.campaign_hash,
        runtime=runtime,
        frozen_data=request.frozen_data,
        requested_lookback_sessions=1,
    )
    result = _pipeline(tmp_path).run(amended)
    assert len(result.terminal_records) == 2
    assert {item.group_id for item in result.terminal_records} == {"market_structure", "meta_alpha"}
