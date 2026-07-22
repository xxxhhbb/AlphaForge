from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from veriquant.contracts import Component, ContractValidationError, FailureCode
from veriquant.research_campaign import (
    CampaignBudget,
    CampaignRoute,
    CampaignState,
    CampaignTaskPlan,
    InteractionStyle,
    MethodModel,
    ResearchCampaign,
    ResearchCampaignDraft,
    ResearchDepth,
    ResearchMode,
    ResearchSubject,
    RoutedDeskBudget,
    StopCondition,
    UserCheckpoint,
)
from veriquant.research_os_v3 import (
    MultiSpecialistBranchStatus,
    MultiSpecialistRunStatus,
    ResearchOSV3MultiSpecialistService,
    load_frozen_research_context,
)
from veriquant.research_os_v3.multi_service import _aggregate_status
from veriquant.research_run_controller import ResearchRunLifecycle
from veriquant.specialist_research import (
    CandidateCritique,
    CandidateFamilyPlan,
    CombinationAxis,
    CritiqueAction,
    ResearchDirectionIdea,
    ResearchExplorationPlan,
    ResearchMechanismIdea,
    ScriptedSpecialistProvider,
    SpecialistCandidateDraft,
    SpecialistTurn,
    TurnKind,
)


REPO_ROOT = Path(__file__).resolve().parents[4]
FREEZE = REPO_ROOT / ".autobuild/veriquant/integration/dynamic_data_active/freeze_manifest.json"
VISIBLE = REPO_ROOT / ".autobuild/veriquant/integration/c8_real_ashare_v1/c8-real-ashare-v1/result.json"
DT = datetime(2026, 7, 20, 8, tzinfo=timezone.utc)
H1 = "sha256:" + "1" * 64
H2 = "sha256:" + "2" * 64


def _context():
    return load_frozen_research_context(FREEZE, decision_time=DT, visible_evidence_path=VISIBLE)


def _campaign(context, *, question="研究大A市场50日可证伪Alpha", llm_calls=12):
    subject = ResearchSubject(
        subject_id="subject.cn_ashare_u4",
        description="Approved proxy A-share U4 research subject.",
        universe=("cn_ashare_approved_pit",),
        horizon="5d",
        decision_time=DT,
        allowed_data=("price_volume", "fundamentals", "macro_regime"),
    )
    draft = ResearchCampaignDraft(
        campaign_id="campaign.u4.multi",
        question=question,
        mode=ResearchMode.NEW_ALPHA_EXPLORATION,
        research_depth=ResearchDepth.STANDARD_RESEARCH,
        interaction_style=InteractionStyle.STANDARD,
        subject=subject,
        method_models=(MethodModel.HYPOTHESIS_DRIVEN, MethodModel.ADVERSARIAL_FALSIFICATION),
        candidate_agents=("market_structure", "macro_regime", "alternative_data"),
        budget=CampaignBudget(max_factor_families=6, max_trials=12, max_llm_calls=llm_calls, max_runtime_seconds=300),
        user_checkpoints=(UserCheckpoint(checkpoint_id="checkpoint.candidates", stage="after_candidate_shortlist"),),
        stop_conditions=(StopCondition.BUDGET_EXHAUSTED, StopCondition.VALIDATION_COMPLETED),
        data_manifest_hash=context.dataset_manifest_hash,
        policy_hash=H1,
        code_hash=H2,
        created_at=DT,
        proposed_by=Component.LLM_RESEARCHER,
        state=CampaignState.NEEDS_CONFIRMATION,
    )
    return ResearchCampaign.confirm(draft, confirmed_at=DT)


def _route(campaign, *, calls=4, promotion=False):
    selected = ("market_structure", "macro_regime", "alternative_data")
    tasks = tuple(
        CampaignTaskPlan(
            task_id=f"task.{item}", desk_id=item, method_model=MethodModel.HYPOTHESIS_DRIVEN,
            artifact_namespace=f"campaign.u4.{item}", budget_units=4,
            requires_official_evidence=False, allows_promotion=promotion,
        )
        for item in selected
    )
    return CampaignRoute(
        route_id="route.u4.multi", campaign_hash=campaign.artifact_hash,
        mode=campaign.mode, research_depth=campaign.research_depth,
        selected_desks=selected, method_models=campaign.method_models, tasks=tasks,
        routed_at=DT, route_reason="Selected specialists only; scheduling has no financial authority.",
        desk_budgets=tuple(RoutedDeskBudget(desk_id=item, max_factor_families=2, max_trials=4, max_llm_calls=calls, max_tasks=1) for item in selected),
    )


