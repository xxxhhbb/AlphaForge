from __future__ import annotations

from dataclasses import fields, replace
from datetime import datetime, timezone

import pytest

from veriquant.research_os_v3 import ResearchOSV3Service, ResearchResultKind
from veriquant.research_run_controller import ResearchRunLifecycle, ResearchRunManifest
from veriquant.run_recovery.api import RecoveryError
from veriquant.specialist_research import (
    BoundedResearchAgentLoop,
    CandidateCritique,
    CandidateFamilyPlan,
    CritiqueAction,
    ResearchDirectionIdea,
    ResearchExplorationPlan,
    ResearchMechanismIdea,
    ScriptedSpecialistProvider,
    SpecialistCandidateDraft,
    SpecialistResearchBudget,
    SpecialistResearchRequest,
    SpecialistTurn,
    TurnKind,
    build_fixture_registry,
    get_specialist_profile,
    stage_json_schema,
)


H0 = "sha256:" + "0" * 64
H1 = "sha256:" + "1" * 64
H2 = "sha256:" + "2" * 64
CLOCK = datetime(2026, 7, 20, 8, 0, tzinfo=timezone.utc)


def _request(*, run_id: str = "v3_single_agent") -> SpecialistResearchRequest:
    return SpecialistResearchRequest(
        run_id=run_id,
        campaign_hash=H1,
        specialist_id="market_structure",
        question="挖掘最近50日大A动量相关Alpha",
        universe="cn_ashare_pit",
        label_horizon_sessions=5,
        decision_time=CLOCK,
        available_fields=("close", "volume", "market_cap"),
        requested_lookback_sessions=50,
        budget=SpecialistResearchBudget(
            max_iterations=4,
            max_tool_calls=4,
            max_submitted_candidates=4,
            family_trial_budget=4,
        ),
    )


