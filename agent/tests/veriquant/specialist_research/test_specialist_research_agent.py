from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from veriquant.contracts import ContractValidationError, FailureCode
from veriquant.specialist_research import (
    CandidateCritique,
    CandidateFamilyPlan,
    CritiqueAction,
    FactorLanguageCapabilityRequest,
    LegacyGroupProposalAdapter,
    PreflightStatus,
    RedactedMachineFeedback,
    ResearchDirectionIdea,
    ResearchExplorationPlan,
    ResearchMechanismIdea,
    SPECIALIST_PROFILES,
    ScriptedSpecialistProvider,
    SpecialistAgentOutcome,
    SpecialistCandidateDraft,
    SpecialistResultKind,
    SpecialistCheckpointStore,
    SpecialistResearchAgent,
    BoundedResearchAgentLoop,
    SpecialistResearchBudget,
    SpecialistResearchRequest,
    SpecialistStage,
    SpecialistTurn,
    ToolCallDraft,
    TurnKind,
    build_fixture_registry,
    get_specialist_profile,
    require_transition,
    turn_from_mapping,
)


def test_checkpoint_roots_initialize_concurrently_without_false_path_escape(tmp_path):
    root = tmp_path / "not-created-yet" / "research_runs"

    def create(index: int):
        return SpecialistCheckpointStore(
            root,
            run_id="concurrent_run",
            specialist_id=f"specialist_{index}",
        ).directory

    with ThreadPoolExecutor(max_workers=6) as executor:
        directories = tuple(executor.map(create, range(6)))

    assert len(set(directories)) == 6
    assert all(path.is_dir() and root.resolve() in path.parents for path in directories)


def test_checkpoint_path_traversal_still_fails_closed(tmp_path):
    with pytest.raises(ContractValidationError) as error:
        SpecialistCheckpointStore(tmp_path / "owned", run_id="../escape", specialist_id="researcher")
    assert error.value.code is FailureCode.UNAUTHORIZED_CAPABILITY


CAMPAIGN_HASH = "sha256:" + "a" * 64


def request(
    specialist_id: str = "market_structure",
    *,
    budget: SpecialistResearchBudget | None = None,
) -> SpecialistResearchRequest:
    return SpecialistResearchRequest(
        run_id="single-agent-run-7",
        campaign_hash=CAMPAIGN_HASH,
        specialist_id=specialist_id,
        question="挖掘最近 50 日大 A 动量相关 Alpha",
        universe="cn-ashare-pit",
        label_horizon_sessions=5,
        decision_time=datetime(2026, 7, 20, 8, 0, tzinfo=timezone.utc),
        available_fields=("close", "volume", "market_cap", "roe", "available_at", "shibor_overnight"),
        requested_lookback_sessions=50,
        budget=budget or SpecialistResearchBudget(),
        seed=7,
    )


def plan(specialist_id: str = "market_structure") -> ResearchExplorationPlan:
    return ResearchExplorationPlan(
        specialist_id=specialist_id,
        scope_summary="从不同市场行为机制探索 50 日信号，而不是只扫描窗口。",
        directions=(
            ResearchDirectionIdea(
                direction_id="raw_momentum",
                mechanism="投资者反应不足形成中期延续",
                differentiation="原始价格延续基线",
                required_fields=("close",),
                falsification="成本后表现为负或跨时期不稳定",
            ),
            ResearchDirectionIdea(
                direction_id="volume_confirmation",
                mechanism="成交量确认信息扩散强度",
                differentiation="区分无量上涨和放量上涨",
                required_fields=("close", "volume"),
                falsification="成交量确认不增加稳定性",
            ),
            ResearchDirectionIdea(
                direction_id="risk_adjusted",
                mechanism="单位风险上的价格延续",
                differentiation="控制高波动股票依赖",
                required_fields=("close",),
                falsification="风险调整后不优于简单基线",
            ),
        ),
        investigation_questions=("当前字段和时间覆盖是否足够？", "已有因子是否高度相似？"),
        stop_conditions=("研究预算耗尽", "没有新的实质机制", "没有可安全表达的候选"),
    )


