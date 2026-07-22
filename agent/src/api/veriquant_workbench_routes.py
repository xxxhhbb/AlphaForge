"""Read-only VeriQuant Workbench routes.

This module is deliberately not registered in ``api_server.py``.  Prompt F owns
only this standalone router and the Integration Agent decides whether/where to
include it.

The router reads frozen Workbench artifacts through ``ArtifactRepository``.  It
does not calculate alpha, promote candidates, construct portfolios, place
orders, read secrets, or expose sealed raw data.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Protocol
from urllib.parse import urlparse

try:  # FastAPI is optional in the local parallel-test environment.
    from fastapi import APIRouter, HTTPException, Query
except ModuleNotFoundError:  # pragma: no cover - exercised by local tests.

    class HTTPException(Exception):
        """Minimal stand-in that preserves FastAPI's public attributes."""

        def __init__(self, status_code: int, detail: str):
            super().__init__(detail)
            self.status_code = status_code
            self.detail = detail

    class APIRouter:
        """Tiny route recorder used when FastAPI is unavailable."""

        def __init__(self) -> None:
            self.routes: list[dict[str, Any]] = []

        def get(self, path: str, **kwargs: Any):  # type: ignore[no-untyped-def]
            def decorator(func):
                self.routes.append({"method": "GET", "path": path, "kwargs": kwargs, "endpoint": func})
                return func

            return decorator

    def Query(default: Any = None, **_: Any) -> Any:  # noqa: N802 - FastAPI API compatibility.
        return default


RUN_STATUS_VALUES = {
    "DRAFT",
    "VALIDATED_RESEARCH",
    "CHALLENGER",
    "SHADOW_ACTIVE",
    "REJECTED",
    "INCOMPLETE",
    "BLOCKED",
}

ARTIFACT_NAMES = {
    "overview": "overview.json",
    "report": "report.json",
    "lineage": "lineage.json",
    "evidence": "evidence.json",
    "critics": "critics.json",
    "promotion": "promotion.json",
    "citations": "citations.json",
    "shadow_portfolio": "shadow_portfolio.json",
    "nav_attribution": "nav_attribution.json",
    "drift_alerts": "drift_alerts.json",
}

SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
SECRET_KEY_RE = re.compile(r"(api[_-]?key|authorization|bearer|cookie|passwd|password|secret|token)", re.I)
SECRET_VALUE_RE = re.compile(
    r"(Bearer\s+[A-Za-z0-9._~+/=-]{8,}|(?<![A-Za-z0-9])sk-[A-Za-z0-9]{8,}|(?<![A-Za-z0-9])xox[baprs]-[A-Za-z0-9-]{8,})",
    re.I,
)
LOCAL_ABSOLUTE_RE = re.compile(r"^[A-Za-z]:[\\/]|^\\\\|^/(?!/)")


class ArtifactRepository(Protocol):
    """Read-only Workbench artifact repository."""

    def list_runs(self, limit: int = 50) -> list[dict[str, Any]]:
        """Return redacted run summaries."""

    def get_run_detail(self, run_id: str) -> dict[str, Any]:
        """Return a redacted aggregate run detail."""

    def get_report(self, run_id: str) -> dict[str, Any]:
        """Return the public Markdown report and its content hash."""

    def get_lineage(self, run_id: str) -> dict[str, Any]:
        """Return candidate/factor lineage."""

    def get_evidence(self, run_id: str) -> dict[str, Any]:
        """Return redacted official/visible evidence only."""

    def get_critics(self, run_id: str) -> dict[str, Any]:
        """Return critic findings and redacted decisions."""

    def get_promotion(self, run_id: str) -> dict[str, Any]:
        """Return promotion status without sealed raw payload."""

    def get_citations(self, run_id: str) -> dict[str, Any]:
        """Return document/news citation chain."""

    def get_shadow_portfolio(self, run_id: str) -> dict[str, Any]:
        """Return shadow portfolio vintage, never live account state."""

    def get_nav_attribution(self, run_id: str) -> dict[str, Any]:
        """Return NAV and attribution artifact."""

    def get_drift_alerts(self, run_id: str) -> dict[str, Any]:
        """Return failure and drift alerts."""


def _validate_safe_id(value: str, field: str = "id") -> str:
    if not value or not SAFE_ID_RE.fullmatch(value):
        raise HTTPException(status_code=400, detail=f"invalid {field}")
    if ".." in value or "/" in value or "\\" in value or "\x00" in value or ":" in value:
        raise HTTPException(status_code=400, detail=f"invalid {field}")
    return value


