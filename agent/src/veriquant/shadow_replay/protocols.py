"""Narrow ports for shadow replay integration."""

from __future__ import annotations

from typing import Protocol

from ..shadow_portfolio.models import ChallengerVersion, DataVintage, ShadowRunRequest
from .models import ReplayChallengerCandidate, ShadowReplayPolicy, ShadowRunOnceResult


class HistoricalVintageProvider(Protocol):
    def list_vintages(self, policy: ShadowReplayPolicy) -> tuple[DataVintage, ...]: ...


class ChallengerProvider(Protocol):
    def get_candidates(
        self, policy: ShadowReplayPolicy, vintage: DataVintage
    ) -> tuple[ReplayChallengerCandidate, ...]: ...


class ShadowRunOncePort(Protocol):
    def run_once(
        self,
        request: ShadowRunRequest,
        vintage: DataVintage,
        challengers: tuple[ChallengerVersion, ...],
    ) -> ShadowRunOnceResult: ...
