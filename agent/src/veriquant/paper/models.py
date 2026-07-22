"""Canonical paper-execution, journal, and reconciliation value objects."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum
from types import MappingProxyType
from typing import Mapping

from ..contracts import (
    CanonicalContract,
    Component,
    FailureCode,
    FillEvent,
    OrderPlan,
    PortfolioPlan,
    RiskDecision,
    RiskVerdict,
)
from ..contracts.base import (
    fail,
    require_aware_datetime,
    require_finite_number,
    require_hash,
    require_identifier,
    require_text,
)


class FillStatus(str, Enum):
    PARTIAL = "PARTIAL"
    FILLED = "FILLED"


class OrderExecutionStatus(str, Enum):
    NEW = "NEW"
    PARTIAL = "PARTIAL"
    FILLED = "FILLED"
    AMBIGUOUS = "AMBIGUOUS"
    BLOCKED = "BLOCKED"


class JournalEventType(str, Enum):
    OPENING_CASH = "OPENING_CASH"
    CASH = "CASH"
    POSITION = "POSITION"
    FEES = "FEES"
    BORROW = "BORROW"
    FINANCING = "FINANCING"
    REALIZED_PNL = "REALIZED_PNL"
    UNREALIZED_PNL = "UNREALIZED_PNL"
    SPREAD_COST = "SPREAD_COST"
    IMPACT_COST = "IMPACT_COST"
    HALT = "HALT"


class ReconciliationStatus(str, Enum):
    RECONCILED = "RECONCILED"
    BLOCKED = "BLOCKED"
    HALT = "HALT"


def _freeze_decimal_mapping(
    owner: object, field_name: str, values: Mapping[str, Decimal]
) -> None:
    if not isinstance(values, Mapping):
        fail(FailureCode.INVALID_VALUE, f"{field_name} must be a mapping")
    frozen: dict[str, Decimal] = {}
    for key, value in values.items():
        require_identifier(key, f"{field_name}.key")
        require_finite_number(value, f"{field_name}.{key}")
        frozen[key] = Decimal(value)
    object.__setattr__(owner, field_name, MappingProxyType(frozen))


def _freeze_float_mapping(owner: object, field_name: str, values: Mapping[str, float]) -> None:
    if not isinstance(values, Mapping):
        fail(FailureCode.INVALID_VALUE, f"{field_name} must be a mapping")
    frozen: dict[str, float] = {}
    for key, value in values.items():
        require_identifier(key, f"{field_name}.key")
        require_finite_number(value, f"{field_name}.{key}")
        frozen[key] = float(value)
    object.__setattr__(owner, field_name, MappingProxyType(frozen))


@dataclass(frozen=True, kw_only=True)
class PaperMarketQuote(CanonicalContract):
    symbol: str
    mid_price: Decimal
    spread_bps: Decimal
    adv_shares: Decimal
    event_time: datetime

    def __post_init__(self) -> None:
        require_identifier(self.symbol, "symbol")
        for name in ("mid_price", "spread_bps", "adv_shares"):
            require_finite_number(getattr(self, name), name)
        if self.mid_price <= 0 or self.adv_shares <= 0 or self.spread_bps < 0:
            fail(FailureCode.INVALID_VALUE, "quote price/ADV must be positive and spread non-negative")
        require_aware_datetime(self.event_time, "event_time")


@dataclass(frozen=True, kw_only=True)
class PaperExecutionPolicy(CanonicalContract):
    max_adv_participation: Decimal
    base_fill_fraction: Decimal
    linear_impact_bps: Decimal
    commission_bps: Decimal
    lot_size: Decimal = Decimal("1")

    def __post_init__(self) -> None:
        for name in (
            "max_adv_participation",
            "base_fill_fraction",
            "linear_impact_bps",
            "commission_bps",
            "lot_size",
        ):
            require_finite_number(getattr(self, name), name)
        if not Decimal("0") < self.max_adv_participation <= Decimal("1"):
            fail(FailureCode.INVALID_VALUE, "max_adv_participation must be in (0,1]")
        if not Decimal("0") < self.base_fill_fraction <= Decimal("1"):
            fail(FailureCode.INVALID_VALUE, "base_fill_fraction must be in (0,1]")
        if self.linear_impact_bps < 0 or self.commission_bps < 0 or self.lot_size <= 0:
            fail(FailureCode.INVALID_VALUE, "impact/commission cannot be negative and lot must be positive")


@dataclass(frozen=True, kw_only=True)
class PaperOrderBatch(CanonicalContract):
    portfolio_plan: PortfolioPlan
    risk_decision: RiskDecision
    order_plan: OrderPlan
    effective_target_weights: Mapping[str, float]
    capital: Decimal
    prices: Mapping[str, Decimal]
    created_at: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.portfolio_plan, PortfolioPlan):
            fail(FailureCode.INVALID_VALUE, "portfolio_plan has the wrong type")
        if not isinstance(self.risk_decision, RiskDecision):
            fail(FailureCode.INVALID_VALUE, "risk_decision has the wrong type")
        if not isinstance(self.order_plan, OrderPlan):
            fail(FailureCode.INVALID_VALUE, "order_plan has the wrong type")
        if self.risk_decision.verdict not in {RiskVerdict.APPROVE, RiskVerdict.SCALE_DOWN}:
            fail(FailureCode.RISK_NOT_APPROVED, "only APPROVE or SCALE_DOWN may create paper orders")
        if self.risk_decision.portfolio_plan_hash != self.portfolio_plan.artifact_hash:
            fail(FailureCode.INVALID_HASH, "risk decision does not bind portfolio plan")
        if self.order_plan.portfolio_plan_hash != self.portfolio_plan.artifact_hash:
            fail(FailureCode.INVALID_HASH, "order plan does not bind portfolio plan")
        if self.order_plan.risk_decision_hash != self.risk_decision.artifact_hash:
            fail(FailureCode.INVALID_HASH, "order plan does not bind risk decision")
        if self.order_plan.environment != "paper":
            fail(FailureCode.INVALID_PAPER_ENVIRONMENT, "paper batch cannot contain a live order plan")
        _freeze_float_mapping(self, "effective_target_weights", self.effective_target_weights)
        _freeze_decimal_mapping(self, "prices", self.prices)
        if set(self.effective_target_weights) != set(self.portfolio_plan.universe):
            fail(FailureCode.INVALID_VALUE, "effective weights do not match universe")
        if set(self.prices) != set(self.portfolio_plan.universe):
            fail(FailureCode.INVALID_VALUE, "prices do not match universe")
        require_finite_number(self.capital, "capital")
        if self.capital <= 0 or any(price <= 0 for price in self.prices.values()):
            fail(FailureCode.INVALID_VALUE, "capital and prices must be positive")
        require_aware_datetime(self.created_at, "created_at")


@dataclass(frozen=True, kw_only=True)
class PaperFill(CanonicalContract):
    fill: FillEvent
    side: str
    requested_quantity: Decimal
    filled_quantity: Decimal
    remaining_quantity: Decimal
    mid_price: Decimal
    spread_cost: Decimal
    impact_cost: Decimal
    fees: Decimal
    participation: Decimal
    status: FillStatus

    def __post_init__(self) -> None:
        if not isinstance(self.fill, FillEvent):
            fail(FailureCode.INVALID_VALUE, "fill must be FillEvent")
        if self.side not in {"BUY", "SELL"}:
            fail(FailureCode.INVALID_VALUE, "side must be BUY or SELL")
        for name in (
            "requested_quantity",
            "filled_quantity",
            "remaining_quantity",
            "mid_price",
            "spread_cost",
            "impact_cost",
            "fees",
            "participation",
        ):
            require_finite_number(getattr(self, name), name)
        if self.requested_quantity <= 0 or self.filled_quantity <= 0 or self.remaining_quantity < 0:
            fail(FailureCode.INVALID_VALUE, "fill quantities are invalid")
        if self.requested_quantity != self.filled_quantity + self.remaining_quantity:
            fail(FailureCode.CONSTRAINT_VIOLATION, "partial-fill quantity conservation failed")
        if self.fill.quantity != self.filled_quantity:
            fail(FailureCode.INVALID_VALUE, "FillEvent quantity does not match PaperFill")
        if self.mid_price <= 0 or min(self.spread_cost, self.impact_cost, self.fees) < 0:
            fail(FailureCode.INVALID_VALUE, "fill costs/prices are invalid")
        if not Decimal("0") < self.participation <= Decimal("1"):
            fail(FailureCode.INVALID_VALUE, "participation must be in (0,1]")
        expected = FillStatus.FILLED if self.remaining_quantity == 0 else FillStatus.PARTIAL
        if self.status is not expected:
            fail(FailureCode.INVALID_STATE, "fill status does not match remaining quantity")
        if self.fill.environment != "paper":
            fail(FailureCode.INVALID_PAPER_ENVIRONMENT, "only paper fills are accepted")


@dataclass(frozen=True, kw_only=True)
class PaperExecutionResult(CanonicalContract):
    order_plan_hash: str
    fills: tuple[PaperFill, ...]
    order_states: Mapping[str, OrderExecutionStatus]
    clock_start: datetime
    seed: int
    halted: bool = False

    def __post_init__(self) -> None:
        require_hash(self.order_plan_hash, "order_plan_hash")
        if not isinstance(self.fills, tuple) or not self.fills:
            fail(FailureCode.INVALID_VALUE, "execution result requires fills")
        if not all(isinstance(fill, PaperFill) for fill in self.fills):
            fail(FailureCode.INVALID_VALUE, "execution result contains invalid fills")
        if not isinstance(self.order_states, Mapping):
            fail(FailureCode.INVALID_VALUE, "order_states must be a mapping")
        frozen: dict[str, OrderExecutionStatus] = {}
        for key, value in self.order_states.items():
            require_identifier(key, "client_order_id")
            if not isinstance(value, OrderExecutionStatus):
                fail(FailureCode.INVALID_VALUE, "unknown order state")
            frozen[key] = value
        object.__setattr__(self, "order_states", MappingProxyType(frozen))
        require_aware_datetime(self.clock_start, "clock_start")
        if isinstance(self.seed, bool) or not isinstance(self.seed, int) or self.seed < 0:
            fail(FailureCode.INVALID_VALUE, "seed must be a non-negative integer")
        if self.halted:
            fail(FailureCode.INVALID_STATE, "successful execution result cannot be halted")


@dataclass(frozen=True, kw_only=True)
class JournalEvent(CanonicalContract):
    event_id: str
    sequence: int
    event_type: JournalEventType
    event_time: datetime
    source_id: str
    source_hash: str
    amount: Decimal
    quantity: Decimal
    symbol: str | None
    price: Decimal | None
    previous_event_hash: str | None
    produced_by: Component = Component.LEDGER_SERVICE

    def __post_init__(self) -> None:
        require_identifier(self.event_id, "event_id")
        if isinstance(self.sequence, bool) or not isinstance(self.sequence, int) or self.sequence < 1:
            fail(FailureCode.INVALID_VALUE, "journal sequence must be positive")
        if not isinstance(self.event_type, JournalEventType):
            fail(FailureCode.INVALID_VALUE, "unknown journal event type")
        require_aware_datetime(self.event_time, "event_time")
        require_identifier(self.source_id, "source_id")
        require_hash(self.source_hash, "source_hash")
        require_finite_number(self.amount, "amount")
        require_finite_number(self.quantity, "quantity")
        if self.symbol is not None:
            require_identifier(self.symbol, "symbol")
        if self.price is not None:
            require_finite_number(self.price, "price")
            if self.price <= 0:
                fail(FailureCode.INVALID_VALUE, "journal price must be positive")
        if self.previous_event_hash is not None:
            require_hash(self.previous_event_hash, "previous_event_hash")
        if self.produced_by is not Component.LEDGER_SERVICE:
            fail(FailureCode.INVALID_ARTIFACT_PRODUCER, "journal belongs to Ledger Service")


@dataclass(frozen=True, kw_only=True)
class JournalState(CanonicalContract):
    opening_cash: Decimal
    cash: Decimal
    positions: Mapping[str, Decimal]
    average_cost: Mapping[str, Decimal]
    latest_prices: Mapping[str, Decimal]
    fees: Decimal
    borrow: Decimal
    financing: Decimal
    realized_pnl: Decimal
    unrealized_pnl: Decimal
    spread_cost: Decimal
    impact_cost: Decimal
    realized_by_symbol: Mapping[str, Decimal]
    unrealized_by_symbol: Mapping[str, Decimal]
    event_count: int
    last_event_hash: str
    halted: bool

    def __post_init__(self) -> None:
        for name in (
            "opening_cash",
            "cash",
            "fees",
            "borrow",
            "financing",
            "realized_pnl",
            "unrealized_pnl",
            "spread_cost",
            "impact_cost",
        ):
            require_finite_number(getattr(self, name), name)
        for name in (
            "positions",
            "average_cost",
            "latest_prices",
            "realized_by_symbol",
            "unrealized_by_symbol",
        ):
            _freeze_decimal_mapping(self, name, getattr(self, name))
        if isinstance(self.event_count, bool) or not isinstance(self.event_count, int) or self.event_count < 1:
            fail(FailureCode.INVALID_VALUE, "journal state requires at least opening event")
        require_hash(self.last_event_hash, "last_event_hash")
        if type(self.halted) is not bool:
            fail(FailureCode.INVALID_VALUE, "halted must be boolean")

    @property
    def market_value(self) -> Decimal:
        return sum(
            (quantity * self.latest_prices.get(symbol, self.average_cost.get(symbol, Decimal("0"))))
            for symbol, quantity in self.positions.items()
        )

    @property
    def equity(self) -> Decimal:
        return self.cash + self.market_value

    @property
    def expected_equity(self) -> Decimal:
        return (
            self.opening_cash
            + self.realized_pnl
            + self.unrealized_pnl
            - self.fees
            - self.borrow
            - self.financing
        )


@dataclass(frozen=True, kw_only=True)
class ReconciliationReport(CanonicalContract):
    report_id: str
    journal_hash: str
    status: ReconciliationStatus
    actual_equity: Decimal
    expected_equity: Decimal
    difference: Decimal
    breaches: tuple[str, ...]
    reconciled_at: datetime
    produced_by: Component = Component.LEDGER_SERVICE

    def __post_init__(self) -> None:
        require_identifier(self.report_id, "report_id")
        require_hash(self.journal_hash, "journal_hash")
        if not isinstance(self.status, ReconciliationStatus):
            fail(FailureCode.INVALID_VALUE, "unknown reconciliation status")
        for name in ("actual_equity", "expected_equity", "difference"):
            require_finite_number(getattr(self, name), name)
        if not isinstance(self.breaches, tuple) or any(not item for item in self.breaches):
            fail(FailureCode.INVALID_VALUE, "breaches must be a tuple of codes")
        if self.status is ReconciliationStatus.RECONCILED and self.breaches:
            fail(FailureCode.INVALID_STATE, "reconciled report cannot contain breaches")
        if self.status is not ReconciliationStatus.RECONCILED and not self.breaches:
            fail(FailureCode.INVALID_STATE, "blocked/halt report requires breaches")
        require_aware_datetime(self.reconciled_at, "reconciled_at")
        if self.produced_by is not Component.LEDGER_SERVICE:
            fail(FailureCode.INVALID_ARTIFACT_PRODUCER, "reconciliation belongs to Ledger Service")
