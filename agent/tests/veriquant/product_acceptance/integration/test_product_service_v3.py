from __future__ import annotations

from pathlib import Path

from veriquant.contracts import ContractValidationError, FailureCode
from veriquant.integration.research_os_v2 import FrozenValidationDataRepository
from veriquant.research_os_v3.product_service import ResearchOSV3ProductService
from veriquant.research_run_controller import ResearchRunLifecycle


REPO_ROOT = Path(__file__).resolve().parents[5]
ACTIVE_DATA = REPO_ROOT / ".autobuild/veriquant/integration/research_os_v2/data_active"
STRICT_DATA = REPO_ROOT / ".autobuild/veriquant/product_acceptance/repair24/real_capture_v2/data"


def _service(tmp_path: Path) -> ResearchOSV3ProductService:
    repository = FrozenValidationDataRepository(ACTIVE_DATA)
    return ResearchOSV3ProductService(
        root=tmp_path / "product",
        data_loader=repository.load,
        dotenv_path=tmp_path / "missing.env",
    )


def _strict_service(tmp_path: Path) -> ResearchOSV3ProductService:
    repository = FrozenValidationDataRepository(STRICT_DATA)

    def unavailable_provider(_specialist_id):
        raise ContractValidationError(FailureCode.ADAPTER_UNAVAILABLE, "provider unavailable")

    return ResearchOSV3ProductService(
        root=tmp_path / "strict-product",
        data_loader=repository.load,
        dotenv_path=tmp_path / "missing.env",
        provider_factory=unavailable_provider,
    )


def test_fifty_sessions_is_lookback_not_automatically_the_label_horizon(tmp_path):
    service = _service(tmp_path)
    try:
        plan = service.create_plan({
            "question": "挖掘过去50日动量对未来5日收益的预测能力",
            "depth": "标准研究",
            "interactionStyle": "标准式",
        })
    finally:
        service.control.close()

    understanding = plan["researchUnderstanding"]
    assert understanding["lookbackSessions"] == [50]
    assert understanding["forecastHorizons"] == [5]
    assert plan["dataScope"]["sourceLabel"] == "APPROVED_PROXY_DATA_VENDOR"
    # The legacy active snapshot predates row-level provenance.  Declared
    # fields alone must never grant official Evidence authority.
    assert plan["dataScope"]["supportLevel"] == "DATA_ACQUISITION_REQUIRED"
    assert plan["dataScope"]["capabilityStatus"] == "UNAVAILABLE"


def test_unavailable_research_scope_still_publishes_a_typed_result_report(tmp_path):
    service = _service(tmp_path)
    try:
        plan = service.create_plan({
            "question": "新闻情绪数据是否值得接入研究系统",
            "depth": "标准研究",
            "interactionStyle": "标准式",
        })
        service.confirm_plan(
            plan["campaignId"],
            answers={
                "data_source_scope_time_semantics": (
                    "source=approved_frozen_documents; fields=sentiment,entity_mapping; "
                    "available_at=provider_publication_time"
                )
            },
        )
        manifest = service.start(plan["campaignId"])
        if manifest.current_stage not in {
            ResearchRunLifecycle.BLOCKED,
            ResearchRunLifecycle.COMPLETED,
            ResearchRunLifecycle.PARTIAL,
            ResearchRunLifecycle.FAILED,
        }:
            manifest = service.control.wait(manifest.run_id, timeout=30)
        result = service.load_result(manifest.run_id)
        report_path = service.report_root / manifest.run_id / "machine_report.json"
    finally:
        service.control.close()

    assert manifest.current_stage in {
        ResearchRunLifecycle.BLOCKED,
        ResearchRunLifecycle.COMPLETED,
        ResearchRunLifecycle.PARTIAL,
        ResearchRunLifecycle.FAILED,
    }
    assert result.status.value in {"BLOCKED", "COMPLETED", "PARTIAL", "FAILED"}
    assert any(branch.status.value in {"DATA_GAP", "PARTIAL_RESULT"} for branch in result.branches)
    assert result.official_evidence_hash is None
    assert report_path.exists()


def test_completion_exception_still_publishes_failed_report_and_receipt(tmp_path):
    service = _strict_service(tmp_path)

    def fail_completion(*_args):
        raise ContractValidationError(FailureCode.ADAPTER_EXECUTION_FAILED, "forced product completion failure")

    service.control.completion_handler = fail_completion
    try:
        plan = service.create_plan({
            "question": "研究过去20日量价因子对未来5日收益的可见证据",
            "depth": "标准研究",
            "interactionStyle": "标准式",
        })
        service.confirm_plan(plan["campaignId"])
        started = service.start(plan["campaignId"])
        final = service.control.wait(started.run_id, timeout=30)
        report_dir = service.report_root / final.run_id
    finally:
        service.control.close()

    assert final.current_stage is ResearchRunLifecycle.FAILED
    assert final.report_hash is not None
    assert (report_dir / "machine_report.json").exists()
    assert (report_dir / "report.md").exists()
    assert (report_dir / "receipt.json").exists()
