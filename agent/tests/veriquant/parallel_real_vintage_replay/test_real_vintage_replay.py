from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from veriquant.contracts import ContractValidationError, FailureCode
from veriquant.data import canonical_content_hash
from veriquant.real_vintage_replay.api import (
    FrozenDailyVintage,
    FrozenVintageBinding,
    FrozenVintageMetadata,
    MarketVintageFrozenProvider,
    RealVintageReplayController,
    RealVintageReplayStageProvider,
)
from veriquant.real_ashare_data.reader import (
    FrozenSnapshotHandle,
    HistoricalUniverseSnapshot,
    MarketStateRow,
    MarketVintage,
)
from veriquant.run_recovery.api import (
    RecoveryInterrupted,
    RecoveryStatus,
    RunCheckpointStore,
    RunRecoveryController,
    StageIdentity,
    StageTask,
)
from veriquant.shadow_replay.api import (
    DeterministicRunOncePort,
    ReplayStatus,
    ShadowReplayJournal,
    make_candidate,
    make_replay_policy,
    make_vintages,
)


H = canonical_content_hash


class FrozenProvider:
    def __init__(self, vintages: tuple[FrozenDailyVintage, ...]) -> None:
        self.vintages = vintages

    def list_frozen_vintages(self, policy):
        return self.vintages


class ChallengerProvider:
    def __init__(self, candidates=()) -> None:
        self.candidates = tuple(candidates)

    def get_eligible_challengers(self, policy, vintage):
        return self.candidates


class FakeMarketVintageReader:
    def __init__(self, *, leak_future_row: bool = False) -> None:
        self.leak_future_row = leak_future_row
        self.snapshot_hash = H({"fake": "snapshot"})
        self.handle = FrozenSnapshotHandle(
            dataset_id="fake-real-20d",
            snapshot_hash=self.snapshot_hash,
            snapshot_dir="snapshots/fake-real-20d",
            manifest_path="snapshots/fake-real-20d/manifest.json",
            raw_path="snapshots/fake-real-20d/raw_response.json",
            manifest_hash=H({"fake": "manifest"}),
            raw_content_hash=H({"fake": "raw"}),
            universe_hash=H({"fake": "universe"}),
            calendar_hash=H({"fake": "calendar"}),
            calendar_manifest_path="calendars/fake-real-20d/manifest.json",
        )
        self.raw_symbols_seen: list[tuple[str, ...]] = []

    def open(self, manifest_path: str) -> FrozenSnapshotHandle:
        assert manifest_path == self.handle.manifest_path
        return self.handle

    def market_vintage(self, handle: FrozenSnapshotHandle, *, decision_time: datetime) -> MarketVintage:
        day = decision_time.date().isoformat()
        available = decision_time - timedelta(hours=1)
        future = decision_time + timedelta(minutes=1)
        members = ("AAA", "BBB", "FUT")
        visible_rows = [
            MarketStateRow(
                symbol="AAA",
                event_time=available,
                published_at=available,
                available_at=available,
                calendar_session=day,
                historical_universe=members,
                values={"close": 10.0, "volume": 1000.0},
            ),
            MarketStateRow(
                symbol="BBB",
                event_time=available,
                published_at=available,
                available_at=available,
                calendar_session=day,
                historical_universe=members,
                values={"close": 11.0, "volume": 1200.0},
            ),
        ]
        future_row = MarketStateRow(
            symbol="FUT",
            event_time=available,
            published_at=available,
            available_at=future,
            calendar_session=day,
            historical_universe=members,
            values={"close": 99.0, "volume": 999.0},
        )
        self.raw_symbols_seen.append(tuple(row.symbol for row in [*visible_rows, future_row]))
        rows = tuple([*visible_rows, future_row] if self.leak_future_row else visible_rows)
        universe = HistoricalUniverseSnapshot(
            dataset_id="fake-real-20d",
            index_code="000300.SH",
            decision_time=decision_time,
            members_by_date={day: members},
            latest_members=members,
            universe_hash=H({"day": day, "members": members}),
            source_snapshot_hash=handle.snapshot_hash,
        )
        return MarketVintage(
            dataset_id="fake-real-20d",
            snapshot_hash=handle.snapshot_hash,
            manifest_hash=handle.manifest_hash,
            raw_content_hash=handle.raw_content_hash,
            calendar_hash=handle.calendar_hash,
            universe_hash=universe.universe_hash,
            decision_time=decision_time,
            rows=rows,
            historical_universe=universe,
            coverage_by_field={"close": 1.0, "volume": 1.0},
            market_state_hash=H({"decision_time": decision_time, "rows": rows}),
        )


