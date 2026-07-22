from __future__ import annotations

from datetime import datetime, timezone

from src.veriquant.contracts import FailureCode
from src.veriquant.research_campaign import (
    BranchRuntimeStatus,
    CampaignBudget,
    CampaignCommittedStage,
    CampaignRoute,
    CampaignRouteCheckpoint,
    CampaignRoutedLLMRuntime,
    CampaignRuntimeRecoveryState,
    CampaignRuntimeStatus,
    CampaignStageResult,
    CampaignState,
    CampaignTaskPlan,
    DataAvailability,
    DeterministicCampaignRouter,
    DeterministicCampaignStageExecutor,
    DeterministicMethodProtocolCompiler,
    InMemoryCampaignArtifactRepository,
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
    stage_key_for,
)
from src.veriquant.contracts import Component


HASH_A = "sha256:" + "a" * 64
HASH_B = "sha256:" + "b" * 64
HASH_C = "sha256:" + "c" * 64


def _dt(hour: int = 9) -> datetime:
    return datetime(2026, 7, 19, hour, 30, tzinfo=timezone.utc)


def _campaign(
    *,
    mode: ResearchMode = ResearchMode.NEW_ALPHA_EXPLORATION,
    allowed_data: tuple[str, ...] = ("price_volume", "fundamentals"),
    existing_factor_ids: tuple[str, ...] = (),
    method_models: tuple[MethodModel, ...] = (MethodModel.HYPOTHESIS_DRIVEN,),
) -> ResearchCampaign:
    subject = ResearchSubject(
        subject_id=f"subject.{mode.value.lower()}",
        description=f"Runtime subject for {mode.value}.",
        universe=("CN_ASHARE_APPROVED_PIT_SAMPLE",),
        horizon="20d",
        decision_time=_dt(),
        allowed_data=allowed_data,
        existing_factor_ids=existing_factor_ids,
    )
    draft = ResearchCampaignDraft(
        campaign_id=f"campaign.{mode.value.lower()}",
        question=f"Runtime test for {mode.value}",
        mode=mode,
        research_depth=ResearchDepth.STANDARD_RESEARCH,
        interaction_style=InteractionStyle.STANDARD,
        subject=subject,
        method_models=method_models,
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
        state=CampaignState.NEEDS_CONFIRMATION,
    )
    return ResearchCampaign.confirm(draft, confirmed_at=_dt(10))


def _data(campaign: ResearchCampaign) -> DataAvailability:
    return DataAvailability(
        available_data=campaign.subject.allowed_data,
        universe_ids=campaign.subject.universe,
        data_manifest_hash=HASH_A,
        calendar_ids=("cn_ashare_sse_szse",),
    )


def _route_and_plan(campaign: ResearchCampaign, *, context: RegisteredResearchContext | None = None):
    route_result = DeterministicCampaignRouter().route(
        campaign,
        data_availability=_data(campaign),
        registered_context=context or RegisteredResearchContext(
            factor_ids=campaign.subject.existing_factor_ids,
        ),
    )
    assert route_result.state is CampaignState.ROUTED
    plan = DeterministicMethodProtocolCompiler().compile(campaign=campaign, route=route_result.route)
    return route_result.route, plan


def test_runtime_calls_only_router_selected_desks_and_sorts_stably() -> None:
    campaign = _campaign()
    route, plan = _route_and_plan(campaign)
    executor = DeterministicCampaignStageExecutor()
    manifest = CampaignRoutedLLMRuntime(executor=executor).run(
        run_id="runtime.selected_desks",
        campaign=campaign,
        route=route,
        protocol_plan=plan,
    )

    selected = set(route.selected_desks)
    called_desks = {call.split(":")[0].split(".")[1] for call in executor.calls}
    assert called_desks == selected
    assert len(selected) <= 3
    assert tuple(branch.route_task_id for branch in manifest.branch_results) == tuple(
        sorted(branch.route_task_id for branch in manifest.branch_results)
    )
    assert manifest.status is CampaignRuntimeStatus.COMPLETED


