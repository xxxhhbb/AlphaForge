from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.veriquant.contracts import Component, ContractValidationError, FailureCode
from src.veriquant.research_campaign.compiler import DeterministicCampaignCompiler
from src.veriquant.research_campaign.models import (
    CampaignState,
    ResearchCampaign,
    ResearchCampaignDraft,
    ResearchMode,
    validate_campaign_transition,
)
from src.veriquant.research_planner.models_v2 import IntentResolution
from src.veriquant.research_planner.semantic import DeterministicResearchIntentCompiler


DT = datetime(2026, 7, 20, 9, 30, tzinfo=timezone.utc)


def _compile(question: str) -> ResearchCampaignDraft:
    result = DeterministicCampaignCompiler().compile_text(question, decision_time=DT)
    assert result.draft is not None
    return result.draft


def _assert_clarification_failure(draft: ResearchCampaignDraft, field: str) -> None:
    with pytest.raises(ContractValidationError) as exc:
        ResearchCampaign.confirm(draft, confirmed_at=DT)
    assert exc.value.code is FailureCode.MISSING_REQUIRED_FIELD
    assert exc.value.details["campaign_failure_code"] == "CAMPAIGN_CLARIFICATION_REQUIRED"
    assert field in exc.value.details["unresolved_fields"]


def test_s22_generic_portfolio_word_does_not_satisfy_portfolio_context() -> None:
    intent = DeterministicResearchIntentCompiler().compile(
        "最近组合回撤主要来自什么",
        decision_time=DT,
    )
    assert intent.mode is ResearchMode.DIAGNOSE_FACTOR_PORTFOLIO
    assert intent.resolution is IntentResolution.NEEDS_CLARIFICATION
    assert intent.portfolio_id is None
    assert intent.existing_factor_ids == ()
    assert any("portfolio_id" in item for item in intent.clarification_questions)

    draft = _compile("最近组合回撤主要来自什么")
    assert draft.unresolved_fields == ("portfolio_or_existing_factor_ids",)
    assert draft.clarification_questions[0].field_name == "portfolio_or_existing_factor_ids"
    _assert_clarification_failure(draft, "portfolio_or_existing_factor_ids")


@pytest.mark.parametrize(
    ("question", "mode", "field_name", "answer"),
    (
        (
            "新闻情绪数据是否值得接入研究系统",
            ResearchMode.NEW_DATA_SOURCE_EVALUATION,
            "data_source_scope_time_semantics",
            "source_id:vendor.news; fields:sentiment,entity; available_at:published_at",
        ),
        (
            "复现一篇论文中的质量因子",
            ResearchMode.PAPER_STRATEGY_REPLICATION,
            "source_document",
            "doi:10.0000/example-quality-factor",
        ),
        (
            "某因子是否适合上线或继续保留",
            ResearchMode.PRELAUNCH_REVIEW,
            "factor_id",
            "quality.v3",
        ),
    ),
)
def test_s18_s23_s24_required_semantics_are_structured_and_resolvable(
    question: str,
    mode: ResearchMode,
    field_name: str,
    answer: str,
) -> None:
    draft = _compile(question)
    assert draft.mode is mode
    assert draft.unresolved_fields == (field_name,)
    assert any(item.field_name == field_name for item in draft.clarification_questions)
    _assert_clarification_failure(draft, field_name)

    resolved = draft.resolve_clarifications({field_name: answer})
    campaign = ResearchCampaign.confirm(resolved, confirmed_at=DT)
    assert campaign.resolution_answers[field_name] == answer
    if field_name == "factor_id":
        assert campaign.subject.existing_factor_ids == (answer,)


@pytest.mark.parametrize(
    ("question", "missing_field"),
    (
        ("帮我找新的股票因子", "forecast_horizons"),
        ("改进这个反转因子降低换手", "parent_factor_id"),
        ("找一个组合缺口 Alpha", "portfolio_or_existing_factor_ids"),
    ),
)
def test_required_research_semantics_must_be_resolved_before_confirmation(
    question: str,
    missing_field: str,
) -> None:
    draft = _compile(question)
    assert missing_field in draft.unresolved_fields
    _assert_clarification_failure(draft, missing_field)


def test_explicit_operator_answers_make_campaign_confirmable() -> None:
    draft = _compile("改进这个反转因子降低换手")
    assert draft.unresolved_fields == ("forecast_horizons", "parent_factor_id")

    resolved = draft.resolve_clarifications(
        {"forecast_horizons": "20d", "parent_factor_id": "reversal.v1"},
        resolved_by=Component.OPERATOR,
    )
    campaign = ResearchCampaign.confirm(resolved, confirmed_at=DT)

    assert campaign.state is CampaignState.CONFIRMED
    assert campaign.subject.horizon == "20d"
    assert campaign.subject.existing_factor_ids == ("reversal.v1",)
    assert campaign.resolution_answers == {
        "forecast_horizons": "20d",
        "parent_factor_id": "reversal.v1",
    }


def test_llm_cannot_resolve_or_write_confirmed_state() -> None:
    draft = _compile("最近组合回撤主要来自什么")
    with pytest.raises(ContractValidationError) as resolver:
        draft.resolve_clarifications(
            {"portfolio_or_existing_factor_ids": "portfolio_id:portfolio.mn.v1"},
            resolved_by=Component.LLM_RESEARCHER,
        )
    assert resolver.value.code is FailureCode.INVALID_ARTIFACT_PRODUCER

    payload = draft.to_dict()
    payload["resolution_answers"] = {
        "portfolio_or_existing_factor_ids": "portfolio_id:portfolio.mn.v1"
    }
    payload["resolved_by"] = Component.OPERATOR.value
    payload["unresolved_fields"] = []
    with pytest.raises(ContractValidationError) as forged:
        ResearchCampaignDraft.from_payload(payload, component=Component.LLM_RESEARCHER)
    assert forged.value.code in {
        FailureCode.INVALID_ARTIFACT_PRODUCER,
        FailureCode.UNAUTHORIZED_CAPABILITY,
    }


def test_illegal_transition_and_hash_stability() -> None:
    with pytest.raises(ContractValidationError) as transition:
        validate_campaign_transition(CampaignState.DRAFT, CampaignState.CONFIRMED)
    assert transition.value.code is FailureCode.INVALID_STATE_TRANSITION

    first = _compile("最近组合回撤主要来自什么")
    second = _compile("最近组合回撤主要来自什么")
    assert first.canonical_json() == second.canonical_json()
    assert first.artifact_hash == second.artifact_hash

    answers = {"portfolio_or_existing_factor_ids": "portfolio_id:portfolio.mn.v1"}
    first_resolved = first.resolve_clarifications(answers)
    second_resolved = second.resolve_clarifications(dict(reversed(tuple(answers.items()))))
    assert first_resolved.canonical_json() == second_resolved.canonical_json()
    assert first_resolved.artifact_hash == second_resolved.artifact_hash


def test_old_payload_without_new_resolution_fields_still_parses() -> None:
    draft = _compile("帮我找新的20日股票因子")
    payload = draft.to_dict()
    payload.pop("clarification_questions")
    payload.pop("unresolved_fields")
    payload.pop("resolution_answers")
    payload.pop("resolved_by")

    parsed = ResearchCampaignDraft.from_payload(payload, component=Component.LLM_RESEARCHER)
    assert parsed.clarification_questions == ()
    assert parsed.unresolved_fields == ()
    assert parsed.resolution_answers == {}
