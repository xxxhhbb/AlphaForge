from __future__ import annotations

from dataclasses import replace
from datetime import datetime, time, timedelta, timezone

import pytest

from src.veriquant.contracts import ContractValidationError
from src.veriquant.real_ashare_scale import (
    BoundedScaleInputRequest,
    FrozenAshareScaleInput,
    FrozenDataVintage,
    HistoricalUniverseSnapshot,
    InMemoryBoundedScaleInputAdapter,
    InputOrigin,
    RealAshareScalePlanner,
    ScaleInputRow,
    ScaleQualificationFailureCode,
    ScaleQualificationStatus,
    stable_content_hash,
)


UTC = timezone.utc
HASH_A = "sha256:" + "a" * 64


def _sessions(count: int) -> tuple[str, ...]:
    output: list[str] = []
    cursor = datetime(2023, 1, 3, tzinfo=UTC).date()
    while len(output) < count:
        if cursor.weekday() < 5:
            output.append(cursor.isoformat())
        cursor += timedelta(days=1)
    return tuple(output)


def _clock(session: str, hour: int, minute: int = 0) -> datetime:
    day = datetime.fromisoformat(session).date()
    return datetime.combine(day, time(hour, minute), tzinfo=UTC)


def _fixture(*, symbol_count: int = 20, session_count: int = 120) -> FrozenAshareScaleInput:
    sessions = _sessions(session_count)
    base_symbols = tuple(f"{index:06d}.SZ" for index in range(symbol_count))
    membership: list[HistoricalUniverseSnapshot] = []
    rows: list[ScaleInputRow] = []
    for session_index, session in enumerate(sessions):
        symbols = base_symbols
        membership.append(HistoricalUniverseSnapshot(session=session, symbols=tuple(sorted(symbols))))
        next_day = datetime.fromisoformat(session).date() + timedelta(days=1)
        for symbol_index, symbol in enumerate(symbols):
            missing_adv = (session_index + symbol_index) % 20 == 0
            rows.append(
                ScaleInputRow(
                    symbol=symbol,
                    session=session,
                    event_time=_clock(session, 7),
                    available_at=datetime.combine(next_day, time(1), tzinfo=UTC),
                    revision_id=f"row-r1-{session_index:03d}-{symbol_index:02d}",
                    features={
                        "close": 10.0 + symbol_index + session_index / 100,
                        "volume": 100000 + 100 * symbol_index,
                        "industry": f"industry-{symbol_index % 4}",
                        "market_cap": 1_000_000_000 + symbol_index * 1_000_000,
                        "adv": None if missing_adv else 5_000_000 + symbol_index * 10_000,
                        "benchmark_return": session_index / 10000,
                    },
                    labels={"future_return_5d": (symbol_index - symbol_count / 2) / 1000},
                )
            )
    vintages: list[FrozenDataVintage] = []
    for index, session in enumerate(sessions[-20:]):
        next_day = datetime.fromisoformat(session).date() + timedelta(days=1)
        decision = datetime.combine(next_day, time(1, 25), tzinfo=UTC)
        vintage_id = f"fixture-vintage-{index:02d}-{session}"
        vintages.append(
            FrozenDataVintage(
                vintage_id=vintage_id,
                session=session,
                available_at=decision - timedelta(minutes=5),
                decision_time=decision,
                revision_id=f"revision-{index:02d}",
                content_hash=stable_content_hash((session, index)),
                supersedes_vintage_id=None if not vintages else vintages[-1].vintage_id,
            )
        )
    membership_tuple = tuple(membership)
    row_tuple = tuple(rows)
    return FrozenAshareScaleInput(
        dataset_id="ashare-scale-test-fixture",
        provider_class="APPROVED_PROXY_DATA_VENDOR",
        source_class="TUSHARE_COMPATIBLE_PROXY",
        input_origin=InputOrigin.TEST_FIXTURE,
        capture_time=datetime(2026, 7, 19, tzinfo=UTC),
        capture_network_used=False,
        calendar_id="sse-szse-frozen-test",
        calendar_sessions=sessions,
        calendar_hash=stable_content_hash(sessions),
        historical_universe=membership_tuple,
        universe_hash=stable_content_hash(membership_tuple),
        rows=row_tuple,
        data_hash=stable_content_hash(row_tuple),
        feature_fields=("close", "volume", "industry", "market_cap", "adv", "benchmark_return"),
        label_fields=("future_return_5d",),
        availability_policy_hash=HASH_A,
        vintages=tuple(vintages),
        adapter_manifest_hash=HASH_A,
    )


def test_same_frozen_input_has_stable_manifest_plan_and_verdict_hashes() -> None:
    planner = RealAshareScalePlanner()
    first = planner.qualify(_fixture())
    second = planner.qualify(_fixture())

    assert first.manifest is not None
    assert first.vintage_plan is not None
    assert first.manifest.artifact_hash == second.manifest.artifact_hash
    assert first.vintage_plan.artifact_hash == second.vintage_plan.artifact_hash
    assert first.verdict.artifact_hash == second.verdict.artifact_hash
    assert first.verdict.status is ScaleQualificationStatus.IMPLEMENTED_NOT_RUN
    assert ScaleQualificationFailureCode.SYNTHETIC_NOT_REAL in first.verdict.failure_codes