def _plan(specialist_id):
    mechanisms = tuple(
        ResearchMechanismIdea(
            mechanism_id=f"mechanism_{index}", family_id=f"family_{index}",
            mechanism=f"bounded mechanism {index}", observable_proxy="past price and volume",
            predicted_direction="POSITIVE", required_fields=("close",), falsification="net evidence fails",
        )
        for index in (1, 2)
    )
    return ResearchExplorationPlan(
        specialist_id=specialist_id, scope_summary="Plan two independent candidate families.",
        directions=tuple(ResearchDirectionIdea(f"direction_{i}", f"mechanism {i}", f"family {i}", ("close",), "net evidence fails") for i in (1, 2)),
        investigation_questions=("What is available at decision time?",),
        stop_conditions=("Budget exhausted",), mechanism_ideas=mechanisms,
        candidate_family_plans=tuple(
            CandidateFamilyPlan(
                f"family_{i}",
                (f"mechanism_{i}",),
                (CombinationAxis("variant", ("rank", "zscore"), "Bounded deterministic variants."),),
                2,
            )
            for i in (1, 2)
        ),
    )


def _candidate(index):
    family = 1 if index <= 2 else 2
    expressions = {
        1: ("rank_cross_section(pct_change(close, 50))", ("close",)),
        2: ("zscore_cross_section(pct_change(close, 50))", ("close",)),
        3: (
            "rank_cross_section(subtract(pct_change(close, 50), rolling_std(close, 20)))",
            ("close",),
        ),
        4: (
            "rank_cross_section(add(pct_change(close, 50), pct_change(volume, 5)))",
            ("close", "volume"),
        ),
    }
    expression, required_fields = expressions[index]
    return SpecialistCandidateDraft(
        candidate_id=f"candidate_{index}", family_id=f"family_{family}",
        hypothesis=f"Candidate {index} is falsifiable.", economic_mechanism="Bounded information diffusion.",
        expected_direction="POSITIVE", forecast_horizon_sessions=5, lookback_sessions=50,
        required_fields=required_fields, expression=expression,
        availability_assumptions=("event_time, published_at, available_at and first_tradable_time do not exceed decision_time.",),
        neutralization=("industry", "market_cap"), falsification_tests=("Net evidence is non-positive.",),
        cost_risks=("Turnover cost.",), exposure_risks=("Industry exposure.",),
        novelty_explanation=f"Bounded family candidate {index}.",
    )


def _provider(specialist_id):
    candidates = tuple(_candidate(i) for i in range(1, 5))
    return ScriptedSpecialistProvider((
        SpecialistTurn(kind=TurnKind.PLAN, plan=_plan(specialist_id)),
        SpecialistTurn(kind=TurnKind.CANDIDATES, candidates=candidates),
        SpecialistTurn(kind=TurnKind.SELF_CRITIQUE, critiques=tuple(
            CandidateCritique(item.candidate_id, CritiqueAction.KEEP, ("Needs machine validation",), "Keep bounded candidate.") for item in candidates
        )),
    ))


class TimeoutProvider:
    provider_id = "timeout_provider"
    model_id = "fake_timeout"
    calls = 0

    def next_turn(self, context):
        del context
        self.calls += 1
        raise ContractValidationError(FailureCode.PROVIDER_TIMEOUT, "provider timed out")


def test_all_partial_branches_are_research_partial_not_system_failed() -> None:
    class Branch:
        status = MultiSpecialistBranchStatus.PARTIAL_RESULT

    status, lifecycle = _aggregate_status((Branch(), Branch()))
    assert status is MultiSpecialistRunStatus.PARTIAL
    assert lifecycle is ResearchRunLifecycle.PARTIAL


