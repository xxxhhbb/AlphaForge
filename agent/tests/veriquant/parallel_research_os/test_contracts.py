from __future__ import annotations

from dataclasses import replace

import pytest

from veriquant.contracts import ContractValidationError
from veriquant.research_campaign import MethodModel, ResearchDepth, ResearchMode
from veriquant.research_os import (
    AnswerLevel,
    AnswerSection,
    CandidateValidationRequest,
    DataNeed,
    DataRequirementPlan,
    ResearchGroupAssignment,
    ResearchGroupSubmission,
    ResearchIntent,
    ResearchQuestionAssessment,
    ResearchRoutePlan,
)


H = "sha256:" + "a" * 64


def test_any_question_can_have_a_scoped_non_official_answer() -> None:
    intent = ResearchIntent(
        question="北向资金是否增强50日动量？",
        mode=ResearchMode.VALIDATE_IDEA,
        depth=ResearchDepth.STANDARD_RESEARCH,
        factor_lookback_sessions=50,
        label_horizon_sessions=5,
        requested_outputs=("formal_report",),
    )
    assessment = ResearchQuestionAssessment(
        intent=intent,
        answerable_levels=(AnswerLevel.CONCEPTUAL, AnswerLevel.SOURCED_RESEARCH),
        unavailable_official_reasons=("northbound PIT feed is not approved",),
    )
    assert assessment.intent.factor_lookback_sessions == 50
    assert AnswerLevel.OFFICIAL_EVALUATED not in assessment.answerable_levels


def test_missing_official_data_does_not_allow_silent_official_claim() -> None:
    intent = ResearchIntent(
        question="测试", mode=ResearchMode.NEW_ALPHA_EXPLORATION,
        depth=ResearchDepth.STANDARD_RESEARCH, factor_lookback_sessions=20,
        label_horizon_sessions=5, requested_outputs=("report",),
    )
    with pytest.raises(ContractValidationError):
        ResearchQuestionAssessment(intent=intent, answerable_levels=(AnswerLevel.CONCEPTUAL,))


def test_data_plan_is_derived_from_full_protocol_not_only_factor_window() -> None:
    plan = DataRequirementPlan(
        universe=("CN_A_PIT",), factor_lookback_sessions=50, label_horizon_sessions=5,
        train_sessions=120, validation_sessions=60, embargo_sessions=5, buffer_sessions=20,
        minimum_total_sessions=260, minimum_symbol_count=30,
        needs=(DataNeed("close", True), DataNeed("northbound_flow", False, acceptable_proxies=("volume",))),
    )
    assert plan.minimum_total_sessions >= 260
    with pytest.raises(ContractValidationError):
        replace(plan, minimum_total_sessions=50)


def test_route_selects_one_to_three_groups_and_is_stable() -> None:
    assignment = ResearchGroupAssignment(
        group_id="market_structure", role="primary", score=0.91,
        reasons=("momentum microstructure",), tool_whitelist=("read_market_data",), candidate_budget=2,
    )
    route = ResearchRoutePlan(
        campaign_hash=H, assignments=(assignment,), method_models=(MethodModel.HYPOTHESIS_DRIVEN,),
        capability_requirements=(),
    )
    assert route.artifact_hash == route.artifact_hash
    with pytest.raises(ContractValidationError):
        replace(route, assignments=(assignment,) * 4)


def test_research_group_cannot_claim_official_evidence() -> None:
    with pytest.raises(ContractValidationError):
        ResearchGroupSubmission(
            campaign_hash=H, group_id="meta_alpha", hypothesis_hash=H,
            candidate_draft_hashes=(H,), answer_level=AnswerLevel.OFFICIAL_EVALUATED,
        )


def test_each_group_submission_has_bounded_candidates() -> None:
    with pytest.raises(ContractValidationError):
        ResearchGroupSubmission(
            campaign_hash=H, group_id="meta_alpha", hypothesis_hash=H,
            candidate_draft_hashes=(), answer_level=AnswerLevel.CONCEPTUAL,
        )


def test_llm_cannot_author_official_report_section() -> None:
    with pytest.raises(ContractValidationError):
        AnswerSection(title="结论", level=AnswerLevel.OFFICIAL_EVALUATED, authority="llm", content="通过")


def test_non_official_section_must_label_limitations() -> None:
    with pytest.raises(ContractValidationError):
        AnswerSection(title="调查", level=AnswerLevel.SOURCED_RESEARCH, authority="research_group", content="发现")


def test_candidate_validation_request_binds_group_and_protocol() -> None:
    request = CandidateValidationRequest(
        campaign_hash=H, group_id="market_structure", candidate_draft_hash=H,
        data_plan_hash=H, protocol_hash=H, family_id="family_momentum_50",
    )
    assert request.group_id == "market_structure"
