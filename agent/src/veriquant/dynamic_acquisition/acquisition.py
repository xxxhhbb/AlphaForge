"""Fail-closed acquisition executor over an injected approved-proxy transport."""

from __future__ import annotations

from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Protocol

from .checkpoint import CheckpointStore
from .models import (
    AcquisitionRunResult,
    PartitionResult,
    PartitionStatus,
    QueryResponse,
    QueryTask,
    ResourceBudget,
    canonical_rows_hash,
    canonical_rows_size,
    stable_content_hash,
)
from .planner import AcquisitionPlan
from .registry import (
    CapabilityRegistry,
    DEFAULT_REGISTRY,
    PaginationStrategy,
)


class ApprovedProxyTransport(Protocol):
    """Transport owns credentials internally; this protocol never accepts one."""

    offline_fixture: bool

    def query(
        self,
        api_name: str,
        *,
        params: Mapping[str, Any],
        fields: tuple[str, ...],
    ) -> QueryResponse | list[Mapping[str, Any]]: ...


class PartitionRowSink(Protocol):
    """Persist canonical partition rows without receiving provider secrets."""

    def write(
        self,
        task: QueryTask,
        rows: tuple[Mapping[str, Any], ...],
    ) -> str: ...


@dataclass(frozen=True, slots=True)
class _QueryOutcome:
    """Worker output; deliberately excludes provider exception text."""

    task: QueryTask
    response: QueryResponse | None
    calls_used: int
    failure_type: str | None = None


