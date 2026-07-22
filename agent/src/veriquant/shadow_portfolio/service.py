"""Deterministic run_once shadow portfolio service."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping

from ..contracts import ContractValidationError, FailureCode
from ..contracts.base import canonicalize, require_hash
from .ledger import ShadowLedger, _hash
from .models import (
    AlertSeverity,
    ChallengerState,
    ChallengerVersion,
    DataVintage,
    DriftAlert,
    FillSide,
    FillStatus,
    MarketPrice,
    PaperFill,
    PerformanceAttribution,
    ShadowRiskPolicy,
    ShadowRunManifest,
    ShadowRunRequest,
    ShadowStatus,
    TargetPosition,
)
from .protocols import (
    ApprovedChallengerProvider,
    Clock,
    FrozenSnapshotProvider,
    MarketPriceProvider,
    PortfolioConstructor,
    RiskPolicyReader,
)


def _fail(code: FailureCode, message: str, **details: Any) -> None:
    raise ContractValidationError(code, message, details=details)


@dataclass(frozen=True)
class DeterministicClock:
    value: datetime

    def now(self) -> datetime:
        return self.value


class EqualWeightPortfolioConstructor:
    """Simple deterministic constructor for shadow monitoring fixtures."""

    def construct_targets(
        self,
        request: ShadowRunRequest,
        vintage: DataVintage,
        challengers: tuple[ChallengerVersion, ...],
        policy: ShadowRiskPolicy,
    ) -> tuple[TargetPosition, ...]:
        scores: dict[str, float] = {symbol: 0.0 for symbol in vintage.universe}
        source: dict[str, ChallengerVersion] = {}
        for challenger in challengers:
            for symbol, score in challenger.scores_by_symbol.items():
                if symbol in scores:
                    scores[symbol] += score
                    source[symbol] = challenger
        active = {symbol: score for symbol, score in scores.items() if score != 0.0}
        if not active:
            _fail(FailureCode.INVALID_ALPHA_SOURCE, "no active CHALLENGER score for the vintage universe")
        gross = sum(abs(score) for score in active.values())
        targets: list[TargetPosition] = []
        for symbol in sorted(active):
            challenger = source[symbol]
            weight = request.max_gross_weight * active[symbol] / gross
            targets.append(
                TargetPosition(
                    symbol=symbol,
                    target_weight=weight,
                    target_quantity=0.0,
                    target_value=weight * request.starting_cash,
                    source_challenger_id=challenger.challenger_id,
                    source_challenger_version=challenger.version,
                )
            )
        return tuple(targets)


class ShadowPortfolioService:
    """Paper/shadow run_once pipeline with no broker or real account access."""

    def __init__(
        self,
        *,
        snapshot_provider: FrozenSnapshotProvider,
        challenger_provider: ApprovedChallengerProvider,
        price_provider: MarketPriceProvider,
        portfolio_constructor: PortfolioConstructor,
        risk_policy_reader: RiskPolicyReader,
        ledger: ShadowLedger,
        clock: Clock,
    ) -> None:
        self.snapshot_provider = snapshot_provider
        self.challenger_provider = challenger_provider
        self.price_provider = price_provider
        self.portfolio_constructor = portfolio_constructor
        self.risk_policy_reader = risk_policy_reader
        self.ledger = ledger
        self.clock = clock

    def run_once(self, request: ShadowRunRequest) -> ShadowRunManifest:
        policy = self.risk_policy_reader.read_policy(request)
        self._validate_policy(request, policy)
        vintage = self.snapshot_provider.get_vintage(request)
        self._validate_vintage(request, vintage)
        challengers = self.challenger_provider.get_challengers(request, vintage)
        self._validate_challengers(request, challengers, vintage)
        run_input_hash = _hash(
            {
                "request": request,
                "policy": policy,
                "vintage": vintage,
                "challengers": tuple(challenger.artifact_hash for challenger in challengers),
            }
        )
        existing = self.ledger.get_manifest(request.run_id, run_input_hash)
        if existing is not None:
            return existing
        targets = self.portfolio_constructor.construct_targets(request, vintage, challengers, policy)
        target_prices = self._execution_prices(request, vintage, targets)
        priced_targets = self._targets_with_quantities(request, targets, target_prices)
        fills = self._simulate_fills(request, vintage, priced_targets, target_prices)
        attribution = self._attribute(request, vintage, priced_targets, fills)
        alerts = self._drift_alerts(policy, vintage, challengers, attribution)

        self.ledger.append_payload(
            run_id=request.run_id,
            event_type="SNAPSHOT",
            event_time=vintage.data_available_time,
            payload=vintage,
        )
        self.ledger.append_payload(
            run_id=request.run_id,
            event_type="CHALLENGERS",
            event_time=request.decision_time,
            payload=tuple(challenger.artifact_hash for challenger in challengers),
        )
        self.ledger.append_payload(
            run_id=request.run_id,
            event_type="TARGETS",
            event_time=request.decision_time,
            payload=priced_targets,
        )
        self.ledger.append_payload(
            run_id=request.run_id,
            event_type="FILLS",
            event_time=request.execution_time,
            payload=fills,
        )
        self.ledger.append_payload(
            run_id=request.run_id,
            event_type="ATTRIBUTION",
            event_time=request.valuation_time,
            payload=attribution,
        )

        manifest = ShadowRunManifest(
            run_id=request.run_id,
            status=ShadowStatus.READY,
            request_hash=run_input_hash,
            vintage_hash=vintage.artifact_hash,
            challenger_hashes=tuple(challenger.artifact_hash for challenger in challengers),
            target_hash=_hash(priced_targets),
            fill_hash=_hash(fills),
            attribution_hash=attribution.artifact_hash,
            ledger_tip_hash=self.ledger.tip_hash,
            report_time=self._clock_time(),
            alerts=tuple(alerts),
        )
        committed = self.ledger.commit_manifest(request_hash=run_input_hash, manifest=manifest)
        return committed

    def _clock_time(self) -> datetime:
        value = self.clock.now()
        if not isinstance(value, datetime):
            _fail(FailureCode.INVALID_TIMESTAMP, "clock must return a datetime")
        return value

    @staticmethod
    def _validate_policy(request: ShadowRunRequest, policy: ShadowRiskPolicy) -> None:
        if policy.policy_version != request.policy_version or policy.policy_hash != request.policy_hash:
            _fail(FailureCode.RISK_INPUT_INVALID, "risk policy does not match request")

    @staticmethod
    def _validate_vintage(request: ShadowRunRequest, vintage: DataVintage) -> None:
        if vintage.vintage_id != request.vintage_id:
            _fail(FailureCode.RISK_INPUT_INVALID, "provider returned the wrong data vintage")
        if vintage.data_available_time > request.decision_time:
            _fail(FailureCode.INVALID_TIME_ORDER, "data vintage was not available at decision time")
        if vintage.corporate_action_available_time > request.decision_time:
            _fail(FailureCode.INVALID_TIME_ORDER, "corporate action data was not available at decision time")

    @staticmethod
    def _validate_challengers(
        request: ShadowRunRequest,
        challengers: tuple[ChallengerVersion, ...],
        vintage: DataVintage,
    ) -> None:
        if not challengers:
            _fail(FailureCode.INVALID_ALPHA_SOURCE, "no approved CHALLENGER versions")
        seen: set[str] = set()
        for challenger in challengers:
            key = f"{challenger.challenger_id}@{challenger.version}"
            if key in seen:
                _fail(FailureCode.INVALID_ALPHA_SOURCE, "duplicate challenger version", challenger=key)
            seen.add(key)
            if challenger.state is not ChallengerState.CHALLENGER:
                _fail(FailureCode.INVALID_ALPHA_SOURCE, "only CHALLENGER state may affect shadow positions")
            if challenger.policy_version != request.policy_version or challenger.policy_hash != request.policy_hash:
                _fail(FailureCode.INVALID_ALPHA_SOURCE, "challenger policy binding does not match request")
            if not (set(challenger.scores_by_symbol) & set(vintage.universe)):
                _fail(FailureCode.INVALID_ALPHA_SOURCE, "challenger has no score in current universe")

    def _execution_prices(
        self,
        request: ShadowRunRequest,
        vintage: DataVintage,
        targets: tuple[TargetPosition, ...],
    ) -> Mapping[str, MarketPrice]:
        prices: dict[str, MarketPrice] = {}
        for target in targets:
            quote = self.price_provider.get_execution_price(target.symbol, request, vintage)
            if quote is None:
                _fail(FailureCode.RISK_INPUT_INVALID, "missing execution price", symbol=target.symbol)
            self._validate_execution_quote(request, target.symbol, quote)
            prices[target.symbol] = quote
        return prices

    @staticmethod
    def _validate_execution_quote(request: ShadowRunRequest, symbol: str, quote: MarketPrice) -> None:
        if quote.symbol != symbol:
            _fail(FailureCode.RISK_INPUT_INVALID, "execution quote symbol mismatch")
        if quote.available_time > request.execution_time:
            _fail(FailureCode.INVALID_TIME_ORDER, "execution quote was unavailable at execution time")
        if quote.price_time > request.execution_time:
            _fail(FailureCode.INVALID_TIME_ORDER, "future price cannot be used for execution")
        if quote.price_kind == "mark":
            _fail(FailureCode.INVALID_TIME_ORDER, "valuation mark cannot be used for execution")
        if quote.suspended:
            _fail(FailureCode.AMBIGUOUS_EXECUTION_STATE, "suspended symbol cannot be filled", symbol=symbol)

    def _targets_with_quantities(
        self,
        request: ShadowRunRequest,
        targets: tuple[TargetPosition, ...],
        prices: Mapping[str, MarketPrice],
    ) -> tuple[TargetPosition, ...]:
        output: list[TargetPosition] = []
        for target in targets:
            current_quantity = request.previous_positions.get(target.symbol, 0.0)
            target_quantity = target.target_value / prices[target.symbol].price
            output.append(
                TargetPosition(
                    symbol=target.symbol,
                    target_weight=target.target_weight,
                    target_quantity=target_quantity - current_quantity,
                    target_value=target.target_value,
                    source_challenger_id=target.source_challenger_id,
                    source_challenger_version=target.source_challenger_version,
                )
            )
        return tuple(output)

    def _simulate_fills(
        self,
        request: ShadowRunRequest,
        vintage: DataVintage,
        targets: tuple[TargetPosition, ...],
        prices: Mapping[str, MarketPrice],
    ) -> tuple[PaperFill, ...]:
        fills: list[PaperFill] = []
        for target in targets:
            quantity = target.target_quantity
            if quantity == 0.0:
                continue
            side = FillSide.BUY if quantity > 0 else FillSide.SELL
            quote = prices[target.symbol]
            if side is FillSide.BUY and quote.limit_up:
                _fail(FailureCode.AMBIGUOUS_EXECUTION_STATE, "buy blocked by limit-up", symbol=target.symbol)
            if side is FillSide.SELL and quote.limit_down:
                _fail(FailureCode.AMBIGUOUS_EXECUTION_STATE, "sell blocked by limit-down", symbol=target.symbol)
            requested = abs(quantity)
            adv = vintage.adv_by_symbol.get(target.symbol)
            if adv is None or adv <= 0:
                _fail(FailureCode.RISK_INPUT_INVALID, "missing or invalid ADV", symbol=target.symbol)
            filled = min(requested, adv * request.max_adv_participation)
            if filled <= 0:
                _fail(FailureCode.AMBIGUOUS_EXECUTION_STATE, "zero fill after ADV cap", symbol=target.symbol)
            signed = 1.0 if side is FillSide.BUY else -1.0
            slippage = quote.price * request.slippage_bps / 10000.0 * signed
            execution_price = quote.price + slippage
            if execution_price <= 0:
                _fail(FailureCode.RISK_INPUT_INVALID, "non-positive execution price")
            notional = filled * execution_price
            fills.append(
                PaperFill(
                    fill_id=_fill_id(request.run_id, target.symbol, side.value, requested, quote.artifact_hash),
                    run_id=request.run_id,
                    symbol=target.symbol,
                    side=side,
                    requested_quantity=requested,
                    filled_quantity=filled,
                    execution_price=execution_price,
                    execution_time=request.execution_time,
                    transaction_cost=abs(notional) * request.transaction_cost_bps / 10000.0,
                    slippage_cost=filled * abs(slippage),
                    status=FillStatus.FILLED if math.isclose(filled, requested) else FillStatus.PARTIAL,
                )
            )
        if not fills:
            _fail(FailureCode.AMBIGUOUS_EXECUTION_STATE, "no paper fills generated")
        return tuple(fills)

    def _attribute(
        self,
        request: ShadowRunRequest,
        vintage: DataVintage,
        targets: tuple[TargetPosition, ...],
        fills: tuple[PaperFill, ...],
    ) -> PerformanceAttribution:
        positions = dict(request.previous_positions)
        cash = request.starting_cash
        transaction_cost = 0.0
        slippage_cost = 0.0
        gross_trade_value = 0.0
        for fill in fills:
            signed_qty = fill.filled_quantity if fill.side is FillSide.BUY else -fill.filled_quantity
            positions[fill.symbol] = positions.get(fill.symbol, 0.0) + signed_qty
            cash -= signed_qty * fill.execution_price
            cash -= fill.transaction_cost
            transaction_cost += fill.transaction_cost
            slippage_cost += fill.slippage_cost
            gross_trade_value += abs(signed_qty * fill.execution_price)
        mark_value = 0.0
        challenger_contribution: dict[str, float] = {}
        target_by_symbol = {target.symbol: target for target in targets}
        for symbol, quantity in positions.items():
            quote = self.price_provider.get_valuation_price(symbol, request, vintage)
            if quote is None:
                _fail(FailureCode.RISK_INPUT_INVALID, "missing valuation price", symbol=symbol)
            self._validate_valuation_quote(request, symbol, quote)
            value = quantity * quote.price
            mark_value += value
            source = target_by_symbol.get(symbol)
            if source is not None:
                key = f"{source.source_challenger_id}@{source.source_challenger_version}"
                challenger_contribution[key] = challenger_contribution.get(key, 0.0) + value / request.starting_cash
        nav_end = cash + mark_value
        gross_pnl = nav_end - request.starting_cash + transaction_cost
        net_pnl = nav_end - request.starting_cash
        weights = {
            symbol: (quantity * self.price_provider.get_valuation_price(symbol, request, vintage).price) / nav_end
            for symbol, quantity in positions.items()
            if nav_end != 0 and self.price_provider.get_valuation_price(symbol, request, vintage) is not None
        }
        industry_exposure: dict[str, float] = {}
        beta_exposure = 0.0
        for symbol, weight in weights.items():
            beta_exposure += weight * vintage.beta_by_symbol.get(symbol, 0.0)
            industry = vintage.industry_by_symbol.get(symbol, "UNKNOWN")
            industry_exposure[industry] = industry_exposure.get(industry, 0.0) + weight
        coverage = sum(vintage.coverage_by_symbol.get(symbol, 0.0) for symbol in vintage.universe) / len(vintage.universe)
        return PerformanceAttribution(
            nav_start=request.starting_cash,
            nav_end=nav_end,
            gross_pnl=gross_pnl,
            net_pnl=net_pnl,
            transaction_cost=transaction_cost,
            slippage_cost=slippage_cost,
            return_pct=net_pnl / request.starting_cash,
            turnover=gross_trade_value / request.starting_cash,
            gross_exposure=sum(abs(weight) for weight in weights.values()),
            beta_exposure=beta_exposure,
            industry_exposure=industry_exposure,
            challenger_contribution=challenger_contribution,
            data_coverage=coverage,
        )

    @staticmethod
    def _validate_valuation_quote(request: ShadowRunRequest, symbol: str, quote: MarketPrice) -> None:
        if quote.symbol != symbol:
            _fail(FailureCode.RISK_INPUT_INVALID, "valuation quote symbol mismatch")
        if quote.available_time > request.valuation_time:
            _fail(FailureCode.INVALID_TIME_ORDER, "valuation quote unavailable at valuation time")
        if quote.price_time > request.valuation_time:
            _fail(FailureCode.INVALID_TIME_ORDER, "future price cannot be used for valuation")
        if quote.suspended:
            _fail(FailureCode.AMBIGUOUS_EXECUTION_STATE, "suspended symbol cannot be valued", symbol=symbol)

    @staticmethod
    def _drift_alerts(
        policy: ShadowRiskPolicy,
        vintage: DataVintage,
        challengers: tuple[ChallengerVersion, ...],
        attribution: PerformanceAttribution,
    ) -> list[DriftAlert]:
        alerts: list[DriftAlert] = []
        scores = [score for challenger in challengers for score in challenger.scores_by_symbol.values()]
        current_mean = sum(scores) / len(scores)
        baseline_mean = sum(challenger.baseline_factor_mean for challenger in challengers) / len(challengers)
        mean_shift = abs(current_mean - baseline_mean)
        if mean_shift > policy.max_factor_mean_shift:
            alerts.append(_alert("factor_distribution_drift", "factor_mean_shift", mean_shift, policy.max_factor_mean_shift))
        ic_decay = max(
            abs(challenger.baseline_ic_proxy - attribution.return_pct)
            for challenger in challengers
        )
        if ic_decay > policy.max_ic_proxy_decay:
            alerts.append(_alert("ic_return_proxy_drift", "ic_proxy_decay", ic_decay, policy.max_ic_proxy_decay))
        if attribution.turnover > policy.max_turnover_alert:
            alerts.append(_alert("turnover_drift", "turnover", attribution.turnover, policy.max_turnover_alert))
        if abs(attribution.beta_exposure) > policy.max_abs_beta_alert:
            alerts.append(_alert("risk_exposure_drift", "beta_exposure", abs(attribution.beta_exposure), policy.max_abs_beta_alert))
        max_industry = max((abs(value) for value in attribution.industry_exposure.values()), default=0.0)
        if max_industry > policy.max_abs_industry_alert:
            alerts.append(_alert("risk_exposure_drift", "industry_exposure", max_industry, policy.max_abs_industry_alert))
        if attribution.data_coverage < policy.min_data_coverage:
            alerts.append(
                DriftAlert(
                    alert_type="data_coverage_change",
                    severity=AlertSeverity.WARN,
                    metric_name="data_coverage",
                    observed_value=attribution.data_coverage,
                    threshold=policy.min_data_coverage,
                    recommendation="Review data vintage before using shadow result.",
                )
            )
        for key, contribution in attribution.challenger_contribution.items():
            if contribution < policy.min_challenger_contribution:
                alerts.append(
                    DriftAlert(
                        alert_type="challenger_contribution",
                        severity=AlertSeverity.WARN,
                        metric_name=key,
                        observed_value=contribution,
                        threshold=policy.min_challenger_contribution,
                        recommendation="Review challenger contribution; alert is not an automatic demotion.",
                    )
                )
        return alerts


def _alert(alert_type: str, metric: str, observed: float, threshold: float) -> DriftAlert:
    return DriftAlert(
        alert_type=alert_type,
        severity=AlertSeverity.WARN,
        metric_name=metric,
        observed_value=observed,
        threshold=threshold,
        recommendation="Investigate drift; do not auto-promote or auto-demote.",
    )


def _fill_id(run_id: str, symbol: str, side: str, requested: float, quote_hash: str) -> str:
    require_hash(quote_hash, "quote_hash")
    raw = _hash({"run_id": run_id, "symbol": symbol, "side": side, "requested": requested, "quote": quote_hash})
    return "fill:" + raw.split(":", 1)[1][:32]
