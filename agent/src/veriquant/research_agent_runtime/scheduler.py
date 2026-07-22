"""Campaign branch scheduler reusing Vibe Swarm DAG algorithms only."""

from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from typing import Mapping

from src.swarm.models import SwarmTask
from src.swarm.task_store import topological_layers, validate_dag
from ..contracts import FailureCode
from ..contracts.base import fail
from ..research_run_controller import ResearchRunManifest
from .checkpoint import BranchCheckpointStore, ResearchCancellationToken
from .contracts import (
    ResearchAgentFailureCode,
    ResearchAgentOutcome,
    ResearchAgentStatus,
    ResearchAgentTask,
    ResearchSwarmResult,
)
from .runtime import ResearchAgentRuntimeProtocol
from .toolbox import ResearchToolboxProtocol


class ResearchSwarmSchedulerAdapter:
    """Parallel specialist scheduling without creating another run manifest."""

    def __init__(self, *, checkpoint_store: BranchCheckpointStore, max_workers: int = 6) -> None:
        if not 1 <= max_workers <= 6:
            fail(FailureCode.INVALID_VALUE, "max_workers must be 1..6")
        self.checkpoint_store = checkpoint_store
        self.max_workers = max_workers

    def run(
        self,
        *,
        manifest: ResearchRunManifest,
        tasks: tuple[ResearchAgentTask, ...],
        runtimes: Mapping[str, ResearchAgentRuntimeProtocol],
        toolboxes: Mapping[str, ResearchToolboxProtocol],
        cancellation: ResearchCancellationToken | None = None,
        resume: bool = False,
    ) -> ResearchSwarmResult:
        token = cancellation or ResearchCancellationToken()
        self._validate(manifest, tasks, runtimes, toolboxes)
        swarm_tasks = [
            SwarmTask(
                id=task.task_id,
                agent_id=task.specialist_id,
                prompt_template=task.question,
                depends_on=list(task.depends_on),
                blocked_by=list(task.depends_on),
                input_from=dict(task.input_from),
            )
            for task in tasks
        ]
        validate_dag(swarm_tasks)
        layers = [tuple(sorted(layer)) for layer in topological_layers(swarm_tasks)]
        by_id = {task.task_id: task for task in tasks}
        outcomes: dict[str, ResearchAgentOutcome] = {}

        for layer in layers:
            runnable: list[ResearchAgentTask] = []
            for task_id in layer:
                task = by_id[task_id]
                committed = self.checkpoint_store.load(task)
                if committed is not None and resume and committed.status is ResearchAgentStatus.CANCELLED:
                    committed = None
                if committed is not None:
                    outcomes[task_id] = committed
                    continue
                if token.cancelled:
                    outcome = _terminal_failure(
                        task,
                        ResearchAgentStatus.CANCELLED,
                        ResearchAgentFailureCode.CANCELLED,
                        "CANCELLED_BEFORE_DISPATCH",
                    )
                    outcomes[task_id] = self.checkpoint_store.save(task, outcome)
                    continue
                blockers = [
                    dep
                    for dep in task.depends_on
                    if dep not in outcomes
                    or outcomes[dep].status not in {ResearchAgentStatus.COMPLETED, ResearchAgentStatus.PARTIAL}
                ]
                if blockers:
                    outcome = _terminal_failure(
                        task,
                        ResearchAgentStatus.BLOCKED,
                        ResearchAgentFailureCode.DEPENDENCY_BLOCKED,
                        "UPSTREAM_NOT_PRODUCTIVE",
                    )
                    outcomes[task_id] = self.checkpoint_store.save(task, outcome)
                    continue
                runnable.append(task)

            if not runnable:
                continue
            with ThreadPoolExecutor(
                max_workers=min(self.max_workers, len(runnable)),
                thread_name_prefix="alphaforge-research-agent",
            ) as executor:
                futures: dict[Future[ResearchAgentOutcome], ResearchAgentTask] = {}
                for task in runnable:
                    upstream = {
                        key: outcomes[source].artifact_hash
                        for key, source in sorted(task.input_from.items())
                    }
                    futures[
                        executor.submit(
                            runtimes[task.specialist_id].run,
                            task=task,
                            toolbox=toolboxes[task.task_id],
                            upstream_context=upstream,
                            cancellation=token,
                        )
                    ] = task
                for future in as_completed(futures):
                    task = futures[future]
                    try:
                        outcome = future.result()
                        if outcome.task_hash != task.artifact_hash or outcome.specialist_id != task.specialist_id:
                            fail(FailureCode.INVALID_HASH, "runtime returned an outcome for another task")
                    except Exception:
                        outcome = _terminal_failure(
                            task,
                            ResearchAgentStatus.FAILED,
                            ResearchAgentFailureCode.RUNTIME_FAILED,
                            "WORKER_EXCEPTION",
                        )
                    outcomes[task.task_id] = self.checkpoint_store.save(
                        task,
                        outcome,
                        resume_cancelled=resume,
                    )

        ordered = tuple(outcomes[task_id] for task_id in sorted(outcomes))
        return ResearchSwarmResult(
            run_id=manifest.run_id,
            manifest_hash=manifest.artifact_hash,
            outcomes=ordered,
            status=_aggregate_status(ordered),
        )

    @staticmethod
    def _validate(
        manifest: ResearchRunManifest,
        tasks: tuple[ResearchAgentTask, ...],
        runtimes: Mapping[str, ResearchAgentRuntimeProtocol],
        toolboxes: Mapping[str, ResearchToolboxProtocol],
    ) -> None:
        if not 1 <= len(tasks) <= 6:
            fail(FailureCode.INVALID_VALUE, "research scheduler requires one to six specialist tasks")
        ids = tuple(task.task_id for task in tasks)
        specialists = tuple(task.specialist_id for task in tasks)
        if ids != tuple(sorted(ids)) or len(ids) != len(set(ids)):
            fail(FailureCode.INVALID_VALUE, "tasks must be unique and stable-sorted")
        if len(specialists) != len(set(specialists)):
            fail(FailureCode.INVALID_VALUE, "one Campaign branch per specialist is required")
        if set(specialists) != set(manifest.selected_group_ids):
            fail(FailureCode.INVALID_HASH, "scheduler tasks differ from canonical manifest specialists")
        for task in tasks:
            if task.run_id != manifest.run_id or task.campaign_hash != manifest.campaign_hash:
                fail(FailureCode.INVALID_HASH, "task identity differs from canonical manifest")
            if task.data_snapshot_hash != manifest.data_snapshot_hash:
                fail(FailureCode.INVALID_HASH, "task data differs from canonical manifest")
            if task.specialist_id not in runtimes or task.task_id not in toolboxes:
                fail(FailureCode.MISSING_REQUIRED_FIELD, "runtime or toolbox is missing")
            if toolboxes[task.task_id].names != task.allowed_tools:
                fail(FailureCode.UNAUTHORIZED_CAPABILITY, "toolbox differs from task whitelist")


