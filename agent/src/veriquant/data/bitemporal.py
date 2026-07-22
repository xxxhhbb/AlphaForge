"""Deterministic bitemporal dataset registry and point-in-time snapshots."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, time
from enum import Enum
from types import MappingProxyType
from typing import Any, Iterable, Mapping

from ..contracts import (
    CanonicalContract,
    Component,
    ContractValidationError,
    DatasetCard,
    FailureCode,
    PointInTimeRecord,
)
from ..contracts.base import canonicalize, require_aware_datetime, require_hash, require_identifier


class FieldRole(str, Enum):
    FEATURE = "FEATURE"
    LABEL = "LABEL"


def _raise(code: FailureCode, message: str, **details: Any) -> None:
    raise ContractValidationError(code, message, details=details)


def canonical_content_hash(value: Any) -> str:
    encoded = json.dumps(
        canonicalize(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, kw_only=True)
class UniverseMembership(CanonicalContract):
    dataset_id: str
    symbol: str
    member_from: datetime
    member_until: datetime | None
    published_at: datetime
    available_at: datetime

    def __post_init__(self) -> None:
        require_identifier(self.dataset_id, "dataset_id")
        require_identifier(self.symbol, "symbol")
        require_aware_datetime(self.member_from, "member_from")
        require_aware_datetime(self.published_at, "published_at")
        require_aware_datetime(self.available_at, "available_at")
        if self.member_until is not None:
            require_aware_datetime(self.member_until, "member_until")
            if self.member_until <= self.member_from:
                _raise(FailureCode.INVALID_TIME_ORDER, "member_until must follow member_from")
        if self.published_at > self.available_at:
            _raise(FailureCode.INVALID_TIME_ORDER, "published_at must not follow available_at")

    def is_active(self, event_time: datetime, knowledge_time: datetime) -> bool:
        return (
            self.available_at <= knowledge_time
            and self.member_from <= event_time
            and (self.member_until is None or event_time < self.member_until)
        )


@dataclass(frozen=True, kw_only=True)
class SnapshotRow:
    event_time: datetime
    symbol: str
    values: Mapping[str, str | int | float | bool | None]

    def __post_init__(self) -> None:
        require_aware_datetime(self.event_time, "event_time")
        require_identifier(self.symbol, "symbol")
        object.__setattr__(self, "values", MappingProxyType(dict(self.values)))


@dataclass(frozen=True, kw_only=True)
class LabelRow:
    event_time: datetime
    symbol: str
    value: float | None
    available_at: datetime

    def __post_init__(self) -> None:
        require_aware_datetime(self.event_time, "event_time")
        require_aware_datetime(self.available_at, "available_at")
        require_identifier(self.symbol, "symbol")


@dataclass(frozen=True, kw_only=True)
class MarketSnapshot(CanonicalContract):
    dataset_id: str
    decision_time: datetime
    start_time: datetime
    fields: tuple[str, ...]
    rows: tuple[SnapshotRow, ...]
    historical_universe: Mapping[str, tuple[str, ...]]
    coverage_by_field: Mapping[str, float]

    def __post_init__(self) -> None:
        require_identifier(self.dataset_id, "dataset_id")
        require_aware_datetime(self.decision_time, "decision_time")
        require_aware_datetime(self.start_time, "start_time")
        if self.start_time > self.decision_time:
            _raise(FailureCode.INVALID_TIME_ORDER, "snapshot start_time follows decision_time")
        if not self.fields or any(not isinstance(item, str) or not item for item in self.fields):
            _raise(FailureCode.INVALID_VALUE, "snapshot fields must be non-empty strings")
        if not isinstance(self.rows, tuple):
            _raise(FailureCode.INVALID_VALUE, "snapshot rows must be a tuple")
        for row in self.rows:
            if not isinstance(row, SnapshotRow):
                _raise(FailureCode.INVALID_VALUE, "snapshot rows must contain SnapshotRow values")
            if row.event_time > self.decision_time:
                _raise(FailureCode.INVALID_TIME_ORDER, "snapshot contains a future event")
            if tuple(row.values) != self.fields:
                _raise(FailureCode.INVALID_VALUE, "snapshot row fields do not match snapshot schema")
        universe = {
            key: tuple(value) for key, value in sorted(self.historical_universe.items())
        }
        coverage = {key: float(value) for key, value in sorted(self.coverage_by_field.items())}
        object.__setattr__(self, "historical_universe", MappingProxyType(universe))
        object.__setattr__(self, "coverage_by_field", MappingProxyType(coverage))


@dataclass(frozen=True, kw_only=True)
class FrozenDataset:
    card: DatasetCard
    records: tuple[PointInTimeRecord, ...]
    memberships: tuple[UniverseMembership, ...]
    field_roles: Mapping[str, FieldRole]
    calendar: tuple[datetime, ...]
    seed: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "field_roles", MappingProxyType(dict(self.field_roles)))


class DatasetRegistry:
    """In-memory, offline registry of frozen content-addressed datasets."""

    def __init__(self) -> None:
        self._datasets: dict[str, FrozenDataset] = {}

    def register(
        self,
        *,
        dataset_id: str,
        records: Iterable[PointInTimeRecord],
        memberships: Iterable[UniverseMembership],
        field_roles: Mapping[str, FieldRole],
        calendar: Iterable[datetime],
        seed: int,
        source: str = "synthetic-offline",
    ) -> DatasetCard:
        require_identifier(dataset_id, "dataset_id")
        record_tuple = tuple(records)
        membership_tuple = tuple(memberships)
        calendar_tuple = tuple(sorted(calendar))
        if not record_tuple or not membership_tuple or not calendar_tuple:
            _raise(FailureCode.INVALID_VALUE, "dataset records, memberships, and calendar are required")
        if any(record.dataset_id != dataset_id for record in record_tuple):
            _raise(FailureCode.INVALID_VALUE, "record dataset_id mismatch")
        if any(member.dataset_id != dataset_id for member in membership_tuple):
            _raise(FailureCode.INVALID_VALUE, "membership dataset_id mismatch")
        if any(not isinstance(role, FieldRole) for role in field_roles.values()):
            _raise(FailureCode.INVALID_VALUE, "field_roles must contain FieldRole values")
        record_fields = {record.field_name for record in record_tuple}
        if record_fields != set(field_roles):
            _raise(
                FailureCode.INVALID_VALUE,
                "field role catalog must exactly cover record fields",
                records=tuple(sorted(record_fields)),
                catalog=tuple(sorted(field_roles)),
            )
        for timestamp in calendar_tuple:
            require_aware_datetime(timestamp, "calendar")

        sorted_records = tuple(
            sorted(
                record_tuple,
                key=lambda item: (
                    item.entity_id,
                    item.field_name,
                    item.event_time,
                    item.available_at,
                    item.record_id,
                ),
            )
        )
        sorted_memberships = tuple(
            sorted(membership_tuple, key=lambda item: (item.symbol, item.member_from))
        )
        content_hash = canonical_content_hash(
            {
                "dataset_id": dataset_id,
                "records": sorted_records,
                "memberships": sorted_memberships,
                "field_roles": {key: value.value for key, value in field_roles.items()},
                "calendar": calendar_tuple,
                "seed": seed,
            }
        )
        final_decision = _end_of_day(calendar_tuple[-1])
        latest_available = max(
            [record.available_at for record in sorted_records]
            + [membership.available_at for membership in sorted_memberships]
        )
        if latest_available > final_decision:
            _raise(FailureCode.INVALID_TIME_ORDER, "fixture contains data after its freeze time")
        card = DatasetCard(
            dataset_id=dataset_id,
            source=source,
            universe=tuple(sorted({item.symbol for item in sorted_memberships})),
            horizon="daily",
            decision_time=final_decision,
            availability_assumptions=(
                "records are usable only when available_at is not after the row decision cutoff",
                "historical membership is effective only inside its interval and after availability",
                "future_return_5d is label-only and excluded from feature snapshots",
            ),
            fields=tuple(sorted(field_roles)),
            event_time=min(record.event_time for record in sorted_records),
            published_at=max(record.published_at for record in sorted_records),
            available_at=latest_available,
            snapshot_hash=content_hash,
            record_count=len(sorted_records),
            produced_by=Component.DATA_SERVICE,
        )
        existing = self._datasets.get(dataset_id)
        if existing is not None:
            if existing.card.snapshot_hash != content_hash:
                _raise(FailureCode.INVALID_VALUE, "dataset_id already owns different content")
            return existing.card
        self._datasets[dataset_id] = FrozenDataset(
            card=card,
            records=sorted_records,
            memberships=sorted_memberships,
            field_roles=field_roles,
            calendar=calendar_tuple,
            seed=seed,
        )
        return card

    def get(self, dataset_id: str) -> FrozenDataset:
        try:
            return self._datasets[dataset_id]
        except KeyError as exc:
            raise ContractValidationError(
                FailureCode.INVALID_IDENTIFIER,
                f"unknown dataset_id: {dataset_id}",
                details={"dataset_id": dataset_id},
            ) from exc

    def card(self, dataset_id: str) -> DatasetCard:
        return self.get(dataset_id).card

    def universe_snapshot(self, dataset_id: str, decision_time: datetime) -> tuple[str, ...]:
        require_aware_datetime(decision_time, "decision_time")
        dataset = self.get(dataset_id)
        if decision_time > dataset.card.decision_time:
            _raise(FailureCode.INVALID_TIME_ORDER, "decision_time exceeds frozen dataset")
        return tuple(
            sorted(
                membership.symbol
                for membership in dataset.memberships
                if membership.is_active(decision_time, decision_time)
            )
        )

    def as_of_snapshot(
        self,
        dataset_id: str,
        *,
        decision_time: datetime,
        fields: Iterable[str],
        start_time: datetime | None = None,
    ) -> MarketSnapshot:
        require_aware_datetime(decision_time, "decision_time")
        dataset = self.get(dataset_id)
        if decision_time > dataset.card.decision_time:
            _raise(FailureCode.INVALID_TIME_ORDER, "decision_time exceeds frozen dataset")
        requested_fields = tuple(fields)
        if not requested_fields or len(set(requested_fields)) != len(requested_fields):
            _raise(FailureCode.INVALID_VALUE, "feature fields must be unique and non-empty")
        for field_name in requested_fields:
            role = dataset.field_roles.get(field_name)
            if role is None:
                _raise(FailureCode.UNKNOWN_FIELD, f"unknown field: {field_name}", field=field_name)
            if role is FieldRole.LABEL:
                _raise(
                    FailureCode.UNAUTHORIZED_CAPABILITY,
                    f"label field cannot enter a feature snapshot: {field_name}",
                    field=field_name,
                )
        first_time = start_time or dataset.calendar[0]
        require_aware_datetime(first_time, "start_time")
        if first_time > decision_time:
            _raise(FailureCode.INVALID_TIME_ORDER, "start_time follows decision_time")

        record_index: dict[tuple[str, str], list[PointInTimeRecord]] = {}
        for record in dataset.records:
            if record.field_name in requested_fields:
                record_index.setdefault((record.entity_id, record.field_name), []).append(record)

        rows: list[SnapshotRow] = []
        universe_history: dict[str, tuple[str, ...]] = {}
        non_missing = {field_name: 0 for field_name in requested_fields}
        expected = 0
        for event_time in dataset.calendar:
            if event_time < first_time or event_time > decision_time:
                continue
            row_cutoff = min(_end_of_day(event_time), decision_time)
            members = tuple(
                sorted(
                    membership.symbol
                    for membership in dataset.memberships
                    if membership.is_active(event_time, row_cutoff)
                )
            )
            universe_history[event_time.isoformat()] = members
            for symbol in members:
                values: dict[str, str | int | float | bool | None] = {}
                for field_name in requested_fields:
                    eligible = [
                        record
                        for record in record_index.get((symbol, field_name), ())
                        if record.event_time <= event_time and record.available_at <= row_cutoff
                    ]
                    selected = max(
                        eligible,
                        key=lambda item: (
                            item.event_time,
                            item.available_at,
                            item.published_at,
                            item.record_id,
                        ),
                        default=None,
                    )
                    value = None if selected is None else selected.value
                    values[field_name] = value
                    if value is not None:
                        non_missing[field_name] += 1
                expected += 1
                rows.append(SnapshotRow(event_time=event_time, symbol=symbol, values=values))
        coverage = {
            field_name: (non_missing[field_name] / expected if expected else 0.0)
            for field_name in requested_fields
        }
        return MarketSnapshot(
            dataset_id=dataset_id,
            decision_time=decision_time,
            start_time=first_time,
            fields=requested_fields,
            rows=tuple(rows),
            historical_universe=universe_history,
            coverage_by_field=coverage,
        )

    def labels(
        self,
        dataset_id: str,
        *,
        label_field: str,
        evaluation_time: datetime,
    ) -> tuple[LabelRow, ...]:
        require_aware_datetime(evaluation_time, "evaluation_time")
        dataset = self.get(dataset_id)
        if dataset.field_roles.get(label_field) is not FieldRole.LABEL:
            _raise(FailureCode.INVALID_VALUE, f"field is not registered as a label: {label_field}")
        if evaluation_time > dataset.card.decision_time:
            _raise(FailureCode.INVALID_TIME_ORDER, "evaluation_time exceeds frozen dataset")
        output: list[LabelRow] = []
        for record in dataset.records:
            if record.field_name != label_field or record.available_at > evaluation_time:
                continue
            if not any(
                membership.symbol == record.entity_id
                and membership.is_active(record.event_time, record.available_at)
                for membership in dataset.memberships
            ):
                continue
            value = None if record.value is None else float(record.value)
            output.append(
                LabelRow(
                    event_time=record.event_time,
                    symbol=record.entity_id,
                    value=value,
                    available_at=record.available_at,
                )
            )
        return tuple(sorted(output, key=lambda item: (item.event_time, item.symbol)))


def _end_of_day(value: datetime) -> datetime:
    require_aware_datetime(value, "value")
    return datetime.combine(value.date(), time.max, tzinfo=value.tzinfo)

