from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

import pytest

from veriquant.factor_data_requirements.analyzer import (
    FactorDataRequirementAnalyzer,
    build_current_missing_field_report,
)
from veriquant.factor_data_requirements.acquisition import (
    CompliantAcquisitionPlanner,
    build_current_acquisition_plan,
)
from veriquant.factor_data_requirements.models import (
    AcquisitionGate,
    AcquisitionPlanStatus,
    ApprovedAcquisitionStep,
    ApprovedProxySnapshotBinding,
    APPROVED_PROXY_POLICY_HASH,
    APPROVED_PROXY_POLICY_ID,
    DataAdequacyRequest,
    DataRequirementError,
    PROVIDER_CLASS,
    RequirementDisposition,
    SOURCE_CLASS,
    stable_hash,
)
from veriquant.factor_data_requirements.planner import DataAdequacyPlanner


HASH = "sha256:" + "a" * 64


@pytest.fixture(scope="module")
def current_requirements():
    return build_current_missing_field_report()


@pytest.fixture(scope="module")
def current_acquisition():
    return build_current_acquisition_plan()


class _Record:
    factor_id = "fixture"
    source_hash = HASH
    required_fields = ("close", "sector")
    missing_fields = ("sector",)


def _adequacy(**changes):
    values = dict(
        maximum_lookback=60,
        label_horizon=5,
        walk_forward_folds=3,
        train_sessions_per_fold=40,
        validation_sessions_per_fold=10,
        warmup=20,
        minimum_cross_section=30,
        minimum_coverage=0.8,
        minimum_effective_observations=3000,
        minimum_independent_sessions=100,
    )
    values.update(changes)
    return DataAdequacyRequest(**values)


def test_real_projection_classifies_exact_actual_23_without_factor_allowlist(current_requirements):
    report = current_requirements
    assert len(report.records) == 23
    assert report.projection_hash == "sha256:539caf5c8683d0816d2dd020ba0d353fb3ff547582185d3182c4c8f842083ca8"
    assert report.disposition_counts == {
        "DERIVABLE_FROM_APPROVED_MARKET_DATA": 0,
        "AVAILABLE_FROM_APPROVED_PROXY_VENDOR": 2,
        "PIT_TIME_UNPROVEN": 19,
        "SOURCE_UNAVAILABLE": 2,
        "AUXILIARY_ONLY": 0,
    }
    assert {item.factor_id for item in report.records if item.disposition is RequirementDisposition.AVAILABLE_FROM_APPROVED_PROXY_VENDOR} == {
        "fund_asset_growth", "fund_roe",
    }
    assert {item.factor_id for item in report.records if item.disposition is RequirementDisposition.SOURCE_UNAVAILABLE} == {
        "fund_earnings_yield", "fund_gross_profitability",
    }


def test_industry_is_pit_unproven_not_silently_backfilled():
    report = FactorDataRequirementAnalyzer().analyze((_Record(),), projection_hash=HASH)
    record = report.records[0]
    assert record.disposition is RequirementDisposition.PIT_TIME_UNPROVEN
    assert "industry_effective_date" in record.approved_source_fields
    assert "CURRENT_CLASSIFICATION_MUST_NOT_BE_BACKFILLED" in record.reason_codes


def test_unknown_field_is_auxiliary_only_not_new_official_path():
    class Unknown(_Record):
        required_fields = ("mystery",)
        missing_fields = ("mystery",)

    record = FactorDataRequirementAnalyzer().analyze((Unknown(),), projection_hash=HASH).records[0]
    assert record.disposition is RequirementDisposition.AUXILIARY_ONLY
    assert record.approved_apis == ()


def test_report_hash_is_deterministic():
    left = FactorDataRequirementAnalyzer().analyze((_Record(),), projection_hash=HASH)
    right = FactorDataRequirementAnalyzer().analyze((_Record(),), projection_hash=HASH)
    assert left.report_hash == right.report_hash
    assert left.records[0].requirement_hash == right.records[0].requirement_hash


def test_adequacy_is_derived_from_all_requested_dimensions():
    request = _adequacy()
    plan = DataAdequacyPlanner().plan(request)
    assert plan.required_symbols == 38  # ceil(30/.8), derived rather than a product cap
    assert plan.analysis_sessions == 150  # max(3*(40+10), ceil(3000/(38*.8)), 100)
    assert plan.effective_warmup_sessions == 60
    assert plan.required_sessions == 215
    assert plan.required_rows == plan.required_symbols * plan.required_sessions
    assert plan.request_hash == stable_hash(request)


