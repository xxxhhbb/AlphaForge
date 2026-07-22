from __future__ import annotations

from dataclasses import replace
from datetime import datetime, time, timedelta, timezone

import pytest

from veriquant.contracts import ContractValidationError
from veriquant.data.bitemporal import canonical_content_hash
from veriquant.real_ashare_scale import (
    FrozenAshareScaleInput,
    FrozenDataVintage,
    HistoricalUniverseSnapshot,
    InputOrigin,
    ScaleInputRow,
)
from veriquant.research_real_data_v2 import (
    APPROVED_PROVIDER_CLASS,
    APPROVED_SOURCE_CLASS,
    REQUIRED_FEATURE_FIELDS,
    InMemoryFrozenResearchDataProvider,
    ResearchDataFailureCode,
    ResearchDataRequest,
    ResearchDataStatus,
    ResearchRealDataService,
)


CN = timezone(timedelta(hours=8))
HASH_A = "sha256:" + "a" * 64


def _sessions(count: int) -> tuple[str, ...]:
    output = []
    cursor = datetime(2024, 1, 2, tzinfo=CN).date()
    while len(output) < count:
        if cursor.weekday() < 5:
            output.append(cursor.isoformat())
        cursor += timedelta(days=1)
    return tuple(output)


def _clock(session: str, hour: int) -> datetime:
    return datetime.combine(datetime.fromisoformat(session).date(), time(hour), tzinfo=CN)


def _fixture(
    *,
    dataset_id: str = "research-real-v2-fixture",
    symbol_count: int = 30,
    session_count: int = 260,
    future_shift: float = 0.0,
) -> FrozenAshareScaleInput:
    sessions = _sessions(session_count)
    original = tuple(f"{index:06d}.SZ" for index in range(symbol_count))
    entrant = "688999.SH"
    memberships = []
    rows = []
    for session_index, session in enumerate(sessions):
        # Freeze real historical membership: one constituent exits and a new
        # constituent enters halfway through the sample.
        symbols = original if session_index < session_count // 2 else original[:-1] + (entrant,)
        symbols = tuple(sorted(symbols))
        memberships.append(HistoricalUniverseSnapshot(session=session, symbols=symbols))
        for symbol_index, symbol in enumerate(symbols):
            perturbation = future_shift if session_index == session_count - 1 else 0.0
            base = 10.0 + symbol_index / 10.0 + session_index / 100.0 + perturbation
            rows.append(ScaleInputRow(
                symbol=symbol,
                session=session,
                event_time=_clock(session, 15),
                available_at=_clock(session, 16),
                revision_id=f"r-{session_index:03d}-{symbol_index:03d}",
                features={
                    "open": base - 0.05,
                    "high": base + 0.10,
                    "low": base - 0.10,
                    "close": base,
                    "volume": 1_000_000 + symbol_index * 1_000,
                    "amount": (1_000_000 + symbol_index * 1_000) * base,
                    "adj_factor": 1.0,
                    "market_cap": 10_000_000_000 + symbol_index * 100_000_000,
                    "industry": f"industry-{symbol_index % 5}",
                    "status": "LISTED",
                },
                labels={"future_return_5d": (symbol_index - symbol_count / 2) / 10_000.0},
            ))
    vintages = tuple(
        FrozenDataVintage(
            vintage_id=f"v-{index:03d}",
            session=session,
            available_at=_clock(session, 16),
            decision_time=_clock(session, 17),
            revision_id=f"vr-{index:03d}",
            content_hash=canonical_content_hash((session, index)),
            supersedes_vintage_id=None,
        )
        for index, session in enumerate(sessions[-20:])
    )
    membership_tuple = tuple(memberships)
    row_tuple = tuple(rows)
    return FrozenAshareScaleInput(
        dataset_id=dataset_id,
        provider_class=APPROVED_PROVIDER_CLASS,
        source_class=APPROVED_SOURCE_CLASS,
        input_origin=InputOrigin.TEST_FIXTURE,
        capture_time=_clock(sessions[-1], 20),
        capture_network_used=False,
        calendar_id="sse-szse-frozen-v2",
        calendar_sessions=sessions,
        calendar_hash=canonical_content_hash(sessions),
        historical_universe=membership_tuple,
        universe_hash=canonical_content_hash(membership_tuple),
        rows=row_tuple,
        data_hash=canonical_content_hash(row_tuple),
        feature_fields=REQUIRED_FEATURE_FIELDS,
        label_fields=("future_return_5d",),
        availability_policy_hash=HASH_A,
        vintages=vintages,
        adapter_manifest_hash=HASH_A,
    )


