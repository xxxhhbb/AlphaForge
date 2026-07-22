from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from veriquant.contracts import ContractValidationError, FailureCode
from veriquant.integration.v1_real_replay.fixture import (
    InterruptOnceBeforeDay,
    PositionAwareFixturePort,
    build_fixture_controller,
    build_fixture_recovery,
    fixture_recovery_task,
    make_frozen_fixture_vintages,
)
from veriquant.integration.v1_real_replay.gate import NoPromotionReceiptProvider, RealReplayReadinessGate
from veriquant.integration.v1_real_replay.models import (
    PromotionChallengerReceipt,
    RealReplayGateStatus,
)
from veriquant.run_recovery.api import RecoveryStatus
from veriquant.shadow_replay.api import ReplayStatus, make_replay_policy


REPO_ROOT = Path(__file__).parents[4]


class ReceiptProvider:
    def __init__(self, receipt):
        self.receipt = receipt

    def challenger_for_official_run(self, official_run_hash):
        return self.receipt


def _valid_receipt(gate: RealReplayReadinessGate) -> PromotionChallengerReceipt:
    run = gate.load_official_run()
    factor = run.factors[0]
    assert factor.validation_evidence is not None and factor.factor_dag_hash is not None
    return PromotionChallengerReceipt(
        candidate_id="real-approved-proxy-candidate",
        version="v1",
        state="CHALLENGER",
        source_authority="INDEPENDENT_PROMOTION_SERVICE",
        official_run_hash=run.artifact_hash,
        official_factor_identity_hash=factor.identity_hash,
        policy_version="policy.v1",
        policy_hash=make_replay_policy().policy_hash,
        promotion_decision_hash="sha256:" + "1" * 64,
        evidence_hash=factor.validation_evidence.artifact_hash,
        factor_hash=factor.factor_dag_hash,
        available_time=run.decision_time.replace(year=run.decision_time.year + 1),
    )


def test_real_entry_is_typed_blocked_without_independent_challenger() -> None:
    gate = RealReplayReadinessGate(REPO_ROOT)

    result, receipt = gate.inspect(NoPromotionReceiptProvider())

    assert receipt is None
    assert result.status is RealReplayGateStatus.BLOCKED
    assert result.failure_code is FailureCode.UPSTREAM_BLOCKED
    assert result.sessions_committed == 0
    assert result.real_challenger_used is False
    assert result.fixture_only is False


def test_official_visible_evidence_is_not_itself_a_challenger() -> None:
    gate = RealReplayReadinessGate(REPO_ROOT)
    run = gate.load_official_run()

    assert run.official_evidence_run is True
    assert run.promotion_run is False
    assert run.challenger_created is False
    assert run.official_tushare_source is False


def test_receipt_must_bind_exact_official_factor_and_evidence() -> None:
    gate = RealReplayReadinessGate(REPO_ROOT)
    bad = replace(_valid_receipt(gate), evidence_hash="sha256:" + "2" * 64)

    with pytest.raises(ContractValidationError) as excinfo:
        gate.inspect(ReceiptProvider(bad))

    assert excinfo.value.code is FailureCode.INVALID_HASH


def test_non_independent_or_non_challenger_receipt_fails_closed() -> None:
    gate = RealReplayReadinessGate(REPO_ROOT)
    receipt = _valid_receipt(gate)

    with pytest.raises(ContractValidationError) as excinfo:
        replace(receipt, source_authority="LLM_RESEARCHER")
    assert excinfo.value.code is FailureCode.UNAUTHORIZED_CAPABILITY

    with pytest.raises(ContractValidationError) as excinfo:
        replace(receipt, state="VALIDATED_RESEARCH")
    assert excinfo.value.code is FailureCode.INVALID_ALPHA_SOURCE


