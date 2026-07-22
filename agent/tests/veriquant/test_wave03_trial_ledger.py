from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import pytest

from src.veriquant.contracts import ContractValidationError, FailureCode
from src.veriquant.data import canonical_content_hash
from src.veriquant.evidence import (
    AttemptController,
    ExperimentIdentity,
    LedgerIntegrityError,
    TrialLedger,
    TrialOutcome,
    TrialStatus,
)


NOW = datetime(2024, 3, 1, 12, 0, tzinfo=timezone.utc)
HASH = "sha256:" + "f" * 64


def identity(index: int) -> ExperimentIdentity:
    return ExperimentIdentity(
        experiment_id=f"experiment-{index:03d}",
        dataset_hash=HASH,
        universe_hash=canonical_content_hash({"universe": index}),
        factor_hash=canonical_content_hash({"factor": index}),
        split_hash=canonical_content_hash({"split": "wf-v1"}),
        label_hash=canonical_content_hash({"label": "future-return-5d"}),
        cost_hash=canonical_content_hash({"cost": "spread-impact-v1"}),
        code_hash=canonical_content_hash({"code": "evidence-v1"}),
        availability_hash=canonical_content_hash({"availability": "pit-v1"}),
        seed=7,
    )


def ledger_at(tmp_path, name: str = "trials.sqlite") -> TrialLedger:
    return TrialLedger(tmp_path / name, clock=lambda: NOW)


def test_same_identity_is_idempotent_and_replay_reconstructs_state(tmp_path) -> None:
    ledger = ledger_at(tmp_path)
    ledger.create_family("family-001", 3)
    item = identity(1)
    first_hash = ledger.register_identity(item)
    second_hash = ledger.register_identity(item)
    first = ledger.reserve_attempt(
        family_id="family-001",
        trial_id="trial-001",
        identity_hash=first_hash,
        proposer_id="researcher-001",
    )
    duplicate = ledger.reserve_attempt(
        family_id="family-001",
        trial_id="trial-duplicate",
        identity_hash=second_hash,
        proposer_id="researcher-001",
    )
    ledger.start_attempt(first.trial_id)
    ledger.commit_outcome(first.trial_id, outcome=TrialOutcome.SUCCESS)
    replay = ledger.replay()

    assert first_hash == second_hash
    assert duplicate.event_hash == first.event_hash
    assert duplicate.trial_id == "trial-001"
    assert replay.families["family-001"].consumed == 1
    assert replay.families["family-001"].terminal_trials == 1
    assert replay.trials["trial-001"].status is TrialStatus.TERMINAL
    assert replay.trials["trial-001"].outcome is TrialOutcome.SUCCESS


@pytest.mark.parametrize(
    ("outcome", "failure_code"),
    [
        (TrialOutcome.PARSE_REJECTION, FailureCode.INVALID_FACTOR_DAG),
        (TrialOutcome.PIT_REJECTION, FailureCode.INVALID_TIME_ORDER),
        (TrialOutcome.TIMEOUT, FailureCode.INVALID_VALUE),
        (TrialOutcome.EVALUATION_REJECTION, FailureCode.INVALID_VALUE),
    ],
)
def test_parse_pit_timeout_and_evaluation_rejections_consume_budget(
    tmp_path, outcome: TrialOutcome, failure_code: FailureCode
) -> None:
    ledger = ledger_at(tmp_path, f"{outcome.value}.sqlite")
    ledger.create_family("family-001", 1)
    controller = AttemptController(ledger)
    attempt = controller.start(
        family_id="family-001",
        trial_id="trial-001",
        identity=identity(1),
        proposer_id="researcher-001",
    )
    controller.commit_failure(
        attempt,
        outcome=outcome,
        failure_code=failure_code,
    )
    replay = ledger.replay()

    assert replay.families["family-001"].consumed == 1
    assert replay.trials["trial-001"].status is TrialStatus.TERMINAL
    assert replay.trials["trial-001"].outcome is outcome


def test_only_truly_unstarted_reservation_can_release(tmp_path) -> None:
    ledger = ledger_at(tmp_path)
    ledger.create_family("family-001", 2)
    first_hash = ledger.register_identity(identity(1))
    ledger.reserve_attempt(
        family_id="family-001",
        trial_id="trial-unstarted",
        identity_hash=first_hash,
        proposer_id="researcher-001",
    )
    ledger.release_reservation("trial-unstarted")
    second_hash = ledger.register_identity(identity(2))
    ledger.reserve_attempt(
        family_id="family-001",
        trial_id="trial-started",
        identity_hash=second_hash,
        proposer_id="researcher-001",
    )
    ledger.start_attempt("trial-started")

    with pytest.raises(ContractValidationError) as caught:
        ledger.release_reservation("trial-started")

    assert caught.value.code is FailureCode.INVALID_STATE_TRANSITION
    replay = ledger.replay()
    assert replay.families["family-001"].consumed == 1
    assert replay.trials["trial-unstarted"].status is TrialStatus.RELEASED


def test_concurrent_reservations_cannot_overspend_family_budget(tmp_path) -> None:
    ledger = ledger_at(tmp_path)
    ledger.create_family("family-concurrent", 5)
    identity_hashes = [ledger.register_identity(identity(index)) for index in range(12)]

    def reserve(index: int) -> bool:
        try:
            ledger.reserve_attempt(
                family_id="family-concurrent",
                trial_id=f"trial-{index:03d}",
                identity_hash=identity_hashes[index],
                proposer_id=f"researcher-{index:03d}",
            )
            return True
        except ContractValidationError:
            return False

    with ThreadPoolExecutor(max_workers=12) as pool:
        successes = list(pool.map(reserve, range(12)))

    replay = ledger.replay()
    assert sum(successes) == 5
    assert replay.families["family-concurrent"].consumed == 5
    assert replay.families["family-concurrent"].consumed <= 5


def test_append_only_trigger_and_hash_replay_detect_tamper(tmp_path) -> None:
    path = tmp_path / "tamper.sqlite"
    ledger = TrialLedger(path, clock=lambda: NOW)
    ledger.create_family("family-001", 1)
    identity_hash = ledger.register_identity(identity(1))
    ledger.reserve_attempt(
        family_id="family-001",
        trial_id="trial-001",
        identity_hash=identity_hash,
        proposer_id="researcher-001",
    )

    connection = sqlite3.connect(path)
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute("UPDATE events SET units = 99 WHERE sequence = 1")
    connection.rollback()
    connection.execute("DROP TRIGGER no_update_events")
    connection.execute("UPDATE events SET units = 99 WHERE sequence = 1")
    connection.commit()
    connection.close()

    with pytest.raises(LedgerIntegrityError):
        ledger.replay()


def test_corrupt_sqlite_file_fails_closed(tmp_path) -> None:
    path = tmp_path / "corrupt.sqlite"
    ledger = TrialLedger(path, clock=lambda: NOW)
    ledger.create_family("family-001", 1)
    path.write_bytes(b"not-a-sqlite-ledger")

    with pytest.raises(LedgerIntegrityError):
        ledger.replay()

