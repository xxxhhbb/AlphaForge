"""Durable, route-neutral job control for bounded AlphaForge research runs.

This layer owns no financial truth. Stage providers remain responsible for their
own immutable artifacts; this service only persists identity, lifecycle and
recovery checkpoints.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Protocol, runtime_checkable

from veriquant.contracts import FailureCode
from veriquant.contracts.base import fail, require_hash
from veriquant.data import canonical_content_hash
from veriquant.run_recovery.api import (
    ProviderUnavailable,
    RecoveryError,
    RecoveryInterrupted,
    RecoveryStatus,
    RunCheckpointStore,
    RunRecoveryController,
    StageExecutionResult,
    StageProvider,
    StageTask,
)

from .models import (
    ConfirmationAuthority,
    JobEventType,
    JobState,
    ProviderMode,
    ResearchJobDraft,
    ResearchJobManifest,
    ResearchJobStatus,
    ResearchReportReference,
    ZERO_HASH,
)
from .store import ResearchJobStore


@runtime_checkable
class ResearchJobServiceProtocol(Protocol):
    """Minimal future API adapter surface; it intentionally contains no routes."""

    def create_draft(self, draft: ResearchJobDraft) -> ResearchJobStatus: ...

    def confirm(
        self,
        run_id: str,
        *,
        campaign_hash: str,
        authority: ConfirmationAuthority,
    ) -> ResearchJobStatus: ...

    def start(self, run_id: str, *, holder_id: str = "research_job_worker") -> ResearchJobStatus: ...

    def status(self, run_id: str) -> ResearchJobStatus: ...

    def cancel(self, run_id: str, *, holder_id: str = "operator") -> ResearchJobStatus: ...

    def resume(self, run_id: str, *, holder_id: str = "research_job_worker") -> ResearchJobStatus: ...

    def attach_report(self, reference: ResearchReportReference) -> ResearchJobStatus: ...


class _FallbackProvider:
    def __init__(
        self,
        *,
        run_id: str,
        primary: StageProvider | None,
        fallback: StageProvider,
        store: ResearchJobStore,
    ) -> None:
        self.run_id = run_id
        self.primary = primary
        self.fallback = fallback
        self.store = store

    def execute(self, task: StageTask, *, committed_inputs: Mapping[str, str]) -> StageExecutionResult:
        if self.primary is not None:
            try:
                return self.primary.execute(task, committed_inputs=committed_inputs)
            except ProviderUnavailable:
                pass
        self.store.append_event(
            self.run_id,
            JobEventType.PROVIDER_FALLBACK_USED,
            {
                "stage_identity_hash": task.identity.artifact_hash,
                "mode": ProviderMode.DETERMINISTIC_FALLBACK.value,
            },
        )
        return self.fallback.execute(task, committed_inputs=committed_inputs)


class ResearchJobService:
    """Create, confirm and recover bounded research jobs without public routing."""

    def __init__(
        self,
        *,
        root: str | Path,
        primary_provider: StageProvider | None,
        deterministic_fallback_provider: StageProvider,
        lease_ttl_seconds: int = 60,
    ) -> None:
        if not isinstance(deterministic_fallback_provider, StageProvider):
            fail(FailureCode.INVALID_VALUE, "deterministic fallback provider is required")
        self.root = Path(root).resolve()
        self.store = ResearchJobStore(self.root / "lifecycle")
        self.recovery_store = RunCheckpointStore(self.root / "recovery")
        self.primary_provider = primary_provider
        self.fallback_provider = deterministic_fallback_provider
        self.lease_ttl_seconds = lease_ttl_seconds

    def create_draft(self, draft: ResearchJobDraft) -> ResearchJobStatus:
        if not isinstance(draft, ResearchJobDraft):
            fail(FailureCode.INVALID_VALUE, "draft must be ResearchJobDraft")
        self.store.save_draft(draft)
        return self.status(draft.run_id)

    def confirm(
        self,
        run_id: str,
        *,
        campaign_hash: str,
        authority: ConfirmationAuthority,
    ) -> ResearchJobStatus:
        require_hash(campaign_hash, "campaign_hash")
        if not isinstance(authority, ConfirmationAuthority):
            fail(FailureCode.UNAUTHORIZED_CAPABILITY, "only USER or OPERATOR may confirm a research job")
        draft = self.store.load_draft(run_id)
        manifest = ResearchJobManifest(
            run_id=run_id,
            draft_hash=draft.artifact_hash,
            campaign_hash=campaign_hash,
            frozen_input_hash=draft.frozen_input_hash,
            stages=draft.stages,
            confirmation_authority=authority,
            confirmation_hash=canonical_content_hash(
                {
                    "draft_hash": draft.artifact_hash,
                    "campaign_hash": campaign_hash,
                    "authority": authority.value,
                }
            ),
        )
        self.store.save_manifest(manifest)
        return self.status(run_id)

    def start(self, run_id: str, *, holder_id: str = "research_job_worker") -> ResearchJobStatus:
        manifest = self.store.try_load_manifest(run_id)
        if manifest is None:
            fail(FailureCode.INVALID_STATE_TRANSITION, "an unconfirmed research draft cannot start")
        current = self.status(run_id)
        if current.state is JobState.CANCELLED:
            fail(FailureCode.INVALID_STATE_TRANSITION, "a cancelled job cannot be restarted; create a new run")
        self.store.append_event(run_id, JobEventType.RUN_STARTED, {"manifest_hash": manifest.artifact_hash})
        provider = _FallbackProvider(
            run_id=run_id,
            primary=self.primary_provider,
            fallback=self.fallback_provider,
            store=self.store,
        )
        providers = {stage.provider_id: provider for stage in manifest.stages}
        recovery = RunRecoveryController(
            store=self.recovery_store,
            providers=providers,
            lease_ttl_seconds=self.lease_ttl_seconds,
        )
        result = recovery.run(run_id=run_id, tasks=manifest.to_tasks(), holder_id=holder_id)
        state = self._state_from_recovery(result.status, result.committed_stage_hashes, result.failed_stage_hashes, result.blocked_stage_hashes)
        self.store.append_event(
            run_id,
            JobEventType.RUN_FINISHED,
            {
                "job_state": state.value,
                "recovery_status": result.status.value,
                "committed_count": len(result.committed_stage_hashes),
                "failed_count": len(result.failed_stage_hashes),
                "blocked_count": len(result.blocked_stage_hashes),
            },
        )
        return self.status(run_id)

    def resume(self, run_id: str, *, holder_id: str = "research_job_worker") -> ResearchJobStatus:
        current = self.status(run_id)
        if current.state is JobState.CANCELLED:
            fail(FailureCode.INVALID_STATE_TRANSITION, "cancelled jobs remain cancelled")
        if current.state is JobState.DRAFT:
            fail(FailureCode.INVALID_STATE_TRANSITION, "unconfirmed drafts cannot resume")
        return self.start(run_id, holder_id=holder_id)

    def cancel(self, run_id: str, *, holder_id: str = "operator") -> ResearchJobStatus:
        manifest = self.store.try_load_manifest(run_id)
        if manifest is None:
            fail(FailureCode.INVALID_STATE_TRANSITION, "unconfirmed drafts are stopped by omission, not runtime cancellation")
        recovery = RunRecoveryController(
            store=self.recovery_store,
            providers={},
            lease_ttl_seconds=self.lease_ttl_seconds,
        )
        recovery.cancel(run_id=run_id, holder_id=holder_id)
        self.store.append_event(run_id, JobEventType.CANCEL_REQUESTED, {"holder_id": holder_id})
        return self.status(run_id)

    def attach_report(self, reference: ResearchReportReference) -> ResearchJobStatus:
        manifest = self.store.try_load_manifest(reference.run_id)
        if manifest is None:
            fail(FailureCode.INVALID_STATE_TRANSITION, "report cannot attach to an unconfirmed draft")
        state = self.status(reference.run_id).state
        if state not in {JobState.COMPLETED, JobState.PARTIAL, JobState.BLOCKED, JobState.FAILED}:
            fail(FailureCode.INVALID_STATE_TRANSITION, "report can attach only after a terminal run outcome")
        self.store.save_report_reference(reference)
        return self.status(reference.run_id)

    def status(self, run_id: str) -> ResearchJobStatus:
        draft = self.store.load_draft(run_id)
        manifest = self.store.try_load_manifest(run_id)
        events = self.store.read_events(run_id)
        state = JobState.DRAFT if manifest is None else JobState.CONFIRMED
        message = ""
        provider_modes: list[ProviderMode] = []
        for event in events:
            if event.event_type is JobEventType.RUN_STARTED:
                state = JobState.RUNNING
            elif event.event_type is JobEventType.RUN_FINISHED:
                state = JobState(str(event.payload["job_state"]))
            elif event.event_type is JobEventType.CANCEL_REQUESTED:
                state = JobState.CANCELLED
            elif event.event_type is JobEventType.PROVIDER_FALLBACK_USED:
                provider_modes.append(ProviderMode.DETERMINISTIC_FALLBACK)
        if events and any(event.event_type is JobEventType.RUN_STARTED for event in events):
            if self.primary_provider is not None:
                provider_modes.append(ProviderMode.CONFIGURED_PROVIDER)
            elif ProviderMode.DETERMINISTIC_FALLBACK not in provider_modes:
                provider_modes.append(ProviderMode.DETERMINISTIC_FALLBACK)
        recovery_state = self.recovery_store.load_state(run_id)
        if state is JobState.RUNNING and not self.recovery_store.lease_path(run_id).exists():
            state = JobState.INTERRUPTED
            message = "worker stopped before a terminal lifecycle event; resume is safe"
        return ResearchJobStatus(
            run_id=run_id,
            state=state,
            draft_hash=draft.artifact_hash,
            manifest_hash=manifest.artifact_hash if manifest is not None else None,
            committed_stage_hashes=tuple(sorted(recovery_state.committed_by_hash)),
            failed_stage_hashes=tuple(sorted(recovery_state.failed_by_hash)),
            blocked_stage_hashes=tuple(sorted(recovery_state.blocked_by_hash)),
            provider_modes=tuple(sorted(set(provider_modes), key=lambda item: item.value)),
            event_log_hash=events[-1].artifact_hash if events else ZERO_HASH,
            report_reference=self.store.try_load_report_reference(run_id),
            message=message,
        )

    @staticmethod
    def _state_from_recovery(
        status: RecoveryStatus,
        committed: tuple[str, ...],
        failed: tuple[str, ...],
        blocked: tuple[str, ...],
    ) -> JobState:
        if status is RecoveryStatus.CANCELLED:
            return JobState.CANCELLED
        if status is RecoveryStatus.INTERRUPTED:
            return JobState.INTERRUPTED
        if failed or blocked:
            return JobState.PARTIAL if committed else (JobState.FAILED if failed else JobState.BLOCKED)
        return JobState.COMPLETED if status is RecoveryStatus.COMMITTED else JobState.RUNNING


@dataclass
class FakeBoundedStageProvider:
    """Deterministic test provider; never performs network or financial calculation."""

    unavailable_stage_ids: frozenset[str] = frozenset()
    fail_stage_ids: frozenset[str] = frozenset()
    interrupt_stage_ids: frozenset[str] = frozenset()
    interrupt_once: bool = True
    calls: list[str] = field(default_factory=list)
    _interrupted: set[str] = field(default_factory=set)

    def execute(self, task: StageTask, *, committed_inputs: Mapping[str, str]) -> StageExecutionResult:
        stage_id = task.identity.stage_id
        self.calls.append(stage_id)
        if stage_id in self.unavailable_stage_ids:
            raise ProviderUnavailable(FailureCode.ADAPTER_UNAVAILABLE, f"provider unavailable for {stage_id}")
        if stage_id in self.fail_stage_ids:
            raise RecoveryError(FailureCode.ADAPTER_EXECUTION_FAILED, f"stage failed closed: {stage_id}")
        if stage_id in self.interrupt_stage_ids and (not self.interrupt_once or stage_id not in self._interrupted):
            self._interrupted.add(stage_id)
            raise RecoveryInterrupted(FailureCode.ADAPTER_EXECUTION_FAILED, f"interrupted before commit: {stage_id}")
        return StageExecutionResult(
            stage_identity_hash=task.identity.artifact_hash,
            output_artifact_hash=task.identity.expected_artifact_hash,
            payload_hash=canonical_content_hash(
                {
                    "stage_identity_hash": task.identity.artifact_hash,
                    "committed_inputs": tuple(sorted(committed_inputs)),
                }
            ),
            metadata={"provider": "deterministic_test_fixture"},
        )


__all__ = [
    "FakeBoundedStageProvider",
    "ResearchJobService",
    "ResearchJobServiceProtocol",
]
