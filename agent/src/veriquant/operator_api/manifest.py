"""Loopback-only route authority manifest for the operator API."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, ClassVar

from veriquant.contracts import FailureCode
from veriquant.contracts.base import CanonicalContract, fail, require_text

from .models import OPERATOR_API_SCHEMA_VERSION, OperatorJobCreateRequest


LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost"})
FORBIDDEN_ROUTE_TOKENS = ("/live", "broker", "order", "oauth")


@dataclass(frozen=True, kw_only=True)
class LoopbackOperatorApiConfig(CanonicalContract):
    SCHEMA_VERSION: ClassVar[str] = OPERATOR_API_SCHEMA_VERSION

    bind_host: str = "127.0.0.1"
    offline_default: bool = True
    deterministic_default: bool = True
    sse_enabled: bool = False

    def __post_init__(self) -> None:
        if self.bind_host not in LOOPBACK_HOSTS:
            fail(FailureCode.UNAUTHORIZED_CAPABILITY, "operator API bind_host must be loopback only")
        if type(self.offline_default) is not bool or not self.offline_default:
            fail(FailureCode.INVALID_PAPER_ENVIRONMENT, "operator API must default offline")
        if type(self.deterministic_default) is not bool or not self.deterministic_default:
            fail(FailureCode.INVALID_PAPER_ENVIRONMENT, "operator API must default deterministic")
        if type(self.sse_enabled) is not bool or self.sse_enabled:
            fail(FailureCode.UNAUTHORIZED_CAPABILITY, "operator API v1 uses polling, not SSE")


@dataclass(frozen=True, kw_only=True)
class OperatorRouteRecord(CanonicalContract):
    SCHEMA_VERSION: ClassVar[str] = OPERATOR_API_SCHEMA_VERSION

    method: str
    path: str
    authority: str
    handler: str
    request_schema: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        require_text(self.method, "method")
        require_text(self.path, "path")
        require_text(self.authority, "authority")
        require_text(self.handler, "handler")
        method = self.method.upper()
        if method not in {"GET", "POST"}:
            fail(FailureCode.UNAUTHORIZED_CAPABILITY, "operator API only exposes GET/POST package-local routes")
        lowered = f"{self.path} {self.authority} {self.handler}".lower()
        if any(token in lowered for token in FORBIDDEN_ROUTE_TOKENS):
            fail(FailureCode.UNAUTHORIZED_CAPABILITY, "operator API route manifest contains forbidden route authority")
        if not self.path.startswith("/veriquant/jobs"):
            fail(FailureCode.UNAUTHORIZED_CAPABILITY, "operator API routes must stay under /veriquant/jobs")


@dataclass(frozen=True, kw_only=True)
class RouteAuthorityManifest(CanonicalContract):
    SCHEMA_VERSION: ClassVar[str] = OPERATOR_API_SCHEMA_VERSION

    config: LoopbackOperatorApiConfig = field(default_factory=LoopbackOperatorApiConfig)
    routes: tuple[OperatorRouteRecord, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.config, LoopbackOperatorApiConfig):
            fail(FailureCode.INVALID_VALUE, "config must be LoopbackOperatorApiConfig")
        if not isinstance(self.routes, tuple) or not self.routes:
            fail(FailureCode.INVALID_VALUE, "routes must be a non-empty tuple")
        seen: set[tuple[str, str]] = set()
        for route in self.routes:
            if not isinstance(route, OperatorRouteRecord):
                fail(FailureCode.INVALID_VALUE, "routes must contain OperatorRouteRecord values")
            key = (route.method.upper(), route.path)
            if key in seen:
                fail(FailureCode.ARTIFACT_COLLISION, "duplicate operator API route")
            seen.add(key)


def build_operator_route_manifest(*, bind_host: str = "127.0.0.1") -> RouteAuthorityManifest:
    control = "OPERATOR_OFFLINE_CONTROL"
    read = "OPERATOR_OFFLINE_READ"
    return RouteAuthorityManifest(
        config=LoopbackOperatorApiConfig(bind_host=bind_host),
        routes=(
            OperatorRouteRecord(
                method="POST",
                path="/veriquant/jobs",
                authority=control,
                handler="post_jobs",
                request_schema=OperatorJobCreateRequest.public_schema(),
            ),
            OperatorRouteRecord(method="GET", path="/veriquant/jobs/{id}", authority=read, handler="get_job"),
            OperatorRouteRecord(
                method="GET",
                path="/veriquant/jobs/{id}/events",
                authority=read,
                handler="get_job_events",
            ),
            OperatorRouteRecord(method="POST", path="/veriquant/jobs/{id}/cancel", authority=control, handler="post_cancel"),
            OperatorRouteRecord(method="POST", path="/veriquant/jobs/{id}/resume", authority=control, handler="post_resume"),
        ),
    )

