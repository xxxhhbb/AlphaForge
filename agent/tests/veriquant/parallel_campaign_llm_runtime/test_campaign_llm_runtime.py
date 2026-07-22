from __future__ import annotations

import time
from datetime import datetime, timezone

import pytest

from src.veriquant.campaign_llm_runtime import (
    CampaignDeskRunPlan,
    CampaignLLMRuntime,
    CampaignRuntimeCheckpoint,
    CampaignRuntimeFailureCode,
    CampaignRuntimeManifest,
)
from src.veriquant.contracts import Component, ContractValidationError, FailureCode
from src.veriquant.research_campaign import (
    BranchRuntimeStatus,
    CampaignBudget,
    CampaignCommittedStage,
    CampaignState,
    DataAvailability,
    DeterministicCampaignRouter,
    DeterministicCampaignStageExecutor,
    DeterministicMethodProtocolCompiler,
    InteractionStyle,
    MethodModel,
    RegisteredResearchContext,
    ResearchCampaign,
    ResearchCampaignDraft,
    ResearchDepth,
    ResearchMode,
    ResearchSubject,
    StageRuntimeStatus,
    StopCondition,
    UserCheckpoint,
    CampaignStageExecutionContext,
    CampaignStageResult,
    stage_key_for,
)


HASH_A = "sha256:" + "a" * 64
HASH_B = "sha256:" + "b" * 64
HASH_C = "sha256:" + "c" * 64


def _dt(hour: int = 9) -> datetime:
    return datetime(2026, 7, 19, hour, 30, tzinfo=timezone.utc)


def _draft(*, state: CampaignState = CampaignState.NEEDS_CONFIRMATION) -> ResearchCampaignDraft:
    subject = ResearchSubject(
        subject_id="subject.campaign_llm_runtime",
        description="Campaign runtime wrapper test subject.",
        universe=("CN_ASHARE_APPROVED_PIT_SAMPLE",),
        horizon="20d",
        decision_time=_dt(),
        allowed_data=("price_volume", "fundamentals"),
    )
    return ResearchCampaignDraft(
        campaign_id="campaign.campaign_llm_runtime",
        question="Find robust A-share value or liquidity effects.",
        mode=ResearchMode.NEW_ALPHA_EXPLORATION,
        research_depth=ResearchDepth.STANDARD_RESEARCH,
        interaction_style=InteractionStyle.STANDARD,
        subject=subject,
        method_models=(MethodModel.HYPOTHESIS_DRIVEN,),
        candidate_agents=(
            "fundamental",
            "expectations_events",
            "market_structure",
            "alternative_data",
            "macro_regime",
            "meta_alpha",
        ),
        budget=CampaignBudget(max_factor_families=3, max_trials=9, max_llm_calls=9, max_runtime_seconds=300),
        user_checkpoints=(
            UserCheckpoint(checkpoint_id="checkpoint.after_research_brief", stage="after_research_brief"),
            UserCheckpoint(checkpoint_id="checkpoint.after_candidate_shortlist", stage="after_candidate_shortlist"),
        ),
        stop_conditions=(StopCondition.BUDGET_EXHAUSTED, StopCondition.VALIDATION_COMPLETED),
        data_manifest_hash=HASH_A,
        policy_hash=HASH_B,
        code_hash=HASH_C,
        created_at=_dt(),
        proposed_by=Component.LLM_RESEARCHER,
        state=state,
    )


def _campaign() -> ResearchCampaign:
    return ResearchCampaign.confirm(_draft(), confirmed_at=_dt(10))


def _route_and_protocol(campaign: ResearchCampaign):
    routed = DeterministicCampaignRouter().route(
        campaign,
        data_availability=DataAvailability(
            available_data=campaign.subject.allowed_data,
            universe_ids=campaign.subject.universe,
            data_manifest_hash=HASH_A,
            calendar_ids=("cn_ashare_sse_szse",),
        ),
        registered_context=RegisteredResearchContext(),
    )
    assert routed.state is CampaignState.ROUTED
    protocol = DeterministicMethodProtocolCompiler().compile(campaign=campaign, route=routed.route)
    return routed.route, protocol