def _request(**kwargs) -> ResearchDataRequest:
    values = {"request_id": "request.research-real-v2", "dataset_id": "research-real-v2-fixture"}
    values.update(kwargs)
    return ResearchDataRequest(**values)


@pytest.fixture(scope="module")
def prepared():
    return ResearchRealDataService().prepare(InMemoryFrozenResearchDataProvider(_fixture()), _request())


def test_260_session_fixture_registers_but_cannot_masquerade_as_real(prepared) -> None:
    assert prepared.qualification.status is ResearchDataStatus.FIXTURE_ONLY
    assert prepared.qualification.manifest is not None
    assert prepared.qualification.manifest.session_count == 260
    assert prepared.qualification.manifest.minimum_symbols_per_session == 30
    assert prepared.qualification.manifest.recommended_50d_history_satisfied is True
    assert prepared.qualification.manifest.provider_class == "APPROVED_PROXY_DATA_VENDOR"
    assert "OFFICIAL_TUSHARE" not in str(prepared.qualification.to_dict())


def test_same_frozen_input_has_stable_manifest_content_and_dataset_hashes() -> None:
    frozen = _fixture()
    service = ResearchRealDataService()
    first = service.prepare(InMemoryFrozenResearchDataProvider(frozen), _request())
    second = service.prepare(InMemoryFrozenResearchDataProvider(frozen), _request())
    assert first.qualification.manifest.artifact_hash == second.qualification.manifest.artifact_hash
    assert first.qualification.manifest.content_hash == second.qualification.manifest.content_hash
    assert first.dataset_card.artifact_hash == second.dataset_card.artifact_hash


def test_future_perturbation_does_not_change_past_pit_snapshot() -> None:
    service = ResearchRealDataService()
    baseline = service.prepare(InMemoryFrozenResearchDataProvider(_fixture()), _request())
    changed = service.prepare(InMemoryFrozenResearchDataProvider(_fixture(future_shift=500.0)), _request())
    decision_time = _clock(_sessions(260)[100], 17)
    left = baseline.registry.as_of_snapshot(
        "research-real-v2-fixture", decision_time=decision_time, fields=("close", "volume")
    )
    right = changed.registry.as_of_snapshot(
        "research-real-v2-fixture", decision_time=decision_time, fields=("close", "volume")
    )
    assert left.artifact_hash == right.artifact_hash


def test_historical_universe_does_not_leak_future_entrant(prepared) -> None:
    sessions = _sessions(260)
    early = prepared.registry.universe_snapshot("research-real-v2-fixture", _clock(sessions[10], 17))
    late = prepared.registry.universe_snapshot("research-real-v2-fixture", _clock(sessions[200], 17))
    assert "688999.SH" not in early
    assert "688999.SH" in late


def test_180_sessions_is_strictly_insufficient_for_50_day_research() -> None:
    outcome = ResearchRealDataService().prepare(
        InMemoryFrozenResearchDataProvider(_fixture(session_count=180)), _request()
    )
    assert outcome.qualification.status is ResearchDataStatus.BLOCKED
    assert ResearchDataFailureCode.INSUFFICIENT_LOOKBACK_HISTORY in outcome.qualification.failure_codes


def test_shorter_research_can_use_180_sessions() -> None:
    outcome = ResearchRealDataService().prepare(
        InMemoryFrozenResearchDataProvider(_fixture(session_count=180)),
        _request(lookback_sessions=20),
    )
    assert outcome.qualification.status is ResearchDataStatus.FIXTURE_ONLY


def test_missing_field_fails_closed() -> None:
    frozen = _fixture(session_count=180)
    broken = replace(
        frozen,
        feature_fields=tuple(field for field in frozen.feature_fields if field != "market_cap"),
    )
    outcome = ResearchRealDataService().prepare(
        InMemoryFrozenResearchDataProvider(broken), _request(lookback_sessions=20)
    )
    assert outcome.qualification.status is ResearchDataStatus.BLOCKED
    assert ResearchDataFailureCode.MISSING_REQUIRED_FIELD in outcome.qualification.failure_codes


def test_incomplete_historical_universe_fails_closed() -> None:
    frozen = _fixture(session_count=180)
    historical = frozen.historical_universe[:-1]
    broken = replace(frozen, historical_universe=historical, universe_hash=canonical_content_hash(historical))
    outcome = ResearchRealDataService().prepare(
        InMemoryFrozenResearchDataProvider(broken), _request(lookback_sessions=20)
    )
    assert ResearchDataFailureCode.UNIVERSE_INCOMPLETE in outcome.qualification.failure_codes