def strict_plan(specialist_id: str = "market_structure") -> ResearchExplorationPlan:
    base = plan(specialist_id)
    mechanisms = (
        ResearchMechanismIdea(
            mechanism_id="underreaction",
            family_id="momentum",
            mechanism="投资者反应不足形成价格延续",
            observable_proxy="过去收益",
            predicted_direction="POSITIVE",
            required_fields=("close",),
            falsification="成本后表现不稳定",
        ),
        ResearchMechanismIdea(
            mechanism_id="volume_confirmation",
            family_id="volume_momentum",
            mechanism="成交量确认信息扩散",
            observable_proxy="价格与成交量变化",
            predicted_direction="POSITIVE",
            required_fields=("close", "volume"),
            falsification="成交量条件没有增量",
        ),
    )
    return ResearchExplorationPlan(
        specialist_id=base.specialist_id,
        scope_summary=base.scope_summary,
        directions=base.directions,
        investigation_questions=base.investigation_questions,
        stop_conditions=base.stop_conditions,
        mechanism_ideas=mechanisms,
        candidate_family_plans=tuple(
            CandidateFamilyPlan(
                family_id=item.family_id,
                mechanism_ids=(item.mechanism_id,),
                combination_axes=(),
                max_combinations=1,
            )
            for item in mechanisms
        ),
    )


def candidate(
    candidate_id: str = "momentum_volume_50",
    *,
    expression: str = "rank_cross_section(subtract(pct_change(close, 50), pct_change(volume, 5)))",
    required_fields: tuple[str, ...] = ("close", "volume"),
    parent_candidate_id: str | None = None,
    revision_number: int = 0,
) -> SpecialistCandidateDraft:
    return SpecialistCandidateDraft(
        candidate_id=candidate_id,
        family_id="momentum_confirmation",
        hypothesis="中期价格延续与成交量变化的差异包含横截面信息。",
        economic_mechanism="价格延续反映反应不足，成交量变化用于区分拥挤交易。",
        expected_direction="POSITIVE",
        forecast_horizon_sessions=5,
        lookback_sessions=50,
        required_fields=required_fields,
        expression=expression,
        availability_assumptions=("价格和成交量在决策时点前已冻结",),
        neutralization=("industry", "market_cap"),
        falsification_tests=("成本后收益为负", "子时期符号不稳定"),
        cost_risks=("小盘股冲击成本",),
        exposure_risks=("行业和市值暴露",),
        novelty_explanation="不是固定模板；组合了中期价格延续与短期成交量变化。",
        parent_candidate_id=parent_candidate_id,
        revision_number=revision_number,
    )


def tool_turn(specialist_id: str = "market_structure") -> SpecialistTurn:
    return SpecialistTurn(
        kind=TurnKind.TOOL_CALL,
        tool_call=ToolCallDraft(
            tool_name=f"{specialist_id}.describe_available_data",
            arguments={"query": "检查 50 日价格成交量覆盖"},
            research_question="数据是否覆盖所需窗口？",
        ),
    )


def critique_turn(*ids: str) -> SpecialistTurn:
    return SpecialistTurn(
        kind=TurnKind.SELF_CRITIQUE,
        critiques=tuple(
            CandidateCritique(
                candidate_id=value,
                action=CritiqueAction.KEEP,
                weaknesses=("可能存在行业和市值暴露",),
                rationale="机制与其他候选不同，保留给机器预检。",
            )
            for value in ids
        ),
    )


def run_script(turns: tuple[SpecialistTurn, ...], *, req: SpecialistResearchRequest | None = None, store=None):
    req = req or request()
    profile = get_specialist_profile(req.specialist_id)
    tools = build_fixture_registry(profile, available_fields=req.available_fields)
    return SpecialistResearchAgent().run(
        request=req,
        profile=profile,
        provider=ScriptedSpecialistProvider(turns),
        tools=tools,
        checkpoint_store=store,
    )


