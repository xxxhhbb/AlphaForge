from __future__ import annotations

import json
from datetime import timedelta

import pytest

from src.veriquant.contracts import ContractValidationError, FailureCode
from src.veriquant.shadow_replay.api import (
    DeterministicRunOncePort,
    InMemoryChallengerProvider,
    InMemoryHistoricalVintageProvider,
    ReplayCandidateState,
    ReplayStatus,
    ShadowReplayController,
    ShadowReplayJournal,
    build_shadow_replay_fixture,
    make_candidate,
    make_replay_policy,
    make_vintages,
)


def test_exactly_executes_20_frozen_trading_days(tmp_path):
    fixture = build_shadow_replay_fixture(tmp_path)

    report = fixture.controller.run(fixture.policy)

    assert report.manifest.status is ReplayStatus.READY
    assert len(report.daily_records) == 20
    assert [record.session_index for record in report.daily_records] == list(range(1, 21))
    assert len({record.vintage_id for record in report.daily_records}) == 20
    assert all(record.status is ReplayStatus.READY for record in report.daily_records)
    assert len(fixture.run_once_port.calls) == 20


def test_each_day_uses_only_available_challenger_information(tmp_path):
    vintages = make_vintages()
    future = make_candidate(
        version="future",
        available_time=vintages[-1].decision_time + timedelta(days=1),
    )
    valid = make_candidate(version="valid", available_time=vintages[0].decision_time - timedelta(days=1))
    fixture = build_shadow_replay_fixture(tmp_path, vintages=vintages, candidates=(future, valid))

    report = fixture.controller.run(fixture.policy)

    assert report.manifest.status is ReplayStatus.READY
    for _, challengers in fixture.run_once_port.calls:
        assert tuple(challenger.version for challenger in challengers) == ("valid",)


def test_unapproved_states_do_not_reach_run_once_port(tmp_path):
    rejected = make_candidate(version="rejected", state=ReplayCandidateState.REJECTED)
    draft = make_candidate(version="draft", state=ReplayCandidateState.DRAFT)
    auxiliary = make_candidate(version="aux", state=ReplayCandidateState.AUXILIARY_RESEARCH_EVIDENCE)
    challenger = make_candidate(version="challenger", state=ReplayCandidateState.CHALLENGER)
    fixture = build_shadow_replay_fixture(tmp_path, candidates=(rejected, draft, auxiliary, challenger))

    report = fixture.controller.run(fixture.policy)

    assert report.manifest.status is ReplayStatus.READY
    for _, challengers in fixture.run_once_port.calls:
        assert tuple(challenger.version for challenger in challengers) == ("challenger",)
    first = report.daily_records[0]
    assert len(first.candidate_hashes_seen) == 4
    assert len(first.challenger_hashes_used) == 1


def test_no_eligible_challenger_halts_without_calling_shadow_port(tmp_path):
    fixture = build_shadow_replay_fixture(
        tmp_path,
        candidates=(make_candidate(state=ReplayCandidateState.DRAFT),),
    )

    report = fixture.controller.run(fixture.policy)

    assert report.manifest.status is ReplayStatus.HALT
    assert len(report.daily_records) == 1
    assert report.daily_records[0].failure_code == FailureCode.INVALID_ALPHA_SOURCE.value
    assert len(fixture.run_once_port.calls) == 0


def test_duplicate_replay_does_not_add_journal_or_shadow_events(tmp_path):
    fixture = build_shadow_replay_fixture(tmp_path)
    first = fixture.controller.run(fixture.policy)
    first_calls = len(fixture.run_once_port.calls)
    first_events = fixture.run_once_port.ledger_events
    first_journal_count = len(fixture.journal.snapshot().records)

    second = fixture.controller.run(fixture.policy)

    assert second.manifest.artifact_hash == first.manifest.artifact_hash
    assert len(fixture.run_once_port.calls) == first_calls
    assert fixture.run_once_port.ledger_events == first_events
    assert len(fixture.journal.snapshot().records) == first_journal_count


