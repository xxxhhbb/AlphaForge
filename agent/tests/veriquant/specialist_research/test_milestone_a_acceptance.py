from __future__ import annotations

from datetime import datetime, timezone

import pytest

from veriquant.contracts import ContractValidationError, FailureCode
from veriquant.evidence import ExperimentIdentity, TrialLedger
from veriquant.specialist_research import (
    BoundedResearchAgentLoop,
    CandidateCombination,
    CandidateCritique,
    CandidateFamilyPlan,
    CombinationAxis,
    CritiqueAction,
    CrossGroupDuplicateDetector,
    DatasetAssessmentStatus,
    MilestoneAAcceptanceGate,
    ResearchDirectionIdea,
    ResearchExplorationPlan,
    ResearchMechanismIdea,
    SafeCombinationExplorer,
    ScriptedSpecialistProvider,
    SpecialistCandidateDraft,
    SpecialistResearchBudget,
    SpecialistResearchRequest,
    SpecialistSubmissionStatus,
    SpecialistTrialSubmissionService,
    SpecialistTurn,
    ToolCallDraft,
    TurnKind,
    build_fixture_registry,
    get_specialist_profile,
    get_specialist_protocol,
)


HASH = "sha256:" + "a" * 64
FIELDS = ("close", "volume", "market_cap", "industry", "roe", "available_at", "shibor_overnight")


def _plan(specialist_id: str) -> ResearchExplorationPlan:
    axes = (CombinationAxis("window", ("20", "50"), "Bounded horizon comparison."),)
    return ResearchExplorationPlan(
        specialist_id=specialist_id,
        scope_summary="Plan distinct mechanisms before investigation and candidate construction.",
        directions=(
            ResearchDirectionIdea("direction_a", "mechanism A", "baseline", ("close",), "net evidence fails"),
            ResearchDirectionIdea("direction_b", "mechanism B", "independent proxy", ("volume",), "stability fails"),
        ),
        investigation_questions=("Are fields available at decision time?",),
        stop_conditions=("Budget exhausted", "No safe expression remains"),
        mechanism_ideas=(
            ResearchMechanismIdea("mechanism_a", "family_a", "underreaction", "past return", "POSITIVE", ("close",), "net evidence fails"),
            ResearchMechanismIdea("mechanism_b", "family_b", "participation", "past volume", "CONDITIONAL", ("volume",), "stability fails"),
        ),
        candidate_family_plans=(
            CandidateFamilyPlan("family_a", ("mechanism_a",), axes, 2),
            CandidateFamilyPlan("family_b", ("mechanism_b",), axes, 2),
        ),
    )


def _candidate(specialist_id: str, suffix: str, family: str, expression: str, fields: tuple[str, ...]) -> SpecialistCandidateDraft:
    novelty = "A separate bounded family rather than a parameter-only duplicate."
    if specialist_id == "fundamental" and set(fields) <= {"open", "high", "low", "close", "volume", "vwap", "amount", "turnover"}:
        novelty += " This is an explicitly declared cross-domain price proxy for a fundamental mechanism."
    return SpecialistCandidateDraft(
        candidate_id=f"candidate_{suffix}",
        family_id=family,
        hypothesis=f"Candidate {suffix} captures a falsifiable cross-sectional mechanism.",
        economic_mechanism="Observable information diffuses with a bounded delay.",
        expected_direction="POSITIVE",
        forecast_horizon_sessions=5,
        lookback_sessions=50,
        required_fields=fields,
        expression=expression,
        availability_assumptions=(
            "event_time, published_at, available_at and first_tradable_time are no later than decision_time.",
        ),
        neutralization=("industry", "market_cap"),
        falsification_tests=("Net evidence is non-positive.",),
        cost_risks=("Turnover may erase gross evidence.",),
        exposure_risks=("Industry and size exposure may dominate.",),
        novelty_explanation=novelty,
    )


def _run(specialist_id: str, *, invalid_second: bool = False):
    profile = get_specialist_profile(specialist_id)
    request = SpecialistResearchRequest(
        run_id=f"milestone-a-{specialist_id}",
        campaign_hash=HASH,
        specialist_id=specialist_id,
        question="研究大 A 市场中可证伪的 50 日 Alpha 机制",
        universe="cn-ashare-pit",
        label_horizon_sessions=5,
        decision_time=datetime(2026, 7, 20, tzinfo=timezone.utc),
        available_fields=FIELDS,
        budget=SpecialistResearchBudget(max_submitted_candidates=6, family_trial_budget=6),
        requested_lookback_sessions=50,
        seed=7,
    )
    first = _candidate(specialist_id, "a", "family_a", "rank_cross_section(pct_change(close, 50))", ("close",))
    second_expression = "unknown_operator(volume, 50)" if invalid_second else "zscore_cross_section(rolling_mean(volume, 50))"
    second = _candidate(specialist_id, "b", "family_b", second_expression, ("volume",))
    tool_name = profile.required_tool_names[0]
    provider = ScriptedSpecialistProvider(
        (
            SpecialistTurn(kind=TurnKind.PLAN, plan=_plan(specialist_id)),
            SpecialistTurn(
                kind=TurnKind.TOOL_CALL,
                tool_call=ToolCallDraft(tool_name, {"query": "visible availability"}, "Can this study run PIT-safely?"),
            ),
            SpecialistTurn(kind=TurnKind.CANDIDATES, candidates=(first, second)),
            SpecialistTurn(
                kind=TurnKind.SELF_CRITIQUE,
                critiques=tuple(
                    CandidateCritique(item.candidate_id, CritiqueAction.KEEP, ("Cost sensitivity",), "Retain for deterministic preflight.")
                    for item in (first, second)
                ),
            ),
        )
    )
    outcome = BoundedResearchAgentLoop().run(
        request=request,
        profile=profile,
        provider=provider,
        tools=build_fixture_registry(profile, available_fields=FIELDS),
    )
    return request, outcome


