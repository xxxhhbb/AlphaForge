"""Deterministic projection of frozen PIT data into inert Qlib manifests."""

from __future__ import annotations

from datetime import datetime, time
from typing import Iterable, Mapping

from ..contracts import DatasetCard, FailureCode, PointInTimeRecord
from ..contracts.base import canonicalize, require_aware_datetime
from ..data import FieldRole, MarketSnapshot, UniverseMembership, canonical_content_hash
from .models import (
    FrozenQlibCalendarManifest,
    FrozenQlibFeatureManifest,
    FrozenQlibProviderManifest,
    FrozenQlibUniverseManifest,
    PITAvailabilityProof,
    QlibDataRequirement,
    QlibFeatureRow,
    StructuredPITFixture,
    provider_fail,
    validate_feature_name,
)


class FrozenQlibProvider:
    """Pure in-memory adapter; Qlib itself is deliberately not imported."""

    def from_market_snapshot(
        self,
        snapshot: MarketSnapshot,
        *,
        dataset_card: DatasetCard,
        requirement: QlibDataRequirement,
    ) -> FrozenQlibProviderManifest:
        if not isinstance(snapshot, MarketSnapshot):
            provider_fail(FailureCode.INVALID_VALUE, "snapshot must be MarketSnapshot")
        if not isinstance(dataset_card, DatasetCard):
            provider_fail(FailureCode.INVALID_VALUE, "dataset_card must be DatasetCard")
        if not isinstance(requirement, QlibDataRequirement):
            provider_fail(FailureCode.INVALID_VALUE, "requirement must be QlibDataRequirement")
        self._validate_card_snapshot_binding(snapshot, dataset_card, requirement)

        sessions, universe = self._snapshot_sessions(snapshot)
        self._validate_session_budget(sessions, requirement)
        rows_by_session: dict[datetime, list] = {session: [] for session in sessions}
        for row in snapshot.rows:
            if row.event_time in rows_by_session:
                rows_by_session[row.event_time].append(row)

        feature_rows: list[QlibFeatureRow] = []
        for session in sessions:
            expected_members = universe[_session_key(session)]
            session_rows = rows_by_session[session]
            if len(session_rows) != len(expected_members):
                provider_fail(
                    FailureCode.INCOMPLETE_RESULT,
                    "MarketSnapshot does not provide one row per historical member",
                    session=_session_key(session),
                )
            by_symbol = {row.symbol: row for row in session_rows}
            if len(by_symbol) != len(session_rows) or set(by_symbol) != set(expected_members):
                provider_fail(
                    FailureCode.INVALID_VALUE,
                    "MarketSnapshot rows do not match historical universe",
                    session=_session_key(session),
                )
            for symbol in expected_members:
                source = by_symbol[symbol]
                values = {
                    field_name: _numeric_value(source.values[field_name], field_name)
                    for field_name in requirement.required_fields
                }
                feature_rows.append(
                    QlibFeatureRow(
                        session=session,
                        symbol=symbol,
                        values=values,
                        revision_ids={},
                    )
                )

        safe_source_hash = canonical_content_hash(
            {
                "dataset_id": dataset_card.dataset_id,
                "source": dataset_card.source,
                "decision_time": snapshot.decision_time,
                "availability_assumptions": dataset_card.availability_assumptions,
                "required_fields": requirement.required_fields,
                "snapshot": snapshot,
            }
        )
        proof = PITAvailabilityProof(
            proof_kind="DATASET_CARD_SNAPSHOT",
            decision_time=snapshot.decision_time,
            maximum_visible_available_at=dataset_card.available_at,
            assumptions=dataset_card.availability_assumptions,
            visible_input_hash=safe_source_hash,
        )
        return self._manifest(
            dataset_id=snapshot.dataset_id,
            source_snapshot_hash=safe_source_hash,
            requirement=requirement,
            sessions=sessions,
            universe=universe,
            feature_rows=tuple(feature_rows),
            proof=proof,
        )

    def from_structured_fixture(
        self,
        fixture: StructuredPITFixture,
        *,
        requirement: QlibDataRequirement,
    ) -> FrozenQlibProviderManifest:
        if not isinstance(fixture, StructuredPITFixture):
            provider_fail(FailureCode.INVALID_VALUE, "fixture must be StructuredPITFixture")
        if not isinstance(requirement, QlibDataRequirement):
            provider_fail(FailureCode.INVALID_VALUE, "requirement must be QlibDataRequirement")
        self._validate_structured_fields(fixture, requirement)
        sessions = tuple(
            session for session in fixture.calendar if session <= fixture.decision_time
        )
        self._validate_session_budget(sessions, requirement)

        visible_records = tuple(
            item
            for item in fixture.records
            if item.available_at <= fixture.decision_time
            and item.event_time <= fixture.decision_time
            and item.field_name in requirement.required_fields
        )
        visible_memberships = tuple(
            item
            for item in fixture.memberships
            if item.available_at <= fixture.decision_time
            and item.member_from <= fixture.decision_time
        )
        if not visible_records or not visible_memberships:
            provider_fail(FailureCode.QLIB_DATA_UNAVAILABLE, "no PIT-visible records or memberships")

        universe: dict[str, tuple[str, ...]] = {}
        feature_rows: list[QlibFeatureRow] = []
        maximum_visible = max(item.available_at for item in visible_records + visible_memberships)
        for session in sessions:
            cutoff = _session_cutoff(session, fixture.decision_time)
            members = tuple(
                sorted(
                    item.symbol
                    for item in visible_memberships
                    if item.is_active(session, cutoff)
                )
            )
            if not members:
                provider_fail(
                    FailureCode.QLIB_DATA_UNAVAILABLE,
                    "historical universe is empty at a required session",
                    session=_session_key(session),
                )
            universe[_session_key(session)] = members
            for symbol in members:
                values: dict[str, int | float | None] = {}
                revisions: dict[str, str] = {}
                for field_name in requirement.required_fields:
                    selected = _select_record(
                        visible_records,
                        symbol=symbol,
                        field_name=field_name,
                        session=session,
                        cutoff=cutoff,
                    )
                    values[field_name] = (
                        None if selected is None else _numeric_value(selected.value, field_name)
                    )
                    revisions[field_name] = "missing" if selected is None else selected.record_id
                feature_rows.append(
                    QlibFeatureRow(
                        session=session,
                        symbol=symbol,
                        values=values,
                        revision_ids=revisions,
                    )
                )

        visible_input_hash = canonical_content_hash(
            {
                "dataset_id": fixture.dataset_id,
                "decision_time": fixture.decision_time,
                "sessions": sessions,
                "visible_records": visible_records,
                "visible_memberships": visible_memberships,
                "required_fields": requirement.required_fields,
            }
        )
        assumptions = (
            "row-level event_time <= session",
            "row-level available_at <= min(session end, decision_time)",
            "latest visible revision selected deterministically",
            "historical membership requires interval activity and available_at cutoff",
        )
        proof = PITAvailabilityProof(
            proof_kind="ROW_LEVEL_BITEMPORAL",
            decision_time=fixture.decision_time,
            maximum_visible_available_at=maximum_visible,
            assumptions=assumptions,
            visible_input_hash=visible_input_hash,
        )
        return self._manifest(
            dataset_id=fixture.dataset_id,
            source_snapshot_hash=visible_input_hash,
            requirement=requirement,
            sessions=sessions,
            universe=universe,
            feature_rows=tuple(feature_rows),
            proof=proof,
        )

    @staticmethod
    def _validate_card_snapshot_binding(
        snapshot: MarketSnapshot,
        card: DatasetCard,
        requirement: QlibDataRequirement,
    ) -> None:
        if snapshot.dataset_id != card.dataset_id:
            provider_fail(FailureCode.INVALID_VALUE, "MarketSnapshot and DatasetCard differ")
        if snapshot.decision_time > card.decision_time:
            provider_fail(FailureCode.INVALID_TIME_ORDER, "snapshot decision_time exceeds DatasetCard")
        if card.available_at > snapshot.decision_time:
            provider_fail(
                FailureCode.INVALID_TIME_ORDER,
                "DatasetCard cannot prove all source data available at snapshot decision_time",
            )
        assumptions = " ".join(card.availability_assumptions).lower()
        if "available_at" not in assumptions or "historical" not in assumptions:
            provider_fail(
                FailureCode.MISSING_REQUIRED_FIELD,
                "DatasetCard lacks available_at and historical-universe proof assumptions",
            )
        if not set(snapshot.fields) <= set(card.fields):
            provider_fail(FailureCode.INVALID_VALUE, "snapshot fields are not bound by DatasetCard")
        if not set(requirement.required_fields) <= set(snapshot.fields):
            missing = tuple(sorted(set(requirement.required_fields) - set(snapshot.fields)))
            provider_fail(FailureCode.UNKNOWN_FIELD, "required fields are absent", fields=missing)
        for field_name in snapshot.fields:
            validate_feature_name(field_name)
        historical_symbols = {
            symbol for symbols in snapshot.historical_universe.values() for symbol in symbols
        }
        if not historical_symbols <= set(card.universe):
            provider_fail(FailureCode.INVALID_VALUE, "historical universe is not bound by DatasetCard")

    @staticmethod
    def _snapshot_sessions(
        snapshot: MarketSnapshot,
    ) -> tuple[tuple[datetime, ...], Mapping[str, tuple[str, ...]]]:
        universe: dict[str, tuple[str, ...]] = {}
        sessions: list[datetime] = []
        for raw_key, members in sorted(snapshot.historical_universe.items()):
            session = _parse_session(raw_key)
            if session < snapshot.start_time or session > snapshot.decision_time:
                provider_fail(FailureCode.INVALID_TIME_ORDER, "historical universe session is out of range")
            key = _session_key(session)
            if key in universe:
                provider_fail(FailureCode.INVALID_VALUE, "duplicate historical universe session")
            if not members:
                provider_fail(FailureCode.QLIB_DATA_UNAVAILABLE, "historical universe session is empty")
            universe[key] = tuple(sorted(members))
            sessions.append(session)
        ordered_sessions = tuple(sorted(sessions))
        ordered_universe = {_session_key(item): universe[_session_key(item)] for item in ordered_sessions}
        if {row.event_time for row in snapshot.rows} != set(ordered_sessions):
            provider_fail(FailureCode.INVALID_VALUE, "snapshot rows and historical universe calendars differ")
        return ordered_sessions, ordered_universe

    @staticmethod
    def _validate_structured_fields(
        fixture: StructuredPITFixture,
        requirement: QlibDataRequirement,
    ) -> None:
        missing = tuple(sorted(set(requirement.required_fields) - set(fixture.field_roles)))
        if missing:
            provider_fail(FailureCode.UNKNOWN_FIELD, "required structured fields are absent", fields=missing)
        for field_name in requirement.required_fields:
            validate_feature_name(field_name)
            if fixture.field_roles[field_name] is not FieldRole.FEATURE:
                provider_fail(
                    FailureCode.UNAUTHORIZED_CAPABILITY,
                    "label field cannot enter Qlib provider",
                    field=field_name,
                )

    @staticmethod
    def _validate_session_budget(
        sessions: tuple[datetime, ...],
        requirement: QlibDataRequirement,
    ) -> None:
        minimum = requirement.warmup_bars + 1
        if len(sessions) < minimum:
            provider_fail(
                FailureCode.QLIB_DATA_UNAVAILABLE,
                "insufficient sessions for required warmup",
                sessions=len(sessions),
                minimum=minimum,
            )

    @staticmethod
    def _manifest(
        *,
        dataset_id: str,
        source_snapshot_hash: str,
        requirement: QlibDataRequirement,
        sessions: tuple[datetime, ...],
        universe: Mapping[str, tuple[str, ...]],
        feature_rows: tuple[QlibFeatureRow, ...],
        proof: PITAvailabilityProof,
    ) -> FrozenQlibProviderManifest:
        non_null = {field_name: 0 for field_name in requirement.required_fields}
        for row in feature_rows:
            for field_name, value in row.values.items():
                if value is not None:
                    non_null[field_name] += 1
        count = len(feature_rows)
        return FrozenQlibProviderManifest(
            dataset_id=dataset_id,
            source_snapshot_hash=source_snapshot_hash,
            requirement=requirement,
            calendar=FrozenQlibCalendarManifest(sessions=sessions),
            universe=FrozenQlibUniverseManifest(members_by_session=universe),
            features=FrozenQlibFeatureManifest(
                fields=requirement.required_fields,
                rows=feature_rows,
                coverage_by_field={
                    field_name: (non_null[field_name] / count if count else 0.0)
                    for field_name in requirement.required_fields
                },
            ),
            availability_proof=proof,
        )


