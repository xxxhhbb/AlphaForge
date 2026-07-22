from __future__ import annotations

from datetime import datetime, timezone
import json

import pytest

from veriquant.contracts import ContractValidationError, FailureCode
from veriquant.specialist_research import (
    CandidateCombination,
    CandidateFamilyPlan,
    CombinationAxis,
    ResearchDirectionIdea,
    ResearchExplorationPlan,
    ResearchMechanismIdea,
    SpecialistAgentBudget,
    SpecialistAgentSpec,
    SpecialistResearchRequest,
    SpecialistProviderContext,
    SpecialistStage,
    build_fixture_registry,
    get_specialist_profile,
    require_transition,
)


def test_milestone_contracts_are_stable_and_cover_family_planning() -> None:
    axis = CombinationAxis(
        axis_id="window",
        values=("20", "50"),
        rationale="Compare bounded time horizons without scanning an unlimited grid.",
    )
    family = CandidateFamilyPlan(
        family_id="momentum",
        mechanism_ids=("underreaction",),
        combination_axes=(axis,),
        max_combinations=2,
    )
    mechanism = ResearchMechanismIdea(
        mechanism_id="underreaction",
        family_id="momentum",
        mechanism="Information diffuses gradually across the cross-section.",
        observable_proxy="Past close-to-close return.",
        predicted_direction="POSITIVE",
        required_fields=("close",),
        falsification="Net evidence is unstable or non-positive after costs.",
    )
    plan = ResearchExplorationPlan(
        specialist_id="market_structure",
        scope_summary="Explore distinct momentum mechanisms and bounded transformations.",
        directions=(
            ResearchDirectionIdea("raw", "underreaction", "raw baseline", ("close",), "net return fails"),
            ResearchDirectionIdea("risk_adjusted", "risk-scaled continuation", "controls volatility", ("close",), "no stability gain"),
        ),
        investigation_questions=("Is the requested history available at decision time?",),
        stop_conditions=("Budget exhausted",),
        mechanism_ideas=(mechanism,),
        candidate_family_plans=(family,),
    )
    assert plan.artifact_hash == ResearchExplorationPlan.from_payload(plan.to_dict()).artifact_hash

    spec = SpecialistAgentSpec(
        specialist_id="market_structure",
        display_name="市场行为与交易可行性研究组",
        mission="研究可交易的价格、成交量、波动和流动性机制。",
        candidate_families=("momentum", "reversal"),
        allowed_tools=("market_structure.query_visible_evidence",),
        protocol_rules=("只读可见证据", "不得访问封闭评审"),
    )
    assert spec.artifact_hash.startswith("sha256:")


def test_budget_presets_and_quick_exploration_authority() -> None:
    quick = SpecialistAgentBudget.for_depth("QUICK_EXPLORATION")
    strict = SpecialistAgentBudget.for_depth("STRICT_RESEARCH")
    assert quick.max_submitted_candidates < strict.max_submitted_candidates
    assert quick.max_revisions == 0
    request = SpecialistResearchRequest(
        run_id="quick-run",
        campaign_hash="sha256:" + "a" * 64,
        specialist_id="market_structure",
        question="快速探索 50 日动量机制",
        universe="cn-ashare-pit",
        label_horizon_sessions=5,
        decision_time=datetime(2026, 7, 20, tzinfo=timezone.utc),
        available_fields=("close",),
        budget=quick,
        research_depth="QUICK_EXPLORATION",
    )
    assert request.official_evidence_allowed is False


def test_normative_state_names_are_explicit_and_fail_closed() -> None:
    require_transition(SpecialistStage.ENUMERATING, SpecialistStage.PREFLIGHT)
    require_transition(SpecialistStage.SUBMITTING, SpecialistStage.AWAITING_EVIDENCE)
    with pytest.raises(ContractValidationError) as exc:
        require_transition(SpecialistStage.AWAITING_EVIDENCE, SpecialistStage.SUBMITTING)
    assert exc.value.code is FailureCode.INVALID_STATE_TRANSITION


def test_candidate_combination_rejects_executable_content() -> None:
    with pytest.raises(ContractValidationError) as exc:
        CandidateCombination(
            combination_id="bad",
            family_id="momentum",
            axis_values={"window": "50"},
            expression="eval('close')",
            required_fields=("close",),
        )
    assert exc.value.code is FailureCode.FORBIDDEN_PROPOSAL_PAYLOAD


