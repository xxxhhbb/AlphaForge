from __future__ import annotations

import shutil
import importlib
import sys
from dataclasses import dataclass
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api.research_os_v3_routes import (
    configure_research_os_v3_roots,
    research_os_v3_route_paths,
    research_os_v3_router,
)


REPO_ROOT = Path(__file__).resolve().parents[4]
U4_SOURCE = REPO_ROOT / ".autobuild/veriquant/research_os_v3_u4"
U5_SOURCE = REPO_ROOT / ".autobuild/veriquant/research_os_v3_u5/reports"
RUN_ID = "u4-real-multi-09e58c539c1c"


def _client(tmp_path: Path) -> TestClient:
    run_root = tmp_path / "u4"
    report_root = tmp_path / "reports"
    shutil.copytree(U4_SOURCE, run_root)
    shutil.copytree(U5_SOURCE, report_root)
    configure_research_os_v3_roots(run_root=run_root, report_root=report_root)
    app = FastAPI()
    app.include_router(research_os_v3_router)
    return TestClient(app)


def _central_api_module():
    """Import the central API after removing package-local config test stubs."""

    accessor = sys.modules.get("src.config.accessor")
    if accessor is not None and not hasattr(accessor, "get_env_config"):
        sys.modules.pop("src.config.accessor", None)
        sys.modules.pop("src.config", None)
        sys.modules.pop("api_server", None)
        importlib.invalidate_caches()
    return importlib.import_module("api_server")


def test_v3_api_projects_one_canonical_manifest_and_public_report(tmp_path):
    client = _client(tmp_path)
    response = client.get(f"/api/veriquant/research-os/v3/runs/{RUN_ID}")
    assert response.status_code == 200
    payload = response.json()
    assert payload["result"]["manifest_hash"] == payload["canonical_manifest_hash"]
    assert payload["report"]["report_hash"]
    assert payload["result"]["candidate_count"] == 20
    assert payload["result"]["official_evidence_hash"] is None
    assert payload["result"]["promotion_hash"] is None
    assert payload["report"]["candidate_count"] == 20
    assert len(payload["report"]["candidates"]) == 20
    assert payload["report"]["selected_candidate_hashes"] == []
    assert all(item["promotion_status"] == "NOT_REQUESTED" for item in payload["report"]["candidates"])
    rendered = response.text
    assert all(token not in rendered for token in ("OFFICIAL_TUSHARE", "api_key", "authorization", "bearer "))


def test_v3_api_lists_and_serves_markdown(tmp_path):
    client = _client(tmp_path)
    listing = client.get("/api/veriquant/research-os/v3/runs")
    assert listing.status_code == 200
    assert RUN_ID in {item["run_id"] for item in listing.json()["runs"]}
    markdown = client.get(f"/api/veriquant/research-os/v3/runs/{RUN_ID}/report.md")
    assert markdown.status_code == 200
    assert "AlphaForge" in markdown.text and "正式指标：无" in markdown.text


def test_v3_api_path_traversal_fails_closed(tmp_path):
    client = _client(tmp_path)
    response = client.get("/api/veriquant/research-os/v3/runs/%2E%2E%5Csecret")
    assert response.status_code in {400, 404}


def test_corrupt_product_artifact_is_not_masked_by_legacy_fallback(tmp_path, monkeypatch):
    """Once a V3 product publication exists, legacy data cannot hide corruption."""

    import src.api.research_os_v3_routes as routes

    client = _client(tmp_path)
    product_reports = tmp_path / "product_reports"
    product_runtime = tmp_path / "product_runtime"
    product_publication = product_reports / RUN_ID
    product_publication.mkdir(parents=True)
    (product_publication / "machine_report.json").write_text("{broken", encoding="utf-8")
    monkeypatch.setattr(routes.research_os_v3_product_service, "report_root", product_reports)
    monkeypatch.setattr(routes.research_os_v3_product_service, "run_root", product_runtime)

    response = client.get(f"/api/veriquant/research-os/v3/runs/{RUN_ID}")

    assert response.status_code == 409
    assert "损坏" in response.json()["detail"]


def test_v3_route_table_is_read_only_and_has_no_trading_path():
    paths = research_os_v3_route_paths()
    assert paths
    assert all(all(token not in path.lower() for token in ("/live", "broker", "order", "oauth", "mandate")) for path in paths)
    assert all("POST" not in route.methods for route in research_os_v3_router.routes)


def test_standalone_offline_app_includes_v3_without_live_routes(tmp_path):
    import veriquant_api_server

    run_root = tmp_path / "u4"
    report_root = tmp_path / "reports"
    shutil.copytree(U4_SOURCE, run_root)
    shutil.copytree(U5_SOURCE, report_root)
    app = veriquant_api_server.create_app(
        artifact_root=tmp_path / "workbench",
        research_run_root=run_root,
        research_report_root=report_root,
    )
    manifest = veriquant_api_server.build_route_manifest(app, artifact_root=tmp_path / "workbench")
    paths = {route.path for route in manifest.routes}
    assert f"/api/veriquant/research-os/v3/runs/{{run_id}}" in paths
    assert all(not any(token in path.lower() for token in ("/live", "broker", "order", "oauth", "mandate")) for path in paths)
    assert manifest.offline_only is True and manifest.provider_keys_required is False