def _plan(campaign: ResearchCampaign | ResearchCampaignDraft | None = None) -> CampaignDeskRunPlan:
    confirmed = _campaign()
    route, protocol = _route_and_protocol(confirmed)
    return CampaignDeskRunPlan(
        run_id="runtime.campaign_llm_wrapper",
        campaign=campaign or confirmed,
        route=route,
        protocol_plan=protocol,
        desk_context_hashes={route.selected_desks[0]: HASH_A},
        desk_tool_whitelists={route.selected_desks[0]: ("read_campaign_brief",)},
        desk_budgets={route.selected_desks[0]: {"max_llm_calls": 3}},
    )


def test_unconfirmed_campaign_is_rejected_before_runtime_execution() -> None:
    plan = _plan(campaign=_draft())
    with pytest.raises(ContractValidationError) as exc:
        CampaignLLMRuntime().run(plan)
    assert exc.value.code is FailureCode.INVALID_STATE
    assert exc.value.details["campaign_runtime_failure_code"] == CampaignRuntimeFailureCode.CAMPAIGN_NOT_CONFIRMED.value


def test_router_unselected_desks_are_not_called_and_get_no_artifact_namespace() -> None:
    plan = _plan()
    executor = DeterministicCampaignStageExecutor()
    manifest = CampaignLLMRuntime(executor=executor).run(plan)

    assert isinstance(manifest, CampaignRuntimeManifest)
    assert set(manifest.selected_desks) == set(plan.route.selected_desks)
    assert set(manifest.not_selected_desks).isdisjoint(manifest.selected_desks)
    called_desks = {call.split(":")[0].split(".")[1] for call in executor.calls}
    assert called_desks == set(plan.route.selected_desks)
    for desk_id in manifest.not_selected_desks:
        assert all(desk_id not in branch.artifact_namespace for branch in manifest.branch_results)
        assert all(desk_id not in key for key in manifest.artifact_hashes)


def test_multi_desk_namespaces_do_not_overwrite() -> None:
    manifest = CampaignLLMRuntime().run(_plan())
    namespaces = tuple(branch.artifact_namespace for branch in manifest.branch_results)
    assert len(set(namespaces)) == len(namespaces)
    assert set(branch.desk_id for branch in manifest.branch_results) == set(manifest.selected_desks)
    assert manifest.status.value == "COMPLETED"


def test_one_desk_timeout_does_not_drag_down_independent_branch() -> None:
    plan = _plan()
    failing_key = stage_key_for(plan.route.tasks[0].task_id, plan.protocol_plan.task_protocols[0].stage_ids[1])
    executor = DeterministicCampaignStageExecutor(failures={failing_key: FailureCode.PROVIDER_TIMEOUT})
    manifest = CampaignLLMRuntime(executor=executor).run(plan)

    by_task = {branch.route_task_id: branch for branch in manifest.branch_results}
    assert by_task[plan.route.tasks[0].task_id].status is BranchRuntimeStatus.BLOCKED
    assert by_task[plan.route.tasks[0].task_id].failure_code is FailureCode.PROVIDER_TIMEOUT
    assert any(branch.status is BranchRuntimeStatus.COMPLETED for branch in manifest.branch_results)
    assert manifest.status.value == "PARTIAL"


def test_context_and_tool_boundaries_fail_closed() -> None:
    confirmed = _campaign()
    route, protocol = _route_and_protocol(confirmed)
    unselected = next(desk for desk in ("alternative_data", "macro_regime", "expectations_events") if desk not in route.selected_desks)

    with pytest.raises(ContractValidationError):
        CampaignDeskRunPlan(
            run_id="runtime.unselected_context",
            campaign=confirmed,
            route=route,
            protocol_plan=protocol,
            desk_context_hashes={unselected: HASH_A},
        )

    with pytest.raises(ContractValidationError):
        CampaignDeskRunPlan(
            run_id="runtime.hidden_payload",
            campaign=confirmed,
            route=route,
            protocol_plan=protocol,
            desk_context_payloads={route.selected_desks[0]: {"sealed_note": "must never enter a prompt"}},
        )


