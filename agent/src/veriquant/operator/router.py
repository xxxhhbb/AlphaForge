"""FastAPI router for the closed-schema offline operator service."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, BackgroundTasks, HTTPException

from .models import OperatorError, OperatorFailureCode
from .service import OfflineJobService


operator_router = APIRouter()
_service: OfflineJobService | None = None


def set_operator_service(service: OfflineJobService | None) -> None:
    global _service
    _service = service


def get_operator_service() -> OfflineJobService:
    if _service is None:
        raise RuntimeError("offline operator service is not configured")
    return _service


def _call(action):  # type: ignore[no-untyped-def]
    try:
        return action()
    except OperatorError as exc:
        status = 404 if exc.code is OperatorFailureCode.NOT_FOUND else 409 if exc.code in {
            OperatorFailureCode.IDEMPOTENCY_CONFLICT,
            OperatorFailureCode.INVALID_TRANSITION,
            OperatorFailureCode.CONCURRENCY_LIMIT,
        } else 422
        raise HTTPException(status_code=status, detail={"code": exc.code.value, "message": str(exc)}) from exc


@operator_router.get("/veriquant/operator/profiles")
async def list_profiles() -> dict[str, Any]:
    return {"status": "ok", "profiles": list(get_operator_service().profiles())}


@operator_router.post("/veriquant/jobs")
async def create_job(payload: dict[str, Any], background_tasks: BackgroundTasks) -> dict[str, Any]:
    service = get_operator_service()
    job = _call(lambda: service.create(payload, auto_start=False))
    background_tasks.add_task(service.start, job["job_id"])
    return {"status": "accepted", "job": job}


@operator_router.get("/veriquant/jobs/{job_id}")
async def get_job(job_id: str) -> dict[str, Any]:
    return {"status": "ok", "job": _call(lambda: get_operator_service().get(job_id))}


@operator_router.get("/veriquant/jobs/{job_id}/events")
async def get_job_events(job_id: str, after: int = 0, limit: int = 200) -> dict[str, Any]:
    return {"status": "ok", **_call(lambda: get_operator_service().events(job_id, after=after, limit=limit))}


@operator_router.post("/veriquant/jobs/{job_id}/cancel")
async def cancel_job(job_id: str) -> dict[str, Any]:
    return {"status": "ok", "job": _call(lambda: get_operator_service().cancel(job_id))}


@operator_router.post("/veriquant/jobs/{job_id}/resume")
async def resume_job(job_id: str) -> dict[str, Any]:
    return {"status": "ok", "job": _call(lambda: get_operator_service().resume(job_id))}
