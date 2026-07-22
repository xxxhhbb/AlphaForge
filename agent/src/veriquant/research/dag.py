"""Fail-closed outer Research DAG validation and topological execution."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Callable, Mapping

from ..contracts import CanonicalContract, ContractValidationError, FailureCode
from ..contracts.base import require_identifier
from .models import TaskStatus


def _fail(code: FailureCode, message: str, **details: Any) -> None:
    raise ContractValidationError(code, message, details=details)


@dataclass(frozen=True, kw_only=True)
class ResearchTaskSpec(CanonicalContract):
    task_id: str
    depends_on: tuple[str, ...] = ()
    input_from: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        require_identifier(self.task_id, "task_id")
        if any(not isinstance(item, str) or not item for item in self.depends_on):
            _fail(FailureCode.DAG_VALIDATION_FAILED, "depends_on must contain task ids")
        if len(set(self.depends_on)) != len(self.depends_on):
            _fail(FailureCode.DAG_VALIDATION_FAILED, "depends_on contains duplicates")
        if not isinstance(self.input_from, Mapping):
            _fail(FailureCode.DAG_VALIDATION_FAILED, "input_from must be a mapping")
        copied: dict[str, str] = {}
        for local_name, source in self.input_from.items():
            if not isinstance(local_name, str) or not local_name or not isinstance(source, str) or not source:
                _fail(FailureCode.DAG_VALIDATION_FAILED, "input_from entries must be task mappings")
            copied[local_name] = source
        object.__setattr__(self, "input_from", MappingProxyType(copied))


@dataclass(frozen=True, kw_only=True)
class TaskExecution:
    status: TaskStatus
    output: Any = None
    failure_code: FailureCode | None = None
    error: str | None = None

    def __post_init__(self) -> None:
        if self.status is TaskStatus.COMPLETED:
            if self.output is None or self.failure_code is not None or self.error is not None:
                _fail(FailureCode.INVALID_VALUE, "completed task requires only substantive output")
        elif self.status in {TaskStatus.FAILED, TaskStatus.INCOMPLETE, TaskStatus.BLOCKED}:
            if self.output is not None or self.failure_code is None or not self.error:
                _fail(FailureCode.INVALID_VALUE, "non-completed task cannot carry a success output")
        else:
            _fail(FailureCode.INVALID_STATE, "TaskExecution must be terminal")


@dataclass(frozen=True, kw_only=True)
class DAGRunResult:
    records: Mapping[str, TaskExecution]

    def __post_init__(self) -> None:
        object.__setattr__(self, "records", MappingProxyType(dict(self.records)))


class ResearchTaskDAG:
    def __init__(self, tasks: tuple[ResearchTaskSpec, ...]) -> None:
        if not tasks:
            _fail(FailureCode.DAG_VALIDATION_FAILED, "Research DAG requires tasks")
        by_id = {task.task_id: task for task in tasks}
        if len(by_id) != len(tasks):
            _fail(FailureCode.DAG_VALIDATION_FAILED, "Research DAG task ids must be unique")
        for task in tasks:
            unknown = set(task.depends_on) - set(by_id)
            if unknown:
                _fail(
                    FailureCode.DAG_VALIDATION_FAILED,
                    "Research DAG contains an unknown dependency",
                    task=task.task_id,
                    dependencies=tuple(sorted(unknown)),
                )
            if task.task_id in task.depends_on:
                _fail(FailureCode.DAG_VALIDATION_FAILED, "task cannot depend on itself")
            illegal_inputs = set(task.input_from.values()) - set(task.depends_on)
            if illegal_inputs:
                _fail(
                    FailureCode.DAG_VALIDATION_FAILED,
                    "input_from source must be a declared direct dependency",
                    task=task.task_id,
                    sources=tuple(sorted(illegal_inputs)),
                )
        self._tasks = MappingProxyType(by_id)
        self._layers = self._topological_layers()

    @property
    def tasks(self) -> Mapping[str, ResearchTaskSpec]:
        return self._tasks

    @property
    def layers(self) -> tuple[tuple[str, ...], ...]:
        return self._layers

    def _topological_layers(self) -> tuple[tuple[str, ...], ...]:
        remaining = {task_id: set(task.depends_on) for task_id, task in self._tasks.items()}
        layers: list[tuple[str, ...]] = []
        resolved: set[str] = set()
        while len(resolved) < len(self._tasks):
            ready = tuple(
                sorted(
                    task_id
                    for task_id, dependencies in remaining.items()
                    if task_id not in resolved and dependencies <= resolved
                )
            )
            if not ready:
                _fail(FailureCode.DAG_VALIDATION_FAILED, "Research DAG contains a cycle")
            layers.append(ready)
            resolved.update(ready)
        return tuple(layers)


TaskHandler = Callable[[Mapping[str, Any]], TaskExecution]


class ResearchDAGRuntime:
    def __init__(self, *, max_workers: int = 6) -> None:
        if isinstance(max_workers, bool) or not isinstance(max_workers, int) or max_workers < 1:
            _fail(FailureCode.INVALID_VALUE, "max_workers must be positive")
        self.max_workers = max_workers

    @staticmethod
    def _run_handler(handler: TaskHandler, inputs: Mapping[str, Any]) -> TaskExecution:
        try:
            result = handler(inputs)
        except IncompleteTask as exc:
            return TaskExecution(
                status=TaskStatus.INCOMPLETE,
                failure_code=FailureCode.INCOMPLETE_RESULT,
                error=str(exc) or "task incomplete",
            )
        except ContractValidationError as exc:
            return TaskExecution(status=TaskStatus.FAILED, failure_code=exc.code, error=str(exc))
        except Exception as exc:
            return TaskExecution(
                status=TaskStatus.FAILED,
                failure_code=FailureCode.ADAPTER_EXECUTION_FAILED,
                error=str(exc) or type(exc).__name__,
            )
        if not isinstance(result, TaskExecution):
            return TaskExecution(
                status=TaskStatus.FAILED,
                failure_code=FailureCode.INCOMPLETE_RESULT,
                error="task handler returned no typed terminal result",
            )
        return result

    def run(
        self,
        dag: ResearchTaskDAG,
        *,
        handlers: Mapping[str, TaskHandler],
    ) -> DAGRunResult:
        missing = set(dag.tasks) - set(handlers)
        if missing:
            _fail(
                FailureCode.DAG_VALIDATION_FAILED,
                "Research DAG has no handler for a task",
                tasks=tuple(sorted(missing)),
            )
        records: dict[str, TaskExecution] = {}
        for layer in dag.layers:
            runnable: list[tuple[str, Mapping[str, Any]]] = []
            for task_id in layer:
                task = dag.tasks[task_id]
                blocked_by = tuple(
                    dependency
                    for dependency in task.depends_on
                    if records[dependency].status is not TaskStatus.COMPLETED
                )
                if blocked_by:
                    records[task_id] = TaskExecution(
                        status=TaskStatus.BLOCKED,
                        failure_code=FailureCode.UPSTREAM_BLOCKED,
                        error="blocked by non-completed upstream: " + ",".join(sorted(blocked_by)),
                    )
                    continue
                if task.input_from:
                    inputs = {
                        local_name: records[source].output
                        for local_name, source in task.input_from.items()
                    }
                else:
                    inputs = {dependency: records[dependency].output for dependency in task.depends_on}
                runnable.append((task_id, MappingProxyType(inputs)))
            if not runnable:
                continue
            with ThreadPoolExecutor(max_workers=min(self.max_workers, len(runnable))) as executor:
                futures = {
                    executor.submit(self._run_handler, handlers[task_id], inputs): task_id
                    for task_id, inputs in runnable
                }
                completed: dict[str, TaskExecution] = {}
                for future in as_completed(futures):
                    completed[futures[future]] = future.result()
                for task_id in sorted(completed):
                    records[task_id] = completed[task_id]
        return DAGRunResult(records=records)


class IncompleteTask(RuntimeError):
    pass
