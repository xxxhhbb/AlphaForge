from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from src.veriquant.contracts import (
    PortfolioPlan,
    ResearchState,
    RiskDecision,
    RiskVerdict,
)
from src.veriquant.downstream import AssetRiskInput
from src.veriquant.paper import (
    DailyReconciler,
    DeterministicClock,
    JournalEventType,
    PaperAttributionService,
    PaperExecutionEngine,
    PaperExecutionPolicy,
    PaperJournal,
    PaperMarketQuote,
    PaperOrderPlanner,
    ReconciliationStatus,
)


NOW = datetime(2025, 2, 3, 14, 30, tzinfo=timezone.utc)
HASH = "sha256:" + "d" * 64


def make_batch_and_execution():
    plan = PortfolioPlan(
        plan_id="portfolio-ledger-001",
        forecast_hash=HASH,
        universe=("AAA", "BBB"),
        decision_time=NOW,
        target_weights={"AAA": 0.10, "BBB": -0.10},
        constraints_hash=HASH,
    )
    risk = RiskDecision(
        risk_decision_id="risk-ledger-approve",
        portfolio_plan_hash=plan.artifact_hash,
        decision_time=NOW,
        approved=True,
        policy_hash=HASH,
        breaches=(),
        state=ResearchState.CHALLENGER,
        verdict=RiskVerdict.APPROVE,
        scale_factor=1.0,
        checked_metrics={"gross_leverage": 0.20},
    )
    batch = PaperOrderPlanner().create(
        portfolio_plan=plan,
        risk_decision=risk,
        capital=Decimal("100000"),
        prices={"AAA": Decimal("100"), "BBB": Decimal("50")},
        current_positions={"AAA": Decimal("0"), "BBB": Decimal("0")},
    )
    quotes = {
        "AAA": PaperMarketQuote(
            symbol="AAA", mid_price=Decimal("100"), spread_bps=Decimal("4"),
            adv_shares=Decimal("1000000"), event_time=NOW,
        ),
        "BBB": PaperMarketQuote(
            symbol="BBB", mid_price=Decimal("50"), spread_bps=Decimal("6"),
            adv_shares=Decimal("1000000"), event_time=NOW,
        ),
    }
    engine = PaperExecutionEngine(
        clock=DeterministicClock(start=NOW, step=timedelta(microseconds=1)),
        seed=19,
    )
    execution = engine.execute_once(
        batch=batch,
        quotes=quotes,
        policy=PaperExecutionPolicy(
            max_adv_participation=Decimal("0.50"),
            base_fill_fraction=Decimal("0.50"),
            linear_impact_bps=Decimal("100"),
            commission_bps=Decimal("1"),
        ),
    )
    return plan, batch, engine, execution


def asset_risk_inputs():
    return (
        AssetRiskInput(
            symbol="AAA", industry="TECH", beta=1.1, volatility=0.20,
            adv_dollars=100_000_000.0, spread_bps=4.0, stress_loss_rate=0.10,
        ),
        AssetRiskInput(
            symbol="BBB", industry="FINANCE", beta=0.9, volatility=0.18,
            adv_dollars=50_000_000.0, spread_bps=6.0, stress_loss_rate=0.09,
        ),
    )


def test_duplicate_fill_is_idempotent_and_replay_is_deterministic(tmp_path) -> None:
    _, batch, _, execution = make_batch_and_execution()
    path = tmp_path / "paper_journal.sqlite3"
    journal = PaperJournal(path, initial_cash=Decimal("1"), opened_at=NOW - timedelta(seconds=1))

    first = journal.post_fill(paper_fill=execution.fills[0], batch=batch)
    count_after_first = first.event_count
    duplicate = journal.post_fill(paper_fill=execution.fills[0], batch=batch)
    assert duplicate.event_count == count_after_first
    assert duplicate.artifact_hash == first.artifact_hash

    replay_one = journal.replay()
    replay_two = journal.replay()
    reopened = PaperJournal(path, initial_cash=Decimal("1"), opened_at=NOW - timedelta(seconds=1))
    replay_three = reopened.replay()
    assert replay_one.artifact_hash == replay_two.artifact_hash == replay_three.artifact_hash
    assert reopened.events() == journal.events()


