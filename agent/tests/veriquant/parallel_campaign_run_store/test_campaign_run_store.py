from datetime import datetime, timezone

import pytest

from src.veriquant.campaign_run_store import (
    CampaignRunEventType,
    CampaignRunRecord,
    CampaignRunStoreStatus,
    FileCampaignRunStore,
    InMemoryCampaignRunStore,
)
from src.veriquant.contracts import ContractValidationError, FailureCode


HASH_A = "sha256:" + "a" * 64
HASH_B = "sha256:" + "b" * 64
HASH_C = "sha256:" + "c" * 64


def _record(run_id: str = "runstore") -> CampaignRunRecord:
    return CampaignRunRecord(
        run_id=run_id,
        request_hash=HASH_A,
        campaign_hash=HASH_B,
        route_hash=HASH_C,
        protocol_plan_hash=HASH_A,
        status=CampaignRunStoreStatus.CREATED,
    )


def test_in_memory_create_and_replay_is_stable() -> None:
    store = InMemoryCampaignRunStore()
    record = store.create_run(_record())
    replayed = store.replay("runstore")
    assert replayed.run_id == record.run_id
    assert replayed.event_log_hash == store.events["runstore"][-1].artifact_hash


def test_file_store_rejects_path_traversal(tmp_path) -> None:
    store = FileCampaignRunStore(tmp_path)
    with pytest.raises(ContractValidationError) as exc:
        store.create_run(_record("../escape"))
    assert exc.value.code is FailureCode.UNAUTHORIZED_CAPABILITY


def test_file_store_detects_event_tamper(tmp_path) -> None:
    store = FileCampaignRunStore(tmp_path)
    store.create_run(_record())
    event_path = tmp_path / "runstore" / "events.jsonl"
    text = event_path.read_text(encoding="utf-8")
    event_path.write_text(text.replace("RUN_CREATED", "RUN_BLOCKED"), encoding="utf-8")
    with pytest.raises(ContractValidationError) as exc:
        store.replay("runstore")
    assert exc.value.code is FailureCode.LEDGER_CORRUPTION


def test_duplicate_run_identity_is_idempotent(tmp_path) -> None:
    store = FileCampaignRunStore(tmp_path)
    first = store.create_run(_record())
    second = store.create_run(_record())
    assert first.artifact_hash == second.artifact_hash
    with pytest.raises(ContractValidationError):
        store.create_run(
            CampaignRunRecord(
                run_id="runstore",
                request_hash=HASH_C,
                campaign_hash=HASH_B,
                route_hash=HASH_C,
                protocol_plan_hash=HASH_A,
                status=CampaignRunStoreStatus.CREATED,
            )
        )


def test_append_event_hash_chain(tmp_path) -> None:
    store = FileCampaignRunStore(tmp_path)
    store.create_run(_record())
    event = store.append_event(
        "runstore",
        CampaignRunEventType.DATA_BOUND,
        {"data_binding_hash": HASH_A},
        event_time=datetime(2026, 7, 19, tzinfo=timezone.utc),
    )
    replayed = store.replay("runstore")
    assert replayed.event_log_hash == event.artifact_hash
