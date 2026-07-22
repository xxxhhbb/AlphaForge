from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from veriquant.contracts import ContractValidationError, FailureCode
from veriquant.research_campaign import MethodModel
from veriquant.research_os import (
    AnswerLevel,
    CandidateValidationRequest,
    ResearchGroupAssignment,
    ResearchGroupSubmission,
    ResearchRoutePlan,
)
from veriquant.research_run_controller import (
    FakeResearchStageProvider,
    ResearchRunController,
    build_research_run_plan,
)
from veriquant.run_recovery.api import RecoveryError, RecoveryStatus


H0 = "sha256:" + "0" * 64
H1 = "sha256:" + "1" * 64
H2 = "sha256:" + "2" * 64
H3 = "sha256:" + "3" * 64
H4 = "sha256:" + "4" * 64
CLOCK = datetime(2026, 7, 19, 9, 30, tzinfo=timezone.utc)


def _assignment(group_id: str) -> ResearchGroupAssignment:
    return ResearchGroupAssignment(
        group_id=group_id,
        role="primary",
        score=0.8,
        reasons=("research route requires this group",),
        tool_whitelist=("read_visible_context",),
        candidate_budget=1,
    )


def _route(groups=("market_structure", "meta_alpha")) -> ResearchRoutePlan:
    return ResearchRoutePlan(
        campaign_hash=H1,
        assignments=tuple(_assignment(group) for group in groups),
        method_models=(MethodModel.HYPOTHESIS_DRIVEN,),
        capability_requirements=(),
    )


def _submission(group_id: str, draft_hash: str) -> ResearchGroupSubmission:
    return ResearchGroupSubmission(
        campaign_hash=H1,
        group_id=group_id,
        hypothesis_hash=H2,
        candidate_draft_hashes=(draft_hash,),
        answer_level=AnswerLevel.AUXILIARY_RESEARCH,
    )


def _validation(group_id: str, draft_hash: str) -> CandidateValidationRequest:
    return CandidateValidationRequest(
        campaign_hash=H1,
        group_id=group_id,
        candidate_draft_hash=draft_hash,
        data_plan_hash=H3,
        protocol_hash=H4,
        family_id=f"family_{group_id}",
    )


def _plan(run_id="run_alpha_1", groups=("market_structure", "meta_alpha")):
    route = _route(groups)
    submissions = tuple(_submission(group, "sha256:" + str(index + 5) * 64) for index, group in enumerate(groups))
    validations = tuple(_validation(group, submission.candidate_draft_hashes[0]) for group, submission in zip(groups, submissions))
    return build_research_run_plan(
        run_id=run_id,
        campaign_hash=H1,
        intent_hash=H2,
        data_plan_hash=H3,
        route_plan=route,
        group_submissions=submissions,
        validation_requests=validations,
        policy_hash=H0,
        code_hash=H4,
        created_at=CLOCK,
    )


def _controller(tmp_path: Path, provider: FakeResearchStageProvider):
    return ResearchRunController(root=tmp_path, providers={"research_stage_provider": provider}, lease_ttl_seconds=30)


def test_build_plan_has_stable_stage_identities() -> None:
    first = _plan()
    second = _plan()
    assert first.artifact_hash == second.artifact_hash
    assert [stage.stage_key for stage in first.stages] == [
        "planning",
        "group_market_structure_selected",
        "group_market_structure_translation",
        "candidate_555555555555_validation",
        "group_meta_alpha_selected",
        "group_meta_alpha_translation",
        "candidate_666666666666_validation",
        "candidate_tournament",
        "formal_report",
    ]
    tasks = first.to_stage_tasks()
    assert len({task.identity.artifact_hash for task in tasks}) == len(tasks)