class FakeMetadataProvider:
    def __init__(self, *, missing: str | None = None) -> None:
        self.missing = missing

    def metadata_for(self, *, binding, vintage) -> FrozenVintageMetadata:
        beta = {"AAA": 0.5, "BBB": -0.4, "FUT": 0.1}
        industry = {"AAA": "FIN", "BBB": "TECH", "FUT": "HEALTH"}
        adv = {"AAA": 1_000_000.0, "BBB": 1_000_000.0, "FUT": 1_000_000.0}
        if self.missing == "beta":
            beta.pop("BBB")
        if self.missing == "industry":
            industry.pop("BBB")
        if self.missing == "adv":
            adv.pop("BBB")
        return FrozenVintageMetadata(
            corporate_action_available_time=binding.decision_time - timedelta(hours=2),
            beta_by_symbol=beta,
            industry_by_symbol=industry,
            adv_by_symbol=adv,
        )


def _frozen_vintages(
    *,
    revision_day: int | None = None,
    count: int = 20,
) -> tuple[FrozenDailyVintage, ...]:
    source = make_vintages(revision_day=revision_day)[:count]
    captured = datetime(2026, 2, 1, 0, 0, tzinfo=timezone.utc)
    output = []
    for index, vintage in enumerate(source, start=1):
        revision = vintage.revision_id
        output.append(
            FrozenDailyVintage(
                snapshot_id=f"snapshot-{index:02d}-{revision}",
                session_date=vintage.decision_time.date().isoformat(),
                snapshot_hash=H({"snapshot": index, "revision": revision}),
                manifest_hash=H({"manifest": index, "revision": revision}),
                calendar_hash=H({"calendar": "sse-szse-20d"}),
                universe_hash=H({"universe": index, "members": vintage.universe}),
                revision_available_time=vintage.data_available_time,
                captured_at=captured,
                vintage=vintage,
            )
        )
    return tuple(output)


def _bindings() -> tuple[FrozenVintageBinding, ...]:
    start = datetime(2026, 1, 5, 9, 30, tzinfo=timezone.utc)
    return tuple(
        FrozenVintageBinding(
            snapshot_id="fake-real-20d",
            manifest_path="snapshots/fake-real-20d/manifest.json",
            decision_time=start + timedelta(days=index),
            revision_id=f"rev{index + 1}",
        )
        for index in range(20)
    )


def _build_controller(
    tmp_path: Path,
    *,
    vintages: tuple[FrozenDailyVintage, ...] | None = None,
    candidates=None,
    port=None,
):
    vintages = vintages if vintages is not None else _frozen_vintages()
    candidates = tuple(candidates) if candidates is not None else (make_candidate(),)
    port = port or DeterministicRunOncePort()
    journal = ShadowReplayJournal(tmp_path / "real-vintage-replay.jsonl")
    controller = RealVintageReplayController(
        vintage_provider=FrozenProvider(vintages),
        challenger_provider=ChallengerProvider(candidates),
        run_once_port=port,
        journal=journal,
    )
    return controller, port, journal


