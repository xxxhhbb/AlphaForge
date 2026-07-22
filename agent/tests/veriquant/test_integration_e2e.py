from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.api.veriquant_workbench_routes import HTTPException, WorkbenchArtifactRepository
from src.veriquant.integration import (
    WorkbenchPublicationError,
    build_acceptance_result,
    probe_optional_capabilities,
    publish_workbench_run,
)
from src.veriquant.integration.e2e import run_offline_acceptance


REPO_ROOT = Path(__file__).resolve().parents[3]


def test_real_a_to_f_handoffs_are_all_eligible() -> None:
    result = build_acceptance_result(REPO_ROOT)
    packages = {item["package_id"]: item for item in result["capability_packages"]}

    assert set(packages) == {
        "real_research",
        "statistical_promotion",
        "llm_desks",
        "research_memory",
        "shadow_portfolio",
        "workbench",
    }
    assert all(item["eligible"] for item in packages.values())
    assert all(not item["blockers"] for item in packages.values())


def test_offline_a_f_chain_is_automatic_reproducible_and_workbench_readable(tmp_path: Path) -> None:
    first = run_offline_acceptance(output_root=tmp_path / "first", seed=7)
    second = run_offline_acceptance(output_root=tmp_path / "second", seed=7)

    assert first["status"] == "PASSED"
    assert first["artifact_hash"] == second["artifact_hash"]
    assert first["artifact_hashes"] == second["artifact_hashes"]
    assert first["promotion"]["state"] == "CHALLENGER"
    assert first["shadow"]["idempotent_replay"] is True
    assert first["shadow"]["ledger_events"] == 5
    assert first["research_desks"]["completed"] == ["expectations_events"]
    assert first["research_desks"]["failed_or_incomplete"]
    assert first["document_branch"] == {
        "status": "DRAFT_ONLY",
        "validation_evidence_hash": first["document_branch"]["validation_evidence_hash"],
        "included_in_promotion_family": False,
    }

    parsed = json.loads((tmp_path / "first" / "result.json").read_text(encoding="utf-8"))
    assert parsed == first
    assert (tmp_path / "first" / "report.md").is_file()
    repository = WorkbenchArtifactRepository(tmp_path / "first" / "workbench_artifacts")
    detail = repository.get_run_detail(first["run_id"])
    assert detail["run"]["status"] == "SHADOW_ACTIVE"
    assert detail["promotion"]["decision"]["to_state"] == "CHALLENGER"
    assert detail["citations"]["status"] == "DRAFT_ONLY"


def test_workbench_projection_contains_no_sealed_values_secrets_or_local_paths(tmp_path: Path) -> None:
    result = run_offline_acceptance(output_root=tmp_path / "run", seed=7)
    detail = WorkbenchArtifactRepository(tmp_path / "run" / "workbench_artifacts").get_run_detail(result["run_id"])
    rendered = json.dumps(detail, ensure_ascii=False, sort_keys=True).lower()

    for forbidden in (
        "sealed_metrics",
        "sealed_handle_hash",
        "raw_sealed",
        "authorization: bearer",
        "tushare_token",
        "api_key",
        str(tmp_path).lower(),
    ):
        assert forbidden not in rendered
    assert "expected_return" not in detail["citations"]
    assert detail["citations"]["direct_downstream_authority"] is False


def test_workbench_publication_rejects_unknown_artifact_and_path_traversal(tmp_path: Path) -> None:
    with pytest.raises(WorkbenchPublicationError):
        publish_workbench_run(
            root=tmp_path,
            run_id="../escape",
            status="DRAFT",
            updated_at="2024-01-01T00:00:00Z",
            artifacts={},
        )
    with pytest.raises(WorkbenchPublicationError):
        publish_workbench_run(
            root=tmp_path,
            run_id="safe-run",
            status="DRAFT",
            updated_at="2024-01-01T00:00:00Z",
            artifacts={"arbitrary_file": {}},
        )
    repository = WorkbenchArtifactRepository(tmp_path)
    with pytest.raises(HTTPException):
        repository.get_run_detail("../escape")


def test_optional_capability_probe_is_boolean_only_and_offline_does_not_require_optional_services() -> None:
    probe = probe_optional_capabilities()

    assert probe["deterministic_offline_ready"] is True
    assert probe["vibe_agentloop_required"] is False
    assert probe["real_broker_enabled"] is False
    assert all(isinstance(value, bool) for value in probe.values())


def test_public_route_nav_and_i18n_are_wired_without_write_or_live_order_surface() -> None:
    api_server = (REPO_ROOT / "agent" / "api_server.py").read_text(encoding="utf-8")
    router = (REPO_ROOT / "frontend" / "src" / "router.tsx").read_text(encoding="utf-8")
    layout = (REPO_ROOT / "frontend" / "src" / "components" / "layout" / "Layout.tsx").read_text(encoding="utf-8")

    assert "app.include_router(veriquant_workbench_router)" in api_server
    assert 'path: "/veriquant/workbench"' in router
    # Product navigation now starts from the research question. Workbench is
    # retained as a read-only evidence-detail route, not a primary nav item.
    assert 'to: "/research"' in layout
    assert 'to: "/veriquant/workbench"' not in layout
    for locale in ("en", "zh-CN", "ja", "ko", "ar"):
        payload = json.loads(
            (REPO_ROOT / "frontend" / "src" / "i18n" / "locales" / f"{locale}.json").read_text(encoding="utf-8")
        )
        assert payload["layout"]["veriquantWorkbench"]

    integration_sources = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (REPO_ROOT / "agent" / "src" / "veriquant" / "integration").glob("*.py")
    )
    assert "place_order" not in integration_sources.lower()
    assert "send_order" not in integration_sources.lower()
    assert "broker_client" not in integration_sources.lower()
