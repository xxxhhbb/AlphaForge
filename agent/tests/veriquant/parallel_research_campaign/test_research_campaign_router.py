from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.veriquant.contracts import Component, ContractValidationError, FailureCode
from src.veriquant.research_campaign import (
    CampaignBudget,
    CampaignRoute,
    CampaignState,
    CampaignTaskPlan,
    DataAvailability,
    DeskCapabilityManifest,
    DeterministicCampaignRouter,
    InteractionStyle,
    MethodModel,
    RegisteredResearchContext,
    ResearchCampaign,
    ResearchCampaignDraft,
    ResearchDepth,
    ResearchMode,
    ResearchSubject,
    StopCondition,
    UserCheckpoint,
    default_desk_capabilities,
    validate_campaign_route_dag,
)


HASH_A = "sha256:" + "a" * 64
HASH_B = "sha256:" + "b" * 64
HASH_C = "sha256:" + "c" * 64


def _dt(hour: int = 9) -> datetime:
    return datetime(2026, 7, 19, hour, 30, tzinfo=timezone.utc)


def _campaign(
    *,
    mode: ResearchMode = ResearchMode.NEW_ALPHA_EXPLORATION,
    subject_allowed_data: tuple[str, ...] = ("price_volume", "fundamentals"),
    subject_existing_factor_ids: tuple[str, ...] = (),
    portfolio_id: str | None = None,
    tags: tuple[str, ...] = (),
    method_models: tuple[MethodModel, ...] = (MethodModel.HYPOTHESIS_DRIVEN,),
    budget: CampaignBudget | None = None,
    depth: ResearchDepth = ResearchDepth.STANDARD_RESEARCH,
) -> ResearchCampaign:
    subject = ResearchSubject(
        subject_id="subject.cn_ashare_sample",
        description="Approved A-share PIT research subject.",
        universe=("CN_ASHARE_APPROVED_PIT_SAMPLE",),
        horizon="20d",
        decision_time=_dt(),
        allowed_data=subject_allowed_data,
        existing_factor_ids=subject_existing_factor_ids,
        portfolio_id=portfolio_id,
        tags=tags,
    )
    draft = ResearchCampaignDraft(
        campaign_id=f"campaign.{mode.value.lower()}",
        question=f"Route test for {mode.value}",
        mode=mode,
        research_depth=depth,
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
        budget=budget
        or CampaignBudget(max_factor_families=3, max_trials=9, max_llm_calls=6, max_runtime_seconds=300),
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


def _data(*available_data: str) -> DataAvailability:
    return DataAvailability(
        available_data=available_data or ("price_volume", "fundamentals"),
        universe_ids=("CN_ASHARE_APPROVED_PIT_SAMPLE",),
        data_manifest_hash=HASH_A,
        calendar_ids=("cn_ashare_sse_szse",),
    )


def test_router_outputs_budgeted_dag_without_defaulting_to_all_six_desks() -> None:
    campaign = _campaign(mode=ResearchMode.NEW_ALPHA_EXPLORATION)

    result = DeterministicCampaignRouter().route(campaign, data_availability=_data("price_volume", "fundamentals"))

    assert result.state is CampaignState.ROUTED
    assert result.route is not None
    assert 1 <= len(result.route.selected_desks) <= 3
    assert len(result.route.selected_desks) < 6
    assert len(result.route.tasks) == len(result.route.selected_desks)
    assert result.context_allowed_desks == result.route.selected_desks
    assert sum(item.max_factor_families for item in result.route.desk_budgets) <= campaign.budget.max_factor_families
    assert sum(item.max_trials for item in result.route.desk_budgets) <= campaign.budget.max_trials
    assert sum(item.max_llm_calls for item in result.route.desk_budgets) <= campaign.budget.max_llm_calls
    assert all(not task.requires_official_evidence and not task.allows_promotion for task in result.route.tasks)
    validate_campaign_route_dag(result.route)


def test_price_momentum_routes_only_relevant_specialists() -> None:
    campaign = _campaign(
        mode=ResearchMode.NEW_ALPHA_EXPLORATION,
        subject_allowed_data=("price_volume",),
        tags=("momentum",),
        method_models=(
            MethodModel.HYPOTHESIS_DRIVEN,
            MethodModel.DATA_ANOMALY,
            MethodModel.ADVERSARIAL_FALSIFICATION,
        ),
    )

    result = DeterministicCampaignRouter().route(
        campaign,
        data_availability=_data("price_volume"),
    )

    assert result.state is CampaignState.ROUTED
    assert result.route.selected_desks == ("market_structure", "meta_alpha")
    assert "fundamental" not in result.route.selected_desks


def test_required_modes_route_to_expected_methods_and_default_desks() -> None:
    cases = (
        (
            ResearchMode.VALIDATE_IDEA,
            ("price_volume", "fundamentals"),
            RegisteredResearchContext(),
            {MethodModel.HYPOTHESIS_DRIVEN, MethodModel.ADVERSARIAL_FALSIFICATION},
            {"fundamental", "market_structure"},
            {},
        ),
        (
            ResearchMode.IMPROVE_EXISTING_FACTOR,
            ("price_volume", "registered_factor_summary"),
            RegisteredResearchContext(factor_ids=("reversal_v1",), factor_domains={"reversal_v1": "market_structure"}),
            {MethodModel.BOUNDED_EVOLUTION, MethodModel.DIAGNOSTIC_ATTRIBUTION},
            {"market_structure", "meta_alpha"},
            {
                "subject_allowed_data": ("price_volume", "registered_factor_summary"),
                "subject_existing_factor_ids": ("reversal_v1",),
                "method_models": (MethodModel.BOUNDED_EVOLUTION,),
            },
        ),
        (
            ResearchMode.PORTFOLIO_GAP_SEARCH,
            ("price_volume", "registered_factor_summary"),
            RegisteredResearchContext(factor_ids=("momentum_v1", "value_v1"), portfolio_ids=("portfolio.mn_v1",)),
            {MethodModel.RESIDUAL_INCREMENTAL, MethodModel.HYPOTHESIS_DRIVEN},
            {"meta_alpha", "market_structure"},
            {
                "subject_allowed_data": ("price_volume", "registered_factor_summary"),
                "subject_existing_factor_ids": ("momentum_v1", "value_v1"),
                "portfolio_id": "portfolio.mn_v1",
                "method_models": (MethodModel.RESIDUAL_INCREMENTAL,),
            },
        ),
        (
            ResearchMode.DIAGNOSE_FACTOR_PORTFOLIO,
            ("price_volume", "registered_factor_summary", "macro_regime"),
            RegisteredResearchContext(factor_ids=("momentum_v1",)),
            {MethodModel.DIAGNOSTIC_ATTRIBUTION, MethodModel.REGIME_CONDITIONING},
            {"meta_alpha", "market_structure", "macro_regime"},
            {
                "subject_allowed_data": ("price_volume", "registered_factor_summary", "macro_regime"),
                "subject_existing_factor_ids": ("momentum_v1",),
            },
        ),
        (
            ResearchMode.EVENT_MARKET_TOPIC,
            ("price_volume", "events", "macro_regime"),
            RegisteredResearchContext(),
            {MethodModel.EVENT_STUDY, MethodModel.REGIME_CONDITIONING},
            {"expectations_events", "macro_regime", "market_structure"},
            {"subject_allowed_data": ("price_volume", "events", "macro_regime"), "tags": ("earnings",)},
        ),
    )
    router = DeterministicCampaignRouter()
    for mode, available_data, context, expected_methods, expected_desks, overrides in cases:
        campaign = _campaign(mode=mode, **overrides)
        result = router.route(campaign, data_availability=_data(*available_data), registered_context=context)
        assert result.state is CampaignState.ROUTED
        assert expected_methods <= set(result.route.method_models)
        assert set(result.route.selected_desks) <= expected_desks
        assert set(result.route.selected_desks)


def test_excluded_desks_receive_no_tasks_budgets_or_context() -> None:
    campaign = _campaign(
        mode=ResearchMode.DIAGNOSE_FACTOR_PORTFOLIO,
        subject_allowed_data=("price_volume", "registered_factor_summary", "macro_regime"),
        subject_existing_factor_ids=("momentum_v1",),
    )

    result = DeterministicCampaignRouter().route(
        campaign,
        data_availability=_data("price_volume", "registered_factor_summary", "macro_regime"),
        registered_context=RegisteredResearchContext(factor_ids=("momentum_v1",)),
    )

    assert result.state is CampaignState.ROUTED
    excluded = {"fundamental", "expectations_events", "alternative_data"} - set(result.route.selected_desks)
    assert excluded
    assert not ({task.desk_id for task in result.route.tasks} & excluded)
    assert not ({budget.desk_id for budget in result.route.desk_budgets} & excluded)
    assert not (set(result.context_allowed_desks) & excluded)


def test_data_unavailable_blocks_without_silent_substitution() -> None:
    campaign = _campaign(
        mode=ResearchMode.EVENT_MARKET_TOPIC,
        subject_allowed_data=("price_volume", "events", "macro_regime"),
        tags=("earnings",),
    )

    result = DeterministicCampaignRouter().route(campaign, data_availability=_data("price_volume", "macro_regime"))

    assert result.state is CampaignState.BLOCKED
    assert result.route is None
    assert result.failure_code is FailureCode.UPSTREAM_BLOCKED
    assert any(reason.startswith("missing_data:") and "events" in reason for reason in result.blocked_reasons)
    assert any("do not silently substitute" in suggestion for suggestion in result.downgrade_suggestions)


def test_registered_scope_is_required_for_improvement_and_gap_search() -> None:
    improve_campaign = _campaign(
        mode=ResearchMode.IMPROVE_EXISTING_FACTOR,
        subject_allowed_data=("price_volume", "registered_factor_summary"),
        subject_existing_factor_ids=("reversal_v1",),
        method_models=(MethodModel.BOUNDED_EVOLUTION,),
    )
    improve_result = DeterministicCampaignRouter().route(
        improve_campaign,
        data_availability=_data("price_volume", "registered_factor_summary"),
        registered_context=RegisteredResearchContext(),
    )
    assert improve_result.state is CampaignState.BLOCKED
    assert any("unregistered_parent_factor" in reason for reason in improve_result.blocked_reasons)

    gap_campaign = _campaign(
        mode=ResearchMode.PORTFOLIO_GAP_SEARCH,
        subject_allowed_data=("price_volume", "registered_factor_summary"),
        subject_existing_factor_ids=("momentum_unregistered_v1",),
        method_models=(MethodModel.RESIDUAL_INCREMENTAL,),
    )
    gap_result = DeterministicCampaignRouter().route(
        gap_campaign,
        data_availability=_data("price_volume", "registered_factor_summary"),
        registered_context=RegisteredResearchContext(),
    )
    assert gap_result.state is CampaignState.BLOCKED
    assert "missing_registered_portfolio_or_factor_scope" in gap_result.blocked_reasons


def test_privileged_or_unavailable_desk_capabilities_fail_closed() -> None:
    with pytest.raises(ContractValidationError) as privileged:
        DeskCapabilityManifest(
            desk_id="market_structure",
            method_models=(MethodModel.HYPOTHESIS_DRIVEN,),
            data_scopes=("price_volume",),
            tool_allowlist=("read_campaign_brief", "promote_candidate"),
        )
    assert privileged.value.code is FailureCode.UNAUTHORIZED_CAPABILITY

    disabled = tuple(
        DeskCapabilityManifest(
            desk_id=manifest.desk_id,
            method_models=manifest.method_models,
            data_scopes=manifest.data_scopes,
            tool_allowlist=manifest.tool_allowlist,
            enabled=False,
        )
        for manifest in default_desk_capabilities()
    )
    result = DeterministicCampaignRouter().route(
        _campaign(),
        data_availability=_data("price_volume", "fundamentals"),
        desk_capabilities=disabled,
    )
    assert result.state is CampaignState.BLOCKED
    assert result.failure_code is FailureCode.ADAPTER_UNAVAILABLE


def test_route_dag_rejects_unknown_dependency_and_cycle() -> None:
    task_a = CampaignTaskPlan(
        task_id="task.a",
        desk_id="fundamental",
        method_model=MethodModel.HYPOTHESIS_DRIVEN,
        input_from=("task.missing",),
        artifact_namespace="campaign.route.fundamental",
    )
    route_unknown = CampaignRoute(
        route_id="route.unknown_dependency",
        campaign_hash=HASH_A,
        mode=ResearchMode.NEW_ALPHA_EXPLORATION,
        research_depth=ResearchDepth.STANDARD_RESEARCH,
        selected_desks=("fundamental",),
        method_models=(MethodModel.HYPOTHESIS_DRIVEN,),
        tasks=(task_a,),
        routed_at=_dt(10),
        route_reason="bad dependency fixture",
    )
    with pytest.raises(ContractValidationError) as unknown:
        validate_campaign_route_dag(route_unknown)
    assert unknown.value.code is FailureCode.DAG_VALIDATION_FAILED

    task_b = CampaignTaskPlan(
        task_id="task.b",
        desk_id="market_structure",
        method_model=MethodModel.HYPOTHESIS_DRIVEN,
        input_from=("task.c",),
        artifact_namespace="campaign.route.market_structure",
    )
    task_c = CampaignTaskPlan(
        task_id="task.c",
        desk_id="meta_alpha",
        method_model=MethodModel.ADVERSARIAL_FALSIFICATION,
        input_from=("task.b",),
        artifact_namespace="campaign.route.meta_alpha",
    )
    route_cycle = CampaignRoute(
        route_id="route.cycle",
        campaign_hash=HASH_A,
        mode=ResearchMode.NEW_ALPHA_EXPLORATION,
        research_depth=ResearchDepth.STANDARD_RESEARCH,
        selected_desks=("market_structure", "meta_alpha"),
        method_models=(MethodModel.HYPOTHESIS_DRIVEN, MethodModel.ADVERSARIAL_FALSIFICATION),
        tasks=(task_b, task_c),
        routed_at=_dt(10),
        route_reason="cycle fixture",
    )
    with pytest.raises(ContractValidationError) as cycle:
        validate_campaign_route_dag(route_cycle)
    assert cycle.value.code is FailureCode.DAG_VALIDATION_FAILED


def test_same_input_produces_same_route_hash() -> None:
    campaign = _campaign(mode=ResearchMode.NEW_ALPHA_EXPLORATION)
    router = DeterministicCampaignRouter()

    first = router.route(campaign, data_availability=_data("price_volume", "fundamentals"))
    second = router.route(campaign, data_availability=_data("price_volume", "fundamentals"))

    assert first.artifact_hash == second.artifact_hash
    assert first.route.artifact_hash == second.route.artifact_hash
