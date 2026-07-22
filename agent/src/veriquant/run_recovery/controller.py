"""Generic run recovery controller with idempotent stage execution."""

from __future__ import annotations

from typing import Mapping, Protocol, Sequence, runtime_checkable

from veriquant.contracts import FailureCode
from veriquant.data import canonical_content_hash

from .models import (
    LeaseUnavailable,
    ProviderUnavailable,
    RecoveryError,
    RecoveryEventType,
    RecoveryInterrupted,
    RecoveryStatus,
    ResumePlan,
    StageExecutionResult,
    StageTask,
)
from .store import RunCheckpointStore


@runtime_checkable
class StageProvider(Protocol):
    """Minimal deterministic provider Protocol for one generic stage."""

    def execute(self, task: StageTask, *, committed_inputs: Mapping[str, str]) -> StageExecutionResult: ...


def _sort_tasks(tasks: Sequence[StageTask]) -> tuple[StageTask, ...]:
    if not isinstance(tasks, (tuple, list)) or not tasks:
        raise RecoveryError(FailureCode.INVALID_VALUE, "stage task sequence is required")
    task_tuple = tuple(tasks)
    if any(not isinstance(task, StageTask) for task in task_tuple):
        raise RecoveryError(FailureCode.INVALID_VALUE, "tasks must contain StageTask values")
    seen: set[str] = set()
    for task in task_tuple:
        identity_hash = task.identity.artifact_hash
        if identity_hash in seen:
            raise RecoveryError(FailureCode.INVALID_VALUE, "duplicate stage identity")
        seen.add(identity_hash)
    return tuple(sorted(task_tuple, key=lambda task: (task.identity.ordinal, task.identity.branch_id, task.identity.stage_id)))