def _terminal_failure(
    task: ResearchAgentTask,
    status: ResearchAgentStatus,
    code: ResearchAgentFailureCode,
    origin: str,
) -> ResearchAgentOutcome:
    return ResearchAgentOutcome(
        task_id=task.task_id,
        task_hash=task.artifact_hash,
        specialist_id=task.specialist_id,
        status=status,
        plan_hash=None,
        investigation_receipts=(),
        draft_artifacts=(),
        provider_calls=0,
        tool_calls=0,
        failure_code=code,
        failure_origin=origin,
    )


def _aggregate_status(outcomes: tuple[ResearchAgentOutcome, ...]) -> ResearchAgentStatus:
    statuses = {item.status for item in outcomes}
    productive = statuses & {ResearchAgentStatus.COMPLETED, ResearchAgentStatus.PARTIAL}
    nonproductive = statuses - {ResearchAgentStatus.COMPLETED, ResearchAgentStatus.PARTIAL}
    if statuses == {ResearchAgentStatus.COMPLETED}:
        return ResearchAgentStatus.COMPLETED
    if productive:
        return ResearchAgentStatus.PARTIAL
    if statuses <= {ResearchAgentStatus.BLOCKED}:
        return ResearchAgentStatus.BLOCKED
    if statuses <= {ResearchAgentStatus.CANCELLED}:
        return ResearchAgentStatus.CANCELLED
    if nonproductive:
        return ResearchAgentStatus.FAILED
    return ResearchAgentStatus.FAILED


__all__ = ["ResearchSwarmSchedulerAdapter"]
