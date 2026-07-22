"""Provider protocols for shadow portfolio integration."""

from __future__ import annotations

from typing import Protocol

from .models import (
    ChallengerVersion,
    DataVintage,
    MarketPrice,
    ShadowRiskPolicy,
    ShadowRunRequest,
    TargetPosition,
)


class FrozenSnapshotProvider(Protocol):
    def get_vintage(self, request: ShadowRunRequest) -> DataVintage: ...


class ApprovedChallengerProvider(Protocol):
    def get_challengers(
        self, request: ShadowRunRequest, vintage: DataVintage
    ) -> tuple[ChallengerVersion, ...]: ...


class MarketPriceProvider(Protocol):
    def get_execution_price(
        self, symbol: str, request: ShadowRunRequest, vintage: DataVintage
    ) -> MarketPrice | None: ...

    def get_valuation_price(
        self, symbol: str, request: ShadowRunRequest, vintage: DataVintage
    ) -> MarketPrice | None: ...


class PortfolioConstructor(Protocol):
    def construct_targets(
        self,
        request: ShadowRunRequest,
        vintage: DataVintage,
        challengers: tuple[ChallengerVersion, ...],
        policy: ShadowRiskPolicy,
    ) -> tuple[TargetPosition, ...]: ...


class RiskPolicyReader(Protocol):
    def read_policy(self, request: ShadowRunRequest) -> ShadowRiskPolicy: ...


class Clock(Protocol):
    def now(self) -> object: ...