def test_run_commits_events_and_atomic_checkpoint(tmp_path: Path) -> None:
    provider = FakeResearchStageProvider()
    controller = _controller(tmp_path, provider)
    summary = controller.run(_plan(), holder_id="worker_1")
    assert summary.status is RecoveryStatus.COMMITTED
    assert len(summary.committed_stage_hashes) == len(_plan().stages)
    assert summary.event_log_hash is not None
    assert (tmp_path / "recovery" / "runs" / "run_alpha_1" / "events.jsonl").exists()
    assert (tmp_path / "recovery" / "runs" / "run_alpha_1" / "state.json").exists()
    assert (tmp_path / "research_runs" / "run_alpha_1" / "manifest.json").exists()


def test_committed_stages_are_not_rerun_on_resume(tmp_path: Path) -> None:
    provider = FakeResearchStageProvider()
    controller = _controller(tmp_path, provider)
    controller.run(_plan(), holder_id="worker_1")
    first_calls = tuple(provider.calls)
    resumed = controller.run(_plan(), holder_id="worker_2")
    assert tuple(provider.calls) == first_calls
    assert resumed.status is RecoveryStatus.COMMITTED
    assert len(resumed.skipped_stage_hashes) == len(_plan().stages)


def test_interruption_resumes_without_repeating_committed_prefix(tmp_path: Path) -> None:
    provider = FakeResearchStageProvider(interrupt_stage_keys=frozenset({"candidate_555555555555_validation"}))
    controller = _controller(tmp_path, provider)
    interrupted = controller.run(_plan(), holder_id="worker_1")
    assert interrupted.status is RecoveryStatus.INTERRUPTED
    prefix_calls = tuple(provider.calls)
    resumed = controller.run(_plan(), holder_id="worker_2")
    assert resumed.status is RecoveryStatus.COMMITTED
    assert provider.calls.count("planning") == 1
    assert len(provider.calls) > len(prefix_calls)


def test_failed_independent_branch_does_not_kill_healthy_branch(tmp_path: Path) -> None:
    provider = FakeResearchStageProvider(fail_stage_keys=frozenset({"candidate_555555555555_validation"}))
    controller = _controller(tmp_path, provider)
    summary = controller.run(_plan(), holder_id="worker_1")
    stage_by_hash = {stage.to_stage_identity(_plan().manifest.run_id).artifact_hash: stage.stage_key for stage in _plan().stages}
    committed_names = {stage_by_hash[item] for item in summary.committed_stage_hashes}
    failed_names = {stage_by_hash[item] for item in summary.failed_stage_hashes}
    blocked_names = {stage_by_hash[item] for item in summary.blocked_stage_hashes}
    assert summary.status is RecoveryStatus.FAILED
    assert "candidate_666666666666_validation" in committed_names
    assert "candidate_555555555555_validation" in failed_names
    assert "candidate_tournament" in blocked_names


def test_cancelled_run_does_not_execute_provider(tmp_path: Path) -> None:
    provider = FakeResearchStageProvider()
    controller = _controller(tmp_path, provider)
    plan = _plan()
    cancelled = controller.cancel(plan, holder_id="operator")
    assert cancelled.status is RecoveryStatus.CANCELLED
    after = controller.run(plan, holder_id="worker_1")
    assert after.status is RecoveryStatus.CANCELLED
    assert provider.calls == []


def test_same_run_id_conflicting_input_fails_closed(tmp_path: Path) -> None:
    provider = FakeResearchStageProvider()
    controller = _controller(tmp_path, provider)
    controller.plan(_plan(run_id="same_run"))
    with pytest.raises(RecoveryError) as caught:
        controller.plan(_plan(run_id="same_run", groups=("fundamental",)))
    assert caught.value.code is FailureCode.ARTIFACT_COLLISION


def test_unselected_submission_and_path_traversal_are_rejected() -> None:
    route = _route(groups=("market_structure",))
    with pytest.raises(ContractValidationError):
        build_research_run_plan(
            run_id="bad/../run",
            campaign_hash=H1,
            intent_hash=H2,
            data_plan_hash=H3,
            route_plan=route,
            group_submissions=(_submission("meta_alpha", H2),),
            validation_requests=(),
            policy_hash=H0,
            code_hash=H4,
            created_at=CLOCK,
        )
