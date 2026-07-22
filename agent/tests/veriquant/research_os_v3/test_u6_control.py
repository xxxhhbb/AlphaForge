from __future__ import annotations

import threading
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

from veriquant.contracts import Component, ContractValidationError, FailureCode
from veriquant.research_campaign import (
    CampaignBudget,
    CampaignState,
    InteractionStyle,
    MethodModel,
    ResearchCampaign,
    ResearchCampaignDraft,
    ResearchDepth,
    ResearchMode,
    ResearchSubject,
    StopCondition,
    UserCheckpoint,
)
from veriquant.research_os_v3.control import ResearchOSV3CampaignControl
from veriquant.research_os_v3.tools import load_frozen_research_context
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


def _draft(context):
    return ResearchCampaignDraft(
        campaign_id="campaign.u6.control",
        question="检验大A市场50日量价和基本面候选",
        mode=ResearchMode.VALIDATE_IDEA,
        research_depth=ResearchDepth.STANDARD_RESEARCH,
        interaction_style=InteractionStyle.STANDARD,
        subject=ResearchSubject(
            subject_id="subject.cn_ashare_u6",
            description="Approved frozen A-share control test.",
            universe=("cn_ashare_approved_pit",),
            horizon="5d",
            decision_time=DT,
            allowed_data=("price_volume", "fundamentals"),
        ),
        method_models=(MethodModel.HYPOTHESIS_DRIVEN, MethodModel.ADVERSARIAL_FALSIFICATION),
        candidate_agents=("fundamental", "market_structure"),
        budget=CampaignBudget(
            max_factor_families=4, max_trials=8, max_llm_calls=8, max_runtime_seconds=120
        ),
        user_checkpoints=(UserCheckpoint(checkpoint_id="checkpoint.candidates", stage="after_candidate_shortlist"),),
        stop_conditions=(StopCondition.BUDGET_EXHAUSTED, StopCondition.VALIDATION_COMPLETED),
        data_manifest_hash=context.dataset_manifest_hash,
        policy_hash=H1,
        code_hash=H2,
        created_at=DT,
        proposed_by=Component.LLM_RESEARCHER,
        state=CampaignState.NEEDS_CONFIRMATION,
    )


def _campaign(context):
    return ResearchCampaign.confirm(_draft(context), confirmed_at=DT)


def _plan(specialist_id):
    mechanisms = tuple(
        ResearchMechanismIdea(
            mechanism_id=f"mechanism_{index}", family_id=f"family_{index}",
            mechanism=f"bounded mechanism {index}", observable_proxy="past price",
            predicted_direction="POSITIVE", required_fields=("close",),
            falsification="net evidence fails",
        )
        for index in (1, 2)
    )
    return ResearchExplorationPlan(
        specialist_id=specialist_id,
        scope_summary="Two bounded independent candidate families.",
        directions=tuple(
            ResearchDirectionIdea(
                f"direction_{i}", f"mechanism {i}", f"family {i}", ("close",), "net evidence fails"
            )
            for i in (1, 2)
        ),
        investigation_questions=("What was available at decision time?",),
        stop_conditions=("Budget exhausted",),
        mechanism_ideas=mechanisms,
        candidate_family_plans=tuple(
            CandidateFamilyPlan(
                f"family_{i}", (f"mechanism_{i}",),
                (CombinationAxis("variant", ("rank", "zscore"), "Bounded variants."),), 2,
            )
            for i in (1, 2)
        ),
    )


def _candidate(index):
    expressions = {
        1: "rank_cross_section(pct_change(close, 50))",
        2: "zscore_cross_section(pct_change(close, 50))",
        3: "rank_cross_section(subtract(pct_change(close, 50),rolling_std(close,20)))",
        4: "rank_cross_section(add(pct_change(close,50),pct_change(volume,5)))",
    }
    return SpecialistCandidateDraft(
        candidate_id=f"candidate_{index}", family_id=f"family_{1 if index <= 2 else 2}",
        hypothesis=f"Candidate {index} is falsifiable.", economic_mechanism="Bounded diffusion.",
        expected_direction="POSITIVE", forecast_horizon_sessions=5, lookback_sessions=50,
        required_fields=("close", "volume") if index == 4 else ("close",),
        expression=expressions[index],
        availability_assumptions=(
            "event_time, published_at, available_at and first_tradable_time do not exceed decision_time.",
        ),
        neutralization=("industry", "market_cap"),
        falsification_tests=("Net evidence is non-positive.",),
        cost_risks=("Turnover cost.",), exposure_risks=("Industry exposure.",),
        novelty_explanation=f"Candidate {index}.",
    )


def _provider(specialist_id):
    candidates = tuple(_candidate(index) for index in range(1, 5))
    return ScriptedSpecialistProvider(
        (
            SpecialistTurn(kind=TurnKind.PLAN, plan=_plan(specialist_id)),
            SpecialistTurn(kind=TurnKind.CANDIDATES, candidates=candidates),
            SpecialistTurn(
                kind=TurnKind.SELF_CRITIQUE,
                critiques=tuple(
                    CandidateCritique(
                        item.candidate_id, CritiqueAction.KEEP,
                        ("Needs deterministic validation",), "Keep bounded candidate."
                    )
                    for item in candidates
                ),
            ),
        )
    )


