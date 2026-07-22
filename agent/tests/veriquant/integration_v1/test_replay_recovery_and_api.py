from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from veriquant.data import canonical_content_hash
from veriquant.integration.v1_recovery import (
    CallableStageProvider,
    IntegrationStageSpec,
    StageArtifact,
    V1RecoveryPlanBuilder,
    stage_hash,
)
from veriquant.integration.v1_replay import (
    FrozenReaderHistoricalVintageProvider,
    FrozenVintageBinding,
    FrozenVintageMetadata,
    PromotionChallengerProvider,
)
from veriquant.real_ashare_data.reader import (
    FrozenSnapshotHandle,
    HistoricalUniverseSnapshot,
    MarketStateRow,
    MarketVintage,
)
from veriquant.run_recovery.api import RecoveryStatus, RunCheckpointStore, RunRecoveryController
from veriquant.shadow_replay.api import (
    DeterministicRunOncePort,
    ReplayStatus,
    ShadowReplayController,
    ShadowReplayJournal,
    make_candidate,
    make_replay_policy,
)


H = lambda value: canonical_content_hash(value)


class VintageReader:
    def __init__(self) -> None:
        self.snapshot_hash = H({"snapshot": "frozen-20d"})
        self.handle = FrozenSnapshotHandle(
            dataset_id="frozen-20d",
            snapshot_hash=self.snapshot_hash,
            snapshot_dir="snapshots/frozen-20d",
            manifest_path="snapshots/frozen-20d/manifest.json",
            raw_path="snapshots/frozen-20d/raw_response.json",
            manifest_hash=H({"manifest": "frozen-20d"}),
            raw_content_hash=H({"raw": "frozen-20d"}),
            universe_hash=H({"universe": ("AAA", "BBB")}),
            calendar_hash=H({"calendar": "20d"}),
            calendar_manifest_path="calendars/frozen-20d/manifest.json",
        )

    def open(self, manifest_path: str) -> FrozenSnapshotHandle:
        assert manifest_path == self.handle.manifest_path
        return self.handle

    def market_vintage(self, handle: FrozenSnapshotHandle, *, decision_time: datetime) -> MarketVintage:
        day = decision_time.date().isoformat()
        available = decision_time - timedelta(hours=1)
        members = ("AAA", "BBB")
        universe = HistoricalUniverseSnapshot(
            dataset_id="frozen-20d",
            index_code="000300.SH",
            decision_time=decision_time,
            members_by_date={day: members},
            latest_members=members,
            universe_hash=H({"day": day, "members": members}),
            source_snapshot_hash=handle.snapshot_hash,
        )
        rows = tuple(
            MarketStateRow(
                symbol=symbol,
                event_time=available,
                published_at=available,
                available_at=available,
                calendar_session=day,
                historical_universe=members,
                values={"close": 100.0 + index, "volume": 1_000_000.0},
            )
            for index, symbol in enumerate(members)
        )
        state_hash = H({"snapshot": handle.snapshot_hash, "decision_time": decision_time, "rows": rows})
        return MarketVintage(
            dataset_id="frozen-20d",
            snapshot_hash=handle.snapshot_hash,
            manifest_hash=handle.manifest_hash,
            raw_content_hash=handle.raw_content_hash,
            calendar_hash=handle.calendar_hash,
            universe_hash=universe.universe_hash,
            decision_time=decision_time,
            rows=rows,
            historical_universe=universe,
            coverage_by_field={"close": 1.0, "volume": 1.0},
            market_state_hash=state_hash,
        )


class MetadataProvider:
    def metadata_for(self, *, binding, vintage):
        return FrozenVintageMetadata(
            corporate_action_available_time=binding.decision_time - timedelta(hours=2),
            beta_by_symbol={"AAA": 0.5, "BBB": -0.4},
            industry_by_symbol={"AAA": "FIN", "BBB": "TECH"},
            adv_by_symbol={"AAA": 1_000_000.0, "BBB": 1_000_000.0},
        )


def _integrated_replay(tmp_path: Path):
    start = datetime(2026, 1, 5, 9, 30, tzinfo=timezone.utc)
    reader = VintageReader()
    bindings = tuple(
        FrozenVintageBinding(
            snapshot_id="frozen-20d",
            manifest_path=reader.handle.manifest_path,
            decision_time=start + timedelta(days=index),
            revision_id=f"rev{index + 1}",
        )
        for index in range(20)
    )
    policy = make_replay_policy(replay_id="integration-replay-20d")
    vintage_provider = FrozenReaderHistoricalVintageProvider(
        reader=reader,
        bindings=bindings,
        metadata_provider=MetadataProvider(),
    )
    challenger_provider = PromotionChallengerProvider((make_candidate(),))
    port = DeterministicRunOncePort()
    journal = ShadowReplayJournal(tmp_path / "shadow-replay.jsonl")
    controller = ShadowReplayController(
        vintage_provider=vintage_provider,
        challenger_provider=challenger_provider,
        run_once_port=port,
        journal=journal,
    )
    return policy, controller, port, journal