def test_selected_specialists_run_in_isolation_and_resume_is_idempotent(tmp_path):
    context = _context()
    campaign = _campaign(context)
    route = _route(campaign)
    market = _provider("market_structure")
    macro = _provider("macro_regime")
    alternative = _provider("alternative_data")
    unselected = _provider("fundamental")
    service = ResearchOSV3MultiSpecialistService(root=tmp_path, max_workers=3)
    providers = {
        "market_structure": market, "macro_regime": macro,
        "alternative_data": alternative, "fundamental": unselected,
    }

    first = service.run(run_id="u4_multi_resume", campaign=campaign, route=route, context=context, providers=providers)
    calls = (market.calls, macro.calls, unselected.calls)
    replay = service.run(run_id="u4_multi_resume", campaign=campaign, route=route, context=context, providers=providers)

    assert first.status is MultiSpecialistRunStatus.COMPLETED
    assert tuple(item.specialist_id for item in first.branches) == tuple(sorted(route.selected_desks))
    assert {item.status for item in first.branches} == {MultiSpecialistBranchStatus.COMPLETED_DRAFT}
    assert first.candidate_count == 12 and first.accepted_preflight_count == 12
    assert unselected.calls == 0
    assert (market.calls, macro.calls, unselected.calls) == calls
    assert replay.artifact_hash == first.artifact_hash
    assert replay.manifest_hash == first.manifest_hash


def test_one_provider_timeout_does_not_destroy_independent_branch(tmp_path):
    context = _context()
    campaign = _campaign(context)
    route = _route(campaign)
    for index in range(6):
        result = ResearchOSV3MultiSpecialistService(root=tmp_path / f"stress-{index}").run(
            run_id=f"u4_timeout_isolated_{index}", campaign=campaign, route=route, context=context,
            providers={
                "market_structure": _provider("market_structure"),
                "macro_regime": TimeoutProvider(),
                "alternative_data": _provider("alternative_data"),
            },
        )
        by_id = {item.specialist_id: item for item in result.branches}
        assert result.status is MultiSpecialistRunStatus.PARTIAL, by_id["market_structure"].failure_origins
        assert by_id["market_structure"].status is MultiSpecialistBranchStatus.COMPLETED_DRAFT
        assert by_id["macro_regime"].status is MultiSpecialistBranchStatus.FAILED
        assert by_id["alternative_data"].status is MultiSpecialistBranchStatus.COMPLETED_DRAFT


def test_insufficient_branch_budget_is_typed_and_does_not_call_provider(tmp_path):
    context = _context()
    campaign = _campaign(context, llm_calls=6)
    route = _route(campaign, calls=2)
    market = _provider("market_structure")
    result = ResearchOSV3MultiSpecialistService(root=tmp_path).run(
        run_id="u4_budget_blocked", campaign=campaign, route=route, context=context,
        providers={"market_structure": market, "macro_regime": _provider("macro_regime")},
    )
    assert result.status is MultiSpecialistRunStatus.BLOCKED
    assert market.calls == 0
    assert all(item.status is MultiSpecialistBranchStatus.BUDGET_BLOCKED for item in result.branches)


def test_cancel_before_start_commits_no_model_output(tmp_path):
    context = _context()
    campaign = _campaign(context)
    route = _route(campaign)
    market = _provider("market_structure")
    result = ResearchOSV3MultiSpecialistService(root=tmp_path).run(
        run_id="u4_cancelled", campaign=campaign, route=route, context=context,
        providers={"market_structure": market}, cancel_requested=True,
    )
    assert result.status is MultiSpecialistRunStatus.CANCELLED
    assert market.calls == 0 and result.candidate_count == 0


def test_route_cannot_grant_promotion_authority(tmp_path):
    context = _context()
    campaign = _campaign(context)
    route = _route(campaign, promotion=True)
    with pytest.raises(ContractValidationError) as exc:
        ResearchOSV3MultiSpecialistService(root=tmp_path).run(
            run_id="u4_forbidden_authority", campaign=campaign, route=route, context=context, providers={}
        )
    assert exc.value.code is FailureCode.UNAUTHORIZED_CAPABILITY


def test_tampered_branch_commit_fails_closed(tmp_path):
    context = _context()
    campaign = _campaign(context)
    route = _route(campaign)
    service = ResearchOSV3MultiSpecialistService(root=tmp_path)
    providers = {"market_structure": _provider("market_structure"), "macro_regime": _provider("macro_regime")}
    service.run(run_id="u4_tampered_commit", campaign=campaign, route=route, context=context, providers=providers)
    path = tmp_path / "research_runs/u4_tampered_commit/branches/market_structure/commit.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["payload"]["provider_calls"] = 999
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ContractValidationError) as exc:
        service.run(run_id="u4_tampered_commit", campaign=campaign, route=route, context=context, providers=providers)
    assert exc.value.code is FailureCode.INVALID_HASH
