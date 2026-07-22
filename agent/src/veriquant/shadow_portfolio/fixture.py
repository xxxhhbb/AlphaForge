"""Offline fixture providers for shadow portfolio tests and integration smoke."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Mapping

from ..contracts.base import canonicalize
from .ledger import ShadowLedger, _hash
from .models import (
    ChallengerState,
    ChallengerVersion,
    DataVintage,
    MarketPrice,
    ShadowRiskPolicy,
    ShadowRunRequest,
)
from .service import DeterministicClock, EqualWeightPortfolioConstructor, ShadowPortfolioService


def _dt(hour: int) -> datetime:
    return datetime(2026, 1, 5, hour, 0, tzinfo=timezone.utc)


POLICY_HASH = _hash({"policy": "shadow", "version": "policy.v1"})


@dataclass
class InMemoryFrozenSnapshotProvider:
    vintages: Mapping[str, DataVintage]

    def get_vintage(self, request: ShadowRunRequest) -> DataVintage:
        return self.vintages[request.vintage_id]


@dataclass
class InMemoryApprovedChallengerProvider:
    challengers: tuple[ChallengerVersion, ...]

    def get_challengers(
        self, request: ShadowRunRequest, vintage: DataVintage
    ) -> tuple[ChallengerVersion, ...]:
        return self.challengers


@dataclass
class InMemoryMarketPriceProvider:
    execution_prices: Mapping[str, MarketPrice]
    valuation_prices: Mapping[str, MarketPrice]

    def get_execution_price(
        self, symbol: str, request: ShadowRunRequest, vintage: DataVintage
    ) -> MarketPrice | None:
        return self.execution_prices.get(symbol)

    def get_valuation_price(
        self, symbol: str, request: ShadowRunRequest, vintage: DataVintage
    ) -> MarketPrice | None:
        return self.valuation_prices.get(symbol)


@dataclass
class StaticRiskPolicyReader:
    policy: ShadowRiskPolicy

    def read_policy(self, request: ShadowRunRequest) -> ShadowRiskPolicy:
        return self.policy


@dataclass
class ShadowPortfolioFixture:
    service: ShadowPortfolioService
    request: ShadowRunRequest
    ledger: ShadowLedger
    snapshot_provider: InMemoryFrozenSnapshotProvider
    challenger_provider: InMemoryApprovedChallengerProvider
    price_provider: InMemoryMarketPriceProvider
    policy: ShadowRiskPolicy


def make_vintage(*, revision_id: str = "rev1", vintage_id: str = "vintage-2026-01-05") -> DataVintage:
    return DataVintage(
        vintage_id=vintage_id,
        revision_id=revision_id,
        dataset_hash=_hash({"dataset": "shadow-fixture", "revision": revision_id}),
        universe=("AAA", "BBB", "CCC"),
        data_available_time=_dt(8),
        decision_time=_dt(9),
        corporate_action_available_time=_dt(8),
        coverage_by_symbol={"AAA": 1.0, "BBB": 0.95, "CCC": 0.90},
        beta_by_symbol={"AAA": 0.8, "BBB": -0.2, "CCC": 0.1},
        industry_by_symbol={"AAA": "tech", "BBB": "health", "CCC": "finance"},
        adv_by_symbol={"AAA": 100000.0, "BBB": 100000.0, "CCC": 100000.0},
    )


def make_challenger(*, version: str = "v1", state: ChallengerState = ChallengerState.CHALLENGER) -> ChallengerVersion:
    return ChallengerVersion(
        challenger_id="challenger.alpha",
        version=version,
        state=state,
        policy_version="policy.v1",
        policy_hash=POLICY_HASH,
        promotion_decision_hash=_hash({"decision": "promote", "version": version}),
        evidence_hash=_hash({"evidence": "official-visible", "version": version}),
        factor_hash=_hash({"factor": "score", "version": version}),
        scores_by_symbol={"AAA": 0.7, "BBB": -0.3, "CCC": 0.2 if version == "v1" else 0.5},
        baseline_factor_mean=0.2,
        baseline_factor_std=0.4,
        baseline_ic_proxy=0.01,
        expected_contribution=0.01,
    )


def make_prices(
    *,
    execution_time: datetime | None = None,
    valuation_time: datetime | None = None,
    missing_symbol: str | None = None,
    suspended_symbol: str | None = None,
    limit_up_symbol: str | None = None,
) -> InMemoryMarketPriceProvider:
    execution_time = execution_time or _dt(10)
    valuation_time = valuation_time or _dt(15)
    execution: dict[str, MarketPrice] = {}
    valuation: dict[str, MarketPrice] = {}
    base = {"AAA": 10.0, "BBB": 20.0, "CCC": 30.0}
    for symbol, price in base.items():
        if symbol != missing_symbol:
            execution[symbol] = MarketPrice(
                symbol=symbol,
                price=price,
                price_time=execution_time,
                available_time=execution_time,
                price_kind="open",
                suspended=symbol == suspended_symbol,
                limit_up=symbol == limit_up_symbol,
            )
        valuation[symbol] = MarketPrice(
            symbol=symbol,
            price=price * 1.01,
            price_time=valuation_time,
            available_time=valuation_time,
            price_kind="mark",
            suspended=symbol == suspended_symbol,
        )
    return InMemoryMarketPriceProvider(execution_prices=execution, valuation_prices=valuation)


def build_shadow_portfolio_fixture(
    *,
    challenger: ChallengerVersion | None = None,
    vintage: DataVintage | None = None,
    price_provider: InMemoryMarketPriceProvider | None = None,
    ledger: ShadowLedger | None = None,
    run_id: str = "shadow-run-1",
) -> ShadowPortfolioFixture:
    vintage = vintage or make_vintage()
    challenger = challenger or make_challenger()
    policy = ShadowRiskPolicy(policy_version="policy.v1", policy_hash=POLICY_HASH)
    request = ShadowRunRequest(
        run_id=run_id,
        account_id="shadow-account",
        vintage_id=vintage.vintage_id,
        policy_version=policy.policy_version,
        policy_hash=policy.policy_hash,
        decision_time=_dt(9),
        execution_time=_dt(10),
        valuation_time=_dt(15),
        starting_cash=100000.0,
        previous_positions={},
        seed=7,
    )
    ledger = ledger or ShadowLedger()
    service = ShadowPortfolioService(
        snapshot_provider=InMemoryFrozenSnapshotProvider({vintage.vintage_id: vintage}),
        challenger_provider=InMemoryApprovedChallengerProvider((challenger,)),
        price_provider=price_provider or make_prices(),
        portfolio_constructor=EqualWeightPortfolioConstructor(),
        risk_policy_reader=StaticRiskPolicyReader(policy),
        ledger=ledger,
        clock=DeterministicClock(_dt(16)),
    )
    return ShadowPortfolioFixture(
        service=service,
        request=request,
        ledger=ledger,
        snapshot_provider=service.snapshot_provider,  # type: ignore[arg-type]
        challenger_provider=service.challenger_provider,  # type: ignore[arg-type]
        price_provider=service.price_provider,  # type: ignore[arg-type]
        policy=policy,
    )