def test_central_api_registers_v3_read_routes():
    api_server = _central_api_module()

    configure_research_os_v3_roots(run_root=U4_SOURCE, report_root=U5_SOURCE)
    client = TestClient(api_server.app)
    response = client.get(f"/api/veriquant/research-os/v3/runs/{RUN_ID}")
    assert response.status_code == 200
    assert response.json()["result"]["run_id"] == RUN_ID


def test_central_api_exposes_v3_confirmed_campaign_controls():
    api_server = _central_api_module()

    client = TestClient(api_server.app)
    planned = client.post(
        "/api/veriquant/research-os/v3/plans",
        json={
            "question": "验证50日动量候选",
            "depth": "标准研究",
            "interactionStyle": "标准式",
        },
    )
    assert planned.status_code == 200
    plan = planned.json()
    assert plan["state"] == "待确认"

    assert plan["researchUnderstanding"]["unresolvedFields"] == ["forecast_horizons"]
    confirmed = client.post(
        f"/api/veriquant/research-os/v3/plans/{plan['campaignId']}/confirm",
        json={"answers": {"forecast_horizons": "5d"}},
    )
    assert confirmed.status_code == 200
    assert confirmed.json()["state"] == "已确认"


def test_campaign_data_preparation_requires_explicit_network_consent():
    api_server = _central_api_module()
    client = TestClient(api_server.app)

    response = client.post(
        "/api/veriquant/research-os/v3/plans/any-plan/prepare-data",
        json={"endDate": "20260720"},
    )

    assert response.status_code == 400
    assert "allowNetwork=true" in response.json()["detail"]


def test_campaign_data_preparation_response_redacts_provider_secret(monkeypatch):
    import src.api.research_os_v3_routes as routes

    app = FastAPI()
    app.include_router(routes.research_os_v3_control_router)
    client = TestClient(app)

    secret = "test-secret-must-never-leak"
    endpoint_globals = routes.prepare_research_plan_data.__globals__
    monkeypatch.setitem(
        endpoint_globals,
        "_load_proxy_settings",
        lambda _path: {
            "TUSHARE_PROXY_URL": "https://ts.gyzcloud.top/api",
            "TUSHARE_TOKEN": secret,
        },
    )
    monkeypatch.setitem(endpoint_globals, "create_proxy_client", lambda _config: object())

    @dataclass(frozen=True)
    class Result:
        status: object
        plan_id: str = "safe-plan"
        data_plan_hash: str = "sha256:" + "1" * 64
        dataset_bundle_hash: str = "sha256:" + "2" * 64
        snapshot_hash: str = "sha256:" + "3" * 64
        freeze_manifest_hash: str = "sha256:" + "4" * 64
        symbol_count: int = 20
        session_count: int = 180
        fields: tuple[str, ...] = ("close", "roe")
        failure_codes: tuple[str, ...] = ()

    class Status:
        value = "READY"

    class Preparer:
        def __init__(self, **_kwargs):
            pass

        def prepare(self, **_kwargs):
            return Result(status=Status())

    monkeypatch.setitem(endpoint_globals, "CampaignDataPreparationService", Preparer)
    updated_plan = {"campaignId": "safe-plan", "state": "待确认", "dataScope": {"availability": "可用"}}
    monkeypatch.setattr(
        endpoint_globals["research_os_v3_product_service"],
        "plan_view",
        lambda plan_id: updated_plan if plan_id == "safe-plan" else None,
    )
    response = client.post(
        "/api/veriquant/research-os/v3/plans/safe-plan/prepare-data",
        json={"allowNetwork": True, "endDate": "20260720"},
    )

    assert response.status_code == 200
    rendered = response.text
    assert response.json()["status"] == "READY"
    assert response.json()["plan"] == updated_plan
    assert secret not in rendered
    assert "TUSHARE_TOKEN" not in rendered


def test_central_status_projects_real_per_specialist_counts():
    api_server = _central_api_module()

    client = TestClient(api_server.app)
    response = client.get("/api/veriquant/research-os/v3/runs/run-736ea8e478cc6b45ee13/status")
    assert response.status_code == 200
    summaries = {item["specialist_id"]: item for item in response.json()["specialist_summaries"]}
    assert summaries["fundamental"]["candidate_count"] == 7
    assert summaries["market_structure"]["candidate_count"] == 12
    assert summaries["meta_alpha"]["status"] == "DATA_GAP"
    assert summaries["meta_alpha"]["candidate_count"] == 0
