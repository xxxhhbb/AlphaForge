from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from veriquant.contracts import Component, DatasetCard, FailureCode, PointInTimeRecord
from veriquant.data import (
    FieldRole,
    MarketSnapshot,
    SnapshotRow,
    UniverseMembership,
    canonical_content_hash,
)
from veriquant.frozen_qlib_provider import (
    FrozenQlibProvider,
    FrozenQlibProviderError,
    QlibDataRequirement,
    StructuredPITFixture,
    validate_staging_path,
)


UTC = timezone.utc


def _day(index: int, hour: int = 0) -> datetime:
    return datetime(2024, 1, index, hour, tzinfo=UTC)


def _requirement(
    fields: tuple[str, ...] = ("close",),
    *,
    lookback: int = 1,
    warmup: int = 2,
) -> QlibDataRequirement:
    return QlibDataRequirement(
        required_fields=fields,
        lookback_bars=lookback,
        warmup_bars=warmup,
    )


def _card(*, available_at: datetime | None = None, decision_time: datetime | None = None) -> DatasetCard:
    decision = decision_time or _day(3, 23)
    available = available_at or _day(3, 16)
    return DatasetCard(
        dataset_id="pit.market",
        source="offline-fixture",
        universe=("AAA", "BBB"),
        horizon="daily",
        decision_time=decision,
        availability_assumptions=(
            "records require available_at <= historical row cutoff",
            "historical membership is resolved at each event session",
        ),
        fields=("close", "volume"),
        event_time=_day(1),
        published_at=_day(3, 12),
        available_at=available,
        snapshot_hash=canonical_content_hash({"fixture": "upstream"}),
        record_count=6,
        produced_by=Component.DATA_SERVICE,
    )


def _snapshot() -> MarketSnapshot:
    rows = tuple(
        SnapshotRow(
            event_time=session,
            symbol=symbol,
            values={"close": close, "volume": volume},
        )
        for session, close, volume in (
            (_day(1), 10.0, 100.0),
            (_day(2), 11.0, 110.0),
            (_day(3), 12.0, 120.0),
        )
        for symbol in ("AAA", "BBB")
    )
    return MarketSnapshot(
        dataset_id="pit.market",
        decision_time=_day(3, 23),
        start_time=_day(1),
        fields=("close", "volume"),
        rows=rows,
        historical_universe={
            _day(1).isoformat(): ("AAA", "BBB"),
            _day(2).isoformat(): ("AAA", "BBB"),
            _day(3).isoformat(): ("AAA", "BBB"),
        },
        coverage_by_field={"close": 1.0, "volume": 1.0},
    )


def _record(
    record_id: str,
    *,
    symbol: str,
    field: str,
    value: float,
    event_time: datetime,
    published_at: datetime,
    available_at: datetime,
) -> PointInTimeRecord:
    return PointInTimeRecord(
        record_id=record_id,
        dataset_id="pit.revisions",
        entity_id=symbol,
        field_name=field,
        value=value,
        event_time=event_time,
        published_at=published_at,
        available_at=available_at,
        source="offline-fixture",
        produced_by=Component.DATA_SERVICE,
    )


def _membership(
    symbol: str,
    *,
    member_from: datetime,
    available_at: datetime,
) -> UniverseMembership:
    return UniverseMembership(
        dataset_id="pit.revisions",
        symbol=symbol,
        member_from=member_from,
        member_until=None,
        published_at=available_at,
        available_at=available_at,
    )