def test_resume_does_not_repeat_committed_stage() -> None:
    plan = _plan()
    committed = CampaignStageResult(
        route_task_id=plan.route.tasks[0].task_id,
        desk_id=plan.route.tasks[0].desk_id,
        stage_id=plan.protocol_plan.task_protocols[0].stage_ids[0],
        stage_index=0,
        status=StageRuntimeStatus.DRAFT,
        produced_by=Component.RESEARCH_DESK,
        output_hash="sha256:" + "d" * 64,
        prompt_hash="sha256:" + "e" * 64,
        trace_hash="sha256:" + "f" * 64,
        artifact_namespace=f"{plan.route.tasks[0].artifact_namespace}.stage.1",
    )
    checkpoint = CampaignRuntimeCheckpoint(
        committed_stages=(
            CampaignCommittedStage(
                stage_key=stage_key_for(committed.route_task_id, committed.stage_id),
                result=committed,
            ),
        )
    )
    executor = DeterministicCampaignStageExecutor()
    manifest = CampaignLLMRuntime(executor=executor).run(plan, checkpoint=checkpoint)

    assert stage_key_for(committed.route_task_id, committed.stage_id) not in executor.calls
    by_task = {branch.route_task_id: branch for branch in manifest.branch_results}
    assert by_task[committed.route_task_id].stage_results[0].artifact_hash == committed.artifact_hash


class _DelayedExecutor(DeterministicCampaignStageExecutor):
    def __init__(self, *, delayed_desk: str) -> None:
        super().__init__()
        self.delayed_desk = delayed_desk

    def run_stage(self, context: CampaignStageExecutionContext) -> CampaignStageResult:
        if context.stage_index == 0 and context.route_task.desk_id == self.delayed_desk:
            time.sleep(0.02)
        return super().run_stage(context)


def test_manifest_hash_is_independent_of_parallel_completion_order() -> None:
    plan = _plan()
    left = CampaignLLMRuntime(executor=_DelayedExecutor(delayed_desk=plan.route.selected_desks[0])).run(plan)
    right = CampaignLLMRuntime(executor=_DelayedExecutor(delayed_desk=plan.route.selected_desks[-1])).run(plan)
    assert left.artifact_hash == right.artifact_hash


class _VerdictInjectingExecutor(DeterministicCampaignStageExecutor):
    def run_stage(self, context: CampaignStageExecutionContext) -> CampaignStageResult:
        result = super().run_stage(context)
        if context.stage_index == 0:
            return CampaignStageResult(
                route_task_id=result.route_task_id,
                desk_id=result.desk_id,
                stage_id=result.stage_id,
                stage_index=result.stage_index,
                status=result.status,
                produced_by=result.produced_by,
                output_hash=result.output_hash,
                prompt_hash=result.prompt_hash,
                trace_hash=result.trace_hash,
                artifact_namespace=result.artifact_namespace,
                message="PROMOTE_CHALLENGER",
            )
        return result


def test_adapter_cannot_write_downstream_verdict_into_visible_manifest() -> None:
    with pytest.raises(ContractValidationError) as exc:
        CampaignLLMRuntime(executor=_VerdictInjectingExecutor()).run(_plan())
    assert exc.value.code is FailureCode.UNAUTHORIZED_CAPABILITY


def test_unavailable_provider_is_not_labeled_real_llm() -> None:
    with pytest.raises(ContractValidationError) as exc:
        CampaignLLMRuntime(runtime_mode="REAL_LLM")
    assert exc.value.code is FailureCode.ADAPTER_UNAVAILABLE
    assert exc.value.details["campaign_runtime_failure_code"] == CampaignRuntimeFailureCode.PROVIDER_UNAVAILABLE.value


def test_unknown_runtime_mode_fails_closed() -> None:
    with pytest.raises(ContractValidationError) as exc:
        CampaignLLMRuntime(runtime_mode="REALISH_LLM")
    assert exc.value.code is FailureCode.INVALID_VALUE
