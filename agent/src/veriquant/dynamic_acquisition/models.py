"""Typed contracts used by the deterministic acquisition kernel."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import hashlib
import json
import math
from typing import Any, Mapping


def stable_content_hash(value: Any) -> str:
    """Hash canonical JSON without accepting non-finite or opaque values."""

    encoded = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
        default=_json_default,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _json_default(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, tuple):
        return list(value)
    raise TypeError(f"value is not canonical JSON: {type(value).__name__}")


@dataclass(frozen=True, slots=True)
class ResourceBudget:
    """Operational governance, never an instruction to shrink research scope."""

    max_api_calls: int | None = None
    max_rows: int | None = None
    max_storage_bytes: int | None = None
    max_parallel_requests: int = 4

    def __post_init__(self) -> None:
        for name in ("max_rows", "max_storage_bytes"):
            value = getattr(self, name)
            if value is not None and (isinstance(value, bool) or value <= 0):
                raise ValueError(f"{name} must be a positive integer or None")
        if self.max_api_calls is not None and (
            isinstance(self.max_api_calls, bool) or self.max_api_calls < 0
        ):
            raise ValueError("max_api_calls must be a non-negative integer or None")
        if (
            isinstance(self.max_parallel_requests, bool)
            or self.max_parallel_requests <= 0
            or self.max_parallel_requests > 64
        ):
            raise ValueError("max_parallel_requests must be an integer from 1 to 64")


@dataclass(frozen=True, slots=True)
class AcquisitionScope:
    api_name: str
    fields: tuple[str, ...]
    start_date: str | None = None
    end_date: str | None = None
    symbols: tuple[str, ...] = ()
    fixed_params: Mapping[str, Any] = field(default_factory=dict)
    offset_page_count: int = 1
    offset_page_size: int = 5000


@dataclass(frozen=True, slots=True)
class QueryTask:
    task_id: str
    api_name: str
    fields: tuple[str, ...]
    params: Mapping[str, Any]
    date_start: str | None
    date_end: str | None
    symbol_shard: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PlanEstimate:
    partition_count: int
    symbol_count: int
    date_chunk_count: int
    estimated_api_calls: int
    budget_warnings: tuple[str, ...]


class PartitionStatus(str, Enum):
    SUCCESS = "SUCCESS"
    EMPTY = "EMPTY"
    TRUNCATED = "TRUNCATED"
    FAILED = "FAILED"
    NETWORK_DISABLED = "NETWORK_DISABLED"
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"


@dataclass(frozen=True, slots=True)
class QueryResponse:
    rows: tuple[Mapping[str, Any], ...]
    truncated: bool = False


@dataclass(frozen=True, slots=True)
class PartitionResult:
    task_id: str
    status: PartitionStatus
    row_count: int
    content_hash: str | None
    failure_type: str | None = None
    content_bytes: int = 0
    api_calls_used: int = 0

    def __post_init__(self) -> None:
        if self.row_count < 0:
            raise ValueError("row_count cannot be negative")
        if self.content_bytes < 0:
            raise ValueError("content_bytes cannot be negative")
        if isinstance(self.api_calls_used, bool) or self.api_calls_used < 0:
            raise ValueError("api_calls_used must be a non-negative integer")
        if self.status in {PartitionStatus.SUCCESS, PartitionStatus.EMPTY}:
            if self.content_hash is None or self.failure_type is not None:
                raise ValueError("completed partitions require only a content hash")
        elif self.content_hash is not None:
            raise ValueError("incomplete partitions cannot carry a content hash")


@dataclass(frozen=True, slots=True)
class AcquisitionRunResult:
    plan_hash: str
    status: PartitionStatus
    partitions: tuple[PartitionResult, ...]
    skipped_completed: int
    manifest_hash: str
    source_label: str
    official_tushare: bool = False
    api_calls_used: int = 0

    def __post_init__(self) -> None:
        if isinstance(self.api_calls_used, bool) or self.api_calls_used < 0:
            raise ValueError("api_calls_used must be a non-negative integer")


def canonical_rows_hash(rows: tuple[Mapping[str, Any], ...]) -> str:
    return stable_content_hash(_canonical_rows(rows))


def canonical_rows_size(rows: tuple[Mapping[str, Any], ...]) -> int:
    return len(
        json.dumps(
            _canonical_rows(rows),
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )


def _canonical_rows(rows: tuple[Mapping[str, Any], ...]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for row in rows:
        clean = {}
        for key, value in row.items():
            if isinstance(value, float) and not math.isfinite(value):
                raise ValueError("non-finite response value")
            clean[str(key)] = value
        normalized.append(clean)
    # Response ordering is not a data semantic. Sorting canonical row
    # encodings makes retries stable when an API changes only row order.
    normalized.sort(
        key=lambda row: json.dumps(
            row,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return normalized


__all__ = [
    "AcquisitionRunResult",
    "AcquisitionScope",
    "PartitionResult",
    "PartitionStatus",
    "PlanEstimate",
    "QueryResponse",
    "QueryTask",
    "ResourceBudget",
    "canonical_rows_hash",
    "canonical_rows_size",
    "stable_content_hash",
]