def test_six_profiles_are_distinct_and_formula_free():
    assert set(SPECIALIST_PROFILES) == {
        "fundamental",
        "expectations_events",
        "market_structure",
        "alternative_data",
        "macro_regime",
        "meta_alpha",
    }
    assert len({profile.artifact_hash for profile in SPECIALIST_PROFILES.values()}) == 6
    assert all("pct_change(" not in profile.canonical_json() for profile in SPECIALIST_PROFILES.values())


def test_illegal_transition_fails_closed():
    with pytest.raises(ContractValidationError) as exc:
        require_transition(SpecialistStage.CREATED, SpecialistStage.COMPLETED)
    assert exc.value.code is FailureCode.INVALID_STATE_TRANSITION


@pytest.mark.parametrize("expression", ["eval('x')", "import os", "factor.py", "```python"])
def test_executable_candidate_is_rejected(expression: str):
    with pytest.raises(ContractValidationError) as exc:
        candidate(expression=expression)
    assert exc.value.code is FailureCode.FORBIDDEN_PROPOSAL_PAYLOAD


def test_single_agent_runs_plan_investigate_freeform_candidate_and_preflight():
    cap = FactorLanguageCapabilityRequest(
        request_id="cap-ts-argmax",
        request_type="NEW_OPERATOR",
        requested_name="ts_argmax",
        economic_reason="研究高点距今时间的反转含义",
        deterministic_semantics="历史窗口最大值的位置",
        lookahead_risk="只允许读取决策时点前窗口",
        example_expression="ts_argmax(close, 20)",
    )
    draft = candidate()
    result = run_script(
        (
            SpecialistTurn(kind=TurnKind.PLAN, plan=plan()),
            tool_turn(),
            SpecialistTurn(kind=TurnKind.CANDIDATES, candidates=(draft,), capability_requests=(cap,)),
            critique_turn(draft.candidate_id),
        )
    )
    assert result.status is SpecialistStage.COMPLETED
    assert result.usage.iterations == 4
    assert result.usage.tool_calls == 1
    assert result.capability_requests == (cap,)
    assert result.preflight_results[0].status is PreflightStatus.ACCEPTED
    assert result.preflight_results[0].factor_dag_hash.startswith("sha256:")
    assert "PIT" in result.message


def test_broad_candidate_search_collects_multiple_bounded_batches():
    req = request(
        budget=SpecialistResearchBudget(
            max_iterations=6,
            max_candidate_families=3,
            max_combinations=8,
            max_submitted_candidates=4,
            family_trial_budget=4,
        )
    )
    drafts = (
        candidate("momentum_50", expression="pct_change(close, 50)"),
        candidate("momentum_40", expression="pct_change(close, 40)"),
        candidate("momentum_smooth", expression="rolling_mean(pct_change(close, 50), 5)"),
        candidate("momentum_risk", expression="divide_safe(pct_change(close, 50), rolling_std(close, 20))"),
    )
    profile = get_specialist_profile(req.specialist_id)
    outcome = BoundedResearchAgentLoop(broad_candidate_search=True).run(
        request=req,
        profile=profile,
        provider=ScriptedSpecialistProvider(
            (
                SpecialistTurn(kind=TurnKind.PLAN, plan=strict_plan()),
                tool_turn(),
                SpecialistTurn(kind=TurnKind.CANDIDATES, candidates=drafts[:2]),
                SpecialistTurn(kind=TurnKind.CANDIDATES, candidates=drafts[2:]),
                critique_turn(*(item.candidate_id for item in drafts)),
            )
        ),
        tools=build_fixture_registry(profile, available_fields=req.available_fields),
    )

    assert outcome.status is SpecialistStage.COMPLETED
    assert len(outcome.candidates) == 4
    assert SpecialistStage.GENERATING in outcome.stage_history