def _is_local_absolute_string(value: str) -> bool:
    if not value:
        return False
    parsed = urlparse(value)
    if parsed.scheme in {"http", "https", "s3", "gs"}:
        return False
    return bool(LOCAL_ABSOLUTE_RE.match(value))


def _is_forbidden_sealed_key(key: str) -> bool:
    lowered = key.lower()
    if "sealed" not in lowered and not lowered.startswith("test_"):
        return False
    allowed = {
        "sealed_status",
        "sealed_decision",
        "sealed_failure_code",
        "sealed_redacted",
        "sealed_summary",
        "sealed_access",
    }
    return lowered not in allowed


def _redacted_marker(reason: str) -> dict[str, str]:
    return {"redacted": reason}


def redact_for_workbench(value: Any, *, key: str = "", depth: int = 0) -> Any:
    """Redact secrets, sealed raw data, and local absolute paths.

    The function is intentionally conservative.  It preserves status and failure
    codes that are allowed to appear in a Workbench but strips raw sealed/test
    payloads and secret-like fields before the API returns JSON.
    """

    if depth > 30:
        return _redacted_marker("max_depth")
    if key and SECRET_KEY_RE.search(key):
        return _redacted_marker("secret_key")
    if key and _is_forbidden_sealed_key(key):
        return _redacted_marker("sealed_raw_data")
    if isinstance(value, dict):
        return {
            str(child_key): redact_for_workbench(child_value, key=str(child_key), depth=depth + 1)
            for child_key, child_value in value.items()
        }
    if isinstance(value, list):
        return [redact_for_workbench(item, key=key, depth=depth + 1) for item in value]
    if isinstance(value, tuple):
        return [redact_for_workbench(item, key=key, depth=depth + 1) for item in value]
    if isinstance(value, str):
        if SECRET_VALUE_RE.search(value):
            return "[REDACTED:secret_value]"
        if _is_local_absolute_string(value):
            return "[REDACTED:local_path]"
        return value
    return value


@dataclass(frozen=True)
class WorkbenchArtifactRepository:
    """Path-safe repository rooted at a frozen Workbench artifact directory."""

    root: Path

    def __post_init__(self) -> None:
        object.__setattr__(self, "root", Path(self.root).resolve())

    def _resolve(self, *parts: str) -> Path:
        for part in parts:
            _validate_safe_id(part.removesuffix(".json"), "artifact segment")
        target = self.root.joinpath(*parts).resolve()
        try:
            target.relative_to(self.root)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="invalid artifact locator") from exc
        return target

    def _read_json(self, *parts: str, default: Any | None = None) -> Any:
        path = self._resolve(*parts)
        if not path.exists():
            if default is not None:
                return default
            raise HTTPException(status_code=404, detail="artifact not found")
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=500, detail="artifact is corrupt") from exc
        return redact_for_workbench(data)

    def _read_named(self, run_id: str, artifact_name: str) -> dict[str, Any]:
        run_id = _validate_safe_id(run_id, "run_id")
        filename = ARTIFACT_NAMES[artifact_name]
        data = self._read_json("runs", run_id, filename, default={})
        return data if isinstance(data, dict) else {"items": data}

    def list_runs(self, limit: int = 50) -> list[dict[str, Any]]:
        limit = max(0, min(int(limit), 200))
        payload = self._read_json("index.json", default={"runs": []})
        runs = payload.get("runs", []) if isinstance(payload, dict) else []
        if not isinstance(runs, list):
            raise HTTPException(status_code=500, detail="artifact is corrupt")
        safe_runs: list[dict[str, Any]] = []
        for row in runs:
            if not isinstance(row, dict):
                continue
            run_id = str(row.get("run_id", ""))
            try:
                _validate_safe_id(run_id, "run_id")
            except HTTPException:
                continue
            status = str(row.get("status", "INCOMPLETE")).upper()
            if status not in RUN_STATUS_VALUES:
                status = "INCOMPLETE"
            safe = redact_for_workbench({**row, "run_id": run_id, "status": status})
            safe_runs.append(safe)
        safe_runs.sort(key=lambda item: str(item.get("updated_at") or item.get("decision_time") or ""), reverse=True)
        return safe_runs[:limit]

    def get_run_detail(self, run_id: str) -> dict[str, Any]:
        run = self._read_named(run_id, "overview")
        status = str(run.get("status", "INCOMPLETE")).upper()
        if status not in RUN_STATUS_VALUES:
            run["status"] = "INCOMPLETE"
        else:
            run["status"] = status
        return {
            "run": run,
            "report": self.get_report(run_id),
            "research_dag": run.get("research_dag", {}),
            "lineage": self.get_lineage(run_id),
            "evidence": self.get_evidence(run_id),
            "critics": self.get_critics(run_id),
            "promotion": self.get_promotion(run_id),
            "citations": self.get_citations(run_id),
            "shadow_portfolio": self.get_shadow_portfolio(run_id),
            "nav_attribution": self.get_nav_attribution(run_id),
            "drift_alerts": self.get_drift_alerts(run_id),
        }

    def get_report(self, run_id: str) -> dict[str, Any]:
        run_id = _validate_safe_id(run_id, "run_id")
        data = self._read_json(
            "runs",
            run_id,
            ARTIFACT_NAMES["report"],
            default={},
        )
        return data if isinstance(data, dict) else {}

    def get_lineage(self, run_id: str) -> dict[str, Any]:
        return self._read_named(run_id, "lineage")

    def get_evidence(self, run_id: str) -> dict[str, Any]:
        return self._read_named(run_id, "evidence")

    def get_critics(self, run_id: str) -> dict[str, Any]:
        return self._read_named(run_id, "critics")

    def get_promotion(self, run_id: str) -> dict[str, Any]:
        return self._read_named(run_id, "promotion")

    def get_citations(self, run_id: str) -> dict[str, Any]:
        return self._read_named(run_id, "citations")

    def get_shadow_portfolio(self, run_id: str) -> dict[str, Any]:
        return self._read_named(run_id, "shadow_portfolio")

    def get_nav_attribution(self, run_id: str) -> dict[str, Any]:
        return self._read_named(run_id, "nav_attribution")

    def get_drift_alerts(self, run_id: str) -> dict[str, Any]:
        return self._read_named(run_id, "drift_alerts")


