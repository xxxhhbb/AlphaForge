"""Standalone loopback VeriQuant Workbench and offline operator API."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from src.api.veriquant_workbench_routes import (
    WorkbenchArtifactRepository,
    route_paths as workbench_route_paths,
    set_artifact_repository,
    veriquant_workbench_router,
)
from src.veriquant.integration.operator_provider import build_integrated_registry
from src.veriquant.operator.router import operator_router, set_operator_service
from src.veriquant.operator.service import OfflineJobService


REPO_ROOT = Path(__file__).resolve().parents[1]
ARTIFACT_ROOT = REPO_ROOT / ".autobuild/veriquant/integration/workbench_artifacts"
JOB_ROOT = REPO_ROOT / ".autobuild/veriquant/integration/operator_jobs"
REAL_RUN_ROOT = REPO_ROOT / ".autobuild/veriquant/integration/operator_real_runs"
OPERATOR_INTENT_HEADER = "offline-ui-v1"
LOOPBACK_NAMES = frozenset({"127.0.0.1", "localhost", "::1"})

OPERATOR_ROUTES = frozenset(
    {
        ("GET", "/veriquant/operator/profiles"),
        ("POST", "/veriquant/jobs"),
        ("GET", "/veriquant/jobs/{job_id}"),
        ("GET", "/veriquant/jobs/{job_id}/events"),
        ("POST", "/veriquant/jobs/{job_id}/cancel"),
        ("POST", "/veriquant/jobs/{job_id}/resume"),
    }
)
EXPECTED_ROUTES = frozenset(
    {
        ("GET", "/healthz"),
        ("GET", "/veriquant/status"),
        *(("GET", path) for path in workbench_route_paths()),
        *OPERATOR_ROUTES,
    }
)


@dataclass(frozen=True)
class IntegratedRouteRecord:
    method: str
    path: str
    authority: str
    financial_authority: bool = False


@dataclass(frozen=True)
class IntegratedRouteManifest:
    routes: tuple[IntegratedRouteRecord, ...]
    loopback_only: bool = True
    offline_only: bool = True
    provider_keys_required: bool = False


def _hostname(value: str) -> str:
    raw = value.strip()
    if "://" not in raw:
        raw = "http://" + raw
    return (urlsplit(raw).hostname or "").lower()


def validate_bind_host(host: str) -> str:
    normalized = _hostname(host)
    if normalized not in LOOPBACK_NAMES:
        raise ValueError("VeriQuant operator server is loopback-only")
    return normalized


def _iter_routes(routes):  # type: ignore[no-untyped-def]
    for route in routes:
        path = getattr(route, "path", None)
        if isinstance(path, str) and path:
            yield route
            continue
        nested = getattr(getattr(route, "original_router", None), "routes", None)
        if nested is not None:
            yield from _iter_routes(nested)


def build_route_manifest(application: FastAPI) -> IntegratedRouteManifest:
    records: list[IntegratedRouteRecord] = []
    actual: set[tuple[str, str]] = set()
    for route in _iter_routes(application.routes):
        path = str(getattr(route, "path"))
        for method in sorted(getattr(route, "methods", ()) or ()):
            key = (str(method), path)
            actual.add(key)
            authority = "OFFLINE_ORCHESTRATION_CONTROL" if key in OPERATOR_ROUTES else (
                "READ_ONLY_WORKBENCH" if path.startswith("/veriquant/workbench/") else "READ_ONLY_STATUS"
            )
            records.append(IntegratedRouteRecord(method=key[0], path=path, authority=authority))
    if actual != set(EXPECTED_ROUTES):
        raise RuntimeError(
            f"VeriQuant route mismatch: missing={sorted(set(EXPECTED_ROUTES)-actual)!r}; "
            f"extra={sorted(actual-set(EXPECTED_ROUTES))!r}"
        )
    forbidden = ("/live", "broker", "order", "oauth", "mandate", "account")
    if any(any(token in item.path.lower() for token in forbidden) for item in records):
        raise RuntimeError("VeriQuant-only application contains a forbidden route")
    return IntegratedRouteManifest(routes=tuple(sorted(records, key=lambda item: (item.path, item.method))))


def create_app(
    *,
    artifact_root: str | Path = ARTIFACT_ROOT,
    job_root: str | Path = JOB_ROOT,
    real_run_root: str | Path = REAL_RUN_ROOT,
    allow_test_host: bool = False,
) -> FastAPI:
    set_artifact_repository(WorkbenchArtifactRepository(Path(artifact_root).resolve()))
    registry = build_integrated_registry(real_run_root=real_run_root, workbench_root=artifact_root)
    set_operator_service(OfflineJobService(root=job_root, registry=registry, max_concurrent_jobs=1))
    application = FastAPI(title="VeriQuant Offline Operator", docs_url=None, redoc_url=None, openapi_url=None)

    @application.middleware("http")
    async def enforce_local_authority(request: Request, call_next):  # type: ignore[no-untyped-def]
        host = _hostname(request.headers.get("host", ""))
        if host not in LOOPBACK_NAMES and not (allow_test_host and host == "testserver"):
            return JSONResponse(status_code=403, content={"status": "FAILED", "failure_code": "NON_LOOPBACK_HOST"})
        origin = request.headers.get("origin")
        if origin and _hostname(origin) not in LOOPBACK_NAMES:
            return JSONResponse(status_code=403, content={"status": "FAILED", "failure_code": "UNTRUSTED_ORIGIN"})
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            if request.headers.get("x-veriquant-operator") != OPERATOR_INTENT_HEADER:
                return JSONResponse(status_code=403, content={"status": "FAILED", "failure_code": "OPERATOR_INTENT_REQUIRED"})
        return await call_next(request)

    @application.get("/healthz")
    async def healthz() -> dict[str, Any]:
        return {"status": "ok", "offline": True, "loopback_only": True, "financial_authority": False}

    @application.get("/veriquant/status")
    async def veriquant_status() -> dict[str, Any]:
        return {
            "status": "ok",
            "offline": True,
            "provider_keys_required": False,
            "approved_proxy_visible_evidence": True,
            "real_promotion": False,
            "real_shadow_replay_20d": False,
            "financial_authority": False,
        }

    application.include_router(veriquant_workbench_router)
    application.include_router(operator_router)
    build_route_manifest(application)
    return application


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the loopback-only VeriQuant operator API")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8900)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    host = validate_bind_host(args.host)
    if not 1 <= args.port <= 65535:
        raise SystemExit("port must be in 1..65535")
    import uvicorn

    uvicorn.run(create_app(), host=host, port=args.port, log_level="info")
    return 0


app = create_app()
route_authority_manifest = build_route_manifest(app)


if __name__ == "__main__":
    raise SystemExit(main())