class DynamicAcquirer:
    def __init__(
        self,
        transport: ApprovedProxyTransport,
        *,
        registry: CapabilityRegistry = DEFAULT_REGISTRY,
        checkpoint: CheckpointStore | None = None,
        row_sink: PartitionRowSink | None = None,
        source_label: str | None = None,
        max_attempts: int = 1,
        continue_after_partition_failure: bool = True,
    ) -> None:
        self._transport = transport
        self._registry = registry
        self._checkpoint = checkpoint
        self._row_sink = row_sink
        if isinstance(max_attempts, bool) or max_attempts < 1 or max_attempts > 3:
            raise ValueError("max_attempts must be between one and three")
        self._max_attempts = max_attempts
        self._continue_after_partition_failure = bool(continue_after_partition_failure)
        self._source_label = source_label or (
            "OFFLINE_FIXTURE"
            if bool(getattr(transport, "offline_fixture", False))
            else "APPROVED_PROXY_DATA_VENDOR"
        )
        lowered = self._source_label.casefold()
        if not self._source_label.strip() or any(
            token in lowered
            for token in ("token", "secret", "authorization", "official_tushare")
        ):
            raise ValueError("source_label must be public and non-official")

    def run(
        self,
        plan: AcquisitionPlan,
        *,
        budget: ResourceBudget | None = None,
        allow_network: bool = False,
    ) -> AcquisitionRunResult:
        budget = budget or ResourceBudget()
        completed = self._checkpoint.load(plan.plan_hash) if self._checkpoint else {}
        results: dict[str, PartitionResult] = dict(completed)
        skipped = 0
        # Calls are part of the resumable budget identity.  A restarted run
        # must not regain calls that were already consumed before checkpoint.
        calls = sum(item.api_calls_used for item in completed.values())
        rows_seen = sum(
            item.row_count
            for item in completed.values()
            if item.status in {PartitionStatus.SUCCESS, PartitionStatus.EMPTY}
        )
        storage_seen = sum(item.content_bytes for item in completed.values())
        if not bool(getattr(self._transport, "offline_fixture", False)) and not allow_network:
            return self._finish(
                plan,
                (
                    PartitionResult(
                        task_id="network-gate",
                        status=PartitionStatus.NETWORK_DISABLED,
                        row_count=0,
                        content_hash=None,
                        failure_type="NETWORK_NOT_ENABLED",
                    ),
                ),
                skipped,
                PartitionStatus.NETWORK_DISABLED,
                calls_used=0,
            )
        terminal = PartitionStatus.SUCCESS
        any_success = any(
            item.status is PartitionStatus.SUCCESS for item in completed.values()
        )
        any_empty = any(
            item.status is PartitionStatus.EMPTY for item in completed.values()
        )
        pending: list[QueryTask] = []
        for task in plan.iter_tasks(self._registry):
            prior = completed.get(task.task_id)
            if prior is not None and prior.status in {
                PartitionStatus.SUCCESS,
                PartitionStatus.EMPTY,
            }:
                skipped += 1
                continue
            pending.append(task)

        stop = False
        cursor = 0
        while cursor < len(pending) and not stop:
            if budget.max_api_calls is not None and calls >= budget.max_api_calls:
                task = pending[cursor]
                result = PartitionResult(
                    task_id=task.task_id,
                    status=PartitionStatus.BUDGET_EXHAUSTED,
                    row_count=0,
                    content_hash=None,
                    failure_type="MAX_API_CALLS",
                )
                results[task.task_id] = result
                terminal = result.status
                break
            # Reserve the entire bounded retry allowance before dispatch.  A wave
            # waits for all workers before unused reservations are released, so
            # admission never depends on thread completion order.
            reservations: list[tuple[QueryTask, int]] = []
            reserved_calls = 0
            while (
                cursor < len(pending)
                and len(reservations) < budget.max_parallel_requests
            ):
                available = (
                    self._max_attempts
                    if budget.max_api_calls is None
                    else budget.max_api_calls - calls - reserved_calls
                )
                if available <= 0:
                    break
                allowance = min(self._max_attempts, available)
                reservations.append((pending[cursor], allowance))
                reserved_calls += allowance
                cursor += 1

            if len(reservations) == 1:
                # Preserve the original coordinator-thread behavior for the
                # explicitly serial profile (including thread-affine fakes).
                task, allowance = reservations[0]
                outcomes = [self._query_partition(task, allowance)]
            else:
                with ThreadPoolExecutor(max_workers=len(reservations)) as executor:
                    futures = [
                        executor.submit(self._query_partition, task, allowance)
                        for task, allowance in reservations
                    ]
                    # Consume in reservation order, never completion order.
                    outcomes = [future.result() for future in futures]

            calls += sum(outcome.calls_used for outcome in outcomes)
            for outcome in outcomes:
                result = self._commit_outcome(
                    outcome,
                    budget=budget,
                    rows_seen=rows_seen,
                    storage_seen=storage_seen,
                    prior_calls=(
                        completed[outcome.task.task_id].api_calls_used
                        if outcome.task.task_id in completed
                        else 0
                    ),
                )
                results[outcome.task.task_id] = result
                if result.status in {PartitionStatus.SUCCESS, PartitionStatus.EMPTY}:
                    rows_seen += result.row_count
                    storage_seen += result.content_bytes
                    any_success = any_success or result.status is PartitionStatus.SUCCESS
                    any_empty = any_empty or result.status is PartitionStatus.EMPTY
                if self._checkpoint:
                    # The coordinator is the sole checkpoint writer.
                    self._checkpoint.save(plan.plan_hash, results)
                if result.status in {
                    PartitionStatus.BUDGET_EXHAUSTED,
                    PartitionStatus.NETWORK_DISABLED,
                }:
                    terminal = result.status
                    stop = True
                    break
                if result.status not in {PartitionStatus.SUCCESS, PartitionStatus.EMPTY}:
                    terminal = result.status
                    if not self._continue_after_partition_failure:
                        stop = True
                        break
        if not stop and cursor >= len(pending):
            if not any_success and any_empty:
                terminal = PartitionStatus.EMPTY
        ordered = tuple(sorted(results.values(), key=lambda value: value.task_id))
        return self._finish(plan, ordered, skipped, terminal, calls_used=calls)

    def _query_partition(self, task: QueryTask, attempt_limit: int) -> _QueryOutcome:
        raw = None
        last_failure_type: str | None = None
        calls_used = 0
        for _attempt in range(attempt_limit):
            try:
                raw = self._transport.query(
                    task.api_name, params=task.params, fields=task.fields
                )
                calls_used += 1
                last_failure_type = None
                break
            except Exception as exc:
                calls_used += 1
                # Provider messages may contain credentials or endpoints.
                last_failure_type = _provider_failure_code(exc)
        if last_failure_type is not None:
            return _QueryOutcome(task, None, calls_used, last_failure_type)
        try:
            if raw is None:
                raise RuntimeError("transport produced no response")
            response = _normalize_response(raw)
            _validate_response_fields(response, task.fields)
            return _QueryOutcome(task, response, calls_used)
        except Exception:
            return _QueryOutcome(
                task,
                None,
                calls_used,
                "INVALID_PROVIDER_RESPONSE",
            )

    def _commit_outcome(
        self,
        outcome: _QueryOutcome,
        *,
        budget: ResourceBudget,
        rows_seen: int,
        storage_seen: int,
        prior_calls: int = 0,
    ) -> PartitionResult:
        task_calls = prior_calls + outcome.calls_used
        if outcome.failure_type is not None or outcome.response is None:
            return PartitionResult(
                task_id=outcome.task.task_id,
                status=PartitionStatus.FAILED,
                row_count=0,
                content_hash=None,
                failure_type=outcome.failure_type or "INVALID_RESPONSE",
                api_calls_used=task_calls,
            )
        try:
            response = outcome.response
            capability = self._registry.get(outcome.task.api_name)
            if response.truncated or (
                capability.pagination is not PaginationStrategy.OFFSET
                and len(response.rows) >= capability.max_rows_per_call
            ):
                return PartitionResult(
                    task_id=outcome.task.task_id,
                    status=PartitionStatus.TRUNCATED,
                    row_count=len(response.rows),
                    content_hash=None,
                    failure_type="EXPLICIT_OR_LIMIT_TRUNCATION",
                    api_calls_used=task_calls,
                )
            if budget.max_rows is not None and rows_seen + len(response.rows) > budget.max_rows:
                return PartitionResult(
                    task_id=outcome.task.task_id,
                    status=PartitionStatus.BUDGET_EXHAUSTED,
                    row_count=0,
                    content_hash=None,
                    failure_type="MAX_ROWS",
                    api_calls_used=task_calls,
                )
            rows = tuple(response.rows)
            content_bytes = canonical_rows_size(rows)
            if (
                budget.max_storage_bytes is not None
                and storage_seen + content_bytes > budget.max_storage_bytes
            ):
                return PartitionResult(
                    task_id=outcome.task.task_id,
                    status=PartitionStatus.BUDGET_EXHAUSTED,
                    row_count=0,
                    content_hash=None,
                    failure_type="MAX_STORAGE_BYTES",
                    api_calls_used=task_calls,
                )
            content_hash = canonical_rows_hash(rows)
            if self._row_sink is not None:
                stored_hash = self._row_sink.write(outcome.task, rows)
                if stored_hash != content_hash:
                    raise ValueError("partition row sink returned a mismatched hash")
            return PartitionResult(
                task_id=outcome.task.task_id,
                status=PartitionStatus.EMPTY if not rows else PartitionStatus.SUCCESS,
                row_count=len(rows),
                content_hash=content_hash,
                content_bytes=content_bytes,
                api_calls_used=task_calls,
            )
        except Exception:
            return PartitionResult(
                task_id=outcome.task.task_id,
                status=PartitionStatus.FAILED,
                row_count=0,
                content_hash=None,
                failure_type="PARTITION_COMMIT_FAILED",
                api_calls_used=task_calls,
            )

    def _finish(
        self,
        plan: AcquisitionPlan,
        partitions: tuple[PartitionResult, ...],
        skipped: int,
        status: PartitionStatus,
        *,
        calls_used: int = 0,
    ) -> AcquisitionRunResult:
        manifest = {
            "schema_version": "veriquant.dynamic-acquisition-manifest/v1",
            "registry_version": self._registry.version,
            "source_label": self._source_label,
            "official_tushare": False,
            "plan_hash": plan.plan_hash,
            "status": status.value,
            "partitions": [
                {
                    "task_id": item.task_id,
                    "status": item.status.value,
                    "row_count": item.row_count,
                    "content_hash": item.content_hash,
                    "failure_type": item.failure_type,
                    "content_bytes": item.content_bytes,
                    "api_calls_used": item.api_calls_used,
                }
                for item in partitions
            ],
        }
        return AcquisitionRunResult(
            plan_hash=plan.plan_hash,
            status=status,
            partitions=partitions,
            skipped_completed=skipped,
            manifest_hash=stable_content_hash(manifest),
            source_label=self._source_label,
            official_tushare=False,
            api_calls_used=calls_used,
        )


def _normalize_response(
    value: QueryResponse | list[Mapping[str, Any]],
) -> QueryResponse:
    if isinstance(value, QueryResponse):
        return value
    if not isinstance(value, list):
        raise TypeError("transport response must be QueryResponse or list")
    if any(not isinstance(row, Mapping) for row in value):
        raise TypeError("transport rows must be mappings")
    return QueryResponse(rows=tuple(dict(row) for row in value))


def _validate_response_fields(
    response: QueryResponse,
    requested_fields: tuple[str, ...],
) -> None:
    requested = set(requested_fields)
    for row in response.rows:
        actual = {str(key) for key in row}
        if not actual.issubset(requested):
            raise ValueError("transport returned fields outside the request")
        if actual != requested:
            raise ValueError("transport omitted requested fields")


def _provider_failure_code(exc: Exception) -> str:
    """Map exceptions to a finite public vocabulary without serializing them."""

    if isinstance(exc, TimeoutError):
        return "PROVIDER_TIMEOUT"
    return "PROVIDER_QUERY_FAILED"


__all__ = ["ApprovedProxyTransport", "DynamicAcquirer", "PartitionRowSink"]
