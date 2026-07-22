from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.veriquant.contracts import Component, ContractValidationError, FailureCode
from src.veriquant.research_campaign import (
    CampaignBudget,
    CampaignState,
    DataAvailability,
    DeterministicCampaignRouter,
    DeterministicMethodProtocolCompiler,
    InteractionStyle,
    MethodModel,
    MethodProtocolRegistry,
    MethodProtocolTemplate,
    ProtocolStageSpec,
    RegisteredResearchContext,
    ResearchCampaign,
    ResearchCampaignDraft,
    ResearchDepth,
    ResearchMode,
    ResearchProtocolKind,
    ResearchSubject,
    StopCondition,
    UserCheckpoint,
    default_method_protocol_templates,
    validate_protocol_transition,
)


HASH_A = "sha256:" + "a" * 64
HASH_B = "sha256:" + "b" * 64
HASH_C = "sha256:" + "c" * 64


def _dt(hour: int = 9) -> datetime:
    return datetime(2026, 7, 19, hour, 30, tzinfo=timezone.utc)


def _campaign(
    *,
    mode: ResearchMode,
    allowed_data: tuple[str, ...] = ("price_volume", "fundamentals"),
    method_models: tuple[MethodModel, ...] = (MethodModel.HYPOTHESIS_DRIVEN,),
    existing_factor_ids: tuple[str, ...] = (),
    portfolio_id: str | None = None,
    depth: ResearchDepth = ResearchDepth.STANDARD_RESEARCH,
    tags: tuple[str, ...] = (),
) -> ResearchCampaign:
    subject = ResearchSubject(
        subject_id=f"subject.{mode.value.lower()}",
        description=f"Protocol test subject for {mode.value}.",
        universe=("CN_ASHARE_APPROVED_PIT_SAMPLE",),
        horizon="20d",
        decision_time=_dt(),
        allowed_data=allowed_data,
        existing_factor_ids=existing_factor_ids,
        portfolio_id=portfolio_id,
        tags=tags,
    )
    draft = ResearchCampaignDraft(
        campaign_id=f"campaign.{mode.value.lower()}",
        question=f"Protocol test for {mode.value}",
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


def _protocol_plan(campaign: ResearchCampaign, *, context: RegisteredResearchContext | None = None):
    route_result = DeterministicCampaignRouter().route(
        campaign,
        data_availability=_data(*campaign.subject.allowed_data),
        registered_context=context or RegisteredResearchContext(
            factor_ids=campaign.subject.existing_factor_ids,
            portfolio_ids=(campaign.subject.portfolio_id,) if campaign.subject.portfolio_id else (),
        ),
    )
    assert route_result.state is CampaignState.ROUTED
    return DeterministicMethodProtocolCompiler().compile(campaign=campaign, route=route_result.route)


def _stage_ids(plan) -> tuple[str, ...]:
    return plan.task_protocols[0].stage_ids


def test_factor_exploration_uses_factor_protocol_template() -> None:
    campaign = _campaign(mode=ResearchMode.NEW_ALPHA_EXPLORATION)
    plan = _protocol_plan(campaign)

    assert plan.task_protocols[0].protocol_kind is ResearchProtocolKind.FACTOR_EXPLORATION_OR_IMPROVEMENT
    assert _stage_ids(plan) == (
        "hypothesis",
        "factor_construct",
        "factor_dag_pit",
        "official_evaluate",
        "redacted_feedback",
    )
    assert plan.task_protocols[0].may_generate_factor_draft is True
    assert plan.task_protocols[0].requires_official_evidence is True
    assert plan.task_protocols[0].allows_automatic_trading is False


def test_validate_idea_uses_preregistered_falsification_not_generic_factor_loop() -> None:
    campaign = _campaign(mode=ResearchMode.VALIDATE_IDEA)
    plan = _protocol_plan(campaign)

    assert plan.task_protocols[0].protocol_kind is ResearchProtocolKind.IDEA_VALIDATION
    assert _stage_ids(plan) == (
        "preregister_falsification",
        "resolve_factor_or_proxy",
        "pit_evaluate",
        "adversarial_tests",
        "validation_conclusion",
    )
    assert "factor_construct" not in _stage_ids(plan)


def test_diagnosis_does_not_generate_new_factor_or_change_holdings() -> None:
    campaign = _campaign(
        mode=ResearchMode.DIAGNOSE_FACTOR_PORTFOLIO,
        allowed_data=("price_volume", "registered_factor_summary", "macro_regime"),
        method_models=(MethodModel.DIAGNOSTIC_ATTRIBUTION,),
        existing_factor_ids=("momentum_v1",),
    )
    plan = _protocol_plan(campaign, context=RegisteredResearchContext(factor_ids=("momentum_v1",)))

    assert plan.task_protocols[0].protocol_kind is ResearchProtocolKind.DIAGNOSTIC
    assert _stage_ids(plan) == (
        "scope_problem",
        "retrieve_registered_evidence",
        "attribution_and_regime_split",
        "adversarial_root_cause_checks",
        "ranked_diagnosis",
    )
    assert "factor_construct" not in _stage_ids(plan)
    assert all(task.may_generate_factor_draft is False for task in plan.task_protocols)
    assert all(task.allows_automatic_trading is False for task in plan.task_protocols)


def test_data_source_replication_and_event_topics_have_distinct_protocols() -> None:
    cases = (
        (
            _campaign(
                mode=ResearchMode.NEW_DATA_SOURCE_EVALUATION,
                allowed_data=("price_volume", "alternative_data"),
                method_models=(MethodModel.DATA_ANOMALY,),
            ),
            ResearchProtocolKind.DATA_SOURCE_EVALUATION,
            ("provenance", "availability_pit", "coverage_and_bias", "sandbox_incrementality", "dataset_assessment"),
        ),
        (
            _campaign(
                mode=ResearchMode.PAPER_STRATEGY_REPLICATION,
                method_models=(MethodModel.REPLICATION,),
            ),
            ResearchProtocolKind.PAPER_REPLICATION,
            (
                "extract_definition",
                "map_data_and_time_semantics",
                "controlled_implementation",
                "reproduce",
                "discrepancy_report",
            ),
        ),
        (
            _campaign(
                mode=ResearchMode.EVENT_MARKET_TOPIC,
                allowed_data=("price_volume", "events", "macro_regime"),
                method_models=(MethodModel.EVENT_STUDY,),
                tags=("earnings",),
            ),
            ResearchProtocolKind.EVENT_STUDY_TOPIC,
            (
                "event_definition",
                "map_event_time_semantics",
                "event_window_pit_evaluate",
                "regime_conditioning",
                "event_topic_conclusion",
            ),
        ),
    )
    for campaign, kind, stage_ids in cases:
        plan = _protocol_plan(campaign)
        assert plan.task_protocols[0].protocol_kind is kind
        assert _stage_ids(plan) == stage_ids
        assert "factor_construct" not in _stage_ids(plan)


def test_online_monitoring_can_only_recommend_human_decision_not_trade_or_search() -> None:
    campaign = _campaign(
        mode=ResearchMode.MONITOR_DECAY_RETIREMENT,
        allowed_data=("price_volume", "registered_factor_summary", "macro_regime"),
        method_models=(MethodModel.ONLINE_MONITORING,),
        existing_factor_ids=("momentum_v1",),
    )
    plan = _protocol_plan(campaign, context=RegisteredResearchContext(factor_ids=("momentum_v1",)))

    assert plan.task_protocols[0].protocol_kind is ResearchProtocolKind.ONLINE_MONITORING
    assert _stage_ids(plan) == ("compare_baseline", "detect_drift", "diagnose", "recommend", "human_decision")
    assert plan.task_protocols[0].human_decision_required is True
    assert plan.task_protocols[0].allowed_human_recommendations == ("KEEP", "SCALE_DOWN", "PAUSE", "RETIRE")
    assert plan.task_protocols[0].may_generate_factor_draft is False
    assert plan.task_protocols[0].allows_automatic_trading is False


def test_quick_exploration_is_draft_only_without_official_evidence() -> None:
    campaign = _campaign(mode=ResearchMode.NEW_ALPHA_EXPLORATION, depth=ResearchDepth.QUICK_EXPLORATION)
    plan = _protocol_plan(campaign)

    assert plan.task_protocols[0].protocol_kind is ResearchProtocolKind.QUICK_DRAFT_ONLY
    assert _stage_ids(plan) == ("scope_question", "draft_research_brief", "draft_only_output", "human_decision")
    assert plan.task_protocols[0].requires_official_evidence is False
    assert plan.task_protocols[0].may_generate_factor_draft is False
    assert plan.task_protocols[0].human_decision_required is True


def test_illegal_protocol_transition_fails_closed() -> None:
    template = MethodProtocolRegistry().template_for(ResearchProtocolKind.DIAGNOSTIC)

    validate_protocol_transition(template, "scope_problem", "retrieve_registered_evidence")
    with pytest.raises(ContractValidationError) as exc:
        validate_protocol_transition(template, "scope_problem", "ranked_diagnosis")
    assert exc.value.code is FailureCode.INVALID_STATE_TRANSITION


def test_protocol_templates_reject_downstream_authority_and_unbounded_search() -> None:
    with pytest.raises(ContractValidationError) as forbidden_component:
        ProtocolStageSpec(
            stage_id="bad_stage",
            stage_name="Bad stage",
            description="Attempts to assign risk authority.",
            allowed_components=(Component.RISK_GATE,),
        )
    assert forbidden_component.value.code is FailureCode.UNAUTHORIZED_CAPABILITY

    stage = ProtocolStageSpec(
        stage_id="scope",
        stage_name="Scope",
        description="Allowed scope stage.",
        allowed_components=(Component.RESEARCH_DESK,),
    )
    with pytest.raises(ContractValidationError) as unbounded:
        MethodProtocolTemplate(
            template_id="protocol.bad_unbounded",
            protocol_kind=ResearchProtocolKind.DIAGNOSTIC,
            research_modes=(ResearchMode.DIAGNOSE_FACTOR_PORTFOLIO,),
            method_models=(MethodModel.DIAGNOSTIC_ATTRIBUTION,),
            stages=(stage,),
            terminal_outputs=("diagnosis",),
            allows_unbounded_search=True,
        )
    assert unbounded.value.code is FailureCode.PROVIDER_BUDGET_EXCEEDED


def test_same_route_produces_same_protocol_plan_hash() -> None:
    campaign = _campaign(mode=ResearchMode.NEW_ALPHA_EXPLORATION)
    route_result = DeterministicCampaignRouter().route(campaign, data_availability=_data("price_volume", "fundamentals"))
    compiler = DeterministicMethodProtocolCompiler()

    first = compiler.compile(campaign=campaign, route=route_result.route)
    second = compiler.compile(campaign=campaign, route=route_result.route)

    assert first.artifact_hash == second.artifact_hash
    assert first.task_protocols[0].protocol_template_hash == second.task_protocols[0].protocol_template_hash
    assert len(default_method_protocol_templates()) >= 7