class RunRecoveryController:
    """Resume-capable executor for A-F stages and daily checkpoints."""

    def __init__(
        self,
        *,
        store: RunCheckpointStore,
        providers: Mapping[str, StageProvider],
        lease_ttl_seconds: int = 60,
    ) -> None:
        if not isinstance(store, RunCheckpointStore):
            raise RecoveryError(FailureCode.INVALID_VALUE, "store must be RunCheckpointStore")
        if not isinstance(providers, Mapping):
            raise RecoveryError(FailureCode.INVALID_VALUE, "providers mapping is required")
        self.store = store
        self.providers = dict(providers)
        self.lease_ttl_seconds = lease_ttl_seconds

    def plan(self, *, run_id: str, tasks: Sequence[StageTask]) -> ResumePlan:
        ordered = _sort_tasks(tasks)
        state = self.store.load_state(run_id)
        committed = set(state.committed_by_hash)
        failed = set(state.failed_by_hash)
        blocked = set(state.blocked_by_hash)
        skipped: list[str] = []
        runnable: list[str] = []
        for task in ordered:
            identity_hash = task.identity.artifact_hash
            if identity_hash in committed:
                skipped.append(identity_hash)
            elif identity_hash in failed or identity_hash in blocked:
                continue
            elif all(dep in committed for dep in task.depends_on):
                runnable.append(identity_hash)
        final_hash = self._final_artifact_hash(ordered, committed)
        status = RecoveryStatus.CANCELLED if state.cancelled else RecoveryStatus.PENDING
        return ResumePlan(
            run_id=run_id,
            status=status,
            committed_stage_hashes=tuple(sorted(committed)),
            skipped_stage_hashes=tuple(skipped),
            runnable_stage_hashes=tuple(runnable),
            failed_stage_hashes=tuple(sorted(failed)),
            blocked_stage_hashes=tuple(sorted(blocked)),
            final_artifact_hash=final_hash,
        )

    def run(self, *, run_id: str, tasks: Sequence[StageTask], holder_id: str) -> ResumePlan:
        ordered = _sort_tasks(tasks)
        token = self.store.acquire_lease(run_id, holder_id=holder_id, ttl_seconds=self.lease_ttl_seconds)
        interrupted = False
        message = ""
        try:
            for task in ordered:
                state = self.store.load_state(run_id)
                if state.cancelled:
                    return self._make_plan(
                        run_id=run_id,
                        tasks=ordered,
                        status=RecoveryStatus.CANCELLED,
                        message="run cancellation requested",
                    )
                committed = set(state.committed_by_hash)
                failed = set(state.failed_by_hash)
                blocked = set(state.blocked_by_hash)
                identity_hash = task.identity.artifact_hash
                if identity_hash in committed or identity_hash in failed or identity_hash in blocked:
                    continue
                failed_dependencies = [dep for dep in task.depends_on if dep in failed or dep in blocked]
                if failed_dependencies:
                    self.store.fail_stage(
                        token=token,
                        identity=task.identity,
                        failure_code=FailureCode.UPSTREAM_BLOCKED,
                        message="upstream dependency failed or blocked",
                        blocked=True,
                    )
                    continue
                if any(dep not in committed for dep in task.depends_on):
                    continue
                provider = self.providers.get(task.provider_id)
                if provider is None:
                    self.store.fail_stage(
                        token=token,
                        identity=task.identity,
                        failure_code=FailureCode.ADAPTER_UNAVAILABLE,
                        message=f"provider unavailable: {task.provider_id}",
                    )
                    continue
                self.store.assert_lease(token)
                self.store.append_event(
                    run_id=run_id,
                    event_type=RecoveryEventType.STAGE_STARTED,
                    stage_identity_hash=identity_hash,
                    lease_token_id=token.token_id,
                    status=RecoveryStatus.PENDING,
                    payload_hash=canonical_content_hash(
                        {
                            "provider_id": task.provider_id,
                            "depends_on": task.depends_on,
                            "identity_hash": identity_hash,
                        }
                    ),
                )
                try:
                    result = provider.execute(
                        task,
                        committed_inputs={
                            checkpoint.identity.artifact_hash: checkpoint.committed_artifact_hash
                            for checkpoint in state.committed
                        },
                    )
                    if not isinstance(result, StageExecutionResult):
                        raise RecoveryError(FailureCode.INVALID_VALUE, "provider returned invalid result")
                    if result.stage_identity_hash != identity_hash:
                        raise RecoveryError(FailureCode.INVALID_HASH, "provider result stage hash mismatch")
                    self.store.commit_stage(
                        token=token,
                        identity=task.identity,
                        artifact_hash=result.output_artifact_hash,
                        payload_hash=result.payload_hash,
                    )
                except RecoveryInterrupted as exc:
                    interrupted = True
                    message = str(exc)
                    self.store.append_event(
                        run_id=run_id,
                        event_type=RecoveryEventType.RUN_INTERRUPTED,
                        stage_identity_hash=identity_hash,
                        lease_token_id=token.token_id,
                        status=RecoveryStatus.INTERRUPTED,
                        failure_code=exc.code,
                        message=message,
                    )
                    break
                except ProviderUnavailable as exc:
                    self.store.fail_stage(
                        token=token,
                        identity=task.identity,
                        failure_code=exc.code,
                        message=str(exc),
                    )
                except RecoveryError as exc:
                    self.store.fail_stage(
                        token=token,
                        identity=task.identity,
                        failure_code=exc.code,
                        message=str(exc),
                    )
                except Exception as exc:
                    self.store.fail_stage(
                        token=token,
                        identity=task.identity,
                        failure_code=FailureCode.ADAPTER_EXECUTION_FAILED,
                        message=f"provider exception: {type(exc).__name__}",
                    )
            status = RecoveryStatus.INTERRUPTED if interrupted else RecoveryStatus.COMMITTED
            return self._make_plan(run_id=run_id, tasks=ordered, status=status, message=message)
        finally:
            try:
                self.store.release_lease(token)
            except (RecoveryError, LeaseUnavailable):
                _ = token

    def cancel(self, *, run_id: str, holder_id: str = "operator") -> ResumePlan:
        self.store.request_cancel(run_id, holder_id=holder_id)
        return self._make_plan(run_id=run_id, tasks=(), status=RecoveryStatus.CANCELLED, message="cancelled")

    def _make_plan(
        self,
        *,
        run_id: str,
        tasks: Sequence[StageTask],
        status: RecoveryStatus,
        message: str = "",
    ) -> ResumePlan:
        state = self.store.load_state(run_id)
        committed = set(state.committed_by_hash)
        failed = set(state.failed_by_hash)
        blocked = set(state.blocked_by_hash)
        runnable = []
        skipped = []
        for task in tasks:
            identity_hash = task.identity.artifact_hash
            if identity_hash in committed:
                skipped.append(identity_hash)
            elif identity_hash not in failed and identity_hash not in blocked and all(dep in committed for dep in task.depends_on):
                runnable.append(identity_hash)
        if state.cancelled:
            status = RecoveryStatus.CANCELLED
        elif failed and status is RecoveryStatus.COMMITTED:
            status = RecoveryStatus.FAILED
        elif blocked and status is RecoveryStatus.COMMITTED:
            status = RecoveryStatus.BLOCKED
        return ResumePlan(
            run_id=run_id,
            status=status,
            committed_stage_hashes=tuple(sorted(committed)),
            skipped_stage_hashes=tuple(skipped),
            runnable_stage_hashes=tuple(runnable),
            failed_stage_hashes=tuple(sorted(failed)),
            blocked_stage_hashes=tuple(sorted(blocked)),
            final_artifact_hash=self._final_artifact_hash(tasks, committed),
            message=message,
        )

    @staticmethod
    def _final_artifact_hash(tasks: Sequence[StageTask], committed_identity_hashes: set[str]) -> str | None:
        committed_tasks = [task for task in tasks if task.identity.artifact_hash in committed_identity_hashes]
        if not committed_tasks:
            return None
        last = max(committed_tasks, key=lambda task: (task.identity.ordinal, task.identity.branch_id, task.identity.stage_id))
        return last.identity.expected_artifact_hash