def test_journal_reconciles_cash_positions_costs_financing_and_pnl(tmp_path) -> None:
    plan, batch, _, execution = make_batch_and_execution()
    journal = PaperJournal(
        tmp_path / "accounting.sqlite3",
        initial_cash=Decimal("1"),
        opened_at=NOW - timedelta(seconds=1),
    )
    for fill in execution.fills:
        journal.post_fill(paper_fill=fill, batch=batch)
    day_one = NOW + timedelta(days=1)
    marks = {"AAA": Decimal("102"), "BBB": Decimal("49")}
    journal.mark_to_market(marks=marks, event_time=day_one)
    journal.post_daily_costs(
        marks=marks,
        borrow_bps=Decimal("2"),
        financing_bps=Decimal("1"),
        event_time=day_one + timedelta(microseconds=1),
    )
    before_reconcile = journal.replay()
    report = DailyReconciler().reconcile(
        journal=journal,
        marks=marks,
        reconciled_at=day_one + timedelta(days=1),
        external_cash=before_reconcile.cash,
        external_positions=before_reconcile.positions,
    )
    state = journal.replay()

    assert report.status is ReconciliationStatus.RECONCILED
    assert abs(state.equity - state.expected_equity) <= Decimal("0.0001")
    assert state.fees > 0
    assert state.borrow > 0
    assert state.financing > 0
    event_types = {event.event_type for event in journal.events()}
    assert {
        JournalEventType.OPENING_CASH,
        JournalEventType.CASH,
        JournalEventType.POSITION,
        JournalEventType.FEES,
        JournalEventType.BORROW,
        JournalEventType.FINANCING,
        JournalEventType.REALIZED_PNL,
        JournalEventType.UNREALIZED_PNL,
        JournalEventType.SPREAD_COST,
        JournalEventType.IMPACT_COST,
    } <= event_types

    attribution = PaperAttributionService().attribute(
        state=state,
        portfolio_plan=plan,
        assets=asset_risk_inputs(),
        factor_loadings={"validated_factor": {"AAA": 1.0, "BBB": -1.0}},
        period_start=NOW,
        period_end=day_one + timedelta(days=1),
    )
    assert "total" in attribution.pnl_components
    assert "total_variance" in attribution.risk_components
    assert "impact" in attribution.cost_components
    assert "validated_factor" in attribution.factor_contributions


def test_out_of_balance_or_ambiguous_execution_triggers_halt(tmp_path) -> None:
    _, batch, engine, execution = make_batch_and_execution()
    journal = PaperJournal(
        tmp_path / "halt.sqlite3",
        initial_cash=Decimal("100000"),
        opened_at=NOW - timedelta(seconds=1),
    )
    for fill in execution.fills:
        journal.post_fill(paper_fill=fill, batch=batch)
    state = journal.replay()
    report = DailyReconciler().reconcile(
        journal=journal,
        marks={"AAA": Decimal("100"), "BBB": Decimal("50")},
        reconciled_at=NOW + timedelta(days=1),
        external_cash=state.cash + Decimal("1"),
        external_positions=state.positions,
    )
    assert report.status is ReconciliationStatus.HALT
    assert "CASH_MISMATCH" in report.breaches
    assert journal.replay().halted is True

    second = PaperJournal(
        tmp_path / "ambiguous.sqlite3",
        initial_cash=Decimal("100000"),
        opened_at=NOW - timedelta(seconds=1),
    )
    engine.mark_ambiguous(batch.order_plan.orders[0].client_order_id)
    ambiguous = DailyReconciler().reconcile(
        journal=second,
        marks={},
        reconciled_at=NOW + timedelta(days=1),
        execution_engine=engine,
    )
    assert ambiguous.status is ReconciliationStatus.HALT
    assert "AMBIGUOUS_EXECUTION_STATE" in ambiguous.breaches
    assert second.replay().halted is True


def test_sqlite_journal_tables_reject_update_and_delete(tmp_path) -> None:
    path = tmp_path / "append_only.sqlite3"
    journal = PaperJournal(path, initial_cash=Decimal("100"), opened_at=NOW)
    assert journal.replay().event_count == 1
    connection = sqlite3.connect(path)
    try:
        with pytest.raises(sqlite3.DatabaseError):
            connection.execute("UPDATE journal_events SET amount = '0' WHERE sequence = 1")
        with pytest.raises(sqlite3.DatabaseError):
            connection.execute("DELETE FROM journal_events WHERE sequence = 1")
    finally:
        connection.close()
