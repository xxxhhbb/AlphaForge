"""Standalone, offline, read-only VeriQuant API application.

This entry point intentionally does not import or register the original Vibe
live API.  It exposes only health, status, and redacted Workbench reads.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from src.api.veriquant_workbench_routes import (
    WorkbenchArtifactRepository,
    route_paths as workbench_route_paths,
    set_artifact_repository,
    veriquant_workbench_router,
)
from src.api.research_os_v3_routes import (
    configure_research_os_v3_roots,
    research_os_v3_route_paths,
    research_os_v3_router,
)
from src.veriquant.v1_security.api import (
    RouteAuthorityManifest,
    RouteRecord,
    VeriQuantOfflineDeploymentProfile,
)


ARTIFACT_ROOT = Path(__file__).resolve().parents[1] / ".autobuild" / "veriquant" / "integration" / "workbench_artifacts"
RESEARCH_RUN_ROOT = Path(__file__).resolve().parents[1] / ".autobuild" / "veriquant" / "research_os_v3_u4"
RESEARCH_REPORT_ROOT = Path(__file__).resolve().parents[1] / ".autobuild" / "veriquant" / "research_os_v3_u5" / "reports"
SAFE_IMPORTED_MODULES = ("src.api.veriquant_workbench_routes", "src.api.research_os_v3_routes")
EXPECTED_ROUTE_PATHS = frozenset(
    {"/healthz", "/veriquant/status", *workbench_route_paths(), *research_os_v3_route_paths()}
)


@dataclass(frozen=True)
class _OfflineRoute:
    path: str
    methods: set[str]
    endpoint: Callable[..., Any]


class _OfflineApp:
    """Small route recorder used when FastAPI is not installed."""

    def __init__(self) -> None:
        self.routes: list[_OfflineRoute] = []

    def get(self, path: str):  # type: ignore[no-untyped-def]
        def decorator(endpoint: Callable[..., Any]) -> Callable[..., Any]:
            self.routes.append(_OfflineRoute(path=path, methods={"GET"}, endpoint=endpoint))
            return endpoint

        return decorator

    def include_router(self, router: Any) -> None:
        for route in getattr(router, "routes", ()):
            if isinstance(route, dict):
                method = str(route.get("method", "GET"))
                self.routes.append(
                    _OfflineRoute(
                        path=str(route.get("path", "")),
                        methods={method},
                        endpoint=route.get("endpoint"),
                    )
                )
            else:
                self.routes.append(route)


def create_app(
    *,
    artifact_root: str | Path | None = None,
    research_run_root: str | Path | None = None,
    research_report_root: str | Path | None = None,
):
    """Create the isolated app without reading provider credentials."""

    root = Path(artifact_root).resolve() if artifact_root is not None else ARTIFACT_ROOT.resolve()
    selected_run_root = Path(research_run_root).resolve() if research_run_root is not None else RESEARCH_RUN_ROOT.resolve()
    selected_report_root = Path(research_report_root).resolve() if research_report_root is not None else RESEARCH_REPORT_ROOT.resolve()
    set_artifact_repository(WorkbenchArtifactRepository(root))
    configure_research_os_v3_roots(run_root=selected_run_root, report_root=selected_report_root)
    try:
        from fastapi import FastAPI
    except ModuleNotFoundError:
        selected_app: Any = _OfflineApp()
    else:
        selected_app = FastAPI(
            title="VeriQuant Offline Workbench",
            docs_url=None,
            redoc_url=None,
            openapi_url=None,
        )

    @selected_app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        return {"status": "ok", "profile": "veriquant-offline-readonly"}

    @selected_app.get("/veriquant/status")
    async def veriquant_status() -> dict[str, Any]:
        return {
            "status": "ok",
            "offline": True,
            "provider_keys_required": False,
            "real_broker_enabled": False,
            "artifact_root_configured": True,
        }

    selected_app.include_router(veriquant_workbench_router)
    selected_app.include_router(research_os_v3_router)
    manifest = build_route_manifest(selected_app, artifact_root=root)
    failed = [finding.check_id for finding in VeriQuantOfflineDeploymentProfile.default().validate_manifest(manifest) if not finding.passed]
    if failed:
        raise RuntimeError("VeriQuant-only route manifest failed closed: " + ", ".join(failed))
    return selected_app


def _iter_api_routes(routes: Any):
    """Yield concrete routes, expanding FastAPI 0.139 lazy includes."""

    for route in routes:
        raw_path = getattr(route, "path", None)
        if isinstance(raw_path, str) and raw_path:
            yield route
            continue
        included_router = getattr(route, "original_router", None)
        nested_routes = getattr(included_router, "routes", None)
        if nested_routes is not None:
            yield from _iter_api_routes(nested_routes)

def build_route_manifest(selected_app: Any, *, artifact_root: str | Path = ARTIFACT_ROOT) -> RouteAuthorityManifest:
    records: list[RouteRecord] = []
    for route in _iter_api_routes(getattr(selected_app, "routes", ())):
        raw_path = getattr(route, "path", None)
        if not isinstance(raw_path, str) or not raw_path:
            # FastAPI/Starlette can expose non-route objects in this collection.
            # They are not API authority surfaces and must not become empty paths.
            continue
        path = raw_path
        methods = tuple(sorted(str(item) for item in (getattr(route, "methods", None) or ("GET",))))
        if path in {"/healthz", "/veriquant/status"}:
            authority = "READ_ONLY_STATUS"
        elif path.startswith("/api/veriquant/research-os/v3"):
            authority = "READ_ONLY_RESEARCH"
        else:
            authority = "READ_ONLY_WORKBENCH"
        records.append(
            RouteRecord(
                path=path,
                methods=methods,
                authority=authority,
                handler=str(getattr(getattr(route, "endpoint", None), "__name__", "")),
            )
        )
    actual_paths = {record.path for record in records}
    if actual_paths != EXPECTED_ROUTE_PATHS:
        missing = sorted(EXPECTED_ROUTE_PATHS - actual_paths)
        extra = sorted(actual_paths - EXPECTED_ROUTE_PATHS)
        raise RuntimeError(
            "VeriQuant-only route table mismatch; "
            f"missing={missing!r}; extra={extra!r}"
        )
    non_readonly = sorted(
        record.path for record in records if set(record.methods) != {"GET"}
    )
    if non_readonly:
        raise RuntimeError(
            "VeriQuant-only route table contains non-GET methods: "
            + ", ".join(non_readonly)
        )
    return VeriQuantOfflineDeploymentProfile.default().build_manifest(
        routes=records,
        imported_modules=SAFE_IMPORTED_MODULES,
        artifact_root=str(Path(artifact_root)),
    )


app = create_app()
route_authority_manifest = build_route_manifest(app)