@dataclass
class InMemoryArtifactRepository:
    """Deterministic test fixture repository implementing ArtifactRepository."""

    runs: list[dict[str, Any]]
    details: dict[str, dict[str, Any]]

    def _detail(self, run_id: str, section: str | None = None) -> dict[str, Any]:
        _validate_safe_id(run_id, "run_id")
        data = self.details.get(run_id)
        if data is None:
            raise HTTPException(status_code=404, detail="artifact not found")
        if section is None:
            return redact_for_workbench(data)
        value = data.get(section, {})
        return redact_for_workbench(value if isinstance(value, dict) else {"items": value})

    def list_runs(self, limit: int = 50) -> list[dict[str, Any]]:
        return redact_for_workbench(self.runs[: max(0, min(int(limit), 200))])

    def get_run_detail(self, run_id: str) -> dict[str, Any]:
        return self._detail(run_id)

    def get_report(self, run_id: str) -> dict[str, Any]:
        return self._detail(run_id, "report")

    def get_lineage(self, run_id: str) -> dict[str, Any]:
        return self._detail(run_id, "lineage")

    def get_evidence(self, run_id: str) -> dict[str, Any]:
        return self._detail(run_id, "evidence")

    def get_critics(self, run_id: str) -> dict[str, Any]:
        return self._detail(run_id, "critics")

    def get_promotion(self, run_id: str) -> dict[str, Any]:
        return self._detail(run_id, "promotion")

    def get_citations(self, run_id: str) -> dict[str, Any]:
        return self._detail(run_id, "citations")

    def get_shadow_portfolio(self, run_id: str) -> dict[str, Any]:
        return self._detail(run_id, "shadow_portfolio")

    def get_nav_attribution(self, run_id: str) -> dict[str, Any]:
        return self._detail(run_id, "nav_attribution")

    def get_drift_alerts(self, run_id: str) -> dict[str, Any]:
        return self._detail(run_id, "drift_alerts")


def _default_root() -> Path:
    configured = os.environ.get("VERIQUANT_WORKBENCH_ARTIFACT_ROOT")
    if configured:
        return Path(configured)
    return Path(".autobuild") / "veriquant" / "workbench_artifacts"


_repository: ArtifactRepository | None = None


