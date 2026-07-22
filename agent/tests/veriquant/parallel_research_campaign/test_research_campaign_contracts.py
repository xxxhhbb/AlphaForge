from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.veriquant.contracts import Component, ContractValidationError, FailureCode
from src.veriquant.research_campaign import (
    CampaignBudget,
    CampaignOutcome,
    CampaignRoute,
    CampaignRunManifest,
    CampaignState,
    CampaignTaskPlan,
    InteractionStyle,
    MethodModel,
    ResearchCampaign,
    ResearchCampaignDraft,
    ResearchDepth,
    ResearchMode,
    ResearchSubject,
    StopCondition,
    UserCheckpoint,
    require_campaign_executable,
    require_new_trial,
    validate_campaign_transition,
)


HASH_A = "sha256:" + "a" * 64
HASH_B = "sha256:" + "b" * 64
HASH_C = "sha256:" + "c" * 64
HASH_D = "sha256:" + "d" * 64
HASH_E = "sha256:" + "e" * 64


def _dt() -> datetime:
    return datetime(2026, 7, 19, 9, 30, tzinfo=timezone.utc)


def _subject(**overrides):
    payload = {
        "subject_id": "subject.cn_ashare_top50",
        "description": "A-share point-in-time universe for a 20d research campaign.",
        "universe": ("000001.SZ", "600000.SH"),
        "horizon": "20d",
        "decision_time": _dt(),
        "allowed_data": ("price_volume", "fundamentals"),
        "existing_factor_ids": ("momentum.v1", "value.v1"),
        "portfolio_id": "portfolio.mn.v1",
        "tags": ("momentum_branch",),
    }
    payload.update(overrides)
    return ResearchSubject(**payload)


def _budget(**overrides):
    payload = {
        "max_factor_families": 2,
        "max_trials": 4,
        "max_llm_calls": 8,
        "max_runtime_seconds": 120,
        "expires_at": datetime(2026, 7, 20, 9, 30, tzinfo=timezone.utc),
    }
    payload.update(overrides)
    return CampaignBudget(**payload)


def _checkpoint(stage: str = "after_research_brief") -> UserCheckpoint:
    return UserCheckpoint(checkpoint_id=f"checkpoint.{stage}", stage=stage, description=stage)


def _draft(**overrides) -> ResearchCampaignDraft:
    payload = {
        "campaign_id": "campaign.20260719.001",
        "question": "Find a low-correlation 20d alpha candidate for the approved A-share universe.",
        "mode": ResearchMode.PORTFOLIO_GAP_SEARCH,
        "research_depth": ResearchDepth.STANDARD_RESEARCH,
        "interaction_style": InteractionStyle.STANDARD,
        "subject": _subject(),
        "method_models": (MethodModel.RESIDUAL_INCREMENTAL, MethodModel.ADVERSARIAL_FALSIFICATION),
        "candidate_agents": ("meta_alpha", "market_structure"),
        "budget": _budget(),
        "user_checkpoints": (_checkpoint(), _checkpoint("before_promotion")),
        "stop_conditions": (StopCondition.BUDGET_EXHAUSTED, StopCondition.VALIDATION_COMPLETED),
        "data_manifest_hash": HASH_A,
        "policy_hash": HASH_B,
        "code_hash": HASH_C,
        "created_at": _dt(),
        "proposed_by": Component.LLM_RESEARCHER,
        "assumptions": ("No sealed evidence is visible to proposal workers.",),
    }
    payload.update(overrides)
    return ResearchCampaignDraft(**payload)


def _confirmed(**draft_overrides) -> ResearchCampaign:
    draft = _draft(**draft_overrides).ready_for_confirmation()
    return ResearchCampaign.confirm(draft, confirmed_at=datetime(2026, 7, 19, 10, 0, tzinfo=timezone.utc))


def test_factor_categories_are_not_research_modes() -> None:
    for value in ("MOMENTUM", "VALUE", "REVERSAL", "动量", "价值", "反转"):
        with pytest.raises(ValueError):
            ResearchMode(value)


