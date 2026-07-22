from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.veriquant.contracts import Component, ContractValidationError, FailureCode
from src.veriquant.research_campaign import (
    CampaignBudget,
    CampaignDecisionAggregator,
    CandidateDraftSource,
    CandidateSelectionBasis,
    CandidateSelectionInput,
    CandidateSelectionPolicy,
    CandidateSet,
    DatasetAssessment,
    DiagnosticReport,
    EventStudyReport,
    FactorComparisonReport,
    InteractionStyle,
    MethodModel,
    MonitoringRecommendation,
    PortfolioGapReport,
    PrelaunchReview,
    ReplicationReport,
    ResearchCampaign,
    ResearchCampaignDraft,
    ResearchDepth,
    ResearchMode,
    ResearchSubject,
    CampaignState,
    SelectionMetricBundle,
    StopCondition,
    UserCheckpoint,
    ValidationConclusion,
)
from src.veriquant.research_campaign.runtime import CampaignRuntimeManifest, CampaignRuntimeStatus


HASH_A = "sha256:" + "a" * 64
HASH_B = "sha256:" + "b" * 64
HASH_C = "sha256:" + "c" * 64
HASH_D = "sha256:" + "d" * 64
HASH_E = "sha256:" + "e" * 64
HASH_F = "sha256:" + "f" * 64


MODE_METHODS = {
    ResearchMode.NEW_ALPHA_EXPLORATION: (MethodModel.HYPOTHESIS_DRIVEN,),
    ResearchMode.VALIDATE_IDEA: (MethodModel.HYPOTHESIS_DRIVEN,),
    ResearchMode.IMPROVE_EXISTING_FACTOR: (MethodModel.BOUNDED_EVOLUTION,),
    ResearchMode.PORTFOLIO_GAP_SEARCH: (MethodModel.RESIDUAL_INCREMENTAL,),
    ResearchMode.DIAGNOSE_FACTOR_PORTFOLIO: (MethodModel.DIAGNOSTIC_ATTRIBUTION,),
    ResearchMode.EVENT_MARKET_TOPIC: (MethodModel.EVENT_STUDY,),
    ResearchMode.NEW_DATA_SOURCE_EVALUATION: (MethodModel.DATA_ANOMALY,),
    ResearchMode.PAPER_STRATEGY_REPLICATION: (MethodModel.REPLICATION,),
    ResearchMode.PRELAUNCH_REVIEW: (MethodModel.DIAGNOSTIC_ATTRIBUTION,),
    ResearchMode.MONITOR_DECAY_RETIREMENT: (MethodModel.ONLINE_MONITORING,),
}


def _dt(hour: int = 9) -> datetime:
    return datetime(2026, 7, 19, hour, 30, tzinfo=timezone.utc)


def _campaign(
    mode: ResearchMode,
    *,
    research_depth: ResearchDepth = ResearchDepth.STANDARD_RESEARCH,
    existing_factor_ids: tuple[str, ...] = (),
    portfolio_id: str | None = None,
) -> ResearchCampaign:
    # These fixture identities are explicit inputs to the confirmed Campaign;
    # they are not inferred by production code or silently filled at confirm.
    if not existing_factor_ids and mode in {
        ResearchMode.DIAGNOSE_FACTOR_PORTFOLIO,
        ResearchMode.PRELAUNCH_REVIEW,
        ResearchMode.MONITOR_DECAY_RETIREMENT,
    }:
        existing_factor_ids = ("registered.factor.v1",)
    subject = ResearchSubject(
        subject_id=f"subject.{mode.value.lower()}",
        description=f"Aggregation test subject for {mode.value}.",
        universe=("CN_ASHARE_APPROVED_PIT_SAMPLE",),
        horizon="20d",
        decision_time=_dt(),
        allowed_data=("price_volume", "fundamentals"),
        existing_factor_ids=existing_factor_ids,
        portfolio_id=portfolio_id,
    )
    draft = ResearchCampaignDraft(
        campaign_id=f"campaign.{mode.value.lower()}",
        question=f"Aggregation test for {mode.value}",
        mode=mode,
        research_depth=research_depth,
        interaction_style=InteractionStyle.STANDARD,
        subject=subject,
        method_models=MODE_METHODS[mode],
        candidate_agents=("fundamental", "market_structure", "meta_alpha"),
        budget=CampaignBudget(max_factor_families=3, max_trials=9, max_llm_calls=9, max_runtime_seconds=300),
        user_checkpoints=(UserCheckpoint(checkpoint_id="checkpoint.after_brief", stage="after_research_brief"),),
        stop_conditions=(StopCondition.BUDGET_EXHAUSTED, StopCondition.VALIDATION_COMPLETED),
        data_manifest_hash=HASH_A,
        policy_hash=HASH_B,
        code_hash=HASH_C,
        created_at=_dt(),
        proposed_by=Component.LLM_RESEARCHER,
        state=CampaignState.NEEDS_CONFIRMATION,
    )
    return ResearchCampaign.confirm(draft, confirmed_at=_dt(10))


