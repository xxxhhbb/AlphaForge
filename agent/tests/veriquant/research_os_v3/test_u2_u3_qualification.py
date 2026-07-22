from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

from veriquant.contracts import ContractValidationError, FailureCode
from veriquant.dynamic_acquisition.models import stable_content_hash
from veriquant.research_os_v3 import (
    ResearchCapabilityStatus,
    SpecialistOperationalStatus,
    U2U3QualificationGate,
    build_frozen_research_registry,
    load_frozen_research_context,
)
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
    SpecialistTurn,
    TurnKind,
    get_specialist_profile,
)


REPO_ROOT = Path(__file__).resolve().parents[4]
ROOT_MANIFEST = REPO_ROOT / ".autobuild/veriquant/integration/dynamic_data_active/freeze_manifest.json"
VISIBLE_RESULT = REPO_ROOT / ".autobuild/veriquant/integration/c8_real_ashare_v1/c8-real-ashare-v1/result.json"
HASH = "sha256:" + "a" * 64


def _context():
    return load_frozen_research_context(
        ROOT_MANIFEST,
        decision_time=datetime(2026, 7, 20, 8, tzinfo=timezone.utc),
        visible_evidence_path=VISIBLE_RESULT,
    )


def _plan(specialist_id: str) -> ResearchExplorationPlan:
    axes = (CombinationAxis("window", ("20", "50"), "Bounded window comparison."),)
    return ResearchExplorationPlan(
        specialist_id=specialist_id,
        scope_summary="Independently plan multiple mechanisms before submitting drafts.",
        directions=(
            ResearchDirectionIdea("direction_a", "mechanism A", "price proxy", ("close",), "net evidence fails"),
            ResearchDirectionIdea("direction_b", "mechanism B", "volume proxy", ("volume",), "stability fails"),
        ),
        investigation_questions=("What frozen data is available at decision time?",),
        stop_conditions=("Budget exhausted", "No safe candidate remains"),
        mechanism_ideas=(
            ResearchMechanismIdea("mechanism_a", "family_a", "underreaction", "past price", "POSITIVE", ("close",), "net evidence fails"),
            ResearchMechanismIdea("mechanism_b", "family_b", "participation", "past volume", "CONDITIONAL", ("volume",), "stability fails"),
        ),
        candidate_family_plans=(
            CandidateFamilyPlan("family_a", ("mechanism_a",), axes, 2),
            CandidateFamilyPlan("family_b", ("mechanism_b",), axes, 2),
        ),
    )


def _candidate(suffix: str, family: str, expression: str, field: str) -> SpecialistCandidateDraft:
    return SpecialistCandidateDraft(
        candidate_id=f"candidate_{suffix}",
        family_id=family,
        hypothesis=f"Candidate {suffix} is falsifiable under frozen inputs.",
        economic_mechanism="Observable information diffuses with a bounded delay.",
        expected_direction="POSITIVE",
        forecast_horizon_sessions=5,
        lookback_sessions=50,
        required_fields=(field,),
        expression=expression,
        availability_assumptions=("event_time, published_at, available_at and first_tradable_time do not exceed decision_time.",),
        neutralization=("industry", "market_cap"),
        falsification_tests=("Cost-adjusted evidence is non-positive.",),
        cost_risks=("Turnover may erase gross evidence.",),
        exposure_risks=("Industry or size exposure may dominate.",),
        novelty_explanation="Independent bounded family for qualification.",
    )


def _outcome(specialist_id: str, context):
    profile = get_specialist_profile(specialist_id)
    request = SpecialistResearchRequest(
        run_id=f"u2-qualification-{specialist_id}",
        campaign_hash=HASH,
        specialist_id=specialist_id,
        question="研究大 A 市场中可证伪的 50 日 Alpha 机制",
        universe="cn-ashare-approved-proxy-pit",
        label_horizon_sessions=5,
        decision_time=context.decision_time,
        available_fields=context.available_fields,
        requested_lookback_sessions=50,
        budget=SpecialistResearchBudget(max_iterations=16, max_tool_calls=8, max_submitted_candidates=6, family_trial_budget=6),
        seed=7,
    )
    candidates = (
        _candidate("a", "family_a", "rank_cross_section(pct_change(close, 50))", "close"),
        _candidate("b", "family_b", "zscore_cross_section(rolling_mean(volume, 20))", "volume"),
    )
    provider = ScriptedSpecialistProvider((
        SpecialistTurn(kind=TurnKind.PLAN, plan=_plan(specialist_id)),
        SpecialistTurn(kind=TurnKind.CANDIDATES, candidates=candidates),
        SpecialistTurn(
            kind=TurnKind.SELF_CRITIQUE,
            critiques=tuple(CandidateCritique(item.candidate_id, CritiqueAction.KEEP, ("cost sensitivity",), "retain for preflight") for item in candidates),
        ),
    ))
    return BoundedResearchAgentLoop().run(
        request=request,
        profile=profile,
        provider=provider,
        tools=build_frozen_research_registry(profile, context=context),
    )