def test_missing_self_critique_preserves_committed_candidates_for_preflight():
    req = request(
        budget=SpecialistResearchBudget(
            max_iterations=6,
            max_candidate_families=3,
            max_combinations=8,
            max_submitted_candidates=4,
            family_trial_budget=4,
        )
    )
    drafts = (
        candidate("fallback_50", expression="pct_change(close, 50)"),
        candidate("fallback_40", expression="pct_change(close, 40)"),
        candidate("fallback_smooth", expression="rolling_mean(pct_change(close, 50), 5)"),
        candidate("fallback_risk", expression="divide_safe(pct_change(close, 50), rolling_std(close, 20))"),
    )
    profile = get_specialist_profile(req.specialist_id)
    outcome = BoundedResearchAgentLoop(broad_candidate_search=True).run(
        request=req,
        profile=profile,
        provider=ScriptedSpecialistProvider(
            (
                SpecialistTurn(kind=TurnKind.PLAN, plan=strict_plan()),
                tool_turn(),
                SpecialistTurn(kind=TurnKind.CANDIDATES, candidates=drafts[:2]),
                SpecialistTurn(kind=TurnKind.CANDIDATES, candidates=drafts[2:]),
            )
        ),
        tools=build_fixture_registry(profile, available_fields=req.available_fields),
    )

    assert outcome.status is SpecialistStage.COMPLETED
    assert len(outcome.candidates) == 4
    assert len(outcome.preflight_results) == 4
    assert any(issue.item_kind == "self_critique" for issue in outcome.draft_issues)


def test_agent_must_investigate_before_submitting_candidates():
    draft = candidate()
    result = run_script(
        (
            SpecialistTurn(kind=TurnKind.PLAN, plan=plan()),
            SpecialistTurn(kind=TurnKind.CANDIDATES, candidates=(draft,)),
        )
    )
    assert result.status is SpecialistStage.FAILED
    assert result.failure_code == FailureCode.INCOMPLETE_RESULT.value
    assert not result.candidates


def test_strict_loop_programmatically_schedules_minimum_investigations():
    req = request(
        budget=SpecialistResearchBudget(
            max_iterations=4,
            max_tool_calls=4,
            max_submitted_candidates=4,
            family_trial_budget=4,
        )
    )
    profile = get_specialist_profile(req.specialist_id)
    tools = build_fixture_registry(profile, available_fields=req.available_fields)
    drafts = tuple(
        candidate(
            candidate_id=f"momentum_{index}",
            expression=f"rank_cross_section(pct_change(close, {window}))",
            required_fields=("close",),
        )
        for index, window in enumerate((20, 40, 50, 60), start=1)
    )
    outcome = BoundedResearchAgentLoop().run(
        request=req,
        profile=profile,
        provider=ScriptedSpecialistProvider(
            (
                SpecialistTurn(kind=TurnKind.PLAN, plan=strict_plan()),
                SpecialistTurn(kind=TurnKind.CANDIDATES, candidates=drafts),
                critique_turn(*(item.candidate_id for item in drafts)),
            )
        ),
        tools=tools,
    )
    assert outcome.status is SpecialistStage.COMPLETED
    assert outcome.usage.tool_calls == 4
    assert len(outcome.findings) == 4
    assert {item.tool_name.rsplit(".", 1)[-1] for item in outcome.findings} == {
        "describe_available_data",
        "describe_factor_language",
        "query_factor_library",
        "query_trading_constraints",
    }
    assert len(outcome.candidates) == 4


def test_strict_loop_fails_closed_when_minimum_investigation_budget_is_too_small():
    req = request(
        budget=SpecialistResearchBudget(
            max_iterations=4,
            max_tool_calls=3,
            max_submitted_candidates=4,
            family_trial_budget=4,
        )
    )
    profile = get_specialist_profile(req.specialist_id)
    outcome = BoundedResearchAgentLoop().run(
        request=req,
        profile=profile,
        provider=ScriptedSpecialistProvider((SpecialistTurn(kind=TurnKind.PLAN, plan=strict_plan()),)),
        tools=build_fixture_registry(profile, available_fields=req.available_fields),
    )
    assert outcome.status is SpecialistStage.BUDGET_EXHAUSTED
    assert outcome.failure_code == FailureCode.PROVIDER_BUDGET_EXCEEDED.value
    assert len(outcome.findings) == 3