def _manifest(campaign: ResearchCampaign) -> CampaignRuntimeManifest:
    return CampaignRuntimeManifest(
        run_id=f"runtime.{campaign.mode.value.lower()}",
        campaign_hash=campaign.artifact_hash,
        route_hash=HASH_D,
        protocol_plan_hash=HASH_E,
        runtime_version="veriquant.campaign-runtime/v1",
        status=CampaignRuntimeStatus.COMPLETED,
        branch_results=(),
        artifact_hashes={"runtime.branch": HASH_F},
    )


def _visible_metrics(*, rank_ic_only: bool = False) -> dict[str, float]:
    if rank_ic_only:
        return {"rank_ic": 0.08}
    return {
        "pearson_ic": 0.04,
        "rank_ic": 0.05,
        "ic_mean": 0.04,
        "ic_std": 0.02,
        "icir": 1.20,
        "quantile_spread": 0.03,
        "turnover": 0.30,
        "gross_return": 0.06,
        "net_return": 0.04,
        "spread_cost": 0.01,
        "linear_impact_cost": 0.01,
        "max_drawdown": 0.10,
        "subperiod_positive_fraction": 0.80,
        "subperiod_min_ic": 0.01,
        "regime_positive_fraction": 0.75,
        "regime_min_ic": 0.01,
        "max_registered_abs_correlation": 0.35,
        "incremental_ic_mean": 0.03,
        "observation_count": 120.0,
        "period_count": 24.0,
    }


def _metrics(
    *,
    net_icir: float = 1.2,
    stability: float = 0.8,
    cost_capacity: float = 0.8,
    novelty: float = 0.7,
    incrementality: float = 0.6,
    family_budget_remaining: int = 2,
) -> SelectionMetricBundle:
    return SelectionMetricBundle(
        net_icir=net_icir,
        stability=stability,
        cost_capacity=cost_capacity,
        novelty=novelty,
        incrementality=incrementality,
        family_budget_remaining=family_budget_remaining,
        family_budget_consumed=1,
        visible_train_evidence_hash=HASH_A,
        visible_validation_evidence_hash=HASH_B,
        visible_train_metrics=_visible_metrics(),
        visible_validation_metrics=_visible_metrics(),
    )


def _candidate(
    candidate_id: str = "candidate.good",
    *,
    candidate_hash: str = HASH_C,
    draft_source: CandidateDraftSource = CandidateDraftSource.RESEARCH_DESK,
    selection_basis: CandidateSelectionBasis = CandidateSelectionBasis.DETERMINISTIC_VISIBLE_EVIDENCE,
    pit_validated: bool = True,
    official_evidence_hash: str | None = HASH_D,
    critic_passed: bool = True,
    selection_metrics: SelectionMetricBundle | None = None,
) -> CandidateSelectionInput:
    return CandidateSelectionInput(
        candidate_id=candidate_id,
        candidate_hash=candidate_hash,
        family_id="family.test",
        proposer_id="desk.fundamental",
        draft_source=draft_source,
        source_component=Component.RESEARCH_DESK,
        selection_basis=selection_basis,
        pit_validated=pit_validated,
        official_evidence_hash=official_evidence_hash,
        critic_passed=critic_passed,
        critic_finding_hashes=(HASH_E,) if critic_passed else (),
        critic_reason_codes=("PIT_PASS", "STATISTICS_PASS", "COST_CAPACITY_PASS", "INCREMENTALITY_PASS")
        if critic_passed
        else (),
        selection_metrics=selection_metrics if selection_metrics is not None else _metrics(),
    )


