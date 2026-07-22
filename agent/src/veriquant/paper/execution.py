"""Deterministic offline partial-fill simulator with no resend path."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_FLOOR
from typing import Mapping

from ..contracts import Component, ContractValidationError, FailureCode, FillEvent
from ..contracts.base import fail, require_aware_datetime
from ..data import canonical_content_hash
from .models import (
    FillStatus,
    OrderExecutionStatus,
    PaperExecutionPolicy,
    PaperExecutionResult,
    PaperFill,
    PaperMarketQuote,
    PaperOrderBatch,
)


@dataclass
class DeterministicClock:
    start: datetime
    step: timedelta = timedelta(microseconds=1)
    _ticks: int = field(default=0, init=False, repr=False)

    def __post_init__(self) -> None:
        require_aware_datetime(self.start, "start")
        if not isinstance(self.step, timedelta) or self.step.total_seconds() <= 0:
            fail(FailureCode.INVALID_VALUE, "clock step must be positive")

    def next(self) -> datetime:
        value = self.start + self.step * self._ticks
        self._ticks += 1
        return value


class PaperExecutionEngine:
    """One-shot paper engine; duplicate/ambiguous state halts and never resends."""

    engine_id = "deterministic-paper-execution-v1"

    def __init__(self, *, clock: DeterministicClock, seed: int) -> None:
        if isinstance(seed, bool) or not isinstance(seed, int) or seed < 0:
            fail(FailureCode.INVALID_VALUE, "seed must be a non-negative integer")
        self.clock = clock
        self.seed = seed
        self._order_states: dict[str, OrderExecutionStatus] = {}
        self._executed_plans: set[str] = set()
        self._halted = False
        self._halt_reason: str | None = None

    @property
    def halted(self) -> bool:
        return self._halted

    @property
    def halt_reason(self) -> str | None:
        return self._halt_reason

    @property
    def order_states(self) -> Mapping[str, OrderExecutionStatus]:
        return dict(self._order_states)

    def _halt(self, reason: str) -> None:
        self._halted = True
        self._halt_reason = reason

    def mark_ambiguous(self, client_order_id: str) -> None:
        self._order_states[client_order_id] = OrderExecutionStatus.AMBIGUOUS
        self._halt("AMBIGUOUS_ORDER_STATE")

    @staticmethod
    def _floor_lot(quantity: Decimal, lot_size: Decimal) -> Decimal:
        return (
            (quantity / lot_size).to_integral_value(rounding=ROUND_FLOOR) * lot_size
        )

    def execute_once(
        self,
        *,
        batch: PaperOrderBatch,
        quotes: Mapping[str, PaperMarketQuote],
        policy: PaperExecutionPolicy,
    ) -> PaperExecutionResult:
        if self._halted:
            raise ContractValidationError(
                FailureCode.EXECUTION_HALTED,
                "paper execution is halted; operator resolution is required",
                details={"reason": self._halt_reason},
            )
        if not isinstance(batch, PaperOrderBatch) or not isinstance(policy, PaperExecutionPolicy):
            fail(FailureCode.INVALID_VALUE, "typed paper batch and policy are required")
        plan_hash = batch.order_plan.artifact_hash
        if plan_hash in self._executed_plans:
            self._halt("DUPLICATE_PLAN_AMBIGUOUS_NO_RESEND")
            raise ContractValidationError(
                FailureCode.AMBIGUOUS_EXECUTION_STATE,
                "order plan already attempted; automatic resend is prohibited",
            )
        quote_map = dict(quotes)
        prepared: list[tuple[object, PaperMarketQuote, Decimal, Decimal]] = []
        for order in batch.order_plan.orders:
            if order.client_order_id in self._order_states:
                self._halt("EXISTING_ORDER_STATE_NO_RESEND")
                raise ContractValidationError(
                    FailureCode.AMBIGUOUS_EXECUTION_STATE,
                    "existing order state prevents resend",
                )
            quote = quote_map.get(order.symbol)
            if not isinstance(quote, PaperMarketQuote):
                self._halt("MISSING_PAPER_QUOTE")
                raise ContractValidationError(
                    FailureCode.AMBIGUOUS_EXECUTION_STATE,
                    "missing frozen paper quote",
                    details={"symbol": order.symbol},
                )
            requested = Decimal(order.quantity)
            fraction_cap = self._floor_lot(
                requested * policy.base_fill_fraction, policy.lot_size
            )
            adv_cap = self._floor_lot(
                quote.adv_shares * policy.max_adv_participation, policy.lot_size
            )
            filled = min(requested, fraction_cap, adv_cap)
            if filled <= 0:
                self._order_states[order.client_order_id] = OrderExecutionStatus.BLOCKED
                self._halt("ZERO_EXECUTABLE_QUANTITY")
                raise ContractValidationError(
                    FailureCode.AMBIGUOUS_EXECUTION_STATE,
                    "paper order cannot fill one lot under declared limits",
                )
            prepared.append((order, quote, requested, filled))

        fills: list[PaperFill] = []
        for order, quote, requested, filled in prepared:
            event_time = self.clock.next()
            if event_time < quote.event_time:
                self._halt("CLOCK_PRECEDES_QUOTE")
                raise ContractValidationError(
                    FailureCode.INVALID_TIME_ORDER,
                    "deterministic execution clock precedes quote time",
                )
            participation = filled / quote.adv_shares
            impact_bps = policy.linear_impact_bps * participation
            half_spread_bps = quote.spread_bps / Decimal("2")
            direction = Decimal("1") if order.side == "BUY" else Decimal("-1")
            fill_price = quote.mid_price * (
                Decimal("1")
                + direction * (half_spread_bps + impact_bps) / Decimal("10000")
            )
            fill_price = fill_price.quantize(Decimal("0.00000001"))
            if fill_price <= 0:
                self._halt("NON_POSITIVE_FILL_PRICE")
                fail(FailureCode.INVALID_VALUE, "calculated fill price is not positive")
            notional_mid = filled * quote.mid_price
            spread_cost = notional_mid * half_spread_bps / Decimal("10000")
            impact_cost = notional_mid * impact_bps / Decimal("10000")
            fees = filled * fill_price * policy.commission_bps / Decimal("10000")
            remaining = requested - filled
            fill_hash = canonical_content_hash(
                {
                    "engine_id": self.engine_id,
                    "seed": self.seed,
                    "order_plan_hash": plan_hash,
                    "client_order_id": order.client_order_id,
                    "filled": filled,
                    "price": fill_price,
                    "event_time": event_time,
                }
            )
            event = FillEvent(
                fill_id="fill-" + fill_hash.removeprefix("sha256:")[:24],
                order_plan_hash=plan_hash,
                client_order_id=order.client_order_id,
                symbol=order.symbol,
                quantity=filled,
                price=fill_price,
                event_time=event_time,
                published_at=event_time,
                available_at=event_time,
                venue="PAPER_FIXTURE",
                environment="paper",
                produced_by=Component.EXECUTION_SERVICE,
            )
            status = FillStatus.FILLED if remaining == 0 else FillStatus.PARTIAL
            paper_fill = PaperFill(
                fill=event,
                side=order.side,
                requested_quantity=requested,
                filled_quantity=filled,
                remaining_quantity=remaining,
                mid_price=quote.mid_price,
                spread_cost=spread_cost,
                impact_cost=impact_cost,
                fees=fees,
                participation=participation,
                status=status,
            )
            fills.append(paper_fill)
            self._order_states[order.client_order_id] = (
                OrderExecutionStatus.FILLED
                if status is FillStatus.FILLED
                else OrderExecutionStatus.PARTIAL
            )
        self._executed_plans.add(plan_hash)
        return PaperExecutionResult(
            order_plan_hash=plan_hash,
            fills=tuple(fills),
            order_states={
                order.client_order_id: self._order_states[order.client_order_id]
                for order in batch.order_plan.orders
            },
            clock_start=self.clock.start,
            seed=self.seed,
            halted=False,
        )