def test_strict_loop_allows_exactly_one_bounded_stage_schema_repair():
    req = request(
        budget=SpecialistResearchBudget(
            max_iterations=5,
            max_tool_calls=4,
            max_submitted_candidates=4,
            family_trial_budget=4,
        )
    )
    profile = get_specialist_profile(req.specialist_id)
    drafts = tuple(
        candidate(
            candidate_id=f"repair_candidate_{index}",
            expression="rank_cross_section(pct_change(close, 50))",
            required_fields=("close",),
        )
        for index in range(1, 5)
    )

    class RepairingProvider:
        def __init__(self):
            self.calls = 0

        def next_turn(self, context):
            self.calls += 1
            if self.calls == 1:
                return SpecialistTurn(kind=TurnKind.PLAN, plan=strict_plan())
            if self.calls == 2:
                raise ContractValidationError(FailureCode.PROVIDER_SCHEMA_REJECTED, "invalid first candidate payload")
            if self.calls == 3:
                return SpecialistTurn(kind=TurnKind.CANDIDATES, candidates=drafts)
            return critique_turn(*(item.candidate_id for item in drafts))

    provider = RepairingProvider()
    outcome = BoundedResearchAgentLoop().run(
        request=req,
        profile=profile,
        provider=provider,
        tools=build_fixture_registry(profile, available_fields=req.available_fields),
    )
    assert outcome.status is SpecialistStage.COMPLETED
    assert provider.calls == 4
    assert sum(item.failure_code == FailureCode.PROVIDER_SCHEMA_REJECTED.value for item in outcome.draft_issues) == 1


def test_iteration_budget_exhaustion_is_explicit():
    req = request(budget=SpecialistResearchBudget(max_iterations=1))
    result = run_script((SpecialistTurn(kind=TurnKind.PLAN, plan=plan()),), req=req)
    assert result.status is SpecialistStage.BUDGET_EXHAUSTED
    assert result.failure_code == FailureCode.PROVIDER_BUDGET_EXCEEDED.value


def test_invalid_formula_can_be_revised_with_lineage():
    first = candidate(candidate_id="unsupported_v1", expression="ts_argmax(close, 50)")
    revised = candidate(
        candidate_id="supported_v2",
        expression="rank_cross_section(pct_change(close, 50))",
        parent_candidate_id="unsupported_v1",
        revision_number=1,
    )
    result = run_script(
        (
            SpecialistTurn(kind=TurnKind.PLAN, plan=plan()),
            tool_turn(),
            SpecialistTurn(kind=TurnKind.CANDIDATES, candidates=(first,)),
            critique_turn(first.candidate_id),
            SpecialistTurn(kind=TurnKind.REVISED_CANDIDATES, candidates=(revised,)),
            critique_turn(revised.candidate_id),
        )
    )
    assert result.status is SpecialistStage.COMPLETED
    assert result.usage.revisions == 1
    assert [item.status for item in result.preflight_results] == [
        PreflightStatus.CAPABILITY_REQUEST,
        PreflightStatus.ACCEPTED,
    ]
    assert revised.parent_candidate_id == first.candidate_id


class InterruptingProvider:
    def __init__(self):
        self.calls = 0

    def next_turn(self, context):
        self.calls += 1
        if self.calls == 1:
            return SpecialistTurn(kind=TurnKind.PLAN, plan=plan())
        if self.calls == 2:
            return tool_turn()
        raise KeyboardInterrupt("forced interruption")


class TimeoutProvider:
    def next_turn(self, context):
        del context
        raise ContractValidationError(FailureCode.PROVIDER_TIMEOUT, "provider request timed out")


