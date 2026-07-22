"""Stable import surface for the parallel shadow replay package."""

from .controller import ShadowPortfolioRunOnceAdapter, ShadowReplayController
from .fixture import (
    DeterministicRunOncePort,
    InMemoryChallengerProvider,
    InMemoryHistoricalVintageProvider,
    ShadowReplayFixture,
    build_shadow_replay_fixture,
    make_candidate,
    make_replay_policy,
    make_vintages,
)
from .journal import JournalSnapshot, ShadowReplayJournal
from .models import (
    DailyReplayRecord,
    ReplayCandidateState,
    ReplayChallengerCandidate,
    ReplayStatus,
    ShadowReplayManifest,
    ShadowReplayPolicy,
    ShadowReplayReport,
    ShadowRunOnceResult,
)
from .protocols import ChallengerProvider, HistoricalVintageProvider, ShadowRunOncePort

__all__ = [
    "ChallengerProvider",
    "DailyReplayRecord",
    "DeterministicRunOncePort",
    "HistoricalVintageProvider",
    "InMemoryChallengerProvider",
    "InMemoryHistoricalVintageProvider",
    "JournalSnapshot",
    "ReplayCandidateState",
    "ReplayChallengerCandidate",
    "ReplayStatus",
    "ShadowPortfolioRunOnceAdapter",
    "ShadowReplayController",
    "ShadowReplayFixture",
    "ShadowReplayJournal",
    "ShadowReplayManifest",
    "ShadowReplayPolicy",
    "ShadowReplayReport",
    "ShadowRunOncePort",
    "ShadowRunOnceResult",
    "build_shadow_replay_fixture",
    "make_candidate",
    "make_replay_policy",
    "make_vintages",
]
