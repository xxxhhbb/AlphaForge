from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from veriquant.research_toolbox.contracts import (
    CoverageFailureCode,
    CoverageStatus,
    ExternalApprovalNeed,
    RegisteredContextReceipt,
    ResearchAuthority,
    canonical_hash,
)
from veriquant.research_toolbox.coverage import DataCoverageResolver
from veriquant.research_toolbox.handles import ContentAddressedHandleStore
from veriquant.research_toolbox.registry import (
    AUXILIARY_DATA_IDS,
    OFFICIAL_DATA_IDS,
    REGISTERED_CONTEXT_DATA_IDS,
    REQUIRED_DATA_IDS,
    REQUIRED_DATA_TO_TOOL,
    ResearchToolRegistry,
    SPECIALIST_TOOL_ALLOWLISTS,
    validate_required_data,
)
from veriquant.research_data_gateway.models import (
    DataAuthority,
    DataSourceReceipt,
    GatewayStatus,
    ResearchDataGatewayManifest,
)


HASH = "sha256:" + "1" * 64


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[4]


def _receipt(source_id: str, *, status: str = "SUCCESS", authority: str = "OFFICIAL_INPUT"):
    return SimpleNamespace(
        source_id=source_id,
        status=status,
        authority=authority,
        content_hash=canonical_hash({"source_id": source_id, "status": status}),
    )


def _manifest(*receipts, status="READY"):
    return SimpleNamespace(status=status, receipts=tuple(receipts))


def test_all_500_required_data_values_have_one_static_mapping():
    catalog = json.loads(
        (_repo_root() / ".autobuild/veriquant/factor_question_500/catalog.json").read_text(
            encoding="utf-8"
        )
    )
    questions = catalog["questions"]
    assert len(questions) == 500
    seen = {item for question in questions for item in question["required_data"]}
    assert seen == REQUIRED_DATA_IDS
    assert set(REQUIRED_DATA_TO_TOOL) == REQUIRED_DATA_IDS
    assert OFFICIAL_DATA_IDS.isdisjoint(AUXILIARY_DATA_IDS)
    assert OFFICIAL_DATA_IDS.isdisjoint(REGISTERED_CONTEXT_DATA_IDS)
    assert AUXILIARY_DATA_IDS.isdisjoint(REGISTERED_CONTEXT_DATA_IDS)
    for question in questions:
        assert validate_required_data(question["required_data"])


def test_specialist_registry_is_static_and_rejects_tool_secret_path_and_endpoint_injection():
    registry = ResearchToolRegistry()
    assert tuple(sorted(SPECIALIST_TOOL_ALLOWLISTS)) == (
        "alternative_data",
        "expectations_events",
        "fundamental",
        "macro_regime",
        "market_structure",
        "meta_alpha",
    )
    call = registry.authorize(
        specialist_id="market_structure",
        tool_id="load_asof_market_data",
        parameters={"symbols": ["000001.SZ"], "start": "2024-01-01"},
    )
    assert call.tool_id == "load_asof_market_data"
    for tool in ("bash", "read_file", "trading_place_order", "mcp_injected"):
        with pytest.raises(PermissionError):
            registry.authorize(specialist_id="market_structure", tool_id=tool)
    bad_parameters = (
        {"token": "not-serialized"},
        {"nested": {"path": "fixture"}},
        {"value": "C:\\private\\data.csv"},
        {"value": "../sealed/data.json"},
        {"value": "Authorization: Bearer hidden"},
        {"endpoint": "https://unapproved.invalid"},
    )
    for value in bad_parameters:
        with pytest.raises(PermissionError):
            registry.authorize(
                specialist_id="market_structure",
                tool_id="load_asof_market_data",
                parameters=value,
            )


def test_missing_run_proof_never_becomes_official_ready():
    decision = DataCoverageResolver().resolve(
        required_data=("daily",),
        gateway_manifest=_manifest(_receipt("daily")),
    )
    assert decision.status is CoverageStatus.RESEARCH_READY
    assert decision.research_only_data == ("daily",)
    assert CoverageFailureCode.CAPABILITY_PROOF_MISSING in decision.failure_codes


def test_official_ready_requires_real_gateway_receipt_and_ready_capability():
    decision = DataCoverageResolver().resolve(
        required_data=("daily", "adj_factor"),
        gateway_manifest=_manifest(_receipt("daily"), _receipt("adj_factor")),
        capability_report=SimpleNamespace(status="READY", artifact_hash=HASH),
    )
    assert decision.status is CoverageStatus.OFFICIAL_READY
    assert decision.satisfied_data == ("adj_factor", "daily")
    assert decision.failure_codes == ()
    assert decision.artifact_hash == DataCoverageResolver().resolve(
        required_data=("adj_factor", "daily"),
        gateway_manifest=_manifest(_receipt("adj_factor"), _receipt("daily")),
        capability_report=SimpleNamespace(status="READY", artifact_hash=HASH),
    ).artifact_hash