def test_all_six_specialists_independently_pass_milestone_a_gate() -> None:
    outcomes = {specialist_id: _run(specialist_id)[1] for specialist_id in (
        "fundamental", "expectations_events", "market_structure",
        "alternative_data", "macro_regime", "meta_alpha",
    )}
    report = MilestoneAAcceptanceGate().evaluate(outcomes)
    replay = MilestoneAAcceptanceGate().evaluate(outcomes)
    assert report.passed is True
    assert report.artifact_hash == replay.artifact_hash
    assert all(item.family_count == 2 for item in report.results.values())
    assert all(item.preflight_count == 2 for item in report.results.values())


def test_alternative_data_protocol_emits_dataset_assessment_before_eligibility() -> None:
    protocol = get_specialist_protocol("alternative_data")
    incomplete = protocol.assess_alternative_dataset(
        dataset_id="news-source-a",
        provenance_hash=HASH,
        published_at_known=True,
        coverage_sufficient=False,
        entity_mapping_validated=True,
        pit_validated=False,
        findings=("Coverage and PIT semantics require revision.",),
    )
    assert incomplete.status is DatasetAssessmentStatus.AUXILIARY_ONLY


def test_safe_combinations_are_bounded_stable_and_pre_submit_duplicates_are_free() -> None:
    request, _ = _run("market_structure")
    first = CandidateCombination("c1", "family_a", {"window": "50"}, "rank_cross_section(pct_change(close, 50))", ("close",))
    duplicate = CandidateCombination("c2", "family_a", {"window": "50"}, " rank_cross_section( pct_change(close, 50) ) ", ("close",))
    expanded = SafeCombinationExplorer().expand(request=request, combinations=(duplicate, first))
    assert [item.combination_id for item in expanded] == ["c1"]

    detector = CrossGroupDuplicateDetector()
    assert detector.register(group_id="g1", candidate_id="c1", identity_hash=HASH, already_submitted=False) is None
    decision = detector.register(group_id="g2", candidate_id="c2", identity_hash=HASH, already_submitted=False)
    assert decision is not None and decision.consume_trial is False
    charged = detector.register(group_id="g3", candidate_id="c3", identity_hash=HASH, already_submitted=True)
    assert charged is not None and charged.consume_trial is True


def _identity(candidate_id: str, factor_hash: str) -> ExperimentIdentity:
    return ExperimentIdentity(
        experiment_id=f"experiment-{candidate_id}",
        dataset_hash=HASH,
        universe_hash="sha256:" + "b" * 64,
        factor_hash=factor_hash,
        split_hash="sha256:" + "c" * 64,
        label_hash="sha256:" + "d" * 64,
        cost_hash="sha256:" + "e" * 64,
        code_hash="sha256:" + "f" * 64,
        availability_hash="sha256:" + "1" * 64,
        seed=7,
    )


def test_every_candidate_reserves_trial_and_resume_is_idempotent(tmp_path) -> None:
    request, outcome = _run("market_structure", invalid_second=True)
    preflight = {item.candidate_id: item for item in outcome.preflight_results}
    identities = {
        candidate.candidate_id: _identity(
            candidate.candidate_id,
            preflight[candidate.candidate_id].factor_dag_hash or candidate.artifact_hash,
        )
        for candidate in outcome.candidates
    }
    ledger = TrialLedger(tmp_path / "trials.sqlite")
    service = SpecialistTrialSubmissionService(ledger)
    first = service.submit(
        outcome=outcome,
        identities=identities,
        family_trial_budget=request.budget.family_trial_budget,
    )
    sequence = ledger.replay().last_sequence
    second = service.submit(
        outcome=outcome,
        identities=identities,
        family_trial_budget=request.budget.family_trial_budget,
    )
    assert {item.status for item in first} == {
        SpecialistSubmissionStatus.READY_FOR_EVIDENCE,
        SpecialistSubmissionStatus.COMPILE_REJECTED,
    }
    assert ledger.replay().last_sequence == sequence
    assert any(item.status is SpecialistSubmissionStatus.REPLAYED for item in second)
    assert sum(item.consumed for item in ledger.replay().families.values()) == 2

    with pytest.raises(ContractValidationError) as exc:
        service.submit(
            outcome=outcome,
            identities=identities,
            family_trial_budget=request.budget.family_trial_budget,
            research_depth="QUICK_EXPLORATION",
        )
    assert exc.value.code is FailureCode.UNAUTHORIZED_CAPABILITY