def _structured_fixture(*, include_future: bool = False) -> StructuredPITFixture:
    records = [
        _record(
            "aaa.original",
            symbol="AAA",
            field="close",
            value=10.0,
            event_time=_day(1),
            published_at=_day(1, 8),
            available_at=_day(1, 9),
        ),
        _record(
            "aaa.revision",
            symbol="AAA",
            field="close",
            value=11.0,
            event_time=_day(1),
            published_at=_day(2, 12),
            available_at=_day(3, 8),
        ),
        _record(
            "bbb.original",
            symbol="BBB",
            field="close",
            value=20.0,
            event_time=_day(1),
            published_at=_day(1, 8),
            available_at=_day(1, 9),
        ),
    ]
    memberships = [
        _membership("AAA", member_from=_day(1), available_at=_day(1, 7)),
        # BBB becomes an economic member on day 2 but that fact is not known
        # until day 3; it must not leak into the day-2 universe.
        _membership("BBB", member_from=_day(2), available_at=_day(3, 7)),
    ]
    calendar = [_day(1), _day(2), _day(3)]
    if include_future:
        records.append(
            _record(
                "ccc.future",
                symbol="CCC",
                field="close",
                value=999.0,
                event_time=_day(4),
                published_at=_day(4, 8),
                available_at=_day(4, 9),
            )
        )
        memberships.append(_membership("CCC", member_from=_day(4), available_at=_day(4, 7)))
        calendar.append(_day(4))
    return StructuredPITFixture(
        dataset_id="pit.revisions",
        decision_time=_day(3, 23),
        calendar=tuple(calendar),
        records=tuple(records),
        memberships=tuple(memberships),
        field_roles={"close": FieldRole.FEATURE},
    )


def _values(manifest) -> dict[tuple[int, str], float | None]:
    return {
        (row.session.day, row.symbol): row.values["close"]
        for row in manifest.features.rows
    }


def test_market_snapshot_builds_read_only_content_addressed_manifests() -> None:
    provider = FrozenQlibProvider()
    first = provider.from_market_snapshot(_snapshot(), dataset_card=_card(), requirement=_requirement())
    second = provider.from_market_snapshot(_snapshot(), dataset_card=_card(), requirement=_requirement())

    assert first.manifest_hash == second.manifest_hash
    assert first.calendar_hash == second.calendar_hash
    assert first.universe_hash == second.universe_hash
    assert first.features_hash == second.features_hash
    assert first.read_only is True
    assert first.network_allowed is False
    assert first.qlib_home_write_allowed is False
    assert first.labels_included is False
    assert first.features.fields == ("close",)
    assert first.availability_proof.proof_kind == "DATASET_CARD_SNAPSHOT"
    assert first.to_payload()["manifest_hash"] == first.manifest_hash

    with pytest.raises(TypeError):
        first.features.coverage_by_field["close"] = 0.0
    with pytest.raises(TypeError):
        first.universe.members_by_session[next(iter(first.universe.members_by_session))] = ("AAA",)
    with pytest.raises(FrozenInstanceError):
        first.labels_included = True


def test_requirement_lookback_and_warmup_are_identity_bound_and_enforced() -> None:
    provider = FrozenQlibProvider()
    baseline = provider.from_market_snapshot(_snapshot(), dataset_card=_card(), requirement=_requirement())
    changed = provider.from_market_snapshot(
        _snapshot(),
        dataset_card=_card(),
        requirement=_requirement(lookback=0, warmup=0),
    )
    assert baseline.manifest_hash != changed.manifest_hash

    with pytest.raises(FrozenQlibProviderError) as exc:
        provider.from_market_snapshot(
            _snapshot(),
            dataset_card=_card(),
            requirement=_requirement(lookback=3, warmup=3),
        )
    assert exc.value.code is FailureCode.QLIB_DATA_UNAVAILABLE


def test_dataset_card_without_as_of_proof_fails_closed() -> None:
    provider = FrozenQlibProvider()
    late_card = _card(available_at=_day(4, 16), decision_time=_day(4, 23))
    with pytest.raises(FrozenQlibProviderError) as exc:
        provider.from_market_snapshot(_snapshot(), dataset_card=late_card, requirement=_requirement())
    assert exc.value.code is FailureCode.INVALID_TIME_ORDER


def test_structured_revisions_apply_only_when_available() -> None:
    manifest = FrozenQlibProvider().from_structured_fixture(
        _structured_fixture(),
        requirement=_requirement(),
    )
    values = _values(manifest)

    assert values[(1, "AAA")] == 10.0
    assert values[(2, "AAA")] == 10.0
    assert values[(3, "AAA")] == 11.0
    assert manifest.availability_proof.proof_kind == "ROW_LEVEL_BITEMPORAL"
    day_three = [row for row in manifest.features.rows if row.session.day == 3 and row.symbol == "AAA"][0]
    assert day_three.revision_ids["close"] == "aaa.revision"