def test_planning_provider_timeout_is_a_structured_terminal_outcome():
    req = request()
    profile = get_specialist_profile(req.specialist_id)
    result = SpecialistResearchAgent().run(
        request=req,
        profile=profile,
        provider=TimeoutProvider(),
        tools=build_fixture_registry(profile, available_fields=req.available_fields),
    )
    assert result.status is SpecialistStage.BUDGET_EXHAUSTED
    assert result.failure_code == FailureCode.PROVIDER_TIMEOUT.value
    assert result.message == "provider request timed out"


def test_checkpoint_resume_skips_committed_plan_and_tool_call(tmp_path: Path):
    req = request()
    profile = get_specialist_profile(req.specialist_id)
    tools = build_fixture_registry(profile, available_fields=req.available_fields)
    store = SpecialistCheckpointStore(tmp_path, run_id=req.run_id, specialist_id=req.specialist_id)
    provider = InterruptingProvider()
    with pytest.raises(KeyboardInterrupt):
        SpecialistResearchAgent().run(
            request=req,
            profile=profile,
            provider=provider,
            tools=tools,
            checkpoint_store=store,
        )
    checkpoint = store.load_checkpoint()
    assert checkpoint is not None
    assert checkpoint.current_stage is SpecialistStage.INVESTIGATING
    assert checkpoint.usage.tool_calls == 1
    draft = candidate()
    resumed_provider = ScriptedSpecialistProvider(
        (
            SpecialistTurn(kind=TurnKind.CANDIDATES, candidates=(draft,)),
            critique_turn(draft.candidate_id),
        )
    )
    result = SpecialistResearchAgent().run(
        request=req,
        profile=profile,
        provider=resumed_provider,
        tools=tools,
        checkpoint_store=store,
    )
    assert result.status is SpecialistStage.COMPLETED
    assert result.usage.tool_calls == 1
    assert resumed_provider.calls == 2
    assert SpecialistResearchAgent().run(
        request=req,
        profile=profile,
        provider=ScriptedSpecialistProvider(()),
        tools=tools,
        checkpoint_store=store,
    ).artifact_hash == result.artifact_hash
    assert {
        "checkpoint.json",
        "outcome.json",
        "exploration_plan.json",
        "candidate_drafts.json",
        "preflight_results.json",
        "capability_requests.json",
        "investigation_findings.jsonl",
        "events.jsonl",
    } <= {item.name for item in store.directory.iterdir()}


def test_checkpoint_tamper_fails_closed(tmp_path: Path):
    req = request()
    store = SpecialistCheckpointStore(tmp_path, run_id=req.run_id, specialist_id=req.specialist_id)
    draft = candidate()
    run_script(
        (
            SpecialistTurn(kind=TurnKind.PLAN, plan=plan()),
            tool_turn(),
            SpecialistTurn(kind=TurnKind.CANDIDATES, candidates=(draft,)),
            critique_turn(draft.candidate_id),
        ),
        req=req,
        store=store,
    )
    envelope = json.loads(store.outcome_path.read_text(encoding="utf-8"))
    envelope["payload"]["message"] = "tampered"
    store.outcome_path.write_text(json.dumps(envelope), encoding="utf-8")
    with pytest.raises(ContractValidationError) as exc:
        store.load_outcome()
    assert exc.value.code is FailureCode.INVALID_HASH


def test_legacy_adapter_projects_only_accepted_draft():
    draft = candidate()
    outcome = run_script(
        (
            SpecialistTurn(kind=TurnKind.PLAN, plan=plan()),
            tool_turn(),
            SpecialistTurn(kind=TurnKind.CANDIDATES, candidates=(draft,)),
            critique_turn(draft.candidate_id),
        )
    )
    projected = LegacyGroupProposalAdapter().project(outcome)
    assert projected.group_id == "market_structure"
    assert len(projected.candidates) == 1
    assert projected.answer_level.value != "OFFICIAL_EVALUATED"


