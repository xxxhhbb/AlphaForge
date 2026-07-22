"""Deterministic implementation of the stage execution protocol."""

from __future__ import annotations

from types import MappingProxyType
from typing import Mapping

from ..contracts import Component, FailureCode
from ..contracts.base import fail
from .models import (
    CampaignStageFailure,
    CampaignStageRequest,
    CampaignStageResult,
    CampaignStageStatus,
    ProviderMode,
    StageExecutionPolicy,
    deterministic_artifact_ref,
    stage_kind,
)


class DeterministicStageFixtureExecutor:
    """Execute one stage at a time for tests and offline integration.

    It never calls a model, never calculates official metrics, and records
    control stages as hash-only references.  In REAL_LLM mode, missing provider
    availability returns a typed unavailable result instead of falling back.
    """

    def __init__(
        self,
        *,
        provider_available: bool = False,
        stage_failures: Mapping[str, CampaignStageFailure] | None = None,
    ) -> None:
        self.provider_available = provider_available
        self.stage_failures = MappingProxyType(dict(stage_failures or {}))
        self.calls: list[str] = []

    def run_stage(self, request: CampaignStageRequest, policy: StageExecutionPolicy) -> CampaignStageResult:
        if not isinstance(request, CampaignStageRequest):
            fail(FailureCode.INVALID_VALUE, "request must be CampaignStageRequest")
        if not isinstance(policy, StageExecutionPolicy):
            fail(FailureCode.INVALID_VALUE, "policy must be StageExecutionPolicy")
        self.calls.append(request.stage_key)

        if not request.upstream_satisfied:
            return _blocked_result(
                request,
                status=CampaignStageStatus.SKIPPED_UPSTREAM,
                failure_code=CampaignStageFailure.UPSTREAM_BLOCKED,
                failure_detail_code=FailureCode.UPSTREAM_BLOCKED,
                message="upstream dependency blocked",
            )

        if policy.provider_mode is ProviderMode.REAL_LLM and not self.provider_available:
            return _blocked_result(
                request,
                status=CampaignStageStatus.UNAVAILABLE,
                failure_code=CampaignStageFailure.PROVIDER_UNAVAILABLE,
                failure_detail_code=FailureCode.ADAPTER_UNAVAILABLE,
                message="REAL_LLM provider unavailable; deterministic fallback is disabled",
            )

        injected_failure = self.stage_failures.get(request.stage_key)
        if injected_failure is not None:
            return _blocked_result(
                request,
                status=CampaignStageStatus.BLOCKED,
                failure_code=injected_failure,
                failure_detail_code=FailureCode.ADAPTER_EXECUTION_FAILED,
                message="stage failed closed",
            )

        status, produced_by = stage_kind(request.stage_id)
        ref = deterministic_artifact_ref(request, status, produced_by)
        return CampaignStageResult(
            run_id=request.run_id,
            stage_key=request.stage_key,
            route_task_id=request.route_task_id,
            desk_id=request.desk_id,
            stage_id=request.stage_id,
            stage_index=request.stage_index,
            status=status,
            produced_by=produced_by,
            input_hash=request.input_hash,
            output_hash=ref.content_hash,
            prompt_hash=_hash_redacted(request.stage_key, "prompt"),
            trace_hash=_hash_redacted(request.stage_key, "trace"),
            artifact_refs=(ref,),
        )


def _blocked_result(
    request: CampaignStageRequest,
    *,
    status: CampaignStageStatus,
    failure_code: CampaignStageFailure,
    failure_detail_code: FailureCode,
    message: str,
) -> CampaignStageResult:
    return CampaignStageResult(
        run_id=request.run_id,
        stage_key=request.stage_key,
        route_task_id=request.route_task_id,
        desk_id=request.desk_id,
        stage_id=request.stage_id,
        stage_index=request.stage_index,
        status=status,
        produced_by=Component.OPERATOR,
        input_hash=request.input_hash,
        output_hash=None,
        prompt_hash=_hash_redacted(request.stage_key, "prompt"),
        trace_hash=_hash_redacted(request.stage_key, "trace"),
        failure_code=failure_code,
        failure_detail_code=failure_detail_code,
        message=message,
    )


def _hash_redacted(stage_key: str, kind: str) -> str:
    import hashlib
    import json

    payload = json.dumps({"kind": kind, "stage_key": stage_key, "redacted": True}, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()