@pytest.mark.parametrize(
    ("symbol_count", "session_count"),
    ((19, 120), (20, 119)),
)
def test_less_than_twenty_symbols_or_120_sessions_is_not_qualified(
    symbol_count: int,
    session_count: int,
) -> None:
    outcome = RealAshareScalePlanner().qualify(
        _fixture(symbol_count=symbol_count, session_count=session_count)
    )

    assert outcome.verdict.eligible is False
    assert ScaleQualificationFailureCode.BELOW_MINIMUM_SCALE in outcome.verdict.failure_codes


def test_calendar_universe_and_data_hash_mismatch_fail_closed() -> None:
    planner = RealAshareScalePlanner()
    frozen = _fixture()

    for field_name in ("calendar_hash", "universe_hash", "data_hash"):
        outcome = planner.qualify(replace(frozen, **{field_name: HASH_A}))
        assert outcome.verdict.status is ScaleQualificationStatus.FAILED
        assert outcome.manifest is None
        assert ScaleQualificationFailureCode.HASH_MISMATCH in outcome.verdict.failure_codes


def test_historical_universe_rejects_future_member_leakage() -> None:
    frozen = _fixture()
    leaked_row = replace(frozen.rows[0], symbol="999999.SZ")
    leaked_rows = (leaked_row,) + frozen.rows[1:]
    leaked = replace(frozen, rows=leaked_rows, data_hash=stable_content_hash(leaked_rows))

    outcome = RealAshareScalePlanner().qualify(leaked)

    assert outcome.verdict.eligible is False
    assert ScaleQualificationFailureCode.FUTURE_MEMBERSHIP_LEAKAGE in outcome.verdict.failure_codes
    assert outcome.manifest is not None
    assert outcome.manifest.universe_coverage.future_membership_leakage is True


def test_revision_appends_new_identity_without_mutating_old_vintage() -> None:
    frozen = _fixture()
    old = frozen.vintages[0]
    old_hash = old.artifact_hash
    revision = FrozenDataVintage(
        vintage_id="fixture-revision-appended",
        session=frozen.vintages[-1].session,
        available_at=frozen.vintages[-1].available_at + timedelta(minutes=1),
        decision_time=frozen.vintages[-1].decision_time + timedelta(minutes=1),
        revision_id="revision-appended",
        content_hash=stable_content_hash((old.content_hash, "revision")),
        supersedes_vintage_id=frozen.vintages[-1].vintage_id,
    )

    revised = replace(frozen, vintages=frozen.vintages + (revision,))
    outcome = RealAshareScalePlanner().qualify(revised)

    assert old.artifact_hash == old_hash
    assert old.content_hash == frozen.vintages[0].content_hash
    assert outcome.verdict.status is ScaleQualificationStatus.FAILED
    assert ScaleQualificationFailureCode.REVISION_HISTORY_INVALID in outcome.verdict.failure_codes


def test_duplicate_vintage_id_cannot_overwrite_prior_content() -> None:
    frozen = _fixture()
    conflicting = replace(
        frozen.vintages[-1],
        content_hash=stable_content_hash("conflicting-rewrite"),
    )
    outcome = RealAshareScalePlanner().qualify(
        replace(frozen, vintages=frozen.vintages + (conflicting,))
    )

    assert outcome.verdict.status is ScaleQualificationStatus.FAILED
    assert ScaleQualificationFailureCode.REVISION_HISTORY_INVALID in outcome.verdict.failure_codes


def test_vendor_label_and_future_return_feature_are_rejected() -> None:
    frozen = _fixture()

    with pytest.raises(ContractValidationError):
        replace(frozen, source_class="OFFICIAL_TUSHARE")

    row = frozen.rows[0]
    with pytest.raises(ContractValidationError):
        replace(
            row,
            features={**dict(row.features), "future_return_5d": 0.1},
        )


def test_unavailable_adapter_needs_no_network_or_provider_key() -> None:
    adapter = InMemoryBoundedScaleInputAdapter(None)
    request = BoundedScaleInputRequest(dataset_id="missing-expanded-snapshot")

    outcome = RealAshareScalePlanner().run_adapter(adapter, request)

    assert adapter.network_capable is False
    assert request.offline_only is True
    assert outcome.verdict.status is ScaleQualificationStatus.UNAVAILABLE
    assert outcome.verdict.operational_smoke_status == "UNAVAILABLE"
    assert ScaleQualificationFailureCode.ADAPTER_UNAVAILABLE in outcome.verdict.failure_codes


def test_coverage_and_twenty_session_plan_are_machine_readable() -> None:
    outcome = RealAshareScalePlanner().qualify(_fixture())

    assert outcome.manifest is not None
    assert outcome.vintage_plan is not None
    assert outcome.manifest.symbol_count == 20
    assert outcome.manifest.session_count == 120
    assert outcome.manifest.universe_coverage.session_coverage == 1.0
    assert outcome.manifest.future_return_feature_present is False
    assert outcome.manifest.label_fields == ("future_return_5d",)
    assert outcome.vintage_plan.replay_session_count == 20
    assert len({item.session for item in outcome.vintage_plan.vintages}) == 20

