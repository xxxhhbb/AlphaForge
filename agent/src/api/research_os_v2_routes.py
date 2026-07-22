"""Typed AlphaForge Research OS V2 routes for the isolated product app."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

try:
    from fastapi import APIRouter, HTTPException
except ModuleNotFoundError:  # lightweight offline contract tests
    class HTTPException(Exception):
        def __init__(self, status_code: int, detail: str):
            super().__init__(detail)
            self.status_code = status_code
            self.detail = detail

    class _Route:
        def __init__(self, path: str, method: str, endpoint) -> None:
            self.path = path
            self.methods = {method}
            self.endpoint = endpoint

    class APIRouter:
        def __init__(self, prefix: str = "") -> None:
            self.prefix = prefix
            self.routes = []

        def _register(self, method: str, path: str):
            def decorator(endpoint):
                self.routes.append(_Route(self.prefix + path, method, endpoint))
                return endpoint
            return decorator

        def post(self, path: str):
            return self._register("POST", path)

        def get(self, path: str):
            return self._register("GET", path)

from src.veriquant.integration.research_os_v2 import (
    FrozenValidationDataRepository,
    ResearchOSV2Service,
)


REPO_ROOT = Path(__file__).resolve().parents[3]
ARTIFACT_ROOT = REPO_ROOT / ".autobuild" / "veriquant" / "integration" / "research_os_v2"
ACTIVE_DATA_REPOSITORY = FrozenValidationDataRepository(
    ARTIFACT_ROOT / "data_active"
)
BASE_DATA_REPOSITORY = FrozenValidationDataRepository(ARTIFACT_ROOT / "data")


def _load_active_data():
    return ACTIVE_DATA_REPOSITORY.load() or BASE_DATA_REPOSITORY.load()

research_os_v2_service = ResearchOSV2Service(
    root=ARTIFACT_ROOT,
    data_loader=_load_active_data,
    dotenv_path=REPO_ROOT / "agent" / ".env",
    dynamic_freeze_root=(
        REPO_ROOT
        / ".autobuild"
        / "veriquant"
        / "integration"
        / "dynamic_data_active"
    ),
)
research_os_v2_router = APIRouter(prefix="/api/veriquant/research-os/v2")


def _mapping(payload: Any) -> Mapping[str, Any]:
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="payload must be an object")
    return payload


def _route(call):  # type: ignore[no-untyped-def]
    try:
        return call()
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="research artifact not found") from exc
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@research_os_v2_router.post("/suggestions")
async def create_research_suggestion(payload: dict[str, Any]) -> dict[str, Any]:
    return _route(lambda: research_os_v2_service.create_suggestion(_mapping(payload)))


@research_os_v2_router.post("/plans/confirm")
async def confirm_research_plan(payload: dict[str, Any]) -> dict[str, Any]:
    return _route(lambda: research_os_v2_service.confirm_plan(_mapping(payload)))


@research_os_v2_router.post("/runs")
async def start_research_run(payload: dict[str, Any]) -> dict[str, Any]:
    selected = _mapping(payload)
    return _route(lambda: research_os_v2_service.start_research(str(selected.get("planId") or "")))


@research_os_v2_router.get("/runs/{run_id}")
async def get_research_run(run_id: str) -> dict[str, Any]:
    return _route(lambda: research_os_v2_service.get_run(run_id))


def route_paths() -> tuple[str, ...]:
    return tuple(str(route.path) for route in research_os_v2_router.routes)


__all__ = ["research_os_v2_router", "research_os_v2_service", "route_paths"]
