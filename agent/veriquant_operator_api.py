"""Standalone loopback-only VeriQuant operator application.

This application registers no Vibe live routes and no financial truth service.
An Integration Agent may compose this router with the read-only Workbench app.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from fastapi import FastAPI

from src.veriquant.operator.models import ProviderProfile, StageDisposition, StageResult, canonical_hash
from src.veriquant.operator.registry import OperatorRegistry
from src.veriquant.operator.router import operator_router, set_operator_service
from src.veriquant.operator.service import OfflineJobService


DEFAULT_ROOT = Path(__file__).resolve().parents[1] / ".autobuild" / "veriquant" / "operator_jobs"
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
EXPECTED_ROUTES = {
    ("GET", "/healthz"),
    ("GET", "/veriquant/operator/profiles"),
    ("POST", "/veriquant/jobs"),
    ("GET", "/veriquant/jobs/{job_id}"),
    ("GET", "/veriquant/jobs/{job_id}/events"),
    ("POST", "/veriquant/jobs/{job_id}/cancel"),
    ("POST", "/veriquant/jobs/{job_id}/resume"),
}


@dataclass(frozen=True)
class OperatorRouteAuthority:
    method: str
    path: str
    authority: str = "OFFLINE_ORCHESTRATION_CONTROL"
    financial_authority: bool = False


@dataclass(frozen=True)
class OperatorRouteManifest:
    routes: tuple[OperatorRouteAuthority, ...]
    offline_only: bool = True
    loopback_only: bool = True
    provider_keys_required: bool = False
    imported_modules: tuple[str, ...] = (
        "src.veriquant.operator.router",
        "src.veriquant.operator.service",
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "veriquant.operator-route-manifest/v1",
            "offline_only": self.offline_only,
            "loopback_only": self.loopback_only,
            "provider_keys_required": self.provider_keys_required,
            "imported_modules": list(self.imported_modules),
            "routes": [route.__dict__ for route in self.routes],
        }

    @property
    def artifact_hash(self) -> str:
        return canonical_hash(self.to_dict())


class DeterministicControlProvider:
    """Key-free fixture proving control-plane operation, not financial work."""

    def execute_stage(
        self,
        *,
        job_id: str,
        stage_id: str,
        seed: int,
        budget: int,
        committed_artifacts: Mapping[str, str],
    ) -> StageResult:
        return StageResult(
            disposition=StageDisposition.COMMITTED,
            artifact_hash=canonical_hash(
                {
                    "provider": "deterministic-control-fixture/v1",
                    "job_id": job_id,
                    "stage_id": stage_id,
                    "seed": seed,
                    "budget": budget,
                    "committed": dict(sorted(committed_artifacts.items())),
                }
            ),
        )


def default_registry() -> OperatorRegistry:
    return OperatorRegistry(
        profiles={
            "control-smoke-v1": ProviderProfile(
                profile_id="control-smoke-v1",
                provider_id="deterministic-control-fixture",
                stage_ids=("validate_request", "record_control_event", "publish_control_status"),
                max_budget=3,
            )
        },
        providers={"deterministic-control-fixture": DeterministicControlProvider()},
    )


def validate_bind_host(host: str) -> str:
    normalized = str(host).strip().lower()
    if normalized not in LOOPBACK_HOSTS:
        raise ValueError("VeriQuant operator API is loopback-only")
    return normalized


def create_app(*, root: str | Path = DEFAULT_ROOT, registry: OperatorRegistry | None = None) -> FastAPI:
    selected = registry or default_registry()
    set_operator_service(OfflineJobService(root=root, registry=selected, max_concurrent_jobs=1))
    application = FastAPI(
        title="VeriQuant Offline Operator",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    @application.get("/healthz")
    async def healthz() -> dict[str, Any]:
        return {
            "status": "ok",
            "offline": True,
            "loopback_only": True,
            "provider_keys_required": False,
            "financial_authority": False,
        }

    application.include_router(operator_router)
    build_route_manifest(application)
    return application


def build_route_manifest(application: FastAPI) -> OperatorRouteManifest:
    records: list[OperatorRouteAuthority] = []
    actual: set[tuple[str, str]] = set()
    for route in _iter_api_routes(application.routes):
        path = getattr(route, "path", "")
        if not path:
            continue
        for method in sorted(getattr(route, "methods", ()) or ()):
            key = (str(method), str(path))
            actual.add(key)
            records.append(OperatorRouteAuthority(method=key[0], path=key[1]))
    if actual != EXPECTED_ROUTES:
        raise RuntimeError(f"operator route table mismatch: expected={sorted(EXPECTED_ROUTES)!r}, actual={sorted(actual)!r}")
    forbidden = ("/live", "broker", "order", "oauth", "mandate", "account", "trade", "sealed")
    if any(any(token in route.path.lower() for token in forbidden) for route in records):
        raise RuntimeError("operator route table contains a forbidden authority path")
    return OperatorRouteManifest(routes=tuple(sorted(records, key=lambda item: (item.path, item.method))))


def _iter_api_routes(routes):  # type: ignore[no-untyped-def]
    """Expand FastAPI versions that materialize included routers lazily."""

    for route in routes:
        path = getattr(route, "path", None)
        if isinstance(path, str) and path:
            yield route
            continue
        included = getattr(route, "original_router", None)
        nested = getattr(included, "routes", None)
        if nested is not None:
            yield from _iter_api_routes(nested)


app = create_app()
route_authority_manifest = build_route_manifest(app)
