"""Thin real-vintage controller that delegates all replay truth."""

from __future__ import annotations

from typing import Any

from veriquant.shadow_replay.api import (
    ShadowReplayController,
    ShadowReplayJournal,
    ShadowReplayPolicy,
    ShadowReplayReport,
    ShadowRunOncePort,
)

from .adapters import EligibleChallengerPortAdapter, FrozenVintagePortAdapter, PositionCarryingRunOncePort
from .protocols import EligibleChallengerProvider, FrozenVintageProvider


class RealVintageReplayController:
    """Bind real frozen inputs, then call the existing ShadowReplayController."""

    def __init__(
        self,
        *,
        vintage_provider: FrozenVintageProvider,
        challenger_provider: EligibleChallengerProvider,
        run_once_port: ShadowRunOncePort,
        journal: ShadowReplayJournal,
        initial_positions: dict[str, float] | None = None,
        position_extractor: Any | None = None,
    ) -> None:
        vintage_adapter = FrozenVintagePortAdapter(vintage_provider)
        challenger_adapter = EligibleChallengerPortAdapter(challenger_provider, vintage_adapter)
        carrying_port = PositionCarryingRunOncePort(
            run_once_port,
            initial_positions=initial_positions,
            position_extractor=position_extractor,
        )
        self.position_carry_port = carrying_port
        self._delegate = ShadowReplayController(
            vintage_provider=vintage_adapter,
            challenger_provider=challenger_adapter,
            run_once_port=carrying_port,
            journal=journal,
        )

    def run(self, policy: ShadowReplayPolicy) -> ShadowReplayReport:
        return self._delegate.run(policy)