def set_artifact_repository(repository: ArtifactRepository | None) -> None:
    """Set the repository used by the standalone router.

    Tests and integration code may inject a repository.  Passing ``None``
    restores the default read-only filesystem repository.
    """

    global _repository
    _repository = repository


def get_artifact_repository() -> ArtifactRepository:
    global _repository
    if _repository is None:
        _repository = WorkbenchArtifactRepository(_default_root())
    return _repository


def _safe_limit(limit: int | None) -> int:
    if limit is None:
        return 50
    # Direct endpoint calls used by offline integration tests receive the
    # FastAPI ``Query`` default object instead of a materialized integer.
    # Resolve only its declared scalar default; arbitrary objects must not be
    # coerced through user-defined conversion hooks.
    candidate: Any = limit
    declared_default = getattr(limit, "default", None)
    if isinstance(declared_default, int) and not isinstance(declared_default, bool):
        candidate = declared_default
    if not isinstance(candidate, int) or isinstance(candidate, bool):
        return 50
    return max(0, min(candidate, 200))


veriquant_workbench_router = APIRouter()


@veriquant_workbench_router.get("/veriquant/workbench/runs")
async def list_workbench_runs(limit: int = Query(50, ge=0, le=200)) -> dict[str, Any]:
    return {"status": "ok", "runs": get_artifact_repository().list_runs(_safe_limit(limit))}


@veriquant_workbench_router.get("/veriquant/workbench/runs/{run_id}")
async def get_workbench_run(run_id: str) -> dict[str, Any]:
    return {"status": "ok", "detail": get_artifact_repository().get_run_detail(run_id)}


@veriquant_workbench_router.get("/veriquant/workbench/runs/{run_id}/lineage")
async def get_workbench_lineage(run_id: str) -> dict[str, Any]:
    return {"status": "ok", "lineage": get_artifact_repository().get_lineage(run_id)}


@veriquant_workbench_router.get("/veriquant/workbench/runs/{run_id}/evidence")
async def get_workbench_evidence(run_id: str) -> dict[str, Any]:
    return {"status": "ok", "evidence": get_artifact_repository().get_evidence(run_id)}


@veriquant_workbench_router.get("/veriquant/workbench/runs/{run_id}/critics")
async def get_workbench_critics(run_id: str) -> dict[str, Any]:
    return {"status": "ok", "critics": get_artifact_repository().get_critics(run_id)}


@veriquant_workbench_router.get("/veriquant/workbench/runs/{run_id}/promotion")
async def get_workbench_promotion(run_id: str) -> dict[str, Any]:
    return {"status": "ok", "promotion": get_artifact_repository().get_promotion(run_id)}


@veriquant_workbench_router.get("/veriquant/workbench/runs/{run_id}/citations")
async def get_workbench_citations(run_id: str) -> dict[str, Any]:
    return {"status": "ok", "citations": get_artifact_repository().get_citations(run_id)}


@veriquant_workbench_router.get("/veriquant/workbench/runs/{run_id}/shadow-portfolio")
async def get_workbench_shadow_portfolio(run_id: str) -> dict[str, Any]:
    return {"status": "ok", "shadow_portfolio": get_artifact_repository().get_shadow_portfolio(run_id)}


@veriquant_workbench_router.get("/veriquant/workbench/runs/{run_id}/nav-attribution")
async def get_workbench_nav_attribution(run_id: str) -> dict[str, Any]:
    return {"status": "ok", "nav_attribution": get_artifact_repository().get_nav_attribution(run_id)}


@veriquant_workbench_router.get("/veriquant/workbench/runs/{run_id}/drift-alerts")
async def get_workbench_drift_alerts(run_id: str) -> dict[str, Any]:
    return {"status": "ok", "drift_alerts": get_artifact_repository().get_drift_alerts(run_id)}


def route_paths() -> list[str]:
    """Return registered paths for integration tests without requiring FastAPI."""

    return [str(getattr(route, "path", route.get("path") if isinstance(route, dict) else "")) for route in getattr(veriquant_workbench_router, "routes", [])]


def summarize_routes(paths: Iterable[str] | None = None) -> dict[str, Any]:
    """Machine-readable summary for Integration Agent handoff."""

    selected = list(paths) if paths is not None else route_paths()
    return {
        "router_symbol": "veriquant_workbench_router",
        "registered": False,
        "paths": selected,
        "default_prefix": "/veriquant/workbench",
        "authority": "read_only_redacted_artifacts",
    }
