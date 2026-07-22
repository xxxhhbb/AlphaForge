from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from src.veriquant.contracts import (
    Component,
    ContractValidationError,
    FailureCode,
    PortfolioPlan,
    ResearchState,
    RiskDecision,
    RiskVerdict,
)
from src.veriquant.paper import (
    DeterministicClock,
    FillStatus,
    PaperExecutionEngine,
    PaperExecutionPolicy,
    PaperMarketQuote,
    PaperOrderPlanner,
)


NOW = datetime(2025, 2, 3, 14, 30, tzinfo=timezone.utc)
HASH = "sha256:" + "a" * 64


def portfolio_plan() -> PortfolioPlan:
    return PortfolioPlan(
        plan_id="portfolio-paper-001",
        forecast_hash=HASH,
        universe=("AAA", "BBB"),
        decision_time=NOW,
        target_weights={"AAA": 0.10, "BBB": -0.10},
        constraints_hash=HASH,
        objective_terms={"objective_value": 0.01},
    )


def risk_decision(plan: PortfolioPlan, verdict: RiskVerdict) -> RiskDecision:
    if verdict is RiskVerdict.APPROVE:
        return RiskDecision(
            risk_decision_id="risk-approve-001",
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
    if verdict is RiskVerdict.SCALE_DOWN:
        return RiskDecision(
            risk_decision_id="risk-scale-001",
            portfolio_plan_hash=plan.artifact_hash,
            decision_time=NOW,
            approved=False,
            policy_hash=HASH,
            breaches=("LEVERAGE_LIMIT",),
            state=ResearchState.BLOCKED,
            verdict=RiskVerdict.SCALE_DOWN,
            scale_factor=0.5,
            checked_metrics={"gross_leverage": 0.20},
        )
    return RiskDecision(
        risk_decision_id="risk-reject-001",
        portfolio_plan_hash=plan.artifact_hash,
        decision_time=NOW,
        approved=False,
        policy_hash=HASH,
        breaches=("STRESS_LOSS_LIMIT",),
        state=ResearchState.BLOCKED,
        verdict=RiskVerdict.REJECT,
        scale_factor=0.0,
        checked_metrics={"stress_loss_rate": 0.50},
    )


def create_batch(*, capital: Decimal, verdict: RiskVerdict = RiskVerdict.APPROVE):
    plan = portfolio_plan()
    decision = risk_decision(plan, verdict)
    return PaperOrderPlanner().create(
        portfolio_plan=plan,
        risk_decision=decision,
        capital=capital,
        prices={"AAA": Decimal("100"), "BBB": Decimal("50")},
        current_positions={"AAA": Decimal("0"), "BBB": Decimal("0")},
    )


def quotes(*, adv: Decimal = Decimal("1000000")):
    return {
        "AAA": PaperMarketQuote(
            symbol="AAA",
            mid_price=Decimal("100"),
            spread_bps=Decimal("4"),
            adv_shares=adv,
            event_time=NOW,
        ),
        "BBB": PaperMarketQuote(
            symbol="BBB",
            mid_price=Decimal("50"),
            spread_bps=Decimal("6"),
            adv_shares=adv,
            event_time=NOW,
        ),
    }


def policy(*, fill_fraction: str = "0.50") -> PaperExecutionPolicy:
    return PaperExecutionPolicy(
        max_adv_participation=Decimal("0.50"),
        base_fill_fraction=Decimal(fill_fraction),
        linear_impact_bps=Decimal("100"),
        commission_bps=Decimal("1"),
        lot_size=Decimal("1"),
    )


def execute(batch, *, seed: int = 7, fill_fraction: str = "0.50"):
    engine = PaperExecutionEngine(
        clock=DeterministicClock(start=NOW, step=timedelta(microseconds=1)),
        seed=seed,
    )
    result = engine.execute_once(batch=batch, quotes=quotes(), policy=policy(fill_fraction=fill_fraction))
    return engine, result


def test_reject_cannot_create_order_plan_but_approve_and_scale_down_can() -> None:
    with pytest.raises(ContractValidationError) as caught:
        create_batch(capital=Decimal("100000"), verdict=RiskVerdict.REJECT)
    assert caught.value.code is FailureCode.RISK_NOT_APPROVED

    approved = create_batch(capital=Decimal("100000"), verdict=RiskVerdict.APPROVE)
    scaled = create_batch(capital=Decimal("100000"), verdict=RiskVerdict.SCALE_DOWN)
    assert approved.order_plan.environment == "paper"
    assert approved.order_plan.produced_by is Component.ORDER_SERVICE
    assert scaled.effective_target_weights == {"AAA": 0.05, "BBB": -0.05}
    assert sum(order.quantity for order in scaled.order_plan.orders) < sum(
        order.quantity for order in approved.order_plan.orders
    )


def test_partial_fill_conserves_quantity_and_is_seed_clock_reproducible() -> None:
    batch = create_batch(capital=Decimal("100000"))
    _, first = execute(batch, seed=11, fill_fraction="0.40")
    _, second = execute(batch, seed=11, fill_fraction="0.40")

    assert first.artifact_hash == second.artifact_hash
    assert all(fill.status is FillStatus.PARTIAL for fill in first.fills)
    for fill in first.fills:
        assert fill.requested_quantity == fill.filled_quantity + fill.remaining_quantity
        assert fill.fill.quantity == fill.filled_quantity


def test_larger_capital_and_participation_do_not_reduce_impact() -> None:
    small = create_batch(capital=Decimal("100000"))
    large = create_batch(capital=Decimal("200000"))
    _, small_result = execute(small, fill_fraction="1")
    _, large_result = execute(large, fill_fraction="1")

    assert sum(fill.participation for fill in large_result.fills) >= sum(
        fill.participation for fill in small_result.fills
    )
    assert sum(fill.impact_cost for fill in large_result.fills) >= sum(
        fill.impact_cost for fill in small_result.fills
    )


def test_duplicate_or_ambiguous_order_state_halts_without_resend() -> None:
    batch = create_batch(capital=Decimal("100000"))
    engine, _ = execute(batch)
    with pytest.raises(ContractValidationError) as caught:
        engine.execute_once(batch=batch, quotes=quotes(), policy=policy())
    assert caught.value.code is FailureCode.AMBIGUOUS_EXECUTION_STATE
    assert engine.halted is True
    assert "NO_RESEND" in (engine.halt_reason or "")

    another = PaperExecutionEngine(clock=DeterministicClock(start=NOW), seed=7)
    another.mark_ambiguous(batch.order_plan.orders[0].client_order_id)
    with pytest.raises(ContractValidationError) as ambiguous:
        another.execute_once(batch=batch, quotes=quotes(), policy=policy())
    assert ambiguous.value.code is FailureCode.EXECUTION_HALTED


def test_paper_package_has_no_live_broker_or_secret_access_surface() -> None:
    package_root = Path(__file__).resolve().parents[2] / "src" / "veriquant" / "paper"
    source = "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted(package_root.glob("*.py"))
    ).lower()
    forbidden = (
        "src.live",
        "from ..live",
        "import broker",
        "brokerapi",
        "os.environ",
        "os.getenv",
        "api_key",
        "secret_key",
    )
    assert all(token not in source for token in forbidden)
