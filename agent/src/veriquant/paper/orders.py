"""Risk-gated target-weight to paper OrderPlan conversion."""

from __future__ import annotations

from decimal import Decimal, ROUND_FLOOR
from typing import Mapping

from ..contracts import (
    Component,
    FailureCode,
    OrderPlan,
    PlannedOrder,
    PortfolioPlan,
    ResearchState,
    RiskDecision,
    RiskVerdict,
)
from ..contracts.base import fail, require_finite_number
from ..data import canonical_content_hash
from .models import PaperOrderBatch


class PaperOrderPlanner:
    """Creates paper-only orders after an independent APPROVE/SCALE_DOWN verdict."""

    planner_id = "paper-order-planner-v1"

    @staticmethod
    def _decimal_mapping(
        values: Mapping[str, Decimal], universe: tuple[str, ...], field_name: str
    ) -> dict[str, Decimal]:
        if not isinstance(values, Mapping) or set(values) != set(universe):
            fail(FailureCode.INVALID_VALUE, f"{field_name} must exactly match universe")
        normalized: dict[str, Decimal] = {}
        for symbol in universe:
            value = values[symbol]
            require_finite_number(value, f"{field_name}.{symbol}")
            normalized[symbol] = Decimal(value)
        return normalized

    def create(
        self,
        *,
        portfolio_plan: PortfolioPlan,
        risk_decision: RiskDecision,
        capital: Decimal,
        prices: Mapping[str, Decimal],
        current_positions: Mapping[str, Decimal],
        lot_size: Decimal = Decimal("1"),
    ) -> PaperOrderBatch:
        if not isinstance(portfolio_plan, PortfolioPlan):
            fail(FailureCode.INVALID_VALUE, "portfolio_plan has the wrong type")
        if not isinstance(risk_decision, RiskDecision):
            fail(FailureCode.RISK_NOT_APPROVED, "typed RiskDecision is required")
        if risk_decision.portfolio_plan_hash != portfolio_plan.artifact_hash:
            fail(FailureCode.INVALID_HASH, "risk decision does not bind portfolio plan")
        if risk_decision.verdict not in {RiskVerdict.APPROVE, RiskVerdict.SCALE_DOWN}:
            fail(FailureCode.RISK_NOT_APPROVED, "REJECT plan cannot enter paper execution")
        if risk_decision.verdict is RiskVerdict.APPROVE:
            scale = Decimal("1")
        else:
            scale = Decimal(str(risk_decision.scale_factor))
            if not Decimal("0") < scale < Decimal("1"):
                fail(FailureCode.RISK_NOT_APPROVED, "invalid SCALE_DOWN factor")
        require_finite_number(capital, "capital")
        require_finite_number(lot_size, "lot_size")
        if capital <= 0 or lot_size <= 0:
            fail(FailureCode.INVALID_VALUE, "capital and lot_size must be positive")
        price_map = self._decimal_mapping(prices, portfolio_plan.universe, "prices")
        current = self._decimal_mapping(
            current_positions, portfolio_plan.universe, "current_positions"
        )
        if any(value <= 0 for value in price_map.values()):
            fail(FailureCode.INVALID_VALUE, "all prices must be positive")
        effective_weights = {
            symbol: float(Decimal(str(portfolio_plan.target_weights[symbol])) * scale)
            for symbol in portfolio_plan.universe
        }
        orders: list[PlannedOrder] = []
        for symbol in portfolio_plan.universe:
            target_shares = (
                capital * Decimal(str(effective_weights[symbol])) / price_map[symbol]
            )
            delta = target_shares - current[symbol]
            rounded = (abs(delta) / lot_size).to_integral_value(rounding=ROUND_FLOOR) * lot_size
            if rounded == 0:
                continue
            side = "BUY" if delta > 0 else "SELL"
            order_hash = canonical_content_hash(
                {
                    "planner_id": self.planner_id,
                    "portfolio_plan_hash": portfolio_plan.artifact_hash,
                    "risk_decision_hash": risk_decision.artifact_hash,
                    "symbol": symbol,
                    "side": side,
                    "quantity": rounded,
                    "capital": capital,
                    "price": price_map[symbol],
                }
            )
            orders.append(
                PlannedOrder(
                    client_order_id="paper-" + order_hash.removeprefix("sha256:")[:24],
                    symbol=symbol,
                    side=side,
                    quantity=rounded,
                    order_type="MARKET",
                )
            )
        if not orders:
            fail(FailureCode.INVALID_VALUE, "target weights produce no paper orders")
        plan_hash = canonical_content_hash(
            {
                "planner_id": self.planner_id,
                "portfolio_plan_hash": portfolio_plan.artifact_hash,
                "risk_decision_hash": risk_decision.artifact_hash,
                "orders": tuple(orders),
            }
        )
        order_plan = OrderPlan(
            order_plan_id="order-plan-" + plan_hash.removeprefix("sha256:")[:24],
            portfolio_plan_hash=portfolio_plan.artifact_hash,
            risk_decision_hash=risk_decision.artifact_hash,
            decision_time=portfolio_plan.decision_time,
            orders=tuple(orders),
            environment="paper",
            produced_by=Component.ORDER_SERVICE,
            state=ResearchState.CHALLENGER,
        )
        return PaperOrderBatch(
            portfolio_plan=portfolio_plan,
            risk_decision=risk_decision,
            order_plan=order_plan,
            effective_target_weights=effective_weights,
            capital=capital,
            prices=price_map,
            created_at=portfolio_plan.decision_time,
        )