def test_exactly_20_distinct_frozen_sessions_delegate_to_existing_shadow_replay(tmp_path: Path) -> None:
    controller, port, _ = _build_controller(tmp_path)
    report = controller.run(make_replay_policy(replay_id="real-vintage-20d"))

    assert report.manifest.status is ReplayStatus.READY
    assert len(report.daily_records) == 20
    assert len({record.trading_day for record in report.daily_records}) == 20
    assert len({record.vintage_id for record in report.daily_records}) == 20
    assert len(port.calls) == 20
    assert all(record.vintage_id.startswith("frozen-") for record in report.daily_records)


def test_market_vintage_provider_generates_20_as_of_vintages_without_future_available_rows() -> None:
    reader = FakeMarketVintageReader()
    provider = MarketVintageFrozenProvider(
        reader=reader,
        bindings=_bindings(),
        metadata_provider=FakeMetadataProvider(),
    )

    frozen = provider.list_frozen_vintages(make_replay_policy())

    assert len(frozen) == 20
    assert all(item.vintage.decision_time == binding.decision_time for item, binding in zip(frozen, _bindings()))
    assert all("FUT" not in item.vintage.universe for item in frozen)
    assert all("FUT" in raw for raw in reader.raw_symbols_seen)
    assert len({item.vintage.artifact_hash for item in frozen}) == 20


def test_market_vintage_provider_fails_closed_if_future_available_row_leaks() -> None:
    provider = MarketVintageFrozenProvider(
        reader=FakeMarketVintageReader(leak_future_row=True),
        bindings=_bindings(),
        metadata_provider=FakeMetadataProvider(),
    )

    with pytest.raises(ContractValidationError) as excinfo:
        provider.list_frozen_vintages(make_replay_policy())

    assert excinfo.value.code is FailureCode.INVALID_TIME_ORDER


@pytest.mark.parametrize("count", [19])
def test_non_20_session_input_fails_closed(tmp_path: Path, count: int) -> None:
    controller, port, _ = _build_controller(tmp_path, vintages=_frozen_vintages(count=count))

    with pytest.raises(ContractValidationError) as excinfo:
        controller.run(make_replay_policy())

    assert excinfo.value.code is FailureCode.INCOMPLETE_RESULT
    assert port.calls == []


def test_duplicate_trading_date_fails_before_shadow_port(tmp_path: Path) -> None:
    vintages = list(_frozen_vintages())
    first = vintages[0]
    second_source = replace(
        vintages[1].vintage,
        decision_time=first.vintage.decision_time,
        data_available_time=first.vintage.data_available_time,
        corporate_action_available_time=first.vintage.corporate_action_available_time,
    )
    vintages[1] = replace(
        vintages[1],
        session_date=first.session_date,
        revision_available_time=first.revision_available_time,
        vintage=second_source,
    )
    controller, port, _ = _build_controller(tmp_path, vintages=tuple(vintages))

    with pytest.raises(ContractValidationError) as excinfo:
        controller.run(make_replay_policy())

    assert excinfo.value.code is FailureCode.INVALID_VALUE
    assert port.calls == []


@pytest.mark.parametrize("missing", ["beta", "industry", "adv"])
def test_missing_beta_industry_or_adv_fails_closed_before_shadow_port(tmp_path: Path, missing: str) -> None:
    base = _frozen_vintages()
    first = base[0]
    if missing == "beta":
        broken_vintage = replace(first.vintage, beta_by_symbol={"AAA": 0.5})
    elif missing == "industry":
        broken_vintage = replace(first.vintage, industry_by_symbol={"AAA": "FIN"})
    else:
        broken_vintage = replace(first.vintage, adv_by_symbol={"AAA": 1_000_000.0})
    broken = (replace(first, vintage=broken_vintage), *base[1:])
    controller, port, _ = _build_controller(tmp_path, vintages=broken)

    with pytest.raises(ContractValidationError) as excinfo:
        controller.run(make_replay_policy())

    assert excinfo.value.code is FailureCode.MISSING_REQUIRED_FIELD
    assert port.calls == []


