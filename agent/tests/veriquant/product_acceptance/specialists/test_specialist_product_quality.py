from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone

import pytest

from veriquant.contracts import ContractValidationError, FailureCode
from veriquant.specialist_research import (
    BoundedResearchAgentLoop,
    CandidateCritique,
    CandidateFamilyPlan,
    CombinationAxis,
    CritiqueAction,
    ResearchDirectionIdea,
    ResearchExplorationPlan,
    ResearchMechanismIdea,
    ScriptedSpecialistProvider,
    SpecialistCandidateDraft,
    SpecialistResearchBudget,
    SpecialistResearchRequest,
    SpecialistResultKind,
    SpecialistStage,
    SpecialistTurn,
    TurnKind,
    build_fixture_registry,
    get_specialist_profile,
    get_specialist_protocol,
)


HASH = "sha256:" + "7" * 64
FIELDS = ("close", "volume", "market_cap", "industry", "roe", "available_at")


def _plan(specialist_id: str) -> ResearchExplorationPlan:
    axis = (CombinationAxis("window", ("20", "50"), "Compare bounded windows."),)
    return ResearchExplorationPlan(
        specialist_id=specialist_id,
        scope_summary="Explore two distinct mechanisms under a bounded DRAFT-only protocol.",
        directions=(
            ResearchDirectionIdea("direction_a", "underreaction", "price diffusion", ("close",), "net evidence fails"),
            ResearchDirectionIdea("direction_b", "participation", "volume confirmation", ("volume",), "stability fails"),
        ),
        investigation_questions=("Which visible fields and time ranges are available?",),
        stop_conditions=("Budget exhausted", "No safe candidate remains"),
        mechanism_ideas=(
            ResearchMechanismIdea("mechanism_a", "family_a", "underreaction", "past return", "POSITIVE", ("close",), "net evidence fails"),
            ResearchMechanismIdea("mechanism_b", "family_b", "participation", "past volume", "CONDITIONAL", ("volume",), "stability fails"),
        ),
        candidate_family_plans=(
            CandidateFamilyPlan("family_a", ("mechanism_a",), axis, 2),
            CandidateFamilyPlan("family_b", ("mechanism_b",), axis, 2),
        ),
    )


def _candidate(specialist_id: str, index: int) -> SpecialistCandidateDraft:
    expressions = (
        "rank_cross_section(pct_change(close, 20))",
        "zscore_cross_section(pct_change(close, 50))",
        "rank_cross_section(rolling_mean(volume, 20))",
        "zscore_cross_section(rolling_std(close, 20))",
    )
    fields = ("volume",) if index == 2 else ("close",)
    novelty = "A differentiated bounded candidate family."
    if specialist_id == "fundamental":
        novelty += " Explicit cross-domain price proxy; official fundamental validity remains unclaimed."
    return SpecialistCandidateDraft(
        candidate_id=f"candidate_{index}",
        family_id="family_a" if index < 2 else "family_b",
        hypothesis="A visible, lagged proxy may contain falsifiable cross-sectional information.",
        economic_mechanism="Information or participation diffuses with a bounded delay.",
        expected_direction="POSITIVE",
        forecast_horizon_sessions=5,
        lookback_sessions=50 if index == 1 else 20,
        required_fields=fields,
        expression=expressions[index],
        availability_assumptions=("event_time, published_at, available_at and first_tradable_time are no later than decision_time.",),
        neutralization=("industry", "market_cap"),
        falsification_tests=("Cost-adjusted visible evidence is non-positive.",),
        cost_risks=("Turnover may erase gross evidence.",),
        exposure_risks=("Industry and size may dominate.",),
        novelty_explanation=novelty,
    )


def _run(specialist_id: str):
    profile = get_specialist_profile(specialist_id)
    budget = SpecialistResearchBudget.for_depth("STANDARD_RESEARCH")
    request = SpecialistResearchRequest(
        run_id=f"product-{specialist_id}",
        campaign_hash=HASH,
        specialist_id=specialist_id,
        question="研究大 A 市场中的可证伪 Alpha",
        universe="cn-ashare-pit",
        label_horizon_sessions=5,
        decision_time=datetime(2026, 7, 20, tzinfo=timezone.utc),
        available_fields=FIELDS,
        budget=budget,
        seed=7,
    )
    candidates = tuple(_candidate(specialist_id, index) for index in range(4))
    provider = ScriptedSpecialistProvider((
        SpecialistTurn(kind=TurnKind.PLAN, plan=_plan(specialist_id)),
        SpecialistTurn(kind=TurnKind.CANDIDATES, candidates=candidates),
        SpecialistTurn(
            kind=TurnKind.SELF_CRITIQUE,
            critiques=tuple(
                CandidateCritique(item.candidate_id, CritiqueAction.KEEP, ("Cost sensitivity",), "Retain for deterministic preflight.")
                for item in candidates
            ),
        ),
    ))
    return BoundedResearchAgentLoop().run(
        request=request,
        profile=profile,
        provider=provider,
        tools=build_fixture_registry(profile, available_fields=FIELDS),
    )


def test_all_six_specialists_produce_four_drafts_after_program_owned_investigation() -> None:
    specialist_ids = (
        "fundamental", "expectations_events", "market_structure",
        "alternative_data", "macro_regime", "meta_alpha",
    )
    outcomes = tuple(_run(item) for item in specialist_ids)
    assert all(item.status is SpecialistStage.COMPLETED for item in outcomes)
    assert all(item.result_kind is SpecialistResultKind.EVALUATED_RESEARCH_RESULT for item in outcomes)
    assert all(len(item.candidates) == 4 for item in outcomes)
    assert all(len({candidate.family_id for candidate in item.candidates}) == 2 for item in outcomes)
    assert all(item.usage.tool_calls == 4 for item in outcomes)
    assert all(item.usage.revisions <= 1 for item in outcomes)
    assert all(item.failure_code is None for item in outcomes)


def test_depth_budgets_support_two_to_four_families_and_four_to_twelve_candidates() -> None:
    budgets = tuple(SpecialistResearchBudget.for_depth(item) for item in (
        "QUICK_EXPLORATION", "STANDARD_RESEARCH", "STRICT_RESEARCH",
    ))
    assert all(2 <= item.max_candidate_families <= 4 for item in budgets)
    assert all(4 <= item.max_submitted_candidates <= 12 for item in budgets)
    assert all(item.max_revisions <= 1 for item in budgets)


def test_fundamental_price_only_candidate_requires_explicit_cross_domain_disclosure() -> None:
    candidate = _candidate("market_structure", 0)
    candidate = replace(candidate, family_id="quality")
    with pytest.raises(ContractValidationError) as exc:
        get_specialist_protocol("fundamental").validate_candidate(candidate)
    assert exc.value.code is FailureCode.INVALID_FIELD_OWNER


def test_product_result_kinds_are_normal_research_outcomes_not_authority_claims() -> None:
    assert SpecialistResultKind.DRAFT_CANDIDATES.value == "DRAFT_CANDIDATES"
    assert {item.value for item in SpecialistResultKind} >= {
        "EVALUATED_RESEARCH_RESULT",
        "NEGATIVE_RESEARCH_RESULT",
        "DATA_OR_CAPABILITY_GAP_REPORT",
        "PARTIAL_RESEARCH_RESULT",
        "SYSTEM_FAILED",
    }