def test_agent_a_vintages_to_challenger_shadow_replay_is_20_day_and_idempotent(tmp_path: Path) -> None:
    policy, controller, port, journal = _integrated_replay(tmp_path)
    first = controller.run(policy)
    calls = len(port.calls)
    events = port.ledger_events
    second = controller.run(policy)

    assert first.manifest.status is ReplayStatus.READY
    assert len(first.daily_records) == 20
    assert len({record.vintage_id for record in first.daily_records}) == 20
    assert second.manifest.artifact_hash == first.manifest.artifact_hash
    assert len(port.calls) == calls == 20
    assert port.ledger_events == events
    assert len(journal.snapshot().records) == 20


def test_run_recovery_forced_interrupt_resumes_without_repeating_committed_stages(tmp_path: Path) -> None:
    policy, replay_controller, port, journal = _integrated_replay(tmp_path / "replay")
    baseline = replay_controller.run(policy)
    expected = {
        "A.frozen": stage_hash("A.frozen", {"snapshot": "verified"}),
        "B.evidence": stage_hash("B.evidence", {"verdict": "CHALLENGER"}),
        "C.replay20": baseline.manifest.artifact_hash,
    }
    specs = (
        IntegrationStageSpec(
            run_id="v1-resume-drill",
            stage_id="A.frozen",
            ordinal=1,
            input_hash=H({"input": "A"}),
            config_hash=H({"config": "A"}),
            code_hash=H({"code": "A"}),
            policy_hash=H({"policy": "A"}),
            expected_artifact_hash=expected["A.frozen"],
        ),
        IntegrationStageSpec(
            run_id="v1-resume-drill",
            stage_id="B.evidence",
            ordinal=2,
            input_hash=expected["A.frozen"],
            config_hash=H({"config": "B"}),
            code_hash=H({"code": "B"}),
            policy_hash=H({"policy": "B"}),
            expected_artifact_hash=expected["B.evidence"],
            depends_on_stage_ids=("A.frozen",),
        ),
        IntegrationStageSpec(
            run_id="v1-resume-drill",
            stage_id="C.replay20",
            ordinal=3,
            input_hash=expected["B.evidence"],
            config_hash=H({"config": "C"}),
            code_hash=H({"code": "C"}),
            policy_hash=policy.policy_hash,
            expected_artifact_hash=expected["C.replay20"],
            depends_on_stage_ids=("B.evidence",),
        ),
    )
    tasks = V1RecoveryPlanBuilder.build(specs)

    def fixed(label):
        return lambda task, inputs: StageArtifact(
            artifact_hash=expected[label],
            payload_hash=H({"label": label, "inputs": inputs}),
            metadata={"stage": label},
        )

    def replay_stage(task, inputs):
        report = replay_controller.run(policy)
        return StageArtifact(
            artifact_hash=report.manifest.artifact_hash,
            payload_hash=H({"report": report.manifest.artifact_hash, "inputs": inputs}),
            metadata={"records": str(len(report.daily_records))},
        )

    provider = CallableStageProvider(
        {
            "A.frozen": fixed("A.frozen"),
            "B.evidence": fixed("B.evidence"),
            "C.replay20": replay_stage,
        },
        interrupt_once_before="C.replay20",
    )
    controller = RunRecoveryController(
        store=RunCheckpointStore(tmp_path / "recovery"),
        providers={"v1-stage-provider": provider},
    )
    interrupted = controller.run(run_id="v1-resume-drill", tasks=tasks, holder_id="worker-one")
    resumed = controller.run(run_id="v1-resume-drill", tasks=tasks, holder_id="worker-two")
    repeated = controller.run(run_id="v1-resume-drill", tasks=tasks, holder_id="worker-three")

    assert interrupted.status is RecoveryStatus.INTERRUPTED
    assert resumed.status is RecoveryStatus.COMMITTED
    assert resumed.final_artifact_hash == expected["C.replay20"]
    assert repeated.final_artifact_hash == resumed.final_artifact_hash
    assert provider.call_counts == {"A.frozen": 1, "B.evidence": 1, "C.replay20": 1}
    assert len(port.calls) == 20
    assert len(journal.snapshot().records) == 20


def test_standalone_veriquant_api_has_only_readonly_routes(tmp_path: Path) -> None:
    import veriquant_api_server

    app = veriquant_api_server.create_app(artifact_root=tmp_path)
    manifest = veriquant_api_server.build_route_manifest(app, artifact_root=tmp_path)
    paths = {route.path for route in manifest.routes}

    assert "/healthz" in paths
    assert "/veriquant/status" in paths
    assert any(path.startswith("/veriquant/workbench") for path in paths)
    assert all(not any(token in path.lower() for token in ("/live", "broker", "order", "oauth", "mandate")) for path in paths)
    assert all(set(route.methods) <= {"GET", "HEAD", "OPTIONS"} for route in manifest.routes)
    assert manifest.offline_only is True
    assert manifest.provider_keys_required is False