def test_confirmed_campaign_runs_async_with_one_provider_instance_per_group(tmp_path):
    context = _context()
    created = []

    def factory(specialist_id):
        provider = _provider(specialist_id)
        created.append((specialist_id, provider))
        return provider

    control = ResearchOSV3CampaignControl(root=tmp_path, provider_factory=factory)
    started = control.start(campaign=_campaign(context), context=context, run_id="u6_async")
    final = control.wait("u6_async", timeout=30)
    replay = control.start(campaign=_campaign(context), context=context, run_id="u6_async")
    control.close()

    assert started.run_id == "u6_async"
    assert final.current_stage is ResearchRunLifecycle.COMPLETED
    assert final.candidate_count == 8
    assert len(created) == 2 and len({id(item[1]) for item in created}) == 2
    assert replay.artifact_hash == final.artifact_hash
    assert len(created) == 2  # terminal idempotency does not even rebuild providers
    assert all(item.official_evidence_hash is None and item.report_hash is None for item in (started, final, replay))


def test_unconfirmed_draft_cannot_start(tmp_path):
    context = _context()
    control = ResearchOSV3CampaignControl(root=tmp_path, provider_factory=lambda item: _provider(item))
    with pytest.raises(ContractValidationError) as exc:
        control.start(campaign=_draft(context), context=context, run_id="u6_unconfirmed")  # type: ignore[arg-type]
    control.close()
    assert exc.value.code is FailureCode.INVALID_STATE


def test_provider_unavailable_is_typed_and_other_group_continues(tmp_path):
    context = _context()

    def factory(specialist_id):
        if specialist_id == "fundamental":
            raise ContractValidationError(FailureCode.ADAPTER_UNAVAILABLE, "configured provider unavailable")
        return _provider(specialist_id)

    control = ResearchOSV3CampaignControl(root=tmp_path, provider_factory=factory)
    control.start(campaign=_campaign(context), context=context, run_id="u6_provider_gap")
    final = control.wait("u6_provider_gap", timeout=30)
    control.close()
    assert final.current_stage is ResearchRunLifecycle.PARTIAL
    assert FailureCode.ADAPTER_UNAVAILABLE.value in final.typed_failures
    assert final.candidate_count == 4


class _BlockingProvider:
    provider_id = "blocking_scripted"
    model_id = "fake_model"

    def __init__(self, specialist_id, entered, release):
        self.delegate = _provider(specialist_id)
        self.entered = entered
        self.release = release
        self.calls = 0

    def next_turn(self, context):
        self.calls += 1
        if self.calls == 1:
            self.entered.set()
            assert self.release.wait(10)
        return self.delegate.next_turn(context)


def test_cancel_is_persisted_and_resume_replays_commits_without_duplicate_calls(tmp_path):
    context = _context()
    entered = threading.Event()
    release = threading.Event()
    providers = {}

    def factory(specialist_id):
        provider = _BlockingProvider(specialist_id, entered, release)
        providers.setdefault(specialist_id, []).append(provider)
        return provider

    control = ResearchOSV3CampaignControl(root=tmp_path, provider_factory=factory)
    campaign = _campaign(context)
    control.start(campaign=campaign, context=context, run_id="u6_cancel_resume")
    assert entered.wait(5)
    cancelled = control.cancel("u6_cancel_resume")
    assert cancelled.current_stage is ResearchRunLifecycle.CANCELLED
    assert control.status("u6_cancel_resume").current_stage is ResearchRunLifecycle.CANCELLED
    release.set()
    settled = control.wait("u6_cancel_resume", timeout=30)
    calls_before = sum(item.calls for values in providers.values() for item in values)
    assert settled.current_stage is ResearchRunLifecycle.CANCELLED

    control.resume(campaign=campaign, context=context, run_id="u6_cancel_resume")
    resumed = control.wait("u6_cancel_resume", timeout=30)
    calls_after = sum(item.calls for values in providers.values() for item in values)
    control.close()
    assert resumed.current_stage is ResearchRunLifecycle.COMPLETED
    assert resumed.candidate_count == 8
    assert calls_after == calls_before


def test_same_run_id_with_changed_identity_fails_closed(tmp_path):
    context = _context()
    control = ResearchOSV3CampaignControl(root=tmp_path, provider_factory=lambda item: _provider(item))
    campaign = _campaign(context)
    control.start(campaign=campaign, context=context, run_id="u6_collision", seed=7)
    control.wait("u6_collision", timeout=30)
    changed_draft = replace(_draft(context), question="不同的研究身份")
    changed = ResearchCampaign.confirm(changed_draft, confirmed_at=DT)
    with pytest.raises(Exception) as exc:
        control.start(campaign=changed, context=context, run_id="u6_collision")
    control.close()
    assert getattr(exc.value, "code", None) is FailureCode.ARTIFACT_COLLISION


def test_completion_failure_invokes_terminal_report_handler(tmp_path):
    context = _context()
    terminal_calls = []

    def fail_completion(*_args):
        raise ContractValidationError(FailureCode.ADAPTER_EXECUTION_FAILED, "forced completion failure")

    def record_terminal(_campaign, _route, _context, manifest):
        terminal_calls.append(manifest)

    control = ResearchOSV3CampaignControl(
        root=tmp_path,
        provider_factory=lambda item: _provider(item),
        completion_handler=fail_completion,
        terminal_handler=record_terminal,
    )
    control.start(campaign=_campaign(context), context=context, run_id="u6_terminal_report")
    final = control.wait("u6_terminal_report", timeout=30)
    control.close()

    assert final.current_stage is ResearchRunLifecycle.FAILED
    assert len(terminal_calls) == 1
    assert terminal_calls[0].current_stage is ResearchRunLifecycle.FAILED