def _plan() -> ResearchExplorationPlan:
    mechanisms = (
        ResearchMechanismIdea(
            mechanism_id="underreaction",
            family_id="momentum",
            mechanism="反应不足形成中期价格延续",
            observable_proxy="过去收益",
            predicted_direction="POSITIVE",
            required_fields=("close",),
            falsification="成本后失效",
        ),
        ResearchMechanismIdea(
            mechanism_id="volume_confirmation",
            family_id="volume_momentum",
            mechanism="成交量确认信息扩散",
            observable_proxy="价格和成交量变化",
            predicted_direction="POSITIVE",
            required_fields=("close", "volume"),
            falsification="没有增量",
        ),
    )
    return ResearchExplorationPlan(
        specialist_id="market_structure",
        scope_summary="从量价和风险调整方向探索50日动量",
        directions=(
            ResearchDirectionIdea(
                direction_id="price_momentum",
                mechanism="价格延续",
                differentiation="原始动量",
                required_fields=("close",),
                falsification="成本后收益为负",
            ),
            ResearchDirectionIdea(
                direction_id="volume_momentum",
                mechanism="成交量确认",
                differentiation="量价联合",
                required_fields=("close", "volume"),
                falsification="没有稳定增量",
            ),
        ),
        investigation_questions=("数据覆盖是否充分", "语言是否支持"),
        stop_conditions=("预算耗尽",),
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


def _candidate(index: int, window: int) -> SpecialistCandidateDraft:
    return SpecialistCandidateDraft(
        candidate_id=f"momentum_{index}",
        family_id="momentum",
        hypothesis="过去价格变化包含可证伪的横截面信息",
        economic_mechanism="投资者反应不足",
        expected_direction="POSITIVE",
        forecast_horizon_sessions=5,
        lookback_sessions=window,
        required_fields=("close",),
        expression=f"rank_cross_section(pct_change(close, {window}))",
        availability_assumptions=("价格在决策时点前可得",),
        neutralization=("industry", "market_cap"),
        falsification_tests=("成本后收益为负",),
        cost_risks=("换手成本",),
        exposure_risks=("行业暴露",),
        novelty_explanation="不同窗口作为有界候选",
    )


def _provider() -> ScriptedSpecialistProvider:
    candidates = tuple(_candidate(index, window) for index, window in enumerate((20, 40, 50, 60), start=1))
    return ScriptedSpecialistProvider(
        (
            SpecialistTurn(kind=TurnKind.PLAN, plan=_plan()),
            SpecialistTurn(kind=TurnKind.CANDIDATES, candidates=candidates),
            SpecialistTurn(
                kind=TurnKind.SELF_CRITIQUE,
                critiques=tuple(
                    CandidateCritique(
                        candidate_id=item.candidate_id,
                        action=CritiqueAction.KEEP,
                        weaknesses=("需要机器验证",),
                        rationale="保留差异化窗口",
                    )
                    for item in candidates
                ),
            ),
        )
    )


def _service(tmp_path) -> ResearchOSV3Service:
    return ResearchOSV3Service(
        root=tmp_path,
        provider_id="deepseek",
        model_id="deepseek_v4_flash",
        data_snapshot_hash=H2,
        policy_hash=H0,
        code_hash=H1,
    )


def test_v3_single_mainline_has_program_scheduled_research_and_consistent_views(tmp_path):
    request = _request()
    profile = get_specialist_profile(request.specialist_id)
    service = _service(tmp_path)
    result = service.run_single_specialist(
        request=request,
        provider=_provider(),
        tools=build_fixture_registry(profile, available_fields=request.available_fields),
    )
    assert result.current_stage == ResearchRunLifecycle.PARTIAL.value
    assert result.result_kind is ResearchResultKind.PARTIAL_RESEARCH_RESULT
    assert result.candidate_count == 4
    assert result.accepted_preflight_count == 1
    assert service.api_status(request.run_id).artifact_hash == result.artifact_hash
    assert service.workbench_status(request.run_id).artifact_hash == result.artifact_hash
    assert service.report_status(request.run_id).artifact_hash == result.artifact_hash


def test_v3_resume_reuses_terminal_specialist_outcome(tmp_path):
    request = _request(run_id="v3_resume")
    profile = get_specialist_profile(request.specialist_id)
    provider = _provider()
    service = _service(tmp_path)
    first = service.run_single_specialist(
        request=request,
        provider=provider,
        tools=build_fixture_registry(profile, available_fields=request.available_fields),
    )
    second = service.run_single_specialist(
        request=request,
        provider=provider,
        tools=build_fixture_registry(profile, available_fields=request.available_fields),
    )
    assert first.artifact_hash == second.artifact_hash


def test_v3_forced_interruption_resumes_from_canonical_manifest_and_checkpoint(tmp_path):
    request = _request(run_id="v3_forced_resume")
    profile = get_specialist_profile(request.specialist_id)
    service = _service(tmp_path)

    class InterruptAfterPlan:
        def __init__(self):
            self.calls = 0

        def next_turn(self, context):
            self.calls += 1
            if self.calls == 1:
                return SpecialistTurn(kind=TurnKind.PLAN, plan=_plan())
            raise KeyboardInterrupt("forced process interruption")

    with pytest.raises(KeyboardInterrupt):
        service.run_single_specialist(
            request=request,
            provider=InterruptAfterPlan(),
            tools=build_fixture_registry(profile, available_fields=request.available_fields),
        )
    interrupted = service.api_status(request.run_id)
    assert interrupted.current_stage == ResearchRunLifecycle.RESEARCHING.value
    candidates = tuple(_candidate(index, window) for index, window in enumerate((20, 40, 50, 60), start=1))
    resumed_provider = ScriptedSpecialistProvider(
        (
            SpecialistTurn(kind=TurnKind.CANDIDATES, candidates=candidates),
            SpecialistTurn(
                kind=TurnKind.SELF_CRITIQUE,
                critiques=tuple(
                    CandidateCritique(
                        candidate_id=item.candidate_id,
                        action=CritiqueAction.KEEP,
                        weaknesses=("需要机器验证",),
                        rationale="恢复后继续预检",
                    )
                    for item in candidates
                ),
            ),
        )
    )
    resumed = service.run_single_specialist(
        request=request,
        provider=resumed_provider,
        tools=build_fixture_registry(profile, available_fields=request.available_fields),
    )
    assert resumed.current_stage == ResearchRunLifecycle.PARTIAL.value
    assert resumed.candidate_count == 4
    assert resumed.accepted_preflight_count == 1


def test_same_run_id_with_changed_research_identity_fails_closed(tmp_path):
    first = _request(run_id="v3_collision")
    profile = get_specialist_profile(first.specialist_id)
    service = _service(tmp_path)
    service.run_single_specialist(
        request=first,
        provider=_provider(),
        tools=build_fixture_registry(profile, available_fields=first.available_fields),
    )
    changed = SpecialistResearchRequest(
        **{**first.__dict__, "question": "另一个研究问题"},
    )
    with pytest.raises(RecoveryError):
        service.run_single_specialist(
            request=changed,
            provider=_provider(),
            tools=build_fixture_registry(profile, available_fields=changed.available_fields),
        )


def test_stage_schema_is_generated_from_contract_fields():
    schema = stage_json_schema(__import__("veriquant.specialist_research", fromlist=["SpecialistStage"]).SpecialistStage.PLANNING)
    plan_properties = schema["properties"]["plan"]["properties"]
    assert set(plan_properties) == {item.name for item in fields(ResearchExplorationPlan)}
    assert schema["additionalProperties"] is False


def test_v3_migration_excludes_legacy_runtime_from_trusted_path(tmp_path):
    migration = _service(tmp_path).migration
    assert migration.specialist_runtime == "BOUNDED_RESEARCH_AGENT_LOOP_ONLY"
    assert migration.research_os_v2 == "DISABLED_FALLBACK"
    assert migration.legacy_group_runtime == "EXCLUDED_FROM_TRUSTED_PATH"


def test_unknown_factor_language_yields_capability_gap_not_system_success(tmp_path):
    base = _request(run_id="v3_capability_gap")
    request = replace(
        base,
        budget=SpecialistResearchBudget(
            max_iterations=3,
            max_tool_calls=4,
            max_submitted_candidates=4,
            max_revisions=0,
            family_trial_budget=4,
        ),
    )
    candidates = tuple(
        replace(_candidate(index, 50), expression="unknown_safe_operator(close, 50)")
        for index in range(1, 5)
    )
    provider = ScriptedSpecialistProvider(
        (
            SpecialistTurn(kind=TurnKind.PLAN, plan=_plan()),
            SpecialistTurn(kind=TurnKind.CANDIDATES, candidates=candidates),
            SpecialistTurn(
                kind=TurnKind.SELF_CRITIQUE,
                critiques=tuple(
                    CandidateCritique(
                        candidate_id=item.candidate_id,
                        action=CritiqueAction.KEEP,
                        weaknesses=("语言能力待验证",),
                        rationale="保留给安全预检",
                    )
                    for item in candidates
                ),
            ),
        )
    )
    profile = get_specialist_profile(request.specialist_id)
    result = _service(tmp_path).run_single_specialist(
        request=request,
        provider=provider,
        tools=build_fixture_registry(profile, available_fields=request.available_fields),
    )
    assert result.result_kind is ResearchResultKind.DATA_OR_CAPABILITY_GAP_REPORT
    assert result.current_stage == ResearchRunLifecycle.BLOCKED.value
    assert result.accepted_preflight_count == 0
