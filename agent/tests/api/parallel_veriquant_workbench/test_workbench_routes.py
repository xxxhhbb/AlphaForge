from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from src.api.veriquant_workbench_routes import (
    HTTPException,
    InMemoryArtifactRepository,
    WorkbenchArtifactRepository,
    get_workbench_citations,
    get_workbench_run,
    list_workbench_runs,
    redact_for_workbench,
    route_paths,
    set_artifact_repository,
    summarize_routes,
)


def _write(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")


def _artifact_root(tmp_path: Path) -> Path:
    root = tmp_path / "artifacts"
    _write(
        root / "index.json",
        {
            "runs": [
                {
                    "run_id": "run-validated",
                    "title": "Validated fixture",
                    "status": "VALIDATED_RESEARCH",
                    "updated_at": "2026-07-18T02:00:00Z",
                    "local_path": r"D:\secret\artifact.json",
                },
                {
                    "run_id": "run-draft",
                    "title": "Draft fixture",
                    "status": "DRAFT",
                    "updated_at": "2026-07-18T01:00:00Z",
                },
            ]
        },
    )
    run_dir = root / "runs" / "run-validated"
    _write(
        run_dir / "report.json",
        {
            "title": "正式中文研究报告",
            "markdown": "# 一页结论\n\n机器证据支持继续研究。",
            "report_hash": "sha256:report",
        },
    )
    _write(
        run_dir / "overview.json",
        {
            "run_id": "run-validated",
            "status": "VALIDATED_RESEARCH",
            "research_dag": {"nodes": [{"id": "desk:fundamental", "status": "VALIDATED_RESEARCH"}]},
            "absolute_path": r"C:\private-test\.env",
        },
    )
    _write(
        run_dir / "lineage.json",
        {
            "candidates": [
                {"candidate_id": "cand-1", "family_id": "fam-1", "status": "VALIDATED_RESEARCH", "operator": "original"}
            ]
        },
    )
    _write(
        run_dir / "evidence.json",
        {
            "items": [{"evidence_id": "ev-visible", "metric": "net_icir", "value": 1.25}],
            "sealed_metrics": {"rank": 1, "return": 99.0},
            "authorization": "Bearer should-not-leak-123456789",
        },
    )
    _write(
        run_dir / "critics.json",
        {"findings": [{"critic": "StatisticalCritic", "verdict": "VALIDATED_RESEARCH", "message": "visible only"}]},
    )
    _write(
        run_dir / "promotion.json",
        {
            "status": "CHALLENGER",
            "sealed_status": "PASSED_REDACTED",
            "sealed_rows": [{"symbol": "LEAK"}],
            "private_reason": r"D:\private\sealed.csv",
        },
    )
    _write(
        run_dir / "citations.json",
        {
            "items": [
                {
                    "document_id": "doc-1",
                    "title": "News fixture",
                    "snippet": "<img src=x onerror=alert(1)>margin pressure described as text",
                    "source_content_hash": "sha256:abc",
                    "available_at": "2026-01-03T00:00:00Z",
                }
            ]
        },
    )
    _write(
        run_dir / "shadow_portfolio.json",
        {"vintages": [{"vintage_id": "paper-20260718", "gross_exposure": 1.0, "net_exposure": 0.0}]},
    )
    _write(
        run_dir / "nav_attribution.json",
        {"attribution": [{"label": "alpha", "value": 0.012}]},
    )
    _write(
        run_dir / "drift_alerts.json",
        {"alerts": [{"code": "COST_DRIFT", "message": "cost increased"}]},
    )
    return root


def test_fixture_api_lists_and_reads_redacted_detail(tmp_path: Path) -> None:
    repo = WorkbenchArtifactRepository(_artifact_root(tmp_path))

    runs = repo.list_runs()
    assert [run["run_id"] for run in runs] == ["run-validated", "run-draft"]
    assert runs[0]["status"] == "VALIDATED_RESEARCH"

    detail = repo.get_run_detail("run-validated")
    encoded = json.dumps(detail, sort_keys=True)
    assert "net_icir" in encoded
    assert "LEAK" not in encoded
    assert "should-not-leak" not in encoded
    assert "C:\\Users" not in encoded
    assert "D:\\private" not in encoded
    assert detail["promotion"]["sealed_status"] == "PASSED_REDACTED"
    assert detail["report"]["markdown"].startswith("# 一页结论")


@pytest.mark.parametrize("bad_id", ["..", "../run-validated", "run/validated", r"run\validated", "C:secret", ""])
def test_path_traversal_and_absolute_locator_rejected(tmp_path: Path, bad_id: str) -> None:
    repo = WorkbenchArtifactRepository(_artifact_root(tmp_path))
    with pytest.raises(HTTPException) as exc:
        repo.get_run_detail(bad_id)
    assert exc.value.status_code == 400
    assert "artifact" not in str(exc.value.detail).lower() or "locator" not in str(exc.value.detail).lower()


def test_empty_repository_returns_empty_state(tmp_path: Path) -> None:
    repo = WorkbenchArtifactRepository(tmp_path / "empty")
    assert repo.list_runs() == []


def test_failed_incomplete_and_unknown_status_are_safe() -> None:
    repo = InMemoryArtifactRepository(
        runs=[
            {"run_id": "r1", "status": "REJECTED"},
            {"run_id": "r2", "status": "INCOMPLETE"},
            {"run_id": "r3", "status": "BLOCKED"},
        ],
        details={
            "r1": {"run": {"run_id": "r1", "status": "REJECTED"}},
            "r2": {"run": {"run_id": "r2", "status": "INCOMPLETE"}},
            "r3": {"run": {"run_id": "r3", "status": "BLOCKED"}},
        },
    )
    assert [row["status"] for row in repo.list_runs()] == ["REJECTED", "INCOMPLETE", "BLOCKED"]


def test_independent_router_endpoints_with_fixture_repository() -> None:
    repo = InMemoryArtifactRepository(
        runs=[{"run_id": "run-fixture", "status": "CHALLENGER"}],
        details={
            "run-fixture": {
                "run": {"run_id": "run-fixture", "status": "CHALLENGER"},
                "citations": {"items": [{"document_id": "doc-1", "snippet": "plain text"}]},
            }
        },
    )
    set_artifact_repository(repo)
    try:
        listed = asyncio.run(list_workbench_runs())
        detail = asyncio.run(get_workbench_run("run-fixture"))
        citations = asyncio.run(get_workbench_citations("run-fixture"))
    finally:
        set_artifact_repository(None)

    assert listed["runs"][0]["run_id"] == "run-fixture"
    assert detail["detail"]["run"]["status"] == "CHALLENGER"
    assert citations["citations"]["items"][0]["snippet"] == "plain text"


def test_citation_chain_preserves_untrusted_html_as_data(tmp_path: Path) -> None:
    repo = WorkbenchArtifactRepository(_artifact_root(tmp_path))
    citations = repo.get_citations("run-validated")
    snippet = citations["items"][0]["snippet"]
    assert "<img src=x onerror=alert(1)>" in snippet
    assert isinstance(snippet, str)


def test_redaction_removes_secret_keys_sealed_raw_data_and_local_paths() -> None:
    payload = {
        "api_key": "sk-abc123456789",
        "cookie": "session=secret",
        "sealed_status": "REDACTED_PASS",
        "sealed_ranking": [1, 2, 3],
        "nested": {"path": r"D:\secret\file.csv"},
        "source_url": "https://example.test/report",
    }
    redacted = redact_for_workbench(payload)
    encoded = json.dumps(redacted, sort_keys=True)
    assert "sk-abc" not in encoded
    assert "session=secret" not in encoded
    assert "D:\\secret" not in encoded
    assert redacted["sealed_status"] == "REDACTED_PASS"
    assert redacted["source_url"] == "https://example.test/report"


def test_router_summary_is_unregistered_and_lists_expected_paths() -> None:
    paths = route_paths()
    summary = summarize_routes(paths)
    assert summary["registered"] is False
    assert summary["router_symbol"] == "veriquant_workbench_router"
    for suffix in [
        "/veriquant/workbench/runs",
        "/veriquant/workbench/runs/{run_id}",
        "/veriquant/workbench/runs/{run_id}/lineage",
        "/veriquant/workbench/runs/{run_id}/evidence",
        "/veriquant/workbench/runs/{run_id}/critics",
        "/veriquant/workbench/runs/{run_id}/promotion",
        "/veriquant/workbench/runs/{run_id}/citations",
        "/veriquant/workbench/runs/{run_id}/shadow-portfolio",
        "/veriquant/workbench/runs/{run_id}/nav-attribution",
        "/veriquant/workbench/runs/{run_id}/drift-alerts",
    ]:
        assert suffix in paths