def test_missing_universe_horizon_budget_or_stop_conditions_rejects_confirmation() -> None:
    with pytest.raises(ContractValidationError) as no_universe:
        _draft(subject=_subject(universe=()))
    assert no_universe.value.code is FailureCode.INVALID_VALUE

    with pytest.raises(ContractValidationError) as no_horizon:
        _draft(subject=_subject(horizon=""))
    assert no_horizon.value.code is FailureCode.INVALID_VALUE

    payload = _draft().to_dict()
    payload.pop("budget")
    with pytest.raises(ContractValidationError) as missing_budget:
        ResearchCampaignDraft.from_payload(payload, component=Component.LLM_RESEARCHER)
    assert missing_budget.value.code is FailureCode.MISSING_REQUIRED_FIELD

    with pytest.raises(ContractValidationError) as no_stop:
        _draft(stop_conditions=())
    assert no_stop.value.code is FailureCode.INVALID_VALUE


def test_same_campaign_has_stable_canonical_hash() -> None:
    campaign = _confirmed()
    shuffled_payload = {
        "schema_version": campaign.schema_version,
        "contract_type": campaign.contract_type,
        "state": campaign.state.value,
        "confirmed_by": campaign.confirmed_by.value,
        "confirmed_at": campaign.confirmed_at.isoformat(),
        "code_hash": campaign.code_hash,
        "policy_hash": campaign.policy_hash,
        "data_manifest_hash": campaign.data_manifest_hash,
        "stop_conditions": [item.value for item in campaign.stop_conditions],
        "user_checkpoints": [item.to_dict() for item in campaign.user_checkpoints],
        "budget": campaign.budget.to_dict(),
        "candidate_agents": list(campaign.candidate_agents),
        "method_models": [item.value for item in campaign.method_models],
        "subject": campaign.subject.to_dict(),
        "interaction_style": campaign.interaction_style.value,
        "research_depth": campaign.research_depth.value,
        "mode": campaign.mode.value,
        "question": campaign.question,
        "draft_hash": campaign.draft_hash,
        "campaign_id": campaign.campaign_id,
    }
    parsed = ResearchCampaign.from_payload(shuffled_payload, component=Component.OPERATOR)
    assert parsed.artifact_hash == campaign.artifact_hash
    assert parsed.canonical_json() == campaign.canonical_json()


def test_unconfirmed_campaign_cannot_execute() -> None:
    with pytest.raises(ContractValidationError) as exc:
        require_campaign_executable(_draft().ready_for_confirmation())  # type: ignore[arg-type]
    assert exc.value.code is FailureCode.INVALID_STATE
    assert exc.value.details["campaign_failure_code"] == "CAMPAIGN_NOT_CONFIRMED"

    campaign = _confirmed()
    require_campaign_executable(campaign)


def test_illegal_campaign_state_transition_fails_closed() -> None:
    with pytest.raises(ContractValidationError) as exc:
        validate_campaign_transition(CampaignState.DRAFT, CampaignState.RUNNING)
    assert exc.value.code is FailureCode.INVALID_STATE_TRANSITION

    with pytest.raises(ContractValidationError):
        validate_campaign_transition(CampaignState.CONFIRMED, CampaignState.VALIDATING)


def test_llm_cannot_write_confirmed_campaign_evidence_or_promotion_state() -> None:
    forged_proposer_payload = _draft(proposed_by=Component.OPERATOR).to_dict()
    with pytest.raises(ContractValidationError) as forged_proposer:
        ResearchCampaignDraft.from_payload(forged_proposer_payload, component=Component.LLM_RESEARCHER)
    assert forged_proposer.value.code is FailureCode.INVALID_ARTIFACT_PRODUCER

    campaign_payload = _confirmed().to_dict()
    with pytest.raises(ContractValidationError) as confirmed_by_llm:
        ResearchCampaign.from_payload(campaign_payload, component=Component.LLM_RESEARCHER)
    assert confirmed_by_llm.value.code is FailureCode.INVALID_ARTIFACT_PRODUCER

    draft_payload = _draft().to_dict()
    draft_payload["state"] = CampaignState.CONFIRMED.value
    with pytest.raises(ContractValidationError) as draft_state:
        ResearchCampaignDraft.from_payload(draft_payload, component=Component.LLM_RESEARCHER)
    assert draft_state.value.code is FailureCode.INVALID_STATE

    privileged_payload = _draft().to_dict()
    privileged_payload["promotion"] = {"verdict": "PROMOTE_CHALLENGER"}
    with pytest.raises(ContractValidationError) as privileged:
        ResearchCampaignDraft.from_payload(privileged_payload, component=Component.LLM_RESEARCHER)
    assert privileged.value.code is FailureCode.UNAUTHORIZED_CAPABILITY