def test_future_perturbation_does_not_change_prior_provider_output_or_hash() -> None:
    provider = FrozenQlibProvider()
    baseline = provider.from_structured_fixture(_structured_fixture(), requirement=_requirement())
    perturbed = provider.from_structured_fixture(
        _structured_fixture(include_future=True),
        requirement=_requirement(),
    )

    assert baseline.manifest_hash == perturbed.manifest_hash
    assert baseline.features_hash == perturbed.features_hash
    assert baseline.universe_hash == perturbed.universe_hash
    assert baseline.calendar_hash == perturbed.calendar_hash


def test_membership_availability_prevents_historical_universe_leakage() -> None:
    manifest = FrozenQlibProvider().from_structured_fixture(
        _structured_fixture(),
        requirement=_requirement(),
    )
    universes = list(manifest.universe.members_by_session.values())

    assert universes[0] == ("AAA",)
    assert universes[1] == ("AAA",)
    assert universes[2] == ("AAA", "BBB")
    assert (2, "BBB") not in _values(manifest)
    assert _values(manifest)[(3, "BBB")] == 20.0


def test_market_snapshot_universe_mismatch_is_rejected() -> None:
    snapshot = _snapshot()
    tampered = MarketSnapshot(
        dataset_id=snapshot.dataset_id,
        decision_time=snapshot.decision_time,
        start_time=snapshot.start_time,
        fields=snapshot.fields,
        rows=tuple(row for row in snapshot.rows if not (row.event_time.day == 2 and row.symbol == "BBB")),
        historical_universe=snapshot.historical_universe,
        coverage_by_field=snapshot.coverage_by_field,
    )
    with pytest.raises(FrozenQlibProviderError) as exc:
        FrozenQlibProvider().from_market_snapshot(
            tampered,
            dataset_card=_card(),
            requirement=_requirement(),
        )
    assert exc.value.code is FailureCode.INCOMPLETE_RESULT


def test_label_and_sealed_fields_are_rejected_before_projection() -> None:
    with pytest.raises(FrozenQlibProviderError) as label_exc:
        QlibDataRequirement(
            required_fields=("future_return_5d",),
            lookback_bars=0,
            warmup_bars=0,
        )
    assert label_exc.value.code is FailureCode.UNAUTHORIZED_CAPABILITY

    with pytest.raises(FrozenQlibProviderError) as sealed_exc:
        QlibDataRequirement(
            required_fields=("sealed_alpha",),
            lookback_bars=0,
            warmup_bars=0,
        )
    assert sealed_exc.value.code is FailureCode.UNAUTHORIZED_CAPABILITY

    fixture = _structured_fixture()
    label_record = _record(
        "outcome.label",
        symbol="AAA",
        field="outcome_5d",
        value=0.1,
        event_time=_day(1),
        published_at=_day(1, 8),
        available_at=_day(1, 9),
    )
    with_label = StructuredPITFixture(
        dataset_id=fixture.dataset_id,
        decision_time=fixture.decision_time,
        calendar=fixture.calendar,
        records=fixture.records + (label_record,),
        memberships=fixture.memberships,
        field_roles={"close": FieldRole.FEATURE, "outcome_5d": FieldRole.LABEL},
    )
    with pytest.raises(FrozenQlibProviderError) as role_exc:
        FrozenQlibProvider().from_structured_fixture(
            with_label,
            requirement=_requirement(("outcome_5d",)),
        )
    assert role_exc.value.code is FailureCode.UNAUTHORIZED_CAPABILITY


def test_staging_path_validation_never_allows_escape_or_qlib_home(tmp_path: Path) -> None:
    allowed = tmp_path / "integration-owned"
    safe = validate_staging_path("run-001", allowed_root=allowed)
    assert safe == (allowed / "run-001").resolve()
    assert not safe.exists()

    with pytest.raises(FrozenQlibProviderError) as escape_exc:
        validate_staging_path("..\\escape", allowed_root=allowed)
    assert escape_exc.value.code is FailureCode.UNAUTHORIZED_CAPABILITY

    with pytest.raises(FrozenQlibProviderError) as home_exc:
        validate_staging_path(Path.home() / ".qlib" / "provider", allowed_root=Path.home())
    assert home_exc.value.code is FailureCode.UNAUTHORIZED_CAPABILITY