def test_integration_fixture_runs_20_sessions_with_positions_and_metrics(tmp_path: Path) -> None:
    controller, port, journal = build_fixture_controller(tmp_path)
    policy = make_replay_policy(replay_id="agent-d-integration-fixture")

    report = controller.run(policy)

    assert report.manifest.status is ReplayStatus.READY
    assert len(report.daily_records) == 20
    assert len({item.trading_day for item in report.daily_records}) == 20
    assert len(journal.snapshot().records) == 20
    assert port.previous_positions_seen[0] == {}
    assert port.previous_positions_seen[1] == {"AAA": 1.0, "BBB": -1.0}
    assert port.previous_positions_seen[-1] == {"AAA": 19.0, "BBB": -19.0}
    assert report.manifest.final_nav > 0
    assert report.manifest.max_drawdown >= 0
    assert report.manifest.total_turnover > 0
    assert report.manifest.total_transaction_cost > 0
    assert all(item.attribution_hash for item in report.daily_records)


def test_fixture_repeat_is_idempotent_and_does_not_duplicate_journal(tmp_path: Path) -> None:
    controller, port, journal = build_fixture_controller(tmp_path)
    policy = make_replay_policy(replay_id="agent-d-idempotent-fixture")

    first = controller.run(policy)
    second = controller.run(policy)

    assert second.manifest.artifact_hash == first.manifest.artifact_hash
    assert len(journal.snapshot().records) == 20
    assert len(port.calls) == 20


def test_fixture_revision_preserves_past_facts_and_branches_from_day_10(tmp_path: Path) -> None:
    policy = make_replay_policy(replay_id="agent-d-revision-fixture")
    original, _, journal = build_fixture_controller(tmp_path, vintages=make_frozen_fixture_vintages())
    first = original.run(policy)
    revised, _, _ = build_fixture_controller(tmp_path, vintages=make_frozen_fixture_vintages(revision_day=10))
    second = revised.run(policy)

    first_hashes = tuple(item.artifact_hash for item in first.daily_records)
    second_hashes = tuple(item.artifact_hash for item in second.daily_records)
    assert first_hashes[:9] == second_hashes[:9]
    assert first_hashes[9] != second_hashes[9]
    assert first_hashes[10:] != second_hashes[10:]
    assert len(journal.snapshot().records) == 31


def test_fixture_day_10_interrupt_resume_matches_uninterrupted(tmp_path: Path) -> None:
    policy = make_replay_policy(replay_id="agent-d-recovery-fixture")
    baseline_controller, _, _ = build_fixture_controller(tmp_path / "baseline")
    baseline = baseline_controller.run(policy)

    delegate = PositionAwareFixturePort()
    interrupting = InterruptOnceBeforeDay(delegate, day=10)
    resumed_controller, _, journal = build_fixture_controller(tmp_path / "resumed", port=interrupting)
    recovery, stage_provider = build_fixture_recovery(tmp_path / "resumed", resumed_controller, policy)
    task = fixture_recovery_task(
        run_id="agent-d-recovery-run",
        expected_hash=baseline.manifest.artifact_hash,
        policy_hash=policy.policy_hash,
    )

    interrupted = recovery.run(run_id="agent-d-recovery-run", tasks=(task,), holder_id="worker-one")
    assert interrupted.status is RecoveryStatus.INTERRUPTED
    assert len(journal.snapshot().records) == 9

    resumed = recovery.run(run_id="agent-d-recovery-run", tasks=(task,), holder_id="worker-two")
    repeated = recovery.run(run_id="agent-d-recovery-run", tasks=(task,), holder_id="worker-three")
    replayed = resumed_controller.run(policy)

    assert resumed.status is RecoveryStatus.COMMITTED
    assert repeated.final_artifact_hash == baseline.manifest.artifact_hash
    assert replayed.manifest.artifact_hash == baseline.manifest.artifact_hash
    assert len(journal.snapshot().records) == 20
    assert len(delegate.calls) == 20
    assert stage_provider.call_count == 2


def test_owned_package_has_no_live_or_arbitrary_execution_surface() -> None:
    package = REPO_ROOT / "agent/src/veriquant/integration/v1_real_replay"
    forbidden = ("eval(", "exec(", "factor.py", "/live/", "oauth", "subprocess", "import broker")
    findings = []
    for path in package.glob("*.py"):
        text = path.read_text(encoding="utf-8").lower()
        for token in forbidden:
            if token in text:
                findings.append((path.name, token))
    assert findings == []