def _parse_session(value: str) -> datetime:
    if not isinstance(value, str) or not value:
        provider_fail(FailureCode.INVALID_TIMESTAMP, "historical universe key must be ISO datetime")
    candidate = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as exc:
        from .models import FrozenQlibProviderError

        raise FrozenQlibProviderError(
            FailureCode.INVALID_TIMESTAMP,
            "historical universe key must be ISO datetime",
            details={"value": value},
        ) from exc
    require_aware_datetime(parsed, "historical universe session")
    return parsed


def _session_key(value: datetime) -> str:
    rendered = canonicalize(value)
    assert isinstance(rendered, str)
    return rendered


def _session_cutoff(session: datetime, decision_time: datetime) -> datetime:
    require_aware_datetime(session, "session")
    require_aware_datetime(decision_time, "decision_time")
    end = datetime.combine(session.date(), time.max, tzinfo=session.tzinfo)
    return min(end, decision_time)


def _select_record(
    records: Iterable[PointInTimeRecord],
    *,
    symbol: str,
    field_name: str,
    session: datetime,
    cutoff: datetime,
) -> PointInTimeRecord | None:
    eligible = (
        item
        for item in records
        if item.entity_id == symbol
        and item.field_name == field_name
        and item.event_time <= session
        and item.available_at <= cutoff
    )
    return max(
        eligible,
        key=lambda item: (item.event_time, item.available_at, item.published_at, item.record_id),
        default=None,
    )


def _numeric_value(value: object, field_name: str) -> int | float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        provider_fail(
            FailureCode.INVALID_VALUE,
            "Qlib feature values must be numeric or null",
            field=field_name,
        )
    return value


__all__ = ["FrozenQlibProvider"]