def test_atomic_family_can_omit_axes_but_cannot_claim_a_grid() -> None:
    atomic = CandidateFamilyPlan(
        family_id="raw_baseline",
        mechanism_ids=("underreaction",),
        combination_axes=(),
        max_combinations=1,
    )
    assert atomic.max_combinations == 1
    with pytest.raises(ContractValidationError) as exc:
        CandidateFamilyPlan(
            family_id="invalid_grid",
            mechanism_ids=("underreaction",),
            combination_axes=(),
            max_combinations=2,
        )
    assert exc.value.code is FailureCode.INVALID_VALUE


def test_frozen_tool_definitions_are_json_serializable_for_real_provider() -> None:
    from veriquant.specialist_research.provider import _provider_messages, _stage_output_shape
    from veriquant.llm_desks.safety import assert_no_forbidden_content

    profile = get_specialist_profile("market_structure")
    request = SpecialistResearchRequest(
        run_id="provider-json-run",
        campaign_hash="sha256:" + "b" * 64,
        specialist_id="market_structure",
        question="规划 50 日动量研究",
        universe="cn-ashare-pit",
        label_horizon_sessions=5,
        decision_time=datetime(2026, 7, 20, tzinfo=timezone.utc),
        available_fields=("close", "volume"),
        requested_lookback_sessions=50,
    )
    registry = build_fixture_registry(profile, available_fields=request.available_fields)
    messages = _provider_messages(
        SpecialistProviderContext(
            request=request,
            profile=profile,
            stage=SpecialistStage.PLANNING,
            iteration=0,
            plan=None,
            finding_summaries=(),
            candidates=(),
            preflight_feedback=(),
            tool_definitions=registry.definitions,
        )
    )
    material = json.loads(messages[1]["content"])
    assert_no_forbidden_content(messages[0]["content"], where="test.system")
    assert_no_forbidden_content(messages[1]["content"], where="test.user")
    assert len(material["tools"]) == len(profile.required_tool_names)
    assert material["tools"][0]["input_schema"]["additionalProperties"] is False
    alternatives = _stage_output_shape(SpecialistStage.INVESTIGATING)["oneOf"]
    allowed = {"kind", "tool_call", "candidates", "capability_requests"}
    assert all(set(item["properties"]) <= allowed for item in alternatives)
    assert all(item["additionalProperties"] is False for item in alternatives)
    assert all("candidate_shape" not in item for item in alternatives)


def test_provider_prompt_does_not_teach_internal_contract_metadata() -> None:
    from veriquant.specialist_research.provider import _provider_messages

    profile = get_specialist_profile("market_structure")
    request = SpecialistResearchRequest(
        run_id="provider-metadata-run",
        campaign_hash="sha256:" + "c" * 64,
        specialist_id="market_structure",
        question="规划并调查 50 日动量研究",
        universe="cn-ashare-pit",
        label_horizon_sessions=5,
        decision_time=datetime(2026, 7, 20, tzinfo=timezone.utc),
        available_fields=("close", "volume"),
        requested_lookback_sessions=50,
    )
    registry = build_fixture_registry(profile, available_fields=request.available_fields)
    plan = ResearchExplorationPlan(
        specialist_id="market_structure",
        scope_summary="研究动量机制",
        directions=(
            ResearchDirectionIdea(
                direction_id="momentum",
                mechanism="价格延续",
                differentiation="不同回看窗口",
                required_fields=("close",),
                falsification="未来收益不随过去收益排序",
            ),
            ResearchDirectionIdea(
                direction_id="volume_confirmation",
                mechanism="成交量确认价格延续",
                differentiation="区分无量与放量趋势",
                required_fields=("close", "volume"),
                falsification="成交量条件不改善稳定性",
            ),
        ),
        investigation_questions=("价格延续是否稳定？",),
        stop_conditions=("达到预算上限",),
        mechanism_ideas=(
            ResearchMechanismIdea(
                mechanism_id="momentum",
                family_id="momentum_family",
                mechanism="价格延续",
                observable_proxy="过去收益",
                predicted_direction="positive",
                required_fields=("close",),
                falsification="排序失效",
            ),
            ResearchMechanismIdea(
                mechanism_id="volume_confirmation",
                family_id="volume_family",
                mechanism="成交量确认价格延续",
                observable_proxy="过去收益与成交量变化",
                predicted_direction="positive",
                required_fields=("close", "volume"),
                falsification="确认条件失效",
            ),
        ),
        candidate_family_plans=(
            CandidateFamilyPlan(
                family_id="momentum_family",
                mechanism_ids=("momentum",),
                combination_axes=(),
                max_combinations=1,
            ),
            CandidateFamilyPlan(
                family_id="volume_family",
                mechanism_ids=("volume_confirmation",),
                combination_axes=(),
                max_combinations=1,
            ),
        ),
    )
    messages = _provider_messages(
        SpecialistProviderContext(
            request=request,
            profile=profile,
            stage=SpecialistStage.INVESTIGATING,
            iteration=1,
            plan=plan,
            finding_summaries=(),
            candidates=(),
            preflight_feedback=(),
            tool_definitions=registry.definitions,
        )
    )
    material = json.loads(messages[1]["content"])
    serialized = json.dumps(material, ensure_ascii=False)
    assert '"contract_type"' not in serialized
    assert '"schema_version"' not in serialized


