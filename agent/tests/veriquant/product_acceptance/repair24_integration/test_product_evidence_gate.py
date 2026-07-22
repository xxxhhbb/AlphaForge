from __future__ import annotations

import json
from pathlib import Path

import pytest

from veriquant.contracts import ContractValidationError
from veriquant.integration.research_os_v2 import FrozenValidationDataRepository
from veriquant.data_capability import DataRequirementPlanner
from veriquant.dynamic_data_planning import DataPlanningRequest, DynamicDataRequirementCompiler
from veriquant.research_os_v3.product_service import ResearchOSV3ProductService
from veriquant.research_real_data_v2 import ResearchDataPolicy
from veriquant.research_run_controller import ResearchRunLifecycle


REPO_ROOT = Path(__file__).resolve().parents[5]
LEGACY_DATA = REPO_ROOT / ".autobuild/veriquant/integration/research_os_v2/data_active"
STRICT_DATA = REPO_ROOT / ".autobuild/veriquant/product_acceptance/repair24/real_capture_v2/data"
TERMINAL = {
    ResearchRunLifecycle.BLOCKED,
    ResearchRunLifecycle.CANCELLED,
    ResearchRunLifecycle.COMPLETED,
    ResearchRunLifecycle.FAILED,
    ResearchRunLifecycle.PARTIAL,
}


def test_research_data_scale_is_request_bound_not_hard_capped_at_fifty() -> None:
    policy = ResearchDataPolicy(
        min_symbols_per_session=120,
        max_symbols_per_session=500,
        min_sessions=260,
        recommended_sessions_for_50d=320,
        max_sessions=520,
    )
    assert policy.min_symbols_per_session == 120
    assert policy.max_symbols_per_session == 500


def test_momentum_requirement_uses_universe_proof_not_listing_dates_as_features() -> None:
    plan = DynamicDataRequirementCompiler().compile(DataPlanningRequest(
        question="挖掘过去50日动量对未来5日收益的预测能力",
        research_mode="NEW_ALPHA_EXPLORATION",
        factor_lookback_sessions=50,
        label_horizon_sessions=5,
    ))
    requirement = DataRequirementPlanner().build(plan)
    assert "list_date" not in requirement.required_fields
    assert "delist_date" not in requirement.required_fields
    assert "turnover_rate" not in requirement.required_fields
    assert "total_mv" in requirement.required_fields


def _service(tmp_path: Path) -> ResearchOSV3ProductService:
    repository = FrozenValidationDataRepository(LEGACY_DATA)
    return ResearchOSV3ProductService(
        root=tmp_path / "product",
        data_loader=repository.load,
        dotenv_path=tmp_path / "missing.env",
    )


def test_unresolved_campaign_cannot_be_confirmed(tmp_path: Path) -> None:
    service = _service(tmp_path)
    try:
        plan = service.create_plan({
            "question": "诊断组合回撤，但portfolio_id未提供",
            "depth": "标准研究",
            "interactionStyle": "标准式",
        })
        assert "portfolio_or_existing_factor_ids" in plan["researchUnderstanding"]["unresolvedFields"]
        with pytest.raises(ContractValidationError):
            service.confirm_plan(plan["campaignId"])
    finally:
        service.control.close()


def test_non_ready_data_capability_cannot_produce_official_evidence(tmp_path: Path) -> None:
    service = _service(tmp_path)
    try:
        plan = service.create_plan({
            "question": "挖掘过去50日动量对未来5日收益的预测能力",
            "depth": "标准研究",
            "interactionStyle": "标准式",
        })
        assert plan["dataScope"]["capabilityStatus"] == "UNAVAILABLE"
        service.confirm_plan(plan["campaignId"])
        manifest = service.start(plan["campaignId"], seed=7)
        if manifest.current_stage not in TERMINAL:
            manifest = service.control.wait(manifest.run_id, timeout=30)
        gate = json.loads(
            (service.root / "products" / manifest.run_id / "evidence_gate.json").read_text(encoding="utf-8")
        )
        assert gate["authorized"] is False
        assert "DATA_CAPABILITY_NOT_READY" in gate["failure_codes"]
        assert service.load_evidence(manifest.run_id) is None
        assert service.load_selection(manifest.run_id) is None
        manifest = service.status(manifest.run_id)
        assert manifest.official_evidence_hash is None
        assert (service.report_root / manifest.run_id / "machine_report.json").exists()
    finally:
        service.control.close()


def test_research_only_question_cannot_use_price_snapshot_as_official_evidence(tmp_path: Path) -> None:
    repository = FrozenValidationDataRepository(STRICT_DATA)
    service = ResearchOSV3ProductService(
        root=tmp_path / "research-only",
        data_loader=lambda: repository.load(require_strict_provenance=True),
        dotenv_path=tmp_path / "missing.env",
    )
    try:
        plan = service.create_plan({
            "question": "新闻情绪数据是否值得接入研究系统",
            "depth": "标准研究",
            "interactionStyle": "标准式",
        })
        answers = {
            field: "source=approved_documents;fields=sentiment,entity_mapping;available_at=publication_time"
            for field in plan["researchUnderstanding"]["unresolvedFields"]
        }
        confirmed = service.confirm_plan(plan["campaignId"], answers=answers)
        started = service.start(confirmed["campaignId"], seed=7)
        terminal = service.control.wait(started.run_id, timeout=60)
        gate = json.loads(
            (service.root / "products" / started.run_id / "evidence_gate.json").read_text(encoding="utf-8")
        )

        assert terminal.report_hash is not None
        assert terminal.official_evidence_hash is None
        assert gate["authorized"] is False
        assert gate["question_data_support_level"] == "RESEARCH_ONLY"
        assert "QUESTION_DATA_SEMANTICS_NOT_OFFICIAL_READY" in gate["failure_codes"]
        assert service.load_evidence(started.run_id) is None
    finally:
        service.control.close()
