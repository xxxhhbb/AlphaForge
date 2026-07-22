"""Package-local route facade for VeriQuant operator jobs.

This module defines route-like callables and exported symbols for integration.
It does not register a FastAPI router and does not import the public API server.
"""

from __future__ import annotations

from typing import Any, Mapping

from .kernel import (
    CheckpointRecoveryKernel,
    DeterministicFakeRecoveryKernel,
    InMemoryOperatorJobStore,
    OperatorJobService,
)
from .manifest import (
    LoopbackOperatorApiConfig,
    OperatorRouteRecord,
    RouteAuthorityManifest,
    build_operator_route_manifest,
)
from .models import (
    OperatorApiError,
    OperatorBudget,
    OperatorEventType,
    OperatorJob,
    OperatorJobCreateRequest,
    OperatorJobEvent,
    OperatorJobStatus,
    RegisteredProfile,
    RegisteredSnapshot,
)
from .registry import OperatorRegistry


class OperatorApiRoutes:
    """Synchronous polling facade equivalent to the requested package routes."""

    def __init__(self, service: OperatorJobService) -> None:
        self.service = service

    @classmethod
    def fixture_default(cls) -> "OperatorApiRoutes":
        return cls(
            OperatorJobService(
                registry=OperatorRegistry.fixture_default(),
                store=InMemoryOperatorJobStore(),
                recovery_kernel=DeterministicFakeRecoveryKernel(),
            )
        )

    @staticmethod
    def manifest(*, bind_host: str = "127.0.0.1") -> RouteAuthorityManifest:
        return build_operator_route_manifest(bind_host=bind_host)

    def post_jobs(self, payload: Mapping[str, Any], *, idempotency_key: str) -> dict[str, Any]:
        return self.service.create_job(payload=payload, idempotency_key=idempotency_key).to_dict()

    def get_job(self, job_id: str) -> dict[str, Any]:
        return self.service.get_job(job_id).to_dict()

    def get_job_events(self, job_id: str, *, after: str | int | None = None) -> dict[str, Any]:
        events = self.service.get_events(job_id, after=after)
        return {
            "events": [event.to_dict() for event in events],
            "next_cursor": events[-1].cursor if events else str(after or 0),
            "transport": "polling",
        }

    def post_cancel(self, job_id: str) -> dict[str, Any]:
        return self.service.cancel_job(job_id).to_dict()

    def post_resume(self, job_id: str) -> dict[str, Any]:
        return self.service.resume_job(job_id).to_dict()


EXPORTED_SYMBOLS = (
    "CheckpointRecoveryKernel",
    "DeterministicFakeRecoveryKernel",
    "InMemoryOperatorJobStore",
    "LoopbackOperatorApiConfig",
    "OperatorApiError",
    "OperatorApiRoutes",
    "OperatorBudget",
    "OperatorEventType",
    "OperatorJob",
    "OperatorJobCreateRequest",
    "OperatorJobEvent",
    "OperatorJobService",
    "OperatorJobStatus",
    "OperatorRegistry",
    "OperatorRouteRecord",
    "RegisteredProfile",
    "RegisteredSnapshot",
    "RouteAuthorityManifest",
    "build_operator_route_manifest",
)


__all__ = EXPORTED_SYMBOLS

