from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.veriquant.campaign_tool_registry import (
    CampaignToolBinding,
    CampaignToolPolicyCompiler,
    DeskToolAvailability,
    build_campaign_tool_registry,
)
from src.veriquant.contracts import Component, ContractValidationError, FailureCode
from src.veriquant.research_campaign import (
    CampaignBudget,
    CampaignRoute,
    CampaignState,
    CampaignTaskPlan,
    DataAvailability,
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
)
from src.veriquant.research_tool_contracts import (
    ResearchToolCapability,
    ResearchToolContractError,
    ResearchToolFailureCode,
    ResearchToolRequest,
    ResearchToolVisibility,
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
    method_models: tuple[MethodModel, ...] = (MethodModel.HYPOTHESIS_DRIVEN,),
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
    )
    draft = ResearchCampaignDraft(
        campaign_id=f"campaign.{mode.value.lower()}",
        question=f"Tool registry test for {mode.value}",
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
        budget=CampaignBudget(max_factor_families=3, max_trials=9, max_llm_calls=6, max_runtime_seconds=300),
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


def _route(campaign: ResearchCampaign) -> CampaignRoute:
    result = DeterministicCampaignRouter().route(
        campaign,
        data_availability=_data(*campaign.subject.allowed_data),
        registered_context=RegisteredResearchContext(
            factor_ids=campaign.subject.existing_factor_ids,
            portfolio_ids=(campaign.subject.portfolio_id,) if campaign.subject.portfolio_id else (),
            factor_domains={factor_id: "market_structure" for factor_id in campaign.subject.existing_factor_ids},
        ),
    )
    assert result.route is not None
    return result.route


def test_unconfirmed_campaign_cannot_build_registry() -> None:
    campaign = _campaign()
    route = _route(campaign)
    draft = ResearchCampaignDraft(
        campaign_id="campaign.unconfirmed",
        question="draft only",
        mode=ResearchMode.NEW_ALPHA_EXPLORATION,
        research_depth=ResearchDepth.STANDARD_RESEARCH,
        interaction_style=InteractionStyle.STANDARD,
        subject=campaign.subject,
        method_models=(MethodModel.HYPOTHESIS_DRIVEN,),
        candidate_agents=("fundamental",),
        budget=campaign.budget,
        user_checkpoints=campaign.user_checkpoints,
        stop_conditions=campaign.stop_conditions,
        data_manifest_hash=HASH_A,
        policy_hash=HASH_B,
        code_hash=HASH_C,
        created_at=_dt(),
        proposed_by=Component.LLM_RESEARCHER,
    )

    with pytest.raises(ContractValidationError) as exc:
        CampaignToolPolicyCompiler().compile(draft, route)  # type: ignore[arg-type]

    assert exc.value.code is FailureCode.INVALID_VALUE


def test_unselected_desk_has_no_tools_and_registry_does_not_open_all_six() -> None:
    campaign = _campaign()
    registry = build_campaign_tool_registry(campaign, _route(campaign))

    assert 1 <= len(registry.desk_ids) < 6
    assert "alternative_data" not in registry.desk_ids
    with pytest.raises(ResearchToolContractError) as exc:
        registry.pack_for_desk("alternative_data")

    assert exc.value.code is ResearchToolFailureCode.UNAUTHORIZED_TOOL


def test_undeclared_tool_fails_closed() -> None:
    campaign = _campaign()
    registry = build_campaign_tool_registry(campaign, _route(campaign))

    with pytest.raises(ResearchToolContractError) as exc:
        registry.binding_for("fundamental", "create_order")

    assert exc.value.code is ResearchToolFailureCode.UNAUTHORIZED_TOOL


def test_same_name_cross_desk_tools_have_separate_context_and_policy() -> None:
    campaign = _campaign()
    registry = build_campaign_tool_registry(campaign, _route(campaign))

    fundamental = registry.binding_for("fundamental", "query_visible_evidence")
    meta = registry.binding_for("meta_alpha", "query_visible_evidence")

    assert fundamental.context_hash != meta.context_hash
    request = ResearchToolRequest(
        campaign_id=campaign.campaign_id,
        campaign_hash=campaign.artifact_hash,
        desk_id="meta_alpha",
        decision_time=campaign.subject.decision_time,
        allowed_data=campaign.subject.allowed_data,
        tool_name="query_visible_evidence",
        request_id="request.meta_alpha.visible",
        offline=True,
    )
    with pytest.raises(ResearchToolContractError) as exc:
        registry.pack_for_desk("fundamental").policy.authorize(request, fundamental.capability)

    assert exc.value.code is ResearchToolFailureCode.DESK_MISMATCH


def test_unavailable_provider_does_not_break_other_selected_desks() -> None:
    campaign = _campaign(
        mode=ResearchMode.DIAGNOSE_FACTOR_PORTFOLIO,
        subject_allowed_data=("price_volume", "registered_factor_summary", "macro_regime"),
        subject_existing_factor_ids=("momentum_v1",),
        method_models=(MethodModel.DIAGNOSTIC_ATTRIBUTION,),
    )
    route = _route(campaign)
    registry = build_campaign_tool_registry(campaign, route)

    macro_pack = registry.pack_for_desk("macro_regime")
    assert macro_pack.require_binding("frozen_macro_fixture").availability is DeskToolAvailability.UNAVAILABLE
    assert "dataset_summary" in macro_pack.available_tools
    assert registry.pack_for_desk("meta_alpha").available_tools


def test_unsafe_or_privileged_tool_binding_is_rejected() -> None:
    common = {
        "binding_id": "binding.bad",
        "campaign_hash": HASH_A,
        "route_hash": HASH_B,
        "desk_id": "fundamental",
        "visibility": ResearchToolVisibility.DRAFT_SUPPORT,
        "source_package": "research_tool_contracts",
        "provider_id": "offline_fixture",
        "allowed_data": ("price_volume",),
        "availability": DeskToolAvailability.AVAILABLE,
        "availability_reason": "available",
        "context_hash": HASH_C,
    }
    with pytest.raises(ResearchToolContractError) as sealed:
        CampaignToolBinding(tool_name="read_sealed", capability=ResearchToolCapability.READ_VISIBLE_EVIDENCE, **common)
    assert sealed.value.code is ResearchToolFailureCode.UNAUTHORIZED_TOOL

    with pytest.raises(ResearchToolContractError) as promotion:
        CampaignToolBinding(tool_name="query_visible_evidence", capability=ResearchToolCapability.PROMOTION, **common)
    assert promotion.value.code is ResearchToolFailureCode.UNAUTHORIZED_CAPABILITY


def test_registry_hash_is_stable_independent_of_status_mapping_order() -> None:
    campaign = _campaign()
    route = _route(campaign)
    first = build_campaign_tool_registry(
        campaign,
        route,
        package_statuses={"vibe_market_adapters": True, "research_explorer_tools": True},
        provider_availability={"documents": False, "macro": False},
    )
    second = build_campaign_tool_registry(
        campaign,
        route,
        package_statuses={"research_explorer_tools": True, "vibe_market_adapters": True},
        provider_availability={"macro": False, "documents": False},
    )

    assert first.artifact_hash == second.artifact_hash
    assert first.authority_manifest.artifact_hash == second.authority_manifest.artifact_hash


def test_default_offline_no_key_no_llm_registry_builds_and_invokes_available_tool() -> None:
    campaign = _campaign()
    registry = build_campaign_tool_registry(campaign, _route(campaign))
    binding = registry.binding_for("fundamental", "dataset_pit_summary")
    request = ResearchToolRequest(
        campaign_id=campaign.campaign_id,
        campaign_hash=campaign.artifact_hash,
        desk_id="fundamental",
        decision_time=campaign.subject.decision_time,
        allowed_data=campaign.subject.allowed_data,
        tool_name=binding.tool_name,
        request_id="request.fundamental.dataset",
        offline=True,
    )

    result = registry.invoke(request, payload={"summary": "offline frozen dataset summary"})

    assert result.failure_code is None
    assert result.capability is ResearchToolCapability.READ_FROZEN_DATA
    assert result.payload["status"] == "available"
    assert all(
        binding.capability is not ResearchToolCapability.NETWORK_PROVIDER
        for pack in registry.packs.values()
        for binding in pack.bindings
    )


def test_all_six_route_is_rejected_by_compiler() -> None:
    campaign = _campaign()
    tasks = tuple(
        CampaignTaskPlan(
            task_id=f"task.{desk}",
            desk_id=desk,
            method_model=MethodModel.HYPOTHESIS_DRIVEN,
            artifact_namespace=f"campaign.route.{desk}",
        )
        for desk in (
            "fundamental",
            "expectations_events",
            "market_structure",
            "alternative_data",
            "macro_regime",
            "meta_alpha",
        )
    )
    route = CampaignRoute(
        route_id="route.all_six",
        campaign_hash=campaign.artifact_hash,
        mode=campaign.mode,
        research_depth=campaign.research_depth,
        selected_desks=tuple(task.desk_id for task in tasks),
        method_models=(MethodModel.HYPOTHESIS_DRIVEN,),
        tasks=tasks,
        routed_at=_dt(11),
        route_reason="bad test route",
    )

    with pytest.raises(ContractValidationError) as exc:
        build_campaign_tool_registry(campaign, route)

    assert exc.value.code is FailureCode.UNAUTHORIZED_CAPABILITY