class PositionAwarePort(DeterministicRunOncePort):
    def __init__(self) -> None:
        super().__init__()
        self.previous_positions_seen: list[dict[str, float]] = []

    def run_once(self, request, vintage, challengers):
        self.previous_positions_seen.append(dict(request.previous_positions))
        return super().run_once(request, vintage, challengers)

    def positions_after(self, request, vintage, challengers, outcome):
        day = int("".join(ch for ch in vintage.revision_id if ch.isdigit()))
        return {"AAA": float(day), "BBB": -float(day)}


def test_previous_positions_carry_across_days_and_do_not_reset_to_empty(tmp_path: Path) -> None:
    port = PositionAwarePort()
    controller, _, _ = _build_controller(tmp_path, port=port)

    report = controller.run(make_replay_policy(replay_id="real-carry-positions"))

    assert report.manifest.status is ReplayStatus.READY
    assert port.previous_positions_seen[0] == {}
    assert port.previous_positions_seen[1] == {"AAA": 1.0, "BBB": -1.0}
    assert port.previous_positions_seen[-1] == {"AAA": 19.0, "BBB": -19.0}
    assert any(position for position in port.previous_positions_seen[1:])


def test_future_challenger_is_not_visible_to_shadow_port(tmp_path: Path) -> None:
    decision = _frozen_vintages()[0].vintage.decision_time
    future = make_candidate(available_time=decision + timedelta(seconds=1))
    controller, port, _ = _build_controller(tmp_path, candidates=(future,))

    with pytest.raises(ContractValidationError) as excinfo:
        controller.run(make_replay_policy())

    assert excinfo.value.code is FailureCode.INVALID_TIME_ORDER
    assert port.calls == []


def test_no_eligible_challenger_halts_without_running_port(tmp_path: Path) -> None:
    controller, port, journal = _build_controller(tmp_path, candidates=())
    report = controller.run(make_replay_policy())

    assert report.manifest.status is ReplayStatus.HALT
    assert report.daily_records[-1].failure_code == FailureCode.INVALID_ALPHA_SOURCE.value
    assert port.calls == []
    assert len(journal.snapshot().records) == 1


def test_duplicate_run_does_not_duplicate_journal_or_shadow_calls(tmp_path: Path) -> None:
    controller, port, journal = _build_controller(tmp_path)
    policy = make_replay_policy(replay_id="real-idempotent")
    first = controller.run(policy)
    second = controller.run(policy)

    assert second.manifest.artifact_hash == first.manifest.artifact_hash
    assert len(port.calls) == 20
    assert len(journal.snapshot().records) == 20


def test_day_10_revision_preserves_days_1_to_9_and_branches_afterward(tmp_path: Path) -> None:
    policy = make_replay_policy(replay_id="real-revision")
    first_controller, _, journal = _build_controller(tmp_path, vintages=_frozen_vintages())
    first = first_controller.run(policy)
    revised_controller, _, _ = _build_controller(tmp_path, vintages=_frozen_vintages(revision_day=10))
    revised = revised_controller.run(policy)

    first_hashes = tuple(record.artifact_hash for record in first.daily_records)
    revised_hashes = tuple(record.artifact_hash for record in revised.daily_records)
    assert revised_hashes[:9] == first_hashes[:9]
    assert revised_hashes[9] != first_hashes[9]
    assert revised_hashes[10:] != first_hashes[10:]
    assert len(journal.snapshot().records) == 31


class InterruptOnceBeforeDay:
    def __init__(self, delegate, day: int) -> None:
        self.delegate = delegate
        self.day = day
        self.interrupted = False

    def run_once(self, request, vintage, challengers):
        marker = f"-d{self.day:02d}-"
        if marker in request.run_id and not self.interrupted:
            self.interrupted = True
            raise RecoveryInterrupted(FailureCode.PROVIDER_TIMEOUT, f"forced interruption before day {self.day}")
        return self.delegate.run_once(request, vintage, challengers)


