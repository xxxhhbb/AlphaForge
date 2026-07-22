"""Clearly labelled integration fixture for replay plumbing acceptance only."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from veriquant.contracts import FailureCode
from veriquant.data import canonical_content_hash
from veriquant.real_vintage_replay.api import FrozenDailyVintage, RealVintageReplayController
from veriquant.run_recovery.api import (
    RecoveryInterrupted,
    RunCheckpointStore,
    RunRecoveryController,
    StageIdentity,
    StageTask,
)
from veriquant.shadow_replay.api import (
    DeterministicRunOncePort,
    ShadowReplayJournal,
    make_candidate,
    make_replay_policy,
    make_vintages,
)
from veriquant.real_vintage_replay.api import RealVintageReplayStageProvider


H = canonical_content_hash


class FixtureVintageProvider:
    def __init__(self, vintages: tuple[FrozenDailyVintage, ...]) -> None:
        self.vintages = vintages

    def list_frozen_vintages(self, policy):
        return self.vintages


class FixtureChallengerProvider:
    def __init__(self) -> None:
        self.candidate = make_candidate()

    def get_eligible_challengers(self, policy, vintage):
        return (self.candidate,)


class PositionAwareFixturePort(DeterministicRunOncePort):
    def __init__(self) -> None:
        super().__init__()
        self.previous_positions_seen: list[dict[str, float]] = []

    def run_once(self, request, vintage, challengers):
        self.previous_positions_seen.append(dict(request.previous_positions))
        return super().run_once(request, vintage, challengers)

    def positions_after(self, request, vintage, challengers, outcome):
        day = int("".join(character for character in vintage.revision_id if character.isdigit()))
        return {"AAA": float(day), "BBB": -float(day)}


class InterruptOnceBeforeDay:
    def __init__(self, delegate, day: int = 10) -> None:
        self.delegate = delegate
        self.day = day
        self.interrupted = False

    def run_once(self, request, vintage, challengers):
        if f"-d{self.day:02d}-" in request.run_id and not self.interrupted:
            self.interrupted = True
            raise RecoveryInterrupted(FailureCode.PROVIDER_TIMEOUT, f"fixture interruption before day {self.day}")
        return self.delegate.run_once(request, vintage, challengers)

    def positions_after(self, request, vintage, challengers, outcome):
        return self.delegate.positions_after(request, vintage, challengers, outcome)


def make_frozen_fixture_vintages(*, revision_day: int | None = None) -> tuple[FrozenDailyVintage, ...]:
    source = make_vintages(revision_day=revision_day)
    captured = datetime(2026, 2, 1, tzinfo=timezone.utc)
    return tuple(
        FrozenDailyVintage(
            snapshot_id=f"integration-fixture-{index:02d}-{vintage.revision_id}",
            session_date=vintage.decision_time.date().isoformat(),
            snapshot_hash=H({"fixture_snapshot": index, "revision": vintage.revision_id}),
            manifest_hash=H({"fixture_manifest": index, "revision": vintage.revision_id}),
            calendar_hash=H({"fixture_calendar": "20-sessions"}),
            universe_hash=H({"fixture_universe": index, "members": vintage.universe}),
            revision_available_time=vintage.data_available_time,
            captured_at=captured,
            vintage=vintage,
        )
        for index, vintage in enumerate(source, start=1)
    )


def build_fixture_controller(root: Path, *, vintages=None, port=None):
    journal = ShadowReplayJournal(root / "integration-fixture-shadow-journal.jsonl")
    actual_port = port or PositionAwareFixturePort()
    controller = RealVintageReplayController(
        vintage_provider=FixtureVintageProvider(vintages or make_frozen_fixture_vintages()),
        challenger_provider=FixtureChallengerProvider(),
        run_once_port=actual_port,
        journal=journal,
    )
    return controller, actual_port, journal


def fixture_recovery_task(*, run_id: str, expected_hash: str, policy_hash: str) -> StageTask:
    return StageTask(
        identity=StageIdentity(
            run_id=run_id,
            branch_id="fixture.real-replay",
            stage_id="fixture.real-replay.20d",
            ordinal=20,
            input_hash=H({"integration_fixture": "frozen-vintages-and-fake-challenger"}),
            config_hash=H({"config": "integration-fixture/v1"}),
            code_hash=H({"code": "delegates-real-vintage-replay/v1"}),
            policy_hash=policy_hash,
            expected_artifact_hash=expected_hash,
        ),
        provider_id="integration-fixture-real-replay",
    )


def build_fixture_recovery(root: Path, controller, policy):
    provider = RealVintageReplayStageProvider(controller, policy)
    recovery = RunRecoveryController(
        store=RunCheckpointStore(root / "integration-fixture-recovery"),
        providers={"integration-fixture-real-replay": provider},
    )
    return recovery, provider