def test_quick_exploration_cannot_require_official_evidence_or_promotion() -> None:
    campaign = _confirmed(research_depth=ResearchDepth.QUICK_EXPLORATION)
    task = CampaignTaskPlan(
        task_id="task.quick.bad",
        desk_id="market_structure",
        method_model=MethodModel.DATA_ANOMALY,
        artifact_namespace="campaign.quick.market_structure",
        requires_official_evidence=True,
    )
    with pytest.raises(ContractValidationError) as route_exc:
        CampaignRoute(
            route_id="route.quick.bad",
            campaign_hash=campaign.artifact_hash,
            mode=campaign.mode,
            research_depth=campaign.research_depth,
            selected_desks=("market_structure",),
            method_models=(MethodModel.DATA_ANOMALY,),
            tasks=(task,),
            routed_at=_dt(),
            route_reason="quick exploration only drafts",
        )
    assert route_exc.value.code is FailureCode.UNAUTHORIZED_CAPABILITY

    with pytest.raises(ContractValidationError) as outcome_exc:
        CampaignOutcome(
            outcome_id="outcome.quick.bad",
            campaign_hash=campaign.artifact_hash,
            run_manifest_hash=HASH_D,
            research_depth=ResearchDepth.QUICK_EXPLORATION,
            state=CampaignState.COMPLETED,
            candidate_hashes=(HASH_E,),
            official_evidence_hashes=(HASH_A,),
            summary="Quick exploration must not include official evidence.",
        )
    assert outcome_exc.value.code is FailureCode.UNAUTHORIZED_CAPABILITY


def test_campaign_events_are_separate_from_trial_ledger_and_recovery_dedupes_trials() -> None:
    campaign = _confirmed()
    route = CampaignRoute(
        route_id="route.standard",
        campaign_hash=campaign.artifact_hash,
        mode=campaign.mode,
        research_depth=campaign.research_depth,
        selected_desks=("meta_alpha",),
        method_models=(MethodModel.RESIDUAL_INCREMENTAL,),
        tasks=(
            CampaignTaskPlan(
                task_id="task.meta_alpha",
                desk_id="meta_alpha",
                method_model=MethodModel.RESIDUAL_INCREMENTAL,
                artifact_namespace="campaign.standard.meta_alpha",
                requires_official_evidence=True,
            ),
        ),
        routed_at=_dt(),
        route_reason="portfolio gap search starts with meta-alpha residual analysis",
    )
    manifest = CampaignRunManifest(
        run_id="run.standard",
        campaign_hash=campaign.artifact_hash,
        route_hash=route.artifact_hash,
        state=CampaignState.RUNNING,
        current_checkpoint_id=None,
        campaign_event_log_hash=HASH_D,
        committed_trial_ids=("trial.001",),
        trial_ledger_ref="trial-ledger.standard",
        artifact_hashes={"route": route.artifact_hash},
    )

    with pytest.raises(ContractValidationError) as duplicate:
        require_new_trial(manifest, "trial.001")
    assert duplicate.value.code is FailureCode.INVALID_STATE

    require_new_trial(manifest, "trial.002")

    with pytest.raises(ContractValidationError) as same_ref:
        CampaignRunManifest(
            run_id="run.bad",
            campaign_hash=campaign.artifact_hash,
            route_hash=route.artifact_hash,
            state=CampaignState.RUNNING,
            current_checkpoint_id=None,
            campaign_event_log_hash=HASH_D,
            committed_trial_ids=(),
            trial_ledger_ref=HASH_D,
        )
    assert same_ref.value.code is FailureCode.INVALID_VALUE