def test_resolver_consumes_real_gateway_contracts():
    receipt = DataSourceReceipt(
        source_id="daily",
        authority=DataAuthority.OFFICIAL_INPUT,
        status="SUCCESS",
        row_count=10,
        content_hash=canonical_hash({"rows": 10}),
    )
    manifest = ResearchDataGatewayManifest(
        request_hash=canonical_hash("request"),
        status=GatewayStatus.READY,
        source_label="APPROVED_PROXY_DATA_VENDOR",
        official_tushare=False,
        historical_symbol_count=2,
        historical_symbols_hash=canonical_hash(["000001.SZ", "600000.SH"]),
        calendar_exchanges=("SSE", "SZSE"),
        official_freeze_hash=canonical_hash("freeze"),
        campaign_dataset_hash=canonical_hash("dataset"),
        receipts=(receipt,),
    )
    decision = DataCoverageResolver().resolve(
        required_data=("daily",),
        gateway_manifest=manifest,
        capability_report=SimpleNamespace(status="READY", artifact_hash=canonical_hash("capability")),
    )
    assert decision.status is CoverageStatus.OFFICIAL_READY
    assert manifest.artifact_hash in decision.evidence_hashes


def test_auxiliary_cannot_masquerade_as_official_gateway_input():
    decision = DataCoverageResolver().resolve(
        required_data=("daily",),
        gateway_manifest=_manifest(
            _receipt("daily", authority=ResearchAuthority.AUXILIARY_RESEARCH_EVIDENCE.value)
        ),
        capability_report=SimpleNamespace(status="READY"),
    )
    assert decision.status is CoverageStatus.UNRESOLVED


def test_empty_required_official_source_is_unresolved():
    decision = DataCoverageResolver().resolve(
        required_data=("forecast",),
        gateway_manifest=_manifest(_receipt("forecast", status="EMPTY")),
        capability_report=SimpleNamespace(status="READY"),
    )
    assert decision.status is CoverageStatus.UNRESOLVED
    assert decision.unresolved_data == ("forecast",)
    assert CoverageFailureCode.REQUIRED_SOURCE_EMPTY in decision.failure_codes


def test_empty_official_source_with_disclosed_auxiliary_proxy_is_research_ready():
    proxy_hash = canonical_hash({"express": "frozen", "reports": "frozen"})
    decision = DataCoverageResolver().resolve(
        required_data=("forecast",),
        gateway_manifest=_manifest(_receipt("forecast", status="EMPTY")),
        capability_report=SimpleNamespace(status="READY", artifact_hash=HASH),
        auxiliary_receipts=(
            {
                "source_id": "forecast_proxy_express_documents",
                "proxy_for": "forecast",
                "status": "SUCCESS",
                "authority": ResearchAuthority.AUXILIARY_RESEARCH_EVIDENCE.value,
                "row_count": 12,
                "content_hash": proxy_hash,
            },
        ),
    )
    assert decision.status is CoverageStatus.RESEARCH_READY
    assert decision.satisfied_data == ()
    assert decision.research_only_data == ("forecast",)
    assert proxy_hash in decision.evidence_hashes
    assert CoverageFailureCode.AUXILIARY_ONLY in decision.failure_codes


def test_auxiliary_receipt_can_never_upgrade_to_official():
    decision = DataCoverageResolver().resolve(
        required_data=("stock_news",),
        gateway_manifest=_manifest(status="READY"),
        capability_report=SimpleNamespace(status="READY"),
        auxiliary_receipts=(
            _receipt(
                "stock_news",
                authority=ResearchAuthority.AUXILIARY_RESEARCH_EVIDENCE.value,
            ),
        ),
    )
    assert decision.status is CoverageStatus.RESEARCH_READY
    assert decision.satisfied_data == ()
    assert decision.research_only_data == ("stock_news",)
    assert CoverageFailureCode.AUXILIARY_ONLY in decision.failure_codes


def test_f25_requires_registered_context_receipts():
    required = ("portfolio_returns", "registered_evidence", "registered_factor_summary")
    missing = DataCoverageResolver().resolve(required_data=required)
    assert missing.status is CoverageStatus.UNRESOLVED
    assert missing.unresolved_data == tuple(sorted(required))
    assert CoverageFailureCode.REGISTERED_CONTEXT_MISSING in missing.failure_codes
    receipts = tuple(
        RegisteredContextReceipt(data_id=item, content_hash=canonical_hash(item))
        for item in required
    )
    ready = DataCoverageResolver().resolve(
        required_data=required,
        registered_context_receipts=receipts,
    )
    assert ready.status is CoverageStatus.RESEARCH_READY
    assert ready.research_only_data == tuple(sorted(required))


def test_external_approval_is_explicit_not_inferred_from_internal_gap():
    ordinary = DataCoverageResolver().resolve(required_data=("research_reports",))
    assert ordinary.status is CoverageStatus.UNRESOLVED
    external = DataCoverageResolver().resolve(
        required_data=("research_reports",),
        external_approval_needs=(
            ExternalApprovalNeed(
                data_id="research_reports", reason_code="NEW_VENDOR_LICENSE_REQUIRED"
            ),
        ),
    )
    assert external.status is CoverageStatus.NEEDS_EXTERNAL_APPROVAL


def test_content_handles_are_stable_pathless_and_detect_tamper(tmp_path):
    store = ContentAddressedHandleStore(tmp_path / "objects")
    first = store.put_json({"symbol": "000001.SZ", "rows": [1, 2]})
    second = store.put_json({"rows": [1, 2], "symbol": "000001.SZ"})
    assert first == second
    assert store.read_json(first) == {"rows": [1, 2], "symbol": "000001.SZ"}
    for injected in ("../secret", "C:\\secret", "/etc/passwd", "sha256:xyz"):
        with pytest.raises(ValueError):
            store.read_bytes(injected)
    object_path = (tmp_path / "objects" / first.handle_id[7:9] / first.handle_id[9:])
    object_path.write_bytes(b"tampered")
    with pytest.raises(ValueError, match="integrity"):
        store.read_bytes(first)
