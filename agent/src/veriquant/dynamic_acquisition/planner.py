"""Deterministic date/symbol partition planning with bounded plan metadata."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Iterator

from .models import AcquisitionScope, PlanEstimate, QueryTask, ResourceBudget, stable_content_hash
from .registry import CapabilityRegistry, DEFAULT_REGISTRY, PaginationStrategy


@dataclass(frozen=True, slots=True)
class AcquisitionPlan:
    scope: AcquisitionScope
    date_chunk_days: int
    symbol_chunk_size: int
    estimate: PlanEstimate
    plan_hash: str
    registry_version: str

    def iter_tasks(
        self, registry: CapabilityRegistry = DEFAULT_REGISTRY
    ) -> Iterator[QueryTask]:
        capability = registry.get(self.scope.api_name)
        for offset, limit in _offset_pages(
            self.scope.offset_page_count,
            self.scope.offset_page_size,
            capability.pagination,
        ):
            for date_start, date_end in _date_chunks(
                self.scope.start_date,
                self.scope.end_date,
                self.date_chunk_days,
                capability.pagination,
            ):
                # Recreate the lazy symbol iterator for every date chunk.
                for symbol_shard in _symbol_chunks(
                    self.scope.symbols,
                    self.symbol_chunk_size,
                    capability.pagination,
                ):
                    params: dict[str, Any] = dict(self.scope.fixed_params)
                    if offset is not None:
                        params["offset"] = offset
                        params["limit"] = limit
                    if date_start is not None:
                        params["start_date"] = date_start
                        params["end_date"] = date_end
                    if symbol_shard:
                        # Approved-proxy batch symbol parameter.
                        params["ts_code"] = ",".join(symbol_shard)
                    capability.validate(params=params, fields=self.scope.fields)
                    identity = {
                        "registry_version": self.registry_version,
                        "api_name": self.scope.api_name,
                        "fields": self.scope.fields,
                        "params": params,
                        "symbols": symbol_shard,
                    }
                    yield QueryTask(
                        task_id=stable_content_hash(identity),
                        api_name=self.scope.api_name,
                        fields=self.scope.fields,
                        params=params,
                        date_start=date_start,
                        date_end=date_end,
                        symbol_shard=symbol_shard,
                    )


def build_plan(
    scope: AcquisitionScope,
    *,
    budget: ResourceBudget | None = None,
    registry: CapabilityRegistry = DEFAULT_REGISTRY,
    date_chunk_days: int = 120,
    symbol_chunk_size: int = 200,
) -> AcquisitionPlan:
    if isinstance(date_chunk_days, bool) or date_chunk_days <= 0:
        raise ValueError("date_chunk_days must be positive")
    if isinstance(symbol_chunk_size, bool) or symbol_chunk_size <= 0:
        raise ValueError("symbol_chunk_size must be positive")
    capability = registry.get(scope.api_name)
    symbols = tuple(sorted(set(scope.symbols)))
    normalized = AcquisitionScope(
        api_name=scope.api_name,
        fields=tuple(scope.fields),
        start_date=_normalize_date(scope.start_date),
        end_date=_normalize_date(scope.end_date),
        symbols=symbols,
        fixed_params=dict(sorted(scope.fixed_params.items())),
        offset_page_count=scope.offset_page_count,
        offset_page_size=scope.offset_page_size,
    )
    if (
        isinstance(normalized.offset_page_count, bool)
        or normalized.offset_page_count <= 0
        or isinstance(normalized.offset_page_size, bool)
        or normalized.offset_page_size <= 0
    ):
        raise ValueError("offset page count and size must be positive integers")
    _validate_range(
        normalized.start_date,
        normalized.end_date,
        capability.pagination,
        normalized.fixed_params,
    )
    date_count = _date_chunk_count(
        normalized.start_date,
        normalized.end_date,
        date_chunk_days,
        capability.pagination,
    )
    symbol_count = _symbol_chunk_count(
        len(symbols), symbol_chunk_size, capability.pagination
    )
    offset_count = (
        normalized.offset_page_count
        if capability.pagination is PaginationStrategy.OFFSET
        else 1
    )
    partition_count = date_count * symbol_count * offset_count
    warnings = []
    if budget and budget.max_api_calls is not None and partition_count > budget.max_api_calls:
        warnings.append("ESTIMATED_API_CALLS_EXCEED_BUDGET")
    # Importantly, no warning causes scope truncation or plan mutation.
    estimate = PlanEstimate(
        partition_count=partition_count,
        symbol_count=len(symbols),
        date_chunk_count=date_count,
        estimated_api_calls=partition_count,
        budget_warnings=tuple(warnings),
    )
    payload = {
        "registry_version": registry.version,
        "scope": {
            "api_name": normalized.api_name,
            "fields": normalized.fields,
            "start_date": normalized.start_date,
            "end_date": normalized.end_date,
            "symbols": normalized.symbols,
            "fixed_params": normalized.fixed_params,
            "offset_page_count": normalized.offset_page_count,
            "offset_page_size": normalized.offset_page_size,
        },
        "date_chunk_days": date_chunk_days,
        "symbol_chunk_size": symbol_chunk_size,
        "partition_count": partition_count,
    }
    return AcquisitionPlan(
        scope=normalized,
        date_chunk_days=date_chunk_days,
        symbol_chunk_size=symbol_chunk_size,
        estimate=estimate,
        plan_hash=stable_content_hash(payload),
        registry_version=registry.version,
    )


def _normalize_date(value: str | None) -> str | None:
    if value is None:
        return None
    parsed = datetime.strptime(value, "%Y%m%d").date()
    return parsed.strftime("%Y%m%d")


def _validate_range(
    start: str | None,
    end: str | None,
    pagination: PaginationStrategy,
    fixed_params: dict[str, Any] | None = None,
) -> None:
    if pagination in {PaginationStrategy.DATE, PaginationStrategy.SYMBOL_DATE}:
        exact_date = any(
            key in (fixed_params or {})
            for key in ("trade_date", "ann_date", "date", "period")
        )
        if (start is None or end is None) and not exact_date:
            raise ValueError("date-paginated capability requires start_date and end_date")
    if (start is None) != (end is None):
        raise ValueError("start_date and end_date must be provided together")
    if start is not None and start > end:
        raise ValueError("start_date must not follow end_date")


def _date_chunk_count(
    start: str | None,
    end: str | None,
    days: int,
    pagination: PaginationStrategy,
) -> int:
    if pagination not in {PaginationStrategy.DATE, PaginationStrategy.SYMBOL_DATE}:
        return 1
    if start is None and end is None:
        return 1
    left = datetime.strptime(start or "", "%Y%m%d").date()
    right = datetime.strptime(end or "", "%Y%m%d").date()
    return ((right - left).days // days) + 1


def _symbol_chunk_count(
    count: int, chunk_size: int, pagination: PaginationStrategy
) -> int:
    if pagination is not PaginationStrategy.SYMBOL_DATE or count == 0:
        return 1
    return ((count - 1) // chunk_size) + 1


def _date_chunks(
    start: str | None,
    end: str | None,
    days: int,
    pagination: PaginationStrategy,
) -> Iterator[tuple[str | None, str | None]]:
    if pagination not in {PaginationStrategy.DATE, PaginationStrategy.SYMBOL_DATE}:
        yield None, None
        return
    if start is None and end is None:
        yield None, None
        return
    cursor = datetime.strptime(start or "", "%Y%m%d").date()
    stop = datetime.strptime(end or "", "%Y%m%d").date()
    while cursor <= stop:
        chunk_end = min(stop, cursor + timedelta(days=days - 1))
        yield cursor.strftime("%Y%m%d"), chunk_end.strftime("%Y%m%d")
        cursor = chunk_end + timedelta(days=1)


def _symbol_chunks(
    symbols: tuple[str, ...],
    size: int,
    pagination: PaginationStrategy,
) -> Iterator[tuple[str, ...]]:
    if pagination is not PaginationStrategy.SYMBOL_DATE or not symbols:
        yield ()
        return
    for index in range(0, len(symbols), size):
        yield symbols[index:index + size]


def _offset_pages(
    count: int,
    size: int,
    pagination: PaginationStrategy,
) -> Iterator[tuple[int | None, int | None]]:
    if pagination is not PaginationStrategy.OFFSET:
        yield None, None
        return
    for page in range(count):
        yield page * size, size


__all__ = ["AcquisitionPlan", "build_plan"]
