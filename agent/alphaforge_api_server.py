"""Loopback-only AlphaForge Research OS V3 API and UI server.

This entry point is intentionally thinner than ``api_server.py``.  It registers
only Research OS V3 controls/results, redacted Workbench reads, health/status, and
the built frontend assets.  It does not import or register Vibe live trading
routes, broker routes, mandates, OAuth, or order endpoints.
"""

from __future__ import annotations

import argparse
from pathlib import Path
import sys
from typing import Any
from urllib.parse import urlsplit


# Direct ``python alphaforge_api_server.py`` launches from ``agent/``.  The
# product package itself lives under ``agent/src`` and must be importable
# without relying on a shell-specific PYTHONPATH used only by test harnesses.
AGENT_ROOT = Path(__file__).resolve().parent
SOURCE_ROOT = AGENT_ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from src.api.veriquant_workbench_routes import (
    WorkbenchArtifactRepository,
    set_artifact_repository,
    veriquant_workbench_router,
)
from src.api.research_os_v3_routes import (
    research_os_v3_control_router,
    research_os_v3_router,
)


REPO_ROOT = AGENT_ROOT.parent
FRONTEND_DIST = REPO_ROOT / "frontend" / "dist"
WORKBENCH_ROOT = REPO_ROOT / ".autobuild" / "veriquant" / "integration" / "workbench_artifacts"
LOOPBACK_NAMES = frozenset({"127.0.0.1", "localhost", "::1"})


class SPAStaticFiles(StaticFiles):
    """Serve index.html for client-side AlphaForge routes."""

    async def get_response(self, path: str, scope: dict[str, Any]):  # type: ignore[override]
        try:
            return await super().get_response(path, scope)
        except StarletteHTTPException as exc:
            if exc.status_code != status.HTTP_404_NOT_FOUND:
                raise
            return await super().get_response("index.html", scope)


def _hostname(value: str) -> str:
    raw = value.strip()
    if "://" not in raw:
        raw = "http://" + raw
    return (urlsplit(raw).hostname or "").lower()


def _validate_loopback_host(host: str) -> str:
    normalized = _hostname(host)
    if normalized not in LOOPBACK_NAMES:
        raise ValueError("AlphaForge API server is loopback-only; use 127.0.0.1")
    return normalized


def create_app() -> FastAPI:
    set_artifact_repository(WorkbenchArtifactRepository(WORKBENCH_ROOT.resolve()))
    app = FastAPI(title="AlphaForge Research OS V3", docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def enforce_loopback_browser(request: Request, call_next):  # type: ignore[no-untyped-def]
        host = _hostname(request.headers.get("host", ""))
        if host not in LOOPBACK_NAMES:
            return JSONResponse(status_code=403, content={"status": "FAILED", "failure_code": "NON_LOOPBACK_HOST"})
        origin = request.headers.get("origin")
        if origin and _hostname(origin) not in LOOPBACK_NAMES:
            return JSONResponse(status_code=403, content={"status": "FAILED", "failure_code": "UNTRUSTED_ORIGIN"})
        return await call_next(request)

    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        return {
            "status": "ok",
            "product": "AlphaForge（千因研究院）",
            "loopback_only": True,
            "live_routes_registered": False,
        }

    @app.get("/veriquant/status")
    async def veriquant_status() -> dict[str, Any]:
        return {
            "status": "ok",
            "product": "AlphaForge（千因研究院）",
            "internal_kernel": "VeriQuant",
            "provider_keys_required": False,
            "live_routes_registered": False,
        }

    app.include_router(research_os_v3_control_router)
    app.include_router(research_os_v3_router)
    app.include_router(veriquant_workbench_router)
    _assert_product_routes(app)
    if FRONTEND_DIST.exists():
        app.mount("/", SPAStaticFiles(directory=str(FRONTEND_DIST), html=True), name="frontend")
    return app


def _iter_routes(routes):  # type: ignore[no-untyped-def]
    for route in routes:
        path = getattr(route, "path", None)
        if isinstance(path, str) and path:
            yield route
            continue
        nested = getattr(getattr(route, "original_router", None), "routes", None)
        if nested is not None:
            yield from _iter_routes(nested)


def _assert_product_routes(application: FastAPI) -> None:
    """Fail closed if a legacy or trading authority route enters this app."""

    paths = tuple(str(route.path).lower() for route in _iter_routes(application.routes))
    forbidden = ("/live", "broker", "order", "oauth", "mandate", "account")
    if any(any(token in path for token in forbidden) for path in paths):
        raise RuntimeError("AlphaForge product route table contains trading authority")
    if any("/research-os/v2" in path for path in paths):
        raise RuntimeError("Research OS V2 is disabled on the AlphaForge product server")
    if not any("/research-os/v3/plans" in path for path in paths):
        raise RuntimeError("Research OS V3 controls are not registered")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the loopback-only AlphaForge Campaign API/UI server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args(argv)
    host = _validate_loopback_host(args.host)
    if not 1 <= args.port <= 65535:
        raise SystemExit("port must be in 1..65535")
    import uvicorn

    # Let Uvicorn import the verified module-level application.  With the
    # current FastAPI lazy-router implementation, passing an ASGI instance
    # directly from a ``__main__`` module can leave the SPA mount ahead of the
    # included API candidates under a real server, producing a misleading 405.
    uvicorn.run("alphaforge_api_server:app", host=host, port=args.port, log_level="info")
    return 0


app = create_app()


if __name__ == "__main__":
    raise SystemExit(main())
