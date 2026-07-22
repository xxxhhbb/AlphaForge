"""Build inert, numeric, FactorDAG-compatible point-in-time field views."""

from __future__ import annotations

from datetime import datetime
from typing import Iterable

from .models import BitemporalError, FactorFeatureView, FeatureRow, SourceKind
from .store import BitemporalStore


def build_feature_view(
    store: BitemporalStore,
    *,
    anchors: Iterable[tuple[str, datetime]],
    decision_time: datetime,
    fields: Iterable[str] | None = None,
    sources: Iterable[SourceKind | str] | None = None,
) -> FactorFeatureView:
    """As-of join source vintages onto ``(entity_id, event_time)`` anchors.

    Global records (currently SHIBOR's ``MARKET:CNY`` entity) are joined onto
    every equity/index anchor. Values remain inert numbers; no FactorDAG is
    parsed, compiled, or executed here.
    """

    anchor_tuple = tuple(sorted(set(anchors), key=lambda item: (item[1], item[0])))
    if not anchor_tuple:
        raise BitemporalError("at least one feature anchor is required")
    requested_sources = (
        tuple(SourceKind(item) for item in sources)
        if sources is not None
        else tuple(SourceKind)
    )

    per_anchor: list[tuple[str, datetime, dict[str, int | float | None], dict[str, str]]] = []
    discovered: set[str] = set()
    for entity_id, event_time in anchor_tuple:
        if event_time.tzinfo is None or event_time.utcoffset() is None:
            raise BitemporalError("anchor event_time must be timezone-aware")
        values: dict[str, int | float | None] = {}
        revisions: dict[str, str] = {}
        for source in requested_sources:
            lookup_entity = "MARKET:CNY" if source is SourceKind.SHIBOR else entity_id
            record = store.latest_as_of(
                source=source,
                entity_id=lookup_entity,
                event_time=event_time,
                decision_time=decision_time,
            )
            if record is None:
                continue
            for field_name, value in record.values.items():
                # Normalizers expose numeric-only values. This guard is a second,
                # fail-closed boundary for future source adapters.
                if value is not None and (
                    isinstance(value, bool) or not isinstance(value, (int, float))
                ):
                    raise BitemporalError(
                        f"non-numeric field cannot enter FactorDAG view: {field_name}"
                    )
                if field_name in values and values[field_name] != value:
                    raise BitemporalError(
                        f"ambiguous feature collision: {field_name}"
                    )
                values[field_name] = value
                revisions[field_name] = record.revision_id
                discovered.add(field_name)
        per_anchor.append((entity_id, event_time, values, revisions))

    field_tuple = tuple(fields) if fields is not None else tuple(sorted(discovered))
    if not field_tuple or len(set(field_tuple)) != len(field_tuple):
        raise BitemporalError("feature fields must be unique and non-empty")
    rows = tuple(
        FeatureRow(
            entity_id=entity_id,
            event_time=event_time,
            decision_time=decision_time,
            values={field_name: values.get(field_name) for field_name in field_tuple},
            revision_ids={
                field_name: revisions[field_name]
                for field_name in field_tuple
                if field_name in revisions
            },
        )
        for entity_id, event_time, values, revisions in per_anchor
    )
    return FactorFeatureView(
        decision_time=decision_time,
        fields=field_tuple,
        rows=rows,
    )


def as_of_join(
    store: BitemporalStore,
    *,
    entity_id: str,
    event_time: datetime,
    decision_time: datetime,
    fields: Iterable[str] | None = None,
    sources: Iterable[SourceKind | str] | None = None,
) -> FeatureRow:
    """Convenience form of :func:`build_feature_view` for one anchor."""

    return build_feature_view(
        store,
        anchors=((entity_id, event_time),),
        decision_time=decision_time,
        fields=fields,
        sources=sources,
    ).rows[0]


def build_historical_feature_rows(
    store: BitemporalStore,
    *,
    anchors: Iterable[tuple[str, datetime, datetime]],
    fields: Iterable[str] | None = None,
    sources: Iterable[SourceKind | str] | None = None,
) -> tuple[FeatureRow, ...]:
    """Build a PIT row using each anchor's own historical decision time.

    This is the safe form for walk-forward datasets. A final snapshot time must
    never be reused for earlier anchors because it would expose later
    revisions of earlier economic periods.
    """

    anchor_tuple = tuple(
        sorted(
            set(anchors),
            key=lambda item: (item[1], item[0], item[2]),
        )
    )
    if not anchor_tuple:
        raise BitemporalError("at least one historical feature anchor is required")
    rows = []
    for entity_id, event_time, decision_time in anchor_tuple:
        rows.append(
            as_of_join(
                store,
                entity_id=entity_id,
                event_time=event_time,
                decision_time=decision_time,
                fields=fields,
                sources=sources,
            )
        )
    return tuple(rows)