def test_day_10_data_revision_preserves_day_1_to_9_and_appends_new_path(tmp_path):
    first_fixture = build_shadow_replay_fixture(tmp_path, vintages=make_vintages())
    first = first_fixture.controller.run(first_fixture.policy)
    first_hashes = [record.artifact_hash for record in first.daily_records]

    revised_policy = make_replay_policy()
    journal = ShadowReplayJournal(tmp_path / "shadow_replay.jsonl")
    revised_port = DeterministicRunOncePort()
    revised_controller = ShadowReplayController(
        vintage_provider=InMemoryHistoricalVintageProvider(make_vintages(revision_day=10)),
        challenger_provider=InMemoryChallengerProvider({}, (make_candidate(),)),
        run_once_port=revised_port,
        journal=journal,
    )
    revised = revised_controller.run(revised_policy)

    revised_hashes = [record.artifact_hash for record in revised.daily_records]
    assert revised_hashes[:9] == first_hashes[:9]
    assert revised_hashes[9] != first_hashes[9]
    assert len(journal.snapshot().records) == 31


def test_append_only_journal_tamper_fails_closed(tmp_path):
    fixture = build_shadow_replay_fixture(tmp_path)
    fixture.controller.run(fixture.policy)
    journal_path = tmp_path / "shadow_replay.jsonl"
    lines = journal_path.read_text(encoding="utf-8").splitlines()
    first_entry = json.loads(lines[0])
    first_entry["record"]["nav_end"] = first_entry["record"]["nav_end"] + 1.0
    lines[0] = json.dumps(first_entry, sort_keys=True, separators=(",", ":"))
    journal_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    with pytest.raises(ContractValidationError) as excinfo:
        ShadowReplayJournal(journal_path).snapshot()
    assert excinfo.value.code is FailureCode.LEDGER_CORRUPTION


def test_nav_cost_turnover_drawdown_and_attribution_are_replayable(tmp_path):
    fixture = build_shadow_replay_fixture(tmp_path)
    report = fixture.controller.run(fixture.policy)

    loaded = ShadowReplayJournal(tmp_path / "shadow_replay.jsonl").snapshot()
    loaded_records = tuple(record for record in loaded.records if record.replay_id == fixture.policy.replay_id)

    assert tuple(record.artifact_hash for record in loaded_records) == report.manifest.daily_record_hashes
    assert report.manifest.final_nav == loaded_records[-1].nav_end
    assert report.manifest.total_transaction_cost == sum(record.transaction_cost for record in loaded_records)
    assert report.manifest.total_turnover == sum(record.turnover for record in loaded_records)
    assert report.manifest.max_drawdown == max(record.drawdown for record in loaded_records)
    assert all("net_pnl" in record.attribution for record in loaded_records)


def test_risk_rejection_halts_replay(tmp_path):
    fixture = build_shadow_replay_fixture(tmp_path, run_once_port=DeterministicRunOncePort(halt_day=7))

    report = fixture.controller.run(fixture.policy)

    assert report.manifest.status is ReplayStatus.HALT
    assert len(report.daily_records) == 7
    assert report.daily_records[-1].failure_code == FailureCode.RISK_NOT_APPROVED.value


def test_unbalanced_shadow_accounting_halts_replay(tmp_path):
    fixture = build_shadow_replay_fixture(tmp_path, run_once_port=DeterministicRunOncePort(unbalanced_day=4))

    report = fixture.controller.run(fixture.policy)

    assert report.manifest.status is ReplayStatus.HALT
    assert len(report.daily_records) == 4
    assert report.daily_records[-1].failure_code == FailureCode.RECONCILIATION_FAILED.value


def test_same_frozen_inputs_produce_same_final_manifest_hash(tmp_path):
    first = build_shadow_replay_fixture(tmp_path / "first")
    second = build_shadow_replay_fixture(tmp_path / "second")

    first_report = first.controller.run(first.policy)
    second_report = second.controller.run(second.policy)

    assert first_report.manifest.artifact_hash == second_report.manifest.artifact_hash