@pytest.mark.parametrize(
    ("change", "field"),
    [
        ({"maximum_lookback": 240}, "required_sessions"),
        ({"label_horizon": 20}, "required_sessions"),
        ({"walk_forward_folds": 5}, "analysis_sessions"),
        ({"warmup": 300}, "effective_warmup_sessions"),
        ({"minimum_cross_section": 100}, "required_symbols"),
        ({"minimum_coverage": 0.5}, "required_symbols"),
        ({"minimum_effective_observations": 10000}, "analysis_sessions"),
        ({"minimum_independent_sessions": 400}, "analysis_sessions"),
    ],
)
def test_adequacy_scale_changes_with_research_requirements(change, field):
    planner = DataAdequacyPlanner()
    baseline = planner.plan(_adequacy())
    changed = planner.plan(_adequacy(**change))
    assert getattr(changed, field) > getattr(baseline, field)


@pytest.mark.parametrize(
    "changes",
    [
        {"maximum_lookback": -1},
        {"label_horizon": 0},
        {"walk_forward_folds": 0},
        {"minimum_cross_section": 0},
        {"minimum_coverage": 0},
        {"minimum_coverage": 1.1},
        {"minimum_effective_observations": 0},
    ],
)
def test_invalid_adequacy_policy_fails_closed(changes):
    with pytest.raises(DataRequirementError):
        _adequacy(**changes)


def _binding(**changes):
    published = datetime(2026, 1, 2, 15, 30, tzinfo=timezone.utc)
    values = dict(
        provider_class=PROVIDER_CLASS,
        source_class=SOURCE_CLASS,
        approved_policy_id=APPROVED_PROXY_POLICY_ID,
        approved_policy_hash=APPROVED_PROXY_POLICY_HASH,
        snapshot_hash=HASH,
        content_hash=HASH,
        provenance_hash=HASH,
        published_at=published,
        available_at=published + timedelta(minutes=30),
        decision_time=published + timedelta(hours=1),
        frozen=True,
    )
    values.update(changes)
    return ApprovedProxySnapshotBinding(**values)


def test_approved_proxy_binding_binds_source_snapshot_clocks_hash_and_provenance():
    binding = _binding()
    assert binding.provider_class == "APPROVED_PROXY_DATA_VENDOR"
    assert binding.source_class == "TUSHARE_COMPATIBLE_PROXY"
    assert binding.binding_hash.startswith("sha256:")


@pytest.mark.parametrize(
    "changes",
    [
        {"provider_class": "OFFICIAL_TUSHARE"},
        {"source_class": "OFFICIAL_TUSHARE"},
        {"frozen": False},
        {"content_hash": "not-a-hash"},
        {"approved_policy_hash": HASH},
        {"approved_policy_id": "caller-invented/v1"},
        {"published_at": datetime(2026, 1, 2)},
        {
            "available_at": datetime(2026, 1, 2, 17, tzinfo=timezone.utc),
            "decision_time": datetime(2026, 1, 2, 16, tzinfo=timezone.utc),
        },
    ],
)
def test_unapproved_unfrozen_or_non_pit_binding_fails_closed(changes):
    with pytest.raises(DataRequirementError):
        _binding(**changes)


def test_plan_and_binding_are_immutable():
    plan = DataAdequacyPlanner().plan(_adequacy())
    binding = _binding()
    with pytest.raises((AttributeError, TypeError)):
        plan.required_sessions = 1
    with pytest.raises((AttributeError, TypeError)):
        binding.frozen = False


def test_real_23_have_compliance_bound_acquisition_plans_without_qualification_increase(current_acquisition):
    report = current_acquisition
    assert len(report.plans) == 23
    assert report.execution_ready_increment_count == 0
    assert report.status_counts == {
        "READY_FOR_APPROVED_CAPTURE": 2,
        "BLOCKED_PIT_PROOF": 19,
        "BLOCKED_SOURCE_APPROVAL": 2,
        "AUXILIARY_ONLY": 0,
    }
    assert all(plan.execution_ready_increment is False for plan in report.plans)