def test_multi_desk_branches_do_not_overwrite_artifacts() -> None:
    campaign = _campaign()
    route, plan = _route_and_plan(campaign)
    repository = InMemoryCampaignArtifactRepository()

    manifest = CampaignRoutedLLMRuntime(repository=repository).run(
        run_id="runtime.artifacts",
        campaign=campaign,
        route=route,
        protocol_plan=plan,
    )

    assert manifest.status is CampaignRuntimeStatus.COMPLETED
    assert len(repository.keys) == sum(len(branch.stage_results) for branch in manifest.branch_results)
    assert len(set(repository.keys)) == len(repository.keys)
    for desk_id in route.selected_desks:
        assert any(f"campaign.route.{desk_id}" in key for key in repository.keys)


def test_one_desk_timeout_does_not_forge_result_or_stop_independent_branches() -> None:
    campaign = _campaign()
    route, plan = _route_and_plan(campaign)
    timeout_key = stage_key_for(route.tasks[0].task_id, plan.task_protocols[0].stage_ids[1])
    executor = DeterministicCampaignStageExecutor(failures={timeout_key: FailureCode.PROVIDER_TIMEOUT})

    manifest = CampaignRoutedLLMRuntime(executor=executor).run(
        run_id="runtime.timeout",
        campaign=campaign,
        route=route,
        protocol_plan=plan,
    )

    by_task = {branch.route_task_id: branch for branch in manifest.branch_results}
    assert by_task[route.tasks[0].task_id].status is BranchRuntimeStatus.BLOCKED
    assert by_task[route.tasks[0].task_id].failure_code is FailureCode.PROVIDER_TIMEOUT
    assert by_task[route.tasks[0].task_id].stage_results[-1].output_hash is None
    assert sum(branch.status is BranchRuntimeStatus.COMPLETED for branch in manifest.branch_results) >= 1
    assert manifest.status is CampaignRuntimeStatus.PARTIAL


def test_untyped_provider_crash_is_isolated_to_one_branch() -> None:
    campaign = _campaign()
    route, plan = _route_and_plan(campaign)
    crashing_task = route.tasks[0].task_id

    class CrashOneBranchExecutor:
        def __init__(self) -> None:
            self.fallback = DeterministicCampaignStageExecutor()

        def run_stage(self, context):
            if context.route_task.task_id == crashing_task:
                raise RuntimeError("synthetic provider transport crash")
            return self.fallback.run_stage(context)

    manifest = CampaignRoutedLLMRuntime(executor=CrashOneBranchExecutor()).run(
        run_id="runtime.untyped_crash",
        campaign=campaign,
        route=route,
        protocol_plan=plan,
    )

    by_task = {branch.route_task_id: branch for branch in manifest.branch_results}
    assert by_task[crashing_task].status is BranchRuntimeStatus.BLOCKED
    assert by_task[crashing_task].failure_code is FailureCode.ADAPTER_EXECUTION_FAILED
    assert by_task[crashing_task].message == "branch executor failed closed: RuntimeError"
    assert sum(branch.status is BranchRuntimeStatus.COMPLETED for branch in manifest.branch_results) >= 1
    assert manifest.status is CampaignRuntimeStatus.PARTIAL