def test_tool_call_accepts_only_matching_redundant_research_question() -> None:
    from veriquant.specialist_research.provider import turn_from_mapping

    payload = {
        "kind": "TOOL_CALL",
        "research_question": "价格延续是否稳定？",
        "tool_call": {
            "tool_name": "inspect_visible_fields",
            "arguments": {"query": "close"},
            "research_question": "价格延续是否稳定？",
        },
    }
    turn = turn_from_mapping(payload)
    assert turn.tool_call is not None
    assert turn.tool_call.research_question == "价格延续是否稳定？"

    with pytest.raises(ContractValidationError) as mismatch:
        turn_from_mapping(
            {
                **payload,
                "research_question": "另一个问题",
            }
        )
    assert mismatch.value.code is FailureCode.INVALID_VALUE

    with pytest.raises(ContractValidationError) as wrong_kind:
        turn_from_mapping(
            {
                "kind": "CANDIDATES",
                "research_question": "不应出现在顶层",
                "candidates": [],
            }
        )
    assert wrong_kind.value.code is FailureCode.UNKNOWN_FIELD


def test_provider_forces_candidates_when_tool_budget_is_exhausted() -> None:
    from veriquant.specialist_research.provider import _provider_messages

    profile = get_specialist_profile("market_structure")
    request = SpecialistResearchRequest(
        run_id="provider-budget-run",
        campaign_hash="sha256:" + "d" * 64,
        specialist_id="market_structure",
        question="调查后提交候选",
        universe="cn-ashare-pit",
        label_horizon_sessions=5,
        decision_time=datetime(2026, 7, 20, tzinfo=timezone.utc),
        available_fields=("close", "volume"),
        requested_lookback_sessions=50,
    )
    registry = build_fixture_registry(profile, available_fields=request.available_fields)
    findings = tuple(
        {"tool_name": "inspect_visible_fields", "result_summary": f"finding-{index}"}
        for index in range(request.budget.max_tool_calls)
    )
    messages = _provider_messages(
        SpecialistProviderContext(
            request=request,
            profile=profile,
            stage=SpecialistStage.INVESTIGATING,
            iteration=7,
            plan=None,
            finding_summaries=findings,
            candidates=(),
            preflight_feedback=(),
            tool_definitions=registry.definitions,
        )
    )
    material = json.loads(messages[1]["content"])
    assert material["remaining_limits"]["tool_calls"] == 0
    assert material["allowed_turn_kinds"] == ["CANDIDATES"]
    schema = material["required_output_schema"]
    assert schema["properties"]["kind"]["const"] == "CANDIDATES"
    assert "oneOf" not in schema


def test_provider_reserves_final_iteration_for_candidate_submission() -> None:
    from veriquant.specialist_research.provider import _provider_messages

    profile = get_specialist_profile("market_structure")
    request = SpecialistResearchRequest(
        run_id="provider-final-iteration-run",
        campaign_hash="sha256:" + "e" * 64,
        specialist_id="market_structure",
        question="最后一轮必须提交候选",
        universe="cn-ashare-pit",
        label_horizon_sessions=5,
        decision_time=datetime(2026, 7, 20, tzinfo=timezone.utc),
        available_fields=("close", "volume"),
        requested_lookback_sessions=50,
    )
    registry = build_fixture_registry(profile, available_fields=request.available_fields)
    messages = _provider_messages(
        SpecialistProviderContext(
            request=request,
            profile=profile,
            stage=SpecialistStage.INVESTIGATING,
            iteration=request.budget.max_iterations - 1,
            plan=None,
            finding_summaries=({"result_summary": "one visible finding"},),
            candidates=(),
            preflight_feedback=(),
            tool_definitions=registry.definitions,
        )
    )
    material = json.loads(messages[1]["content"])
    assert material["remaining_limits"]["tool_calls"] > 0
    assert material["allowed_turn_kinds"] == ["CANDIDATES"]
    assert material["required_output_schema"]["properties"]["kind"]["const"] == "CANDIDATES"