def test_real_frozen_context_preserves_provenance_and_capability_gaps() -> None:
    context = _context()
    assert context.source_label == "APPROVED_PROXY_DATA_VENDOR"
    assert context.official_tushare is False
    assert {"close", "volume", "market_cap", "roe", "shibor_overnight"} <= set(context.available_fields)
    assert not any("future" in item or "label" in item for item in context.available_fields)
    assert context.capability("market_structure")[0] is ResearchCapabilityStatus.AVAILABLE
    assert context.capability("fundamental")[0] is ResearchCapabilityStatus.AVAILABLE
    assert context.capability("macro_regime")[0] is ResearchCapabilityStatus.AVAILABLE
    assert context.capability("meta_alpha")[0] is ResearchCapabilityStatus.AVAILABLE
    assert context.capability("expectations_events") == (ResearchCapabilityStatus.DATA_GAP, ("forecast", "express"))
    assert context.capability("alternative_data") == (ResearchCapabilityStatus.DATA_GAP, ("documents", "entity_mapping"))


def test_fundamental_can_start_from_valuation_data_without_all_statements() -> None:
    context = _context()
    valuation_only = replace(
        context,
        api_statuses={"daily": "SUCCESS", "daily_basic": "SUCCESS"},
        api_row_counts={"daily": 100, "daily_basic": 100},
    )

    assert valuation_only.capability("fundamental") == (ResearchCapabilityStatus.AVAILABLE, ())
    assert valuation_only.capability("expectations_events") == (
        ResearchCapabilityStatus.DATA_GAP,
        ("forecast", "express"),
    )


def test_each_registry_is_exactly_scoped_stable_and_read_only() -> None:
    context = _context()
    first_hashes = {}
    for specialist_id in ("fundamental", "expectations_events", "market_structure", "alternative_data", "macro_regime", "meta_alpha"):
        profile = get_specialist_profile(specialist_id)
        registry = build_frozen_research_registry(profile, context=context)
        assert set(registry.names) == set(profile.required_tool_names)
        summary = registry.call(profile.required_tool_names[0], {"query": "visible frozen context"})
        first_hashes[specialist_id] = summary.result_hash
        replay = build_frozen_research_registry(profile, context=context).call(profile.required_tool_names[0], {"query": "visible frozen context"})
        assert replay.result_hash == summary.result_hash
        other = "market_structure.describe_available_data" if specialist_id != "market_structure" else "fundamental.describe_available_data"
        with pytest.raises(ContractValidationError) as exc:
            registry.call(other, {"query": "cross group"})
        assert exc.value.code is FailureCode.UNAUTHORIZED_CAPABILITY

    assert len(set(first_hashes.values())) == 6


def test_tampered_freeze_manifest_fails_closed(tmp_path) -> None:
    payload = json.loads(open(ROOT_MANIFEST, encoding="utf-8").read())
    payload["source_label"] = "UNAPPROVED"
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ContractValidationError) as exc:
        load_frozen_research_context(path, decision_time=datetime(2026, 7, 20, tzinfo=timezone.utc))
    assert exc.value.code is FailureCode.INVALID_HASH


def test_non_official_frozen_source_can_support_auxiliary_agent_research(tmp_path) -> None:
    payload = json.loads(open(ROOT_MANIFEST, encoding="utf-8").read())
    payload.pop("artifact_hash")
    payload["source_label"] = "FROZEN_VIBE_MATERIAL"
    payload["official_tushare"] = False
    payload["artifact_hash"] = stable_content_hash(payload)
    path = tmp_path / "auxiliary_manifest.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    context = load_frozen_research_context(
        path, decision_time=datetime(2026, 7, 20, tzinfo=timezone.utc)
    )

    assert context.source_label == "FROZEN_VIBE_MATERIAL"
    assert context.official_tushare is False
    registry = build_frozen_research_registry(get_specialist_profile("market_structure"), context=context)
    result = registry.call("market_structure.describe_available_data", {"query": "研究现有资料"})
    assert "FROZEN_VIBE_MATERIAL" in str(result.result_summary)


def test_all_six_runtimes_qualify_while_real_data_gaps_remain_typed() -> None:
    context = _context()
    outcomes = {specialist_id: _outcome(specialist_id, context) for specialist_id in (
        "fundamental", "expectations_events", "market_structure", "alternative_data", "macro_regime", "meta_alpha"
    )}
    report = U2U3QualificationGate().evaluate(outcomes=outcomes, context=context)
    replay = U2U3QualificationGate().evaluate(outcomes=outcomes, context=context)
    assert report.runtime_passed is True
    assert report.operationally_available_count == 4
    assert report.typed_gap_count == 2
    assert report.artifact_hash == replay.artifact_hash
    assert report.results["expectations_events"].operational_status is SpecialistOperationalStatus.DATA_GAP
    assert report.results["alternative_data"].operational_status is SpecialistOperationalStatus.DATA_GAP
    assert all(item.accepted_preflight_count >= 1 for item in report.results.values())