def test_upstream_failure_blocks_dependent_task_but_independent_branch_continues() -> None:
    campaign = _campaign()
    base_route, _ = _route_and_plan(campaign)
    tasks = (
        CampaignTaskPlan(
            task_id="task.alpha",
            desk_id=base_route.selected_desks[0],
            method_model=MethodModel.HYPOTHESIS_DRIVEN,
            artifact_namespace=f"campaign.route.{base_route.selected_desks[0]}",
        ),
        CampaignTaskPlan(
            task_id="task.beta",
            desk_id=base_route.selected_desks[1],
            method_model=MethodModel.HYPOTHESIS_DRIVEN,
            input_from=("task.alpha",),
            artifact_namespace=f"campaign.route.{base_route.selected_desks[1]}",
        ),
        CampaignTaskPlan(
            task_id="task.gamma",
            desk_id=base_route.selected_desks[2],
            method_model=MethodModel.HYPOTHESIS_DRIVEN,
            artifact_namespace=f"campaign.route.{base_route.selected_desks[2]}",
        ),
    )
    route = CampaignRoute(
        route_id="route.dependency_test",
        campaign_hash=campaign.artifact_hash,
        mode=campaign.mode,
        research_depth=campaign.research_depth,
        selected_desks=base_route.selected_desks,
        method_models=base_route.method_models,
        tasks=tasks,
        routed_at=_dt(10),
        route_reason="dependency test route",
        checkpoint_plan=(
            CampaignRouteCheckpoint(
                checkpoint_id="checkpoint.after_alpha_gamma",
                stage="after_candidate_shortlist",
                after_task_ids=("task.alpha", "task.gamma"),
            ),
        ),
    )
    plan = DeterministicMethodProtocolCompiler().compile(campaign=campaign, route=route)
    failing_key = stage_key_for("task.alpha", plan.task_protocols[0].stage_ids[0])
    manifest = CampaignRoutedLLMRuntime(
        executor=DeterministicCampaignStageExecutor(failures={failing_key: FailureCode.PROVIDER_TIMEOUT})
    ).run(run_id="runtime.deps", campaign=campaign, route=route, protocol_plan=plan)

    by_task = {branch.route_task_id: branch for branch in manifest.branch_results}
    assert by_task["task.alpha"].status is BranchRuntimeStatus.BLOCKED
    assert by_task["task.beta"].status is BranchRuntimeStatus.SKIPPED_UPSTREAM
    assert by_task["task.gamma"].status is BranchRuntimeStatus.COMPLETED
    assert manifest.status is CampaignRuntimeStatus.PARTIAL


def test_sealed_and_privileged_information_do_not_enter_manifest_trace_or_feedback() -> None:
    campaign = _campaign()
    route, plan = _route_and_plan(campaign)
    manifest = CampaignRoutedLLMRuntime().run(
        run_id="runtime.redaction",
        campaign=campaign,
        route=route,
        protocol_plan=plan,
    )

    rendered = manifest.canonical_json().lower()
    for forbidden in ("sealed", "holdout", "promotion", "target_weights", "risk_approval", "broker", "api_key"):
        assert forbidden not in rendered


def test_llm_outputs_are_only_draft_or_explanation_not_official_verdict() -> None:
    campaign = _campaign()
    route, plan = _route_and_plan(campaign)
    manifest = CampaignRoutedLLMRuntime().run(
        run_id="runtime.authority",
        campaign=campaign,
        route=route,
        protocol_plan=plan,
    )

    for branch in manifest.branch_results:
        for stage in branch.stage_results:
            if stage.produced_by in {Component.LLM_RESEARCHER, Component.RESEARCH_DESK}:
                assert stage.status in {StageRuntimeStatus.DRAFT, StageRuntimeStatus.EXPLANATION}
            assert "PROMOTE" not in stage.status.value
            assert "OFFICIAL" not in stage.status.value


def test_resume_skips_committed_stage_without_duplicate_executor_call() -> None:
    campaign = _campaign()
    route, plan = _route_and_plan(campaign)
    committed_result = CampaignStageResult(
        route_task_id=route.tasks[0].task_id,
        desk_id=route.tasks[0].desk_id,
        stage_id=plan.task_protocols[0].stage_ids[0],
        stage_index=0,
        status=StageRuntimeStatus.DRAFT,
        produced_by=Component.RESEARCH_DESK,
        output_hash="sha256:" + "d" * 64,
        prompt_hash="sha256:" + "e" * 64,
        trace_hash="sha256:" + "f" * 64,
        artifact_namespace=f"{route.tasks[0].artifact_namespace}.stage.1",
    )
    recovery = CampaignRuntimeRecoveryState(
        committed_stages=(
            CampaignCommittedStage(
                stage_key=stage_key_for(committed_result.route_task_id, committed_result.stage_id),
                result=committed_result,
            ),
        )
    )
    executor = DeterministicCampaignStageExecutor()

    manifest = CampaignRoutedLLMRuntime(executor=executor).run(
        run_id="runtime.resume",
        campaign=campaign,
        route=route,
        protocol_plan=plan,
        recovery=recovery,
    )

    skipped_key = stage_key_for(committed_result.route_task_id, committed_result.stage_id)
    assert skipped_key not in executor.calls
    first_branch = {branch.route_task_id: branch for branch in manifest.branch_results}[route.tasks[0].task_id]
    assert first_branch.stage_results[0].artifact_hash == committed_result.artifact_hash
    assert manifest.status is CampaignRuntimeStatus.COMPLETED
