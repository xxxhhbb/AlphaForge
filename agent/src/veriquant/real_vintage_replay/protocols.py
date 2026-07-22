"""Minimal read-only provider ports for real-vintage replay."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from veriquant.shadow_replay.models import ReplayChallengerCandidate, ShadowReplayPolicy

from .models import FrozenDailyVintage, FrozenVintageBinding, FrozenVintageMetadata


@runtime_checkable
class FrozenVintageProvider(Protocol):
    """Return the frozen, content-addressed vintages selected for one replay."""

    def list_frozen_vintages(self, policy: ShadowReplayPolicy) -> tuple[FrozenDailyVintage, ...]: ...


@runtime_checkable
class EligibleChallengerProvider(Protocol):
    """Return independently promoted challengers visible for one frozen vintage."""

    def get_eligible_challengers(
        self,
        policy: ShadowReplayPolicy,
        vintage: FrozenDailyVintage,
    ) -> tuple[ReplayChallengerCandidate, ...]: ...


@runtime_checkable
class MarketVintageReader(Protocol):
    """Open a frozen snapshot and project its PIT-visible as-of market vintage."""

    def open(self, manifest_path: str) -> Any: ...

    def market_vintage(self, handle: Any, *, decision_time: datetime) -> Any: ...


@runtime_checkable
class FrozenVintageMetadataProvider(Protocol):
    """Return required beta, industry, ADV, and corporate-action timing metadata."""

    def metadata_for(self, *, binding: FrozenVintageBinding, vintage: Any) -> FrozenVintageMetadata: ...