def test_undeclared_tool_and_sealed_feedback_are_rejected():
    profile = get_specialist_profile("market_structure")
    registry = build_fixture_registry(profile, available_fields=("close", "volume"))
    with pytest.raises(ContractValidationError) as tool_exc:
        registry.call("market_structure.read_secret_file", {"query": "x"})
    assert tool_exc.value.code is FailureCode.UNAUTHORIZED_CAPABILITY
    with pytest.raises(ContractValidationError) as feedback_exc:
        RedactedMachineFeedback(
            candidate_id="c1",
            compile_status="ACCEPTED",
            pit_status="ACCEPTED",
            visible_failure_codes=(),
            cost_warning="sealed rank was high",
            stability_warning=None,
            incrementality_warning=None,
            revision_allowed=True,
        )
    assert feedback_exc.value.code is FailureCode.UNAUTHORIZED_CAPABILITY


def test_provider_payload_cannot_smuggle_authority_fields():
    with pytest.raises(ContractValidationError) as exc:
        turn_from_mapping({"kind": "PLAN", "plan": {}, "promotion": "APPROVE"})
    assert exc.value.code is FailureCode.UNKNOWN_FIELD

    raw_candidate = candidate().to_dict()
    raw_candidate.pop("contract_type")
    raw_candidate.pop("schema_version")
    raw_candidate["official_metrics"] = {"rank_ic": 1.0}
    with pytest.raises(ContractValidationError) as nested_exc:
        turn_from_mapping({"kind": "CANDIDATES", "candidates": [raw_candidate]})
    assert nested_exc.value.code is FailureCode.UNKNOWN_FIELD


def test_provider_quarantines_bad_optional_request_without_losing_valid_candidate():
    raw_candidate = candidate().to_dict()
    raw_candidate.pop("contract_type")
    raw_candidate.pop("schema_version")
    turn = turn_from_mapping(
        {
            "kind": "CANDIDATES",
            "candidates": [raw_candidate],
            "capability_requests": [
                {
                    "request_id": "bad-capability",
                    "request_type": "NEW_OPERATOR",
                    "requested_name": "safe_operator",
                    "economic_reason": "test a safe deterministic extension",
                    "deterministic_semantics": "past-only deterministic transform",
                    "lookahead_risk": "must not read the future",
                    "example_expression": "safe_operator(close, 20)",
                    "implementation_code": "not accepted",
                }
            ],
        }
    )
    assert tuple(item.candidate_id for item in turn.candidates) == ("momentum_volume_50",)
    assert turn.capability_requests == ()
    assert len(turn.draft_issues) == 1
    assert turn.draft_issues[0].failure_code == FailureCode.UNKNOWN_FIELD.value


def test_provider_keeps_valid_candidate_when_another_candidate_is_invalid():
    valid = candidate().to_dict()
    valid.pop("contract_type")
    valid.pop("schema_version")
    invalid = dict(valid)
    invalid["candidate_id"] = "invalid_authority_candidate"
    invalid["promotion"] = "APPROVE"
    turn = turn_from_mapping({"kind": "CANDIDATES", "candidates": [valid, invalid]})
    assert tuple(item.candidate_id for item in turn.candidates) == ("momentum_volume_50",)
    assert len(turn.draft_issues) == 1
    assert turn.draft_issues[0].item_kind == "candidate"


def test_duplicate_investigation_is_skipped_and_draft_still_completes():
    profile = get_specialist_profile("market_structure")
    req = request()
    calls = {"count": 0}
    tool_name = "market_structure.describe_available_data"

    def handler(arguments):
        calls["count"] += 1
        return {
            "summary": "visible coverage is sufficient",
            "coverage": 1.0,
            "source_hash": "sha256:" + "b" * 64,
            "available_at": "fixture-visible-at-decision-time",
        }

    tools = build_fixture_registry(
        profile,
        available_fields=req.available_fields,
        handlers={tool_name: handler},
    )
    draft = candidate()
    outcome = SpecialistResearchAgent().run(
        request=req,
        profile=profile,
        provider=ScriptedSpecialistProvider(
            (
                SpecialistTurn(kind=TurnKind.PLAN, plan=plan()),
                tool_turn(),
                tool_turn(),
                SpecialistTurn(kind=TurnKind.CANDIDATES, candidates=(draft,)),
                critique_turn(draft.candidate_id),
            )
        ),
        tools=tools,
    )
    assert calls["count"] == 1
    assert outcome.status is SpecialistStage.COMPLETED
    assert outcome.result_kind is SpecialistResultKind.EVALUATED_RESEARCH_RESULT
    assert any(item.failure_code == "DUPLICATE_TOOL_CALL" for item in outcome.draft_issues)