def _replay_stage(run_id: str, expected_hash: str, policy_hash: str) -> StageTask:
    return StageTask(
        identity=StageIdentity(
            run_id=run_id,
            branch_id="C.real-replay",
            stage_id="C.real-replay.20d",
            ordinal=20,
            input_hash=H({"input": "frozen-vintages-and-challenger"}),
            config_hash=H({"config": "real-vintage-replay/v1"}),
            code_hash=H({"code": "real-vintage-replay/v1"}),
            policy_hash=policy_hash,
            expected_artifact_hash=expected_hash,
        ),
        provider_id="real-vintage-replay",
    )


def test_day_10_interrupt_resume_matches_uninterrupted_hash(tmp_path: Path) -> None:
    policy = make_replay_policy(replay_id="real-recovery")
    baseline_controller, baseline_port, _ = _build_controller(tmp_path / "baseline")
    baseline = baseline_controller.run(policy)
    expected_hash = baseline.manifest.artifact_hash
    assert len(baseline_port.calls) == 20

    delegate = DeterministicRunOncePort()
    interrupted_port = InterruptOnceBeforeDay(delegate, day=10)
    replay_controller, _, journal = _build_controller(tmp_path / "resumed", port=interrupted_port)
    stage_provider = RealVintageReplayStageProvider(replay_controller, policy)
    recovery = RunRecoveryController(
        store=RunCheckpointStore(tmp_path / "recovery"),
        providers={"real-vintage-replay": stage_provider},
    )
    task = _replay_stage("real-recovery-run", expected_hash, policy.policy_hash)

    interrupted = recovery.run(run_id="real-recovery-run", tasks=(task,), holder_id="worker-one")
    assert interrupted.status is RecoveryStatus.INTERRUPTED
    assert len(journal.snapshot().records) == 9

    resumed = recovery.run(run_id="real-recovery-run", tasks=(task,), holder_id="worker-two")
    repeated = recovery.run(run_id="real-recovery-run", tasks=(task,), holder_id="worker-three")
    resumed_report = replay_controller.run(policy)

    assert resumed.status is RecoveryStatus.COMMITTED
    assert resumed.final_artifact_hash == expected_hash
    assert repeated.final_artifact_hash == expected_hash
    assert resumed_report.manifest.artifact_hash == baseline.manifest.artifact_hash
    assert stage_provider.call_count == 2
    assert len(delegate.calls) == 20
    assert len(journal.snapshot().records) == 20


def test_risk_rejection_halts_replay(tmp_path: Path) -> None:
    controller, _, _ = _build_controller(tmp_path, port=DeterministicRunOncePort(halt_day=7))
    report = controller.run(make_replay_policy())

    assert report.manifest.status is ReplayStatus.HALT
    assert len(report.daily_records) == 7
    assert report.daily_records[-1].failure_code == FailureCode.RISK_NOT_APPROVED.value


def test_unbalanced_accounting_halts_replay(tmp_path: Path) -> None:
    controller, _, _ = _build_controller(tmp_path, port=DeterministicRunOncePort(unbalanced_day=4))
    report = controller.run(make_replay_policy())

    assert report.manifest.status is ReplayStatus.HALT
    assert len(report.daily_records) == 4
    assert report.daily_records[-1].failure_code == FailureCode.RECONCILIATION_FAILED.value


def test_package_source_contains_no_live_broker_order_oauth_or_secret_surface() -> None:
    package_root = Path(__file__).parents[3] / "src" / "veriquant" / "real_vintage_replay"
    forbidden = ("/live", "broker", "/order", "oauth", "secret")
    findings = []
    for path in package_root.glob("*.py"):
        text = path.read_text(encoding="utf-8").lower()
        for token in forbidden:
            if token in text:
                findings.append((path.name, token))
    assert findings == []
