from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from veriquant.contracts import FailureCode
from veriquant.research_run_controller import (
    ResearchRunIdentityStore,
    ResearchRunLifecycle,
    ResearchRunManifest,
)
from veriquant.run_recovery.api import RecoveryError


HASHES = tuple("sha256:" + char * 64 for char in "1234567")


def _manifest(run_id: str) -> ResearchRunManifest:
    return ResearchRunManifest(
        run_id=run_id,
        campaign_hash=HASHES[0],
        intent_hash=HASHES[1],
        data_plan_hash=HASHES[2],
        route_plan_hash=HASHES[3],
        policy_hash=HASHES[4],
        code_hash=HASHES[5],
        selected_group_ids=("market_structure",),
        candidate_request_hashes=(),
        created_at=datetime(2026, 7, 20, 9, 30, tzinfo=timezone.utc),
        runtime_version="research_os_v3_u4",
        provider_id="bounded_provider",
        model_id="test_model",
        data_snapshot_hash=HASHES[6],
        remaining_iterations=4,
        remaining_tool_calls=8,
        remaining_candidate_slots=6,
    )


def _active(store: ResearchRunIdentityStore, run_id: str) -> ResearchRunManifest:
    planned = store.ensure_manifest(_manifest(run_id))
    return store.ensure_manifest(
        replace(planned, current_stage=ResearchRunLifecycle.RESEARCHING),
        expected_artifact_hash=planned.artifact_hash,
    )


def test_cancel_and_finalize_race_is_fenced_fifty_times(tmp_path):
    """Whichever writer wins stays canonical; stale completion never regresses it."""

    for index in range(50):
        store = ResearchRunIdentityStore(tmp_path / f"race_{index}")
        active = _active(store, f"race_{index}")
        barrier = threading.Barrier(2)
        cancelled = replace(
            active,
            current_stage=ResearchRunLifecycle.CANCELLED,
            typed_failures=("CANCELLED",),
        )
        completed = replace(
            active,
            current_stage=ResearchRunLifecycle.COMPLETED,
            remaining_iterations=0,
            remaining_tool_calls=0,
            remaining_candidate_slots=2,
            candidate_count=4,
        )

        def attempt(value):
            barrier.wait()
            try:
                return store.ensure_manifest(
                    value,
                    expected_artifact_hash=active.artifact_hash,
                ).current_stage
            except RecoveryError as exc:
                assert exc.code is FailureCode.ARTIFACT_COLLISION
                return None

        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = tuple(executor.map(attempt, (cancelled, completed)))

        assert sum(item is not None for item in outcomes) == 1
        final = store.load_manifest(active.run_id)
        assert final is not None
        assert final.current_stage in {
            ResearchRunLifecycle.CANCELLED,
            ResearchRunLifecycle.COMPLETED,
        }
        assert final.current_stage in outcomes


def test_cancelled_manifest_survives_stale_finalize(tmp_path):
    store = ResearchRunIdentityStore(tmp_path)
    active = _active(store, "cancel_persisted")
    cancelled = store.ensure_manifest(
        replace(
            active,
            current_stage=ResearchRunLifecycle.CANCELLED,
            typed_failures=("CANCELLED",),
        ),
        expected_artifact_hash=active.artifact_hash,
    )

    stale_final = replace(
        active,
        current_stage=ResearchRunLifecycle.COMPLETED,
        remaining_iterations=0,
        remaining_tool_calls=0,
        candidate_count=4,
    )
    with pytest.raises(RecoveryError) as fenced:
        store.ensure_manifest(stale_final, expected_artifact_hash=active.artifact_hash)
    with pytest.raises(RecoveryError) as terminal_guard:
        store.ensure_manifest(stale_final)

    assert fenced.value.code is FailureCode.ARTIFACT_COLLISION
    assert terminal_guard.value.code is FailureCode.INVALID_STATE
    assert store.load_manifest(active.run_id).artifact_hash == cancelled.artifact_hash


def test_resume_uses_one_manifest_and_preserves_committed_counters(tmp_path):
    store = ResearchRunIdentityStore(tmp_path)
    active = _active(store, "resume_once")
    progressed = store.ensure_manifest(
        replace(
            active,
            remaining_iterations=2,
            remaining_tool_calls=5,
            remaining_candidate_slots=3,
            candidate_count=3,
            trial_count=3,
            typed_failures=(FailureCode.INVALID_VALUE.value,),
        ),
        expected_artifact_hash=active.artifact_hash,
    )
    cancelled = store.ensure_manifest(
        replace(
            progressed,
            current_stage=ResearchRunLifecycle.CANCELLED,
            typed_failures=tuple(sorted((*progressed.typed_failures, "CANCELLED"))),
        ),
        expected_artifact_hash=progressed.artifact_hash,
    )
    resumed = store.ensure_manifest(
        replace(
            cancelled,
            current_stage=ResearchRunLifecycle.PLANNING,
            typed_failures=tuple(item for item in cancelled.typed_failures if item != "CANCELLED"),
        ),
        expected_artifact_hash=cancelled.artifact_hash,
        allow_cancelled_resume=True,
    )
    replay = store.ensure_manifest(resumed)

    assert replay.artifact_hash == resumed.artifact_hash
    assert replay.candidate_count == 3
    assert replay.trial_count == 3
    assert replay.remaining_iterations == 2
    assert replay.typed_failures == (FailureCode.INVALID_VALUE.value,)


@pytest.mark.parametrize(
    "proposal",
    (
        lambda completed: replace(completed, current_stage=ResearchRunLifecycle.RESEARCHING),
        lambda completed: replace(completed, candidate_count=0),
        lambda completed: replace(completed, remaining_iterations=4),
    ),
)
def test_terminal_regression_and_counter_rewrites_fail_closed(tmp_path, proposal):
    store = ResearchRunIdentityStore(tmp_path)
    active = _active(store, "terminal_guard")
    completed = store.ensure_manifest(
        replace(
            active,
            current_stage=ResearchRunLifecycle.COMPLETED,
            remaining_iterations=0,
            remaining_tool_calls=0,
            remaining_candidate_slots=2,
            candidate_count=4,
            trial_count=4,
        ),
        expected_artifact_hash=active.artifact_hash,
    )

    with pytest.raises(RecoveryError) as exc:
        store.ensure_manifest(proposal(completed))

    assert exc.value.code is FailureCode.INVALID_STATE
    assert store.load_manifest(completed.run_id).artifact_hash == completed.artifact_hash


def test_canonical_hash_and_replay_are_stable_across_roots(tmp_path):
    first = ResearchRunIdentityStore(tmp_path / "first").ensure_manifest(_manifest("stable_replay"))
    second_store = ResearchRunIdentityStore(tmp_path / "second")
    second = second_store.ensure_manifest(_manifest("stable_replay"))
    replay = second_store.ensure_manifest(_manifest("stable_replay"))

    assert first.artifact_hash == second.artifact_hash == replay.artifact_hash
