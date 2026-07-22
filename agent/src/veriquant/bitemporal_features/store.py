"""Append-only revision store and deterministic as-of selection."""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import replace
from datetime import datetime
from typing import Iterable

from .models import (
    BitemporalError,
    EvidenceEligibility,
    NormalizedRecord,
    SourceKind,
    canonical_record_hash,
)


class BitemporalStore:
    """Small deterministic kernel; storage adapters may persist these records."""

    def __init__(self, records: Iterable[NormalizedRecord] = ()) -> None:
        self._revisions: dict[
            tuple[str, str, datetime, str], NormalizedRecord
        ] = {}
        self._by_source_entity: dict[
            tuple[str, str], dict[tuple[str, str, datetime, str], NormalizedRecord]
        ] = {}
        self._ordered_by_source_entity: dict[
            tuple[str, str], tuple[NormalizedRecord, ...]
        ] = {}
        self._event_times_by_source_entity: dict[
            tuple[str, str], tuple[datetime, ...]
        ] = {}
        self._ordered_records: tuple[NormalizedRecord, ...] | None = None
        self._artifact_hash: str | None = None
        self.append(records)

    def append(self, records: Iterable[NormalizedRecord]) -> None:
        for record in records:
            if not isinstance(record, NormalizedRecord):
                raise BitemporalError("store accepts only NormalizedRecord")
            current = self._revisions.get(record.revision_key)
            if current is not None and current.artifact_hash != record.artifact_hash:
                raise BitemporalError(
                    "revision identity already exists with different content"
                )
            self._revisions[record.revision_key] = record
            bucket_key = (record.source.value, record.entity_id)
            bucket = self._by_source_entity.setdefault(bucket_key, {})
            bucket[record.revision_key] = record
            self._ordered_by_source_entity.pop(bucket_key, None)
            self._event_times_by_source_entity.pop(bucket_key, None)
            self._ordered_records = None
            self._artifact_hash = None

    @property
    def records(self) -> tuple[NormalizedRecord, ...]:
        if self._ordered_records is None:
            self._ordered_records = tuple(
                sorted(
                    self._revisions.values(),
                    key=lambda item: (
                        item.source.value,
                        item.entity_id,
                        item.event_time,
                        item.available_at,
                        item.published_at,
                        item.revision_id,
                    ),
                )
            )
        return self._ordered_records

    @property
    def artifact_hash(self) -> str:
        if self._artifact_hash is None:
            self._artifact_hash = canonical_record_hash(self.records)
        return self._artifact_hash

    def history(
        self,
        *,
        source: SourceKind | str,
        entity_id: str,
        event_time: datetime | None = None,
    ) -> tuple[NormalizedRecord, ...]:
        source_value = SourceKind(source).value
        return tuple(
            item
            for item in self.records
            if item.source.value == source_value
            and item.entity_id == entity_id
            and (event_time is None or item.event_time == event_time)
        )

    def as_of(
        self,
        *,
        decision_time: datetime,
        event_time: datetime | None = None,
        sources: Iterable[SourceKind | str] | None = None,
        entities: Iterable[str] | None = None,
    ) -> tuple[NormalizedRecord, ...]:
        if decision_time.tzinfo is None or decision_time.utcoffset() is None:
            raise BitemporalError("decision_time must be timezone-aware")
        source_set = (
            None if sources is None else {SourceKind(item) for item in sources}
        )
        entity_set = None if entities is None else set(entities)
        eligible = [
            item
            for item in self.records
            if item.eligibility is EvidenceEligibility.FEATURE_ELIGIBLE
            and item.available_at <= decision_time
            and (event_time is None or item.event_time <= event_time)
            and (source_set is None or item.source in source_set)
            and (entity_set is None or item.entity_id in entity_set)
        ]
        selected: dict[tuple[str, str, datetime], NormalizedRecord] = {}
        for item in eligible:
            key = item.fact_key
            previous = selected.get(key)
            if previous is None or _vintage_order(item) > _vintage_order(previous):
                selected[key] = item
        return tuple(
            replace(item, decision_time=decision_time)
            for item in sorted(
                selected.values(),
                key=lambda value: (
                    value.event_time,
                    value.entity_id,
                    value.source.value,
                ),
            )
        )

    def latest_as_of(
        self,
        *,
        source: SourceKind | str,
        entity_id: str,
        event_time: datetime,
        decision_time: datetime,
        bind_decision_time: bool = True,
    ) -> NormalizedRecord | None:
        if decision_time.tzinfo is None or decision_time.utcoffset() is None:
            raise BitemporalError("decision_time must be timezone-aware")
        if type(bind_decision_time) is not bool:
            raise BitemporalError("bind_decision_time must be boolean")
        source_value = SourceKind(source)
        bucket_key = (source_value.value, entity_id)
        ordered, event_times = self._ordered_bucket(bucket_key)
        stop = bisect_right(event_times, event_time)
        # Do not materialize ``ordered[:stop]``.  Projection invokes this path
        # once per market row and source; copying every prefix made a replay
        # quadratic in practice even though the lookup boundary is binary.
        selected = None
        for index in range(stop - 1, -1, -1):
            item = ordered[index]
            if (
                item.eligibility is EvidenceEligibility.FEATURE_ELIGIBLE
                and item.available_at <= decision_time
            ):
                selected = item
                break
        if selected is None:
            return None
        return replace(selected, decision_time=decision_time) if bind_decision_time else selected

    def _ordered_bucket(
        self, bucket_key: tuple[str, str]
    ) -> tuple[tuple[NormalizedRecord, ...], tuple[datetime, ...]]:
        ordered = self._ordered_by_source_entity.get(bucket_key)
        event_times = self._event_times_by_source_entity.get(bucket_key)
        if ordered is None or event_times is None:
            ordered = tuple(
                sorted(
                    self._by_source_entity.get(bucket_key, {}).values(),
                    key=lambda item: (item.event_time, *_vintage_order(item)),
                )
            )
            event_times = tuple(item.event_time for item in ordered)
            self._ordered_by_source_entity[bucket_key] = ordered
            self._event_times_by_source_entity[bucket_key] = event_times
        return ordered, event_times


def _vintage_order(item: NormalizedRecord) -> tuple[datetime, datetime, str, str]:
    return (
        item.available_at,
        item.published_at,
        item.revision_id,
        item.raw_hash,
    )
