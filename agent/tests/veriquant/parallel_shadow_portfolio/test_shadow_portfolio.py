from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.veriquant.contracts import ContractValidationError, FailureCode
from src.veriquant.shadow_portfolio.api import (
    ChallengerState,
    InMemoryMarketPriceProvider,
    MarketPrice,
    ShadowLedger,
    ShadowRunRequest,
    build_shadow_portfolio_fixture,
    make_challenger,
    make_prices,
    make_vintage,
)


def dt(hour: int) -> datetime:
    return datetime(2026, 1, 5, hour, 0, tzinfo=timezone.utc)


def test_run_once_reproducible_with_same_frozen_inputs_and_clock() -> None:
    first = build_shadow_portfolio_fixture()
    second = build_shadow_portfolio_fixture()

    manifest_a = first.service.run_once(first.request)
    manifest_b = second.service.run_once(second.request)

    assert manifest_a.artifact_hash == manifest_b.artifact_hash
    assert manifest_a.status.value == "READY"
    assert manifest_a.ledger_tip_hash == manifest_b.ledger_tip_hash


def test_duplicate_run_id_is_idempotent_and_append_only() -> None:
    fixture = build_shadow_portfolio_fixture()
    manifest_a = fixture.service.run_once(fixture.request)
    event_count = fixture.ledger.event_count
    manifest_b = fixture.service.run_once(fixture.request)

    assert manifest_a.artifact_hash == manifest_b.artifact_hash
    assert fixture.ledger.event_count == event_count
    snapshot_events = fixture.ledger.snapshot().events
    assert tuple(event.sequence for event in snapshot_events) == tuple(range(1, event_count + 1))
    assert all(event.event_hash and event.event_hash.startswith("sha256:") for event in snapshot_events)


def test_duplicate_run_id_with_different_input_is_rejected() -> None:
    ledger = ShadowLedger()
    fixture = build_shadow_portfolio_fixture(ledger=ledger)
    fixture.service.run_once(fixture.request)
    other = build_shadow_portfolio_fixture(
        ledger=ledger,
        challenger=make_challenger(version="v2"),
        run_id=fixture.request.run_id,
    )

    with pytest.raises(ContractValidationError) as exc:
        other.service.run_once(other.request)

    assert exc.value.code is FailureCode.DUPLICATE_FILL_CONFLICT


def test_unpromoted_factor_cannot_affect_positions() -> None:
    fixture = build_shadow_portfolio_fixture(challenger=make_challenger(state=ChallengerState.DRAFT))

    with pytest.raises(ContractValidationError) as exc:
        fixture.service.run_once(fixture.request)

    assert exc.value.code is FailureCode.INVALID_ALPHA_SOURCE


def test_missing_execution_price_fails_closed() -> None:
    fixture = build_shadow_portfolio_fixture(price_provider=make_prices(missing_symbol="AAA"))

    with pytest.raises(ContractValidationError) as exc:
        fixture.service.run_once(fixture.request)

    assert exc.value.code is FailureCode.RISK_INPUT_INVALID


def test_suspension_fails_closed() -> None:
    fixture = build_shadow_portfolio_fixture(price_provider=make_prices(suspended_symbol="AAA"))

    with pytest.raises(ContractValidationError) as exc:
        fixture.service.run_once(fixture.request)

    assert exc.value.code is FailureCode.AMBIGUOUS_EXECUTION_STATE


def test_limit_up_blocks_buy_fill() -> None:
    fixture = build_shadow_portfolio_fixture(price_provider=make_prices(limit_up_symbol="AAA"))

    with pytest.raises(ContractValidationError) as exc:
        fixture.service.run_once(fixture.request)

    assert exc.value.code is FailureCode.AMBIGUOUS_EXECUTION_STATE


def test_future_close_price_cannot_be_used_for_execution() -> None:
    future_close = MarketPrice(
        symbol="AAA",
        price=10.5,
        price_time=dt(15),
        available_time=dt(15),
        price_kind="mark",
    )
    prices = make_prices()
    price_provider = InMemoryMarketPriceProvider(
        execution_prices={**prices.execution_prices, "AAA": future_close},
        valuation_prices=prices.valuation_prices,
    )
    fixture = build_shadow_portfolio_fixture(price_provider=price_provider)

    with pytest.raises(ContractValidationError) as exc:
        fixture.service.run_once(fixture.request)

    assert exc.value.code is FailureCode.INVALID_TIME_ORDER


def test_transaction_cost_reduces_net_pnl() -> None:
    fixture = build_shadow_portfolio_fixture()
    manifest = fixture.service.run_once(fixture.request)
    snapshot = fixture.ledger.snapshot()
    attribution_event = snapshot.events[-1]
    attribution = snapshot.payloads[-1]

    assert manifest.fill_hash.startswith("sha256:")
    assert attribution_event.event_type == "ATTRIBUTION"
    assert attribution.transaction_cost > 0.0
    assert attribution.slippage_cost > 0.0
    assert attribution.net_pnl < attribution.gross_pnl
    assert manifest.attribution_hash == attribution.artifact_hash

    event_hashes = tuple(event.event_hash for event in snapshot.events)
    assert len(event_hashes) == len(set(event_hashes))


def test_data_revision_creates_new_vintage_hash() -> None:
    first = build_shadow_portfolio_fixture(vintage=make_vintage(revision_id="rev1"))
    second = build_shadow_portfolio_fixture(vintage=make_vintage(revision_id="rev2"), run_id="shadow-run-2")

    manifest_a = first.service.run_once(first.request)
    manifest_b = second.service.run_once(second.request)

    assert manifest_a.vintage_hash != manifest_b.vintage_hash
    assert manifest_a.artifact_hash != manifest_b.artifact_hash


def test_challenger_version_changes_manifest() -> None:
    first = build_shadow_portfolio_fixture(challenger=make_challenger(version="v1"))
    second = build_shadow_portfolio_fixture(challenger=make_challenger(version="v2"), run_id="shadow-run-v2")

    manifest_a = first.service.run_once(first.request)
    manifest_b = second.service.run_once(second.request)

    assert manifest_a.challenger_hashes != manifest_b.challenger_hashes
    assert manifest_a.target_hash != manifest_b.target_hash


def test_append_only_snapshot_is_immutable_to_external_mutation() -> None:
    fixture = build_shadow_portfolio_fixture()
    fixture.service.run_once(fixture.request)
    snapshot = fixture.ledger.snapshot()
    copied = list(snapshot.events)
    copied.clear()

    assert fixture.ledger.event_count == 5
    assert len(fixture.ledger.snapshot().events) == 5


def test_policy_and_vintage_binding_are_enforced() -> None:
    fixture = build_shadow_portfolio_fixture()
    bad_request = ShadowRunRequest(
        run_id="shadow-bad-policy",
        account_id=fixture.request.account_id,
        vintage_id=fixture.request.vintage_id,
        policy_version=fixture.request.policy_version,
        policy_hash=fixture.request.policy_hash,
        decision_time=dt(7),
        execution_time=dt(10),
        valuation_time=dt(15),
        starting_cash=fixture.request.starting_cash,
        previous_positions={},
    )

    with pytest.raises(ContractValidationError) as exc:
        fixture.service.run_once(bad_request)

    assert exc.value.code is FailureCode.INVALID_TIME_ORDER