def test_future_member_row_fails_closed() -> None:
    frozen = _fixture(session_count=180)
    leaked = replace(frozen.rows[0], symbol="999999.SZ")
    rows = (leaked,) + frozen.rows[1:]
    broken = replace(frozen, rows=rows, data_hash=canonical_content_hash(rows))
    outcome = ResearchRealDataService().prepare(
        InMemoryFrozenResearchDataProvider(broken), _request(lookback_sessions=20)
    )
    assert ResearchDataFailureCode.FUTURE_MEMBERSHIP_LEAKAGE in outcome.qualification.failure_codes


def test_network_provider_requires_explicit_enable_and_is_not_called() -> None:
    class NetworkProvider:
        provider_class = APPROVED_PROVIDER_CLASS
        source_class = APPROVED_SOURCE_CLASS
        network_capable = True

        def __init__(self):
            self.called = False

        def load(self, request):
            self.called = True
            return _fixture()

    provider = NetworkProvider()
    outcome = ResearchRealDataService().prepare(provider, _request())
    assert provider.called is False
    assert outcome.qualification.status is ResearchDataStatus.BLOCKED
    assert ResearchDataFailureCode.NETWORK_NOT_ENABLED in outcome.qualification.failure_codes


def test_unavailable_and_wrong_source_are_typed_without_secrets() -> None:
    unavailable = ResearchRealDataService().prepare(InMemoryFrozenResearchDataProvider(None), _request())
    assert unavailable.qualification.status is ResearchDataStatus.UNAVAILABLE
    assert ResearchDataFailureCode.ADAPTER_UNAVAILABLE in unavailable.qualification.failure_codes

    class WrongProvider:
        provider_class = "OFFICIAL_TUSHARE"
        source_class = "OFFICIAL_TUSHARE"
        network_capable = False

        def load(self, request):
            raise AssertionError("must not be called")

    rejected = ResearchRealDataService().prepare(WrongProvider(), _request())
    assert rejected.qualification.status is ResearchDataStatus.BLOCKED
    assert ResearchDataFailureCode.SOURCE_LABEL_REJECTED in rejected.qualification.failure_codes


def test_future_return_cannot_be_declared_as_feature() -> None:
    frozen = _fixture(session_count=180)
    with pytest.raises(ContractValidationError):
        replace(frozen, feature_fields=frozen.feature_fields + ("future_return_5d",))


def test_price_core_request_accepts_missing_optional_industry_and_records_limitation() -> None:
    frozen = _fixture()
    rows = tuple(replace(row, features={key: value for key, value in row.features.items() if key != "industry"}) for row in frozen.rows)
    reduced = replace(
        frozen,
        rows=rows,
        data_hash=canonical_content_hash(rows),
        feature_fields=tuple(field for field in frozen.feature_fields if field != "industry"),
    )
    outcome = ResearchRealDataService().prepare(InMemoryFrozenResearchDataProvider(reduced), _request())
    assert outcome.qualification.status is ResearchDataStatus.FIXTURE_ONLY
    assert outcome.qualification.manifest.missing_optional_feature_fields == ("industry",)
    assert outcome.qualification.manifest.limitations == ("OPTIONAL_FIELD_UNAVAILABLE:industry",)


def test_explicit_industry_requirement_blocks_same_price_core_data() -> None:
    frozen = _fixture()
    rows = tuple(replace(row, features={key: value for key, value in row.features.items() if key != "industry"}) for row in frozen.rows)
    reduced = replace(
        frozen,
        rows=rows,
        data_hash=canonical_content_hash(rows),
        feature_fields=tuple(field for field in frozen.feature_fields if field != "industry"),
    )
    request = _request(
        required_feature_fields=("open", "high", "low", "close", "volume", "amount", "adj_factor", "market_cap", "status", "industry"),
        optional_feature_fields=(),
    )
    outcome = ResearchRealDataService().prepare(InMemoryFrozenResearchDataProvider(reduced), request)
    assert outcome.qualification.status is ResearchDataStatus.BLOCKED
    assert ResearchDataFailureCode.MISSING_REQUIRED_FIELD in outcome.qualification.failure_codes


def test_required_and_optional_field_demands_must_be_disjoint() -> None:
    with pytest.raises(ContractValidationError):
        _request(required_feature_fields=("close",), optional_feature_fields=("close",))