@pytest.mark.parametrize(
    ("specialist_id", "expression", "fields"),
    (
        ("fundamental", "zscore_cross_section(add(roe, pct_change(close, 50)))", ("roe", "close")),
        ("expectations_events", "rank_cross_section(pct_change(close, 50))", ("close",)),
        ("market_structure", "rank_cross_section(subtract(pct_change(close, 50), pct_change(volume, 5)))", ("close", "volume")),
        ("alternative_data", "rank_cross_section(rolling_mean(volume, 50))", ("volume",)),
        ("macro_regime", "rank_cross_section(multiply(pct_change(close, 50), shibor_overnight))", ("close", "shibor_overnight")),
        ("meta_alpha", "rank_cross_section(divide_safe(pct_change(close, 50), rolling_std(close, 50)))", ("close",)),
    ),
)
def test_each_specialist_profile_runs_the_same_open_ended_kernel(specialist_id, expression, fields):
    req = request(specialist_id)
    profile_plan = plan(specialist_id)
    draft = candidate(candidate_id=f"{specialist_id}_draft", expression=expression, required_fields=fields)
    result = run_script(
        (
            SpecialistTurn(kind=TurnKind.PLAN, plan=profile_plan),
            tool_turn(specialist_id),
            SpecialistTurn(kind=TurnKind.CANDIDATES, candidates=(draft,)),
            critique_turn(draft.candidate_id),
        ),
        req=req,
    )
    assert result.status is SpecialistStage.COMPLETED
    assert result.preflight_results[-1].status is PreflightStatus.ACCEPTED


def test_redacted_machine_feedback_can_create_a_lineaged_revision():
    req = request()
    first = candidate(candidate_id="momentum_v1", expression="rank_cross_section(pct_change(close, 50))", required_fields=("close",))
    prior = run_script(
        (
            SpecialistTurn(kind=TurnKind.PLAN, plan=plan()),
            tool_turn(),
            SpecialistTurn(kind=TurnKind.CANDIDATES, candidates=(first,)),
            critique_turn(first.candidate_id),
        ),
        req=req,
    )
    feedback = RedactedMachineFeedback(
        candidate_id=first.candidate_id,
        compile_status="ACCEPTED",
        pit_status="ACCEPTED",
        visible_failure_codes=("HIGH_TURNOVER",),
        cost_warning="可见成本摘要显示需要降低换手",
        stability_warning=None,
        incrementality_warning="与已登记动量基线相似",
        revision_allowed=True,
    )
    revised = candidate(
        candidate_id="momentum_v2",
        expression="rank_cross_section(divide_safe(pct_change(close, 50), rolling_std(close, 50)))",
        required_fields=("close",),
        parent_candidate_id=first.candidate_id,
        revision_number=1,
    )
    provider = ScriptedSpecialistProvider(
        (
            SpecialistTurn(kind=TurnKind.REVISED_CANDIDATES, candidates=(revised,)),
            critique_turn(revised.candidate_id),
        )
    )
    profile = get_specialist_profile(req.specialist_id)
    result = SpecialistResearchAgent().revise_from_feedback(
        request=req,
        profile=profile,
        provider=provider,
        tools=build_fixture_registry(profile, available_fields=req.available_fields),
        prior=prior,
        feedback=(feedback,),
    )
    assert result.status is SpecialistStage.COMPLETED
    assert result.usage.revisions == 1
    assert result.candidates[-1].parent_candidate_id == first.candidate_id
    assert result.preflight_results[-1].status is PreflightStatus.ACCEPTED