def test_mode_specific_aggregation_outputs_distinct_artifacts() -> None:
    expected = {
        ResearchMode.NEW_ALPHA_EXPLORATION: CandidateSet,
        ResearchMode.VALIDATE_IDEA: ValidationConclusion,
        ResearchMode.IMPROVE_EXISTING_FACTOR: FactorComparisonReport,
        ResearchMode.PORTFOLIO_GAP_SEARCH: PortfolioGapReport,
        ResearchMode.DIAGNOSE_FACTOR_PORTFOLIO: DiagnosticReport,
        ResearchMode.EVENT_MARKET_TOPIC: EventStudyReport,
        ResearchMode.NEW_DATA_SOURCE_EVALUATION: DatasetAssessment,
        ResearchMode.PAPER_STRATEGY_REPLICATION: ReplicationReport,
        ResearchMode.PRELAUNCH_REVIEW: PrelaunchReview,
        ResearchMode.MONITOR_DECAY_RETIREMENT: MonitoringRecommendation,
    }
    aggregator = CampaignDecisionAggregator()

    for mode, expected_type in expected.items():
        campaign = _campaign(
            mode,
            existing_factor_ids=("factor.parent",) if mode is ResearchMode.IMPROVE_EXISTING_FACTOR else (),
            portfolio_id="portfolio.mn.v1" if mode is ResearchMode.PORTFOLIO_GAP_SEARCH else None,
        )
        artifact = aggregator.aggregate(
            campaign=campaign,
            runtime_manifest=_manifest(campaign),
            candidates=(_candidate(),),
            generated_at=_dt(12),
        )
        assert isinstance(artifact, expected_type)


def test_candidate_set_only_selects_pit_evidence_critic_visible_candidates() -> None:
    campaign = _campaign(ResearchMode.NEW_ALPHA_EXPLORATION)
    good = _candidate("candidate.good", candidate_hash=HASH_C)
    weak = _candidate("candidate.weak", candidate_hash=HASH_D, selection_metrics=_metrics(net_icir=-0.2))
    no_budget = _candidate(
        "candidate.no_budget",
        candidate_hash=HASH_E,
        selection_metrics=_metrics(family_budget_remaining=0),
    )

    artifact = CampaignDecisionAggregator().aggregate(
        campaign=campaign,
        runtime_manifest=_manifest(campaign),
        candidates=(weak, no_budget, good),
        generated_at=_dt(12),
    )

    assert isinstance(artifact, CandidateSet)
    assert tuple(record.candidate_id for record in artifact.selected_candidates) == ("candidate.good",)
    assert artifact.promotion_candidate_hashes == (good.candidate_hash,)
    rejected_reasons = {record.candidate_id: set(record.reason_codes) for record in artifact.rejected_candidates}
    assert "NET_ICIR_BELOW_POLICY" in rejected_reasons["candidate.weak"]
    assert "FAMILY_BUDGET_EXHAUSTED" in rejected_reasons["candidate.no_budget"]


def test_quick_exploration_cannot_aggregate_official_evidence_or_promotion() -> None:
    campaign = _campaign(ResearchMode.NEW_ALPHA_EXPLORATION, research_depth=ResearchDepth.QUICK_EXPLORATION)

    with pytest.raises(ContractValidationError) as exc:
        CampaignDecisionAggregator().aggregate(
            campaign=campaign,
            runtime_manifest=_manifest(campaign),
            candidates=(_candidate(),),
            generated_at=_dt(12),
        )

    assert exc.value.code is FailureCode.UNAUTHORIZED_CAPABILITY


def test_diagnostic_report_cannot_directly_produce_challenger() -> None:
    campaign = _campaign(ResearchMode.DIAGNOSE_FACTOR_PORTFOLIO)
    artifact = CampaignDecisionAggregator().aggregate(
        campaign=campaign,
        runtime_manifest=_manifest(campaign),
        candidates=(_candidate(),),
        generated_at=_dt(12),
    )

    assert isinstance(artifact, DiagnosticReport)
    assert "CHALLENGER" not in artifact.canonical_json()

    payload = artifact.to_dict()
    payload["recommendation"] = "create CHALLENGER immediately"
    with pytest.raises(ContractValidationError) as exc:
        DiagnosticReport.from_payload(payload)
    assert exc.value.code is FailureCode.UNAUTHORIZED_CAPABILITY