def test_every_plan_binds_approval_snapshot_clocks_hashes_and_provenance_gates(current_acquisition):
    required = {
        AcquisitionGate.APPROVED_PROVIDER_POLICY,
        AcquisitionGate.APPROVED_API_AND_FIELD_ALLOWLIST,
        AcquisitionGate.FROZEN_SNAPSHOT,
        AcquisitionGate.PUBLISHED_AT,
        AcquisitionGate.AVAILABLE_AT,
        AcquisitionGate.DECISION_TIME,
        AcquisitionGate.CONTENT_HASH,
        AcquisitionGate.ROW_PROVENANCE,
        AcquisitionGate.REVISION_AS_OF,
        AcquisitionGate.EXACT_FIELD_SEMANTICS,
    }
    report = current_acquisition
    assert all(set(plan.compliance_gates) == required for plan in report.plans)
    assert all(plan.requirement_hash.startswith("sha256:") for plan in report.plans)


def test_vendor_ready_plans_use_only_closed_approved_api_schemas(current_acquisition):
    plans = {
        plan.factor_id: plan
        for plan in current_acquisition.plans
        if plan.status is AcquisitionPlanStatus.READY_FOR_APPROVED_CAPTURE
    }
    assert plans["fund_asset_growth"].steps[0].api_name == "balancesheet"
    assert plans["fund_roe"].steps[0].api_name == "fina_indicator"
    serialized = repr(tuple(plans.values())).casefold()
    assert "official_tushare" not in serialized
    assert "endpoint" not in serialized
    assert "token" not in serialized


def test_sector_plan_is_blocked_until_effective_available_history_is_proven(current_acquisition):
    plan = next(item for item in current_acquisition.plans if item.factor_id == "alpha101_048")
    assert plan.status is AcquisitionPlanStatus.BLOCKED_PIT_PROOF
    assert plan.steps[0].api_name == "stock_basic"
    assert plan.steps[0].requested_fields == (
        "industry", "industry_available_date", "industry_effective_date", "ts_code",
    )
    assert "decision_time" in plan.terminal_precondition


def test_unavailable_exact_fields_expose_no_capture_step_or_silent_proxy(current_acquisition):
    plans = {
        item.factor_id: item
        for item in current_acquisition.plans
        if item.status is AcquisitionPlanStatus.BLOCKED_SOURCE_APPROVAL
    }
    assert set(plans) == {"fund_earnings_yield", "fund_gross_profitability"}
    assert all(item.steps == () for item in plans.values())
    assert all("substitutions remain forbidden" in item.terminal_precondition for item in plans.values())


def test_acquisition_report_replay_hash_is_stable_and_input_bound(current_acquisition, current_requirements):
    first = current_acquisition
    second = CompliantAcquisitionPlanner().build(current_requirements)
    requirements = current_requirements
    assert first.report_hash == second.report_hash
    assert first.requirements_report_hash == requirements.report_hash
    assert first.projection_hash == requirements.projection_hash


def test_acquisition_contract_rejects_credential_or_endpoint_parameter_names():
    for forbidden in ("token", "api_key", "authorization", "url", "endpoint"):
        with pytest.raises(DataRequirementError):
            ApprovedAcquisitionStep(
                sequence=1,
                api_name="daily",
                requested_fields=("close",),
                parameter_names=(forbidden,),
                purpose="invalid fixture",
            )


def test_plan_compiler_has_no_capture_side_effect_and_preserves_record_count(current_requirements):
    requirements = current_requirements
    result = CompliantAcquisitionPlanner().build(requirements)
    assert tuple(item.factor_id for item in result.plans) == tuple(item.factor_id for item in requirements.records)
    assert all("plan-only" in item.authority_note for item in result.plans)


def test_checked_in_acquisition_artifact_matches_all_23_runtime_plan_identities(current_acquisition):
    repo_root = Path(__file__).resolve().parents[4]
    path = repo_root / ".autobuild" / "veriquant" / "parallel" / "factor_data_requirements" / "acquisition_plan.json"
    artifact = json.loads(path.read_text(encoding="utf-8"))
    assert artifact["projection_hash"] == current_acquisition.projection_hash
    assert artifact["requirements_report_hash"] == current_acquisition.requirements_report_hash
    assert artifact["acquisition_report_hash"] == current_acquisition.report_hash
    assert artifact["execution_ready_increment_count"] == 0
    runtime = {plan.factor_id: (plan.plan_hash, plan.requirement_hash) for plan in current_acquisition.plans}
    checked_in = {item["factor_id"]: (item["plan_hash"], item["requirement_hash"]) for item in artifact["plans"]}
    assert checked_in == runtime
