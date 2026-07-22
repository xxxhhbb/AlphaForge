"""Deterministic PnL, risk, cost, and factor attribution for paper journal state."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Mapping, Sequence

from ..contracts import AttributionReport, Component, FailureCode, PortfolioPlan, ResearchState
from ..contracts.base import fail, require_aware_datetime, require_finite_number
from ..data import canonical_content_hash
from ..downstream import AssetRiskInput
from .models import JournalState


class PaperAttributionService:
    service_id = "paper-attribution-service-v1"

    def attribute(
        self,
        *,
        state: JournalState,
        portfolio_plan: PortfolioPlan,
        assets: Sequence[AssetRiskInput],
        factor_loadings: Mapping[str, Mapping[str, float]],
        period_start: datetime,
        period_end: datetime,
    ) -> AttributionReport:
        if not isinstance(state, JournalState):
            fail(FailureCode.INVALID_VALUE, "state must be JournalState")
        if not isinstance(portfolio_plan, PortfolioPlan):
            fail(FailureCode.INVALID_VALUE, "portfolio_plan has the wrong type")
        require_aware_datetime(period_start, "period_start")
        require_aware_datetime(period_end, "period_end")
        if period_start > period_end:
            fail(FailureCode.INVALID_TIME_ORDER, "period_start follows period_end")
        by_symbol = {asset.symbol: asset for asset in assets}
        if set(by_symbol) != set(portfolio_plan.universe):
            fail(FailureCode.INVALID_VALUE, "attribution assets must match portfolio universe")
        symbol_pnl = {
            symbol: state.realized_by_symbol.get(symbol, Decimal("0"))
            + state.unrealized_by_symbol.get(symbol, Decimal("0"))
            for symbol in portfolio_plan.universe
        }
        factor_contributions: dict[str, float] = {}
        for factor, loadings in sorted(factor_loadings.items()):
            if set(loadings) != set(portfolio_plan.universe):
                fail(FailureCode.INVALID_VALUE, "factor loadings must match portfolio universe")
            contribution = Decimal("0")
            for symbol in portfolio_plan.universe:
                loading = loadings[symbol]
                require_finite_number(loading, f"factor_loadings.{factor}.{symbol}")
                contribution += symbol_pnl[symbol] * Decimal(str(loading))
            factor_contributions[factor] = float(contribution)
        risk_components: dict[str, float] = {}
        total_variance = Decimal("0")
        capital_base = max(abs(state.opening_cash), Decimal("0.00000001"))
        for symbol in portfolio_plan.universe:
            asset = by_symbol[symbol]
            price = state.latest_prices.get(symbol, state.average_cost.get(symbol, Decimal("0")))
            weight = state.positions.get(symbol, Decimal("0")) * price / capital_base
            contribution = weight * weight * Decimal(str(asset.volatility**2))
            risk_components[f"asset:{symbol}"] = float(contribution)
            total_variance += contribution
        risk_components["total_variance"] = float(total_variance)
        cost_components = {
            "fees": float(state.fees),
            "borrow": float(state.borrow),
            "financing": float(state.financing),
            "spread": float(state.spread_cost),
            "impact": float(state.impact_cost),
            "total_cash_cost": float(state.fees + state.borrow + state.financing),
        }
        pnl_components = {
            "realized": float(state.realized_pnl),
            "unrealized": float(state.unrealized_pnl),
            "fees": -float(state.fees),
            "borrow": -float(state.borrow),
            "financing": -float(state.financing),
            "total": float(
                state.realized_pnl
                + state.unrealized_pnl
                - state.fees
                - state.borrow
                - state.financing
            ),
        }
        report_material = canonical_content_hash(
            {
                "service_id": self.service_id,
                "ledger_snapshot_hash": state.artifact_hash,
                "portfolio_plan_hash": portfolio_plan.artifact_hash,
                "period_start": period_start,
                "period_end": period_end,
                "pnl_components": pnl_components,
                "factor_contributions": factor_contributions,
                "risk_components": risk_components,
                "cost_components": cost_components,
            }
        )
        return AttributionReport(
            report_id="attribution-" + report_material.removeprefix("sha256:")[:24],
            ledger_snapshot_hash=state.artifact_hash,
            portfolio_plan_hash=portfolio_plan.artifact_hash,
            period_start=period_start,
            period_end=period_end,
            decision_time=period_end,
            pnl_components=pnl_components,
            factor_contributions=factor_contributions,
            risk_components=risk_components,
            cost_components=cost_components,
            produced_by=Component.ATTRIBUTION_SERVICE,
            state=ResearchState.RETIRED,
        )