def test_document_or_news_citation_only_cannot_enter_promotion_queue() -> None:
    campaign = _campaign(ResearchMode.NEW_ALPHA_EXPLORATION)
    document_candidate = _candidate(
        "candidate.document",
        candidate_hash=HASH_D,
        draft_source=CandidateDraftSource.DOCUMENT,
        selection_basis=CandidateSelectionBasis.SOURCE_CITATION_ONLY,
        pit_validated=False,
        official_evidence_hash=None,
        critic_passed=False,
        selection_metrics=None,
    )
    news_candidate = _candidate(
        "candidate.news",
        candidate_hash=HASH_E,
        draft_source=CandidateDraftSource.NEWS,
        selection_basis=CandidateSelectionBasis.SOURCE_CITATION_ONLY,
        pit_validated=False,
        official_evidence_hash=None,
        critic_passed=False,
        selection_metrics=None,
    )

    artifact = CampaignDecisionAggregator().aggregate(
        campaign=campaign,
        runtime_manifest=_manifest(campaign),
        candidates=(document_candidate, news_candidate),
        generated_at=_dt(12),
    )

    assert isinstance(artifact, CandidateSet)
    assert artifact.selected_candidates == ()
    assert artifact.promotion_candidate_hashes == ()
    assert {record.candidate_id for record in artifact.rejected_candidates} == {
        "candidate.document",
        "candidate.news",
    }


def test_llm_ranking_and_rankic_only_selection_fail_closed() -> None:
    with pytest.raises(ContractValidationError) as exc:
        _candidate(selection_basis=CandidateSelectionBasis.LLM_RANKING)
    assert exc.value.code is FailureCode.UNAUTHORIZED_CAPABILITY

    with pytest.raises(ContractValidationError) as exc:
        SelectionMetricBundle(
            net_icir=1.0,
            stability=0.8,
            cost_capacity=0.8,
            novelty=0.8,
            incrementality=0.8,
            family_budget_remaining=1,
            family_budget_consumed=1,
            visible_train_evidence_hash=HASH_A,
            visible_validation_evidence_hash=HASH_B,
            visible_train_metrics=_visible_metrics(rank_ic_only=True),
            visible_validation_metrics=_visible_metrics(rank_ic_only=True),
        )
    assert exc.value.code is FailureCode.UNAUTHORIZED_CAPABILITY


def test_selection_policy_must_use_all_required_factors() -> None:
    with pytest.raises(ContractValidationError) as exc:
        CandidateSelectionPolicy(net_icir_weight=0.0, family_budget_weight=0.35)
    assert exc.value.code is FailureCode.UNAUTHORIZED_CAPABILITY


def test_candidate_set_hash_is_stable_under_input_order() -> None:
    campaign = _campaign(ResearchMode.NEW_ALPHA_EXPLORATION)
    first = _candidate("candidate.a", candidate_hash=HASH_C, selection_metrics=_metrics(net_icir=1.0))
    second = _candidate("candidate.b", candidate_hash=HASH_D, selection_metrics=_metrics(net_icir=1.1))
    manifest = _manifest(campaign)
    aggregator = CampaignDecisionAggregator()

    left = aggregator.aggregate(
        campaign=campaign,
        runtime_manifest=manifest,
        candidates=(first, second),
        generated_at=_dt(12),
    )
    right = aggregator.aggregate(
        campaign=campaign,
        runtime_manifest=manifest,
        candidates=(second, first),
        generated_at=_dt(12),
    )

    assert left.artifact_hash == right.artifact_hash
    assert tuple(record.candidate_id for record in left.selected_candidates) == ("candidate.b", "candidate.a")


def test_non_candidate_reports_reject_direct_promotion_payloads() -> None:
    campaign = _campaign(ResearchMode.DIAGNOSE_FACTOR_PORTFOLIO)
    artifact = CampaignDecisionAggregator().aggregate(
        campaign=campaign,
        runtime_manifest=_manifest(campaign),
        candidates=(),
        generated_at=_dt(12),
    )
    payload = artifact.to_dict()
    payload["promotion_candidate_hashes"] = (HASH_A,)

    with pytest.raises(ContractValidationError) as exc:
        DiagnosticReport.from_payload(payload)

    assert exc.value.code is FailureCode.UNKNOWN_FIELD
