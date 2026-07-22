from __future__ import annotations

from itertools import islice
import threading
import time

import pytest

from veriquant.dynamic_acquisition import (
    AcquisitionScope,
    CAPABILITIES,
    CheckpointStore,
    CheckpointCorruptionError,
    ContentAddressedRowStore,
    DynamicAcquirer,
    PartitionStatus,
    QueryRejected,
    QueryResponse,
    ResourceBudget,
    build_plan,
)


def _scope(**overrides):
    values = dict(
        api_name="daily",
        fields=("ts_code", "trade_date", "close"),
        start_date="20160101",
        end_date="20251231",
        symbols=tuple(f"{index:06d}.SZ" for index in range(5000)),
    )
    values.update(overrides)
    return AcquisitionScope(**values)


class FakeTransport:
    offline_fixture = True

    def __init__(self, responses=None):
        self.responses = list(responses or [])
        self.calls = []

    def query(self, api_name, *, params, fields):
        self.calls.append((api_name, dict(params), fields))
        if self.responses:
            response = self.responses.pop(0)
            if isinstance(response, Exception):
                raise response
            return response
        return [{"ts_code": "000000.SZ", "trade_date": "20200101", "close": 1.0}]


class NetworkTransport(FakeTransport):
    offline_fixture = False


def test_registry_covers_all_verified_data_groups():
    expected = {
        "daily", "adj_factor", "daily_basic", "trade_cal", "stock_basic",
        "index_weight", "index_daily", "suspend_d", "stk_limit",
        "income", "balancesheet", "cashflow", "fina_indicator",
        "forecast", "express", "dividend", "moneyflow", "shibor",
    }
    assert expected.issubset(CAPABILITIES)
    assert {item.group.value for item in CAPABILITIES.values()} == {
        "MARKET", "REFERENCE", "FUNDAMENTAL", "EVENT",
        "FLOW_LIQUIDITY", "MACRO_REGIME",
    }


def test_allowlist_rejects_unknown_api_field_parameter_and_secrets():
    with pytest.raises(QueryRejected):
        build_plan(_scope(api_name="invented"))
    with pytest.raises(QueryRejected):
        tuple(build_plan(_scope(fields=("future_return_5d",))).iter_tasks())
    with pytest.raises(QueryRejected):
        tuple(build_plan(_scope(fixed_params={"token": "never"})).iter_tasks())
    with pytest.raises(QueryRejected):
        tuple(build_plan(_scope(fixed_params={"unknown": 1})).iter_tasks())


def test_5000_symbols_ten_year_plan_is_compact_deterministic_and_complete():
    first = build_plan(_scope(), date_chunk_days=120, symbol_chunk_size=200)
    second = build_plan(_scope(symbols=tuple(reversed(_scope().symbols))),
                        date_chunk_days=120, symbol_chunk_size=200)
    assert first.plan_hash == second.plan_hash
    assert first.estimate.symbol_count == 5000
    assert first.estimate.partition_count == 31 * 25
    assert first.__sizeof__() < 1024
    tasks = first.iter_tasks()
    sample = tuple(islice(tasks, 2))
    assert len(sample) == 2
    assert all(len(item.symbol_shard) == 200 for item in sample)
    assert sum(1 for _ in first.iter_tasks()) == first.estimate.partition_count


def test_budget_warns_without_silently_changing_research_scope():
    plan = build_plan(_scope(), budget=ResourceBudget(max_api_calls=10))
    assert plan.estimate.partition_count > 10
    assert plan.estimate.budget_warnings == ("ESTIMATED_API_CALLS_EXCEED_BUDGET",)
    assert plan.scope.symbols == tuple(sorted(_scope().symbols))
    assert plan.scope.start_date == "20160101"
    assert plan.scope.end_date == "20251231"


def test_parallel_budget_is_fail_closed_and_operationally_bounded():
    with pytest.raises(ValueError, match="1 to 64"):
        ResourceBudget(max_parallel_requests=65)


def test_default_network_gate_makes_no_call():
    client = NetworkTransport()
    result = DynamicAcquirer(client).run(build_plan(_scope(symbols=("000001.SZ",))))
    assert result.status is PartitionStatus.NETWORK_DISABLED
    assert client.calls == []
    assert "token" not in repr(result).lower()


def test_success_empty_truncated_and_failure_are_distinct(tmp_path):
    plan = build_plan(
        _scope(start_date="20200101", end_date="20200102", symbols=()),
        date_chunk_days=60,
    )
    cases = [
        ([{"ts_code": "x", "trade_date": "20200101", "close": 1.0}], PartitionStatus.SUCCESS),
        ([], PartitionStatus.EMPTY),
        (QueryResponse(rows=({"ts_code": "x", "trade_date": "20200101", "close": 1.0},), truncated=True), PartitionStatus.TRUNCATED),
        (RuntimeError("token=must-not-leak"), PartitionStatus.FAILED),
    ]
    for index, (response, status) in enumerate(cases):
        client = FakeTransport([response])
        result = DynamicAcquirer(
            client, checkpoint=CheckpointStore(tmp_path / f"{index}.json")
        ).run(plan, budget=ResourceBudget(max_api_calls=1))
        assert result.status is status
        assert "must-not-leak" not in repr(result)
        if status in {PartitionStatus.SUCCESS, PartitionStatus.EMPTY}:
            assert result.partitions[0].content_hash.startswith("sha256:")
        else:
            assert result.partitions[0].content_hash is None


def test_checkpoint_resume_skips_completed_partition(tmp_path):
    plan = build_plan(
        _scope(start_date="20200101", end_date="20201231", symbols=()),
        date_chunk_days=180,
    )
    store = CheckpointStore(tmp_path / "checkpoint.json")
    first_client = FakeTransport()
    first = DynamicAcquirer(first_client, checkpoint=store).run(
        plan, budget=ResourceBudget(max_api_calls=1)
    )
    assert first.status is PartitionStatus.BUDGET_EXHAUSTED
    assert len(first_client.calls) == 1
    second_client = FakeTransport()
    second = DynamicAcquirer(second_client, checkpoint=store).run(plan)
    assert second.status is PartitionStatus.SUCCESS
    assert second.skipped_completed == 1
    assert len(second_client.calls) == plan.estimate.partition_count - 1


def test_same_rows_have_stable_content_hash(tmp_path):
    plan = build_plan(_scope(start_date="20200101", end_date="20200102", symbols=()))
    first = DynamicAcquirer(FakeTransport()).run(plan)
    second = DynamicAcquirer(FakeTransport()).run(plan)
    assert first.partitions[0].content_hash == second.partitions[0].content_hash
    assert first.manifest_hash == second.manifest_hash


def test_offset_pagination_is_deterministic():
    scope = AcquisitionScope(
        api_name="stock_basic",
        fields=("ts_code", "list_date"),
        fixed_params={"list_status": "L"},
        offset_page_count=3,
        offset_page_size=1000,
    )
    plan = build_plan(scope)
    tasks = tuple(plan.iter_tasks())
    assert plan.estimate.partition_count == 3
    assert [task.params["offset"] for task in tasks] == [0, 1000, 2000]
    assert all(task.params["limit"] == 1000 for task in tasks)


def test_content_hash_ignores_transport_row_order():
    plan = build_plan(_scope(start_date="20200101", end_date="20200102", symbols=()))
    rows = [
        {"ts_code": "b", "trade_date": "20200101", "close": 2.0},
        {"ts_code": "a", "trade_date": "20200101", "close": 1.0},
    ]
    first = DynamicAcquirer(FakeTransport([rows])).run(plan)
    second = DynamicAcquirer(FakeTransport([list(reversed(rows))])).run(plan)
    assert first.partitions[0].content_hash == second.partitions[0].content_hash


def test_storage_budget_stops_without_shrinking_plan():
    plan = build_plan(_scope(start_date="20200101", end_date="20200102", symbols=()))
    result = DynamicAcquirer(FakeTransport()).run(
        plan, budget=ResourceBudget(max_storage_bytes=1)
    )
    assert result.status is PartitionStatus.BUDGET_EXHAUSTED
    assert result.partitions[0].failure_type == "MAX_STORAGE_BYTES"
    assert plan.scope.start_date == "20200101"
    assert plan.scope.end_date == "20200102"


def test_content_addressed_row_store_round_trips_and_detects_tamper(tmp_path):
    plan = build_plan(
        _scope(start_date="20200101", end_date="20200102", symbols=())
    )
    store = ContentAddressedRowStore(tmp_path / "rows")
    result = DynamicAcquirer(FakeTransport(), row_sink=store).run(plan)
    task = next(plan.iter_tasks())
    rows = store.load(task.task_id)
    assert result.status is PartitionStatus.SUCCESS
    assert rows[0]["close"] == 1.0
    artifact = next((tmp_path / "rows").glob("*.json"))
    artifact.write_text(artifact.read_text(encoding="utf-8") + " ", encoding="utf-8")
    # Whitespace alone does not alter JSON content; corrupt a stored value.
    content = artifact.read_text(encoding="utf-8").replace('"close":1.0', '"close":2.0')
    artifact.write_text(content, encoding="utf-8")
    with pytest.raises(ValueError, match="hash mismatch"):
        store.load(task.task_id)


def test_transport_cannot_return_unrequested_or_missing_fields():
    plan = build_plan(
        _scope(start_date="20200101", end_date="20200102", symbols=())
    )
    extra = DynamicAcquirer(
        FakeTransport([{
            "ts_code": "000001.SZ",
            "trade_date": "20200101",
            "close": 1.0,
            "future_return_5d": 9.0,
        }])
    ).run(plan)
    missing = DynamicAcquirer(
        FakeTransport([{"ts_code": "000001.SZ", "trade_date": "20200101"}])
    ).run(plan)
    assert extra.status is PartitionStatus.FAILED
    assert missing.status is PartitionStatus.FAILED


def test_corrupt_checkpoint_fails_closed(tmp_path):
    checkpoint = tmp_path / "checkpoint.json"
    checkpoint.write_text('{"plan_hash":"forged"}', encoding="utf-8")
    with pytest.raises(CheckpointCorruptionError):
        CheckpointStore(checkpoint).load("sha256:" + "0" * 64)


def test_failed_symbol_partition_does_not_hide_independent_success(tmp_path):
    plan = build_plan(
        _scope(
            start_date="20200101",
            end_date="20200102",
            symbols=("000001.SZ", "000002.SZ"),
        ),
        symbol_chunk_size=1,
    )
    client = FakeTransport([
        RuntimeError("provider failure with token=never-serialize"),
        [{"ts_code": "000002.SZ", "trade_date": "20200101", "close": 2.0}],
    ])
    result = DynamicAcquirer(
        client,
        checkpoint=CheckpointStore(tmp_path / "checkpoint.json"),
    ).run(plan)
    assert result.status is PartitionStatus.FAILED
    assert len(client.calls) == 2
    assert {item.status for item in result.partitions} == {
        PartitionStatus.FAILED,
        PartitionStatus.SUCCESS,
    }
    assert {item.failure_type for item in result.partitions if item.failure_type} == {
        "PROVIDER_QUERY_FAILED"
    }
    assert "never-serialize" not in repr(result)


def test_bounded_retry_recovers_without_exposing_provider_error():
    plan = build_plan(
        _scope(start_date="20200101", end_date="20200102", symbols=())
    )
    client = FakeTransport([
        TimeoutError("Authorization: secret"),
        [{"ts_code": "000001.SZ", "trade_date": "20200101", "close": 1.0}],
    ])
    result = DynamicAcquirer(client, max_attempts=2).run(plan)
    assert result.status is PartitionStatus.SUCCESS
    assert len(client.calls) == 2
    assert "Authorization" not in repr(result)


class ConcurrencyTransport:
    offline_fixture = True

    def __init__(self, *, reverse_delay=False, fail=False):
        self.reverse_delay = reverse_delay
        self.fail = fail
        self.calls = 0
        self.active = 0
        self.max_active = 0
        self.lock = threading.Lock()

    def query(self, api_name, *, params, fields):
        symbol = str(params.get("ts_code", "000000.SZ"))
        with self.lock:
            self.calls += 1
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            ordinal = int(symbol[:6]) if symbol[:6].isdigit() else 0
            delay_rank = 9 - ordinal if self.reverse_delay else ordinal
            time.sleep(0.003 * max(1, delay_rank))
            if self.fail:
                raise TimeoutError("Authorization: never serialize")
            values = {
                "ts_code": symbol,
                "trade_date": "20200101",
                "close": float(ordinal),
            }
            return [{field: values[field] for field in fields}]
        finally:
            with self.lock:
                self.active -= 1


def _parallel_plan(count=8):
    return build_plan(
        _scope(
            start_date="20200101",
            end_date="20200102",
            symbols=tuple(f"{index:06d}.SZ" for index in range(1, count + 1)),
        ),
        symbol_chunk_size=1,
    )


def test_parallel_request_cap_is_enforced():
    client = ConcurrencyTransport()
    result = DynamicAcquirer(client).run(
        _parallel_plan(), budget=ResourceBudget(max_parallel_requests=3)
    )
    assert result.status is PartitionStatus.SUCCESS
    assert client.max_active == 3
    assert client.calls == 8


def test_parallel_one_preserves_coordinator_thread_behavior():
    coordinator = threading.get_ident()
    called_from = []

    class ThreadAffineTransport(FakeTransport):
        def query(self, api_name, *, params, fields):
            called_from.append(threading.get_ident())
            return super().query(api_name, params=params, fields=fields)

    result = DynamicAcquirer(ThreadAffineTransport()).run(
        _parallel_plan(3), budget=ResourceBudget(max_parallel_requests=1)
    )
    assert result.status is PartitionStatus.SUCCESS
    assert set(called_from) == {coordinator}


def test_api_budget_reservation_never_overspends_under_concurrency():
    client = ConcurrencyTransport(fail=True)
    result = DynamicAcquirer(client, max_attempts=2).run(
        _parallel_plan(),
        budget=ResourceBudget(max_api_calls=5, max_parallel_requests=4),
    )
    assert client.calls == 5
    assert result.status is PartitionStatus.BUDGET_EXHAUSTED
    assert any(
        item.failure_type == "MAX_API_CALLS" for item in result.partitions
    )
    assert "Authorization" not in repr(result)


def test_completion_order_does_not_change_result_or_manifest_hash():
    fast_first = DynamicAcquirer(ConcurrencyTransport()).run(
        _parallel_plan(), budget=ResourceBudget(max_parallel_requests=4)
    )
    slow_first = DynamicAcquirer(ConcurrencyTransport(reverse_delay=True)).run(
        _parallel_plan(), budget=ResourceBudget(max_parallel_requests=4)
    )
    assert fast_first.partitions == slow_first.partitions
    assert fast_first.manifest_hash == slow_first.manifest_hash


class CoordinatorCheckpoint(CheckpointStore):
    def __init__(self, path):
        super().__init__(path)
        self.writer_threads = []

    def save(self, plan_hash, partitions):
        self.writer_threads.append(threading.get_ident())
        super().save(plan_hash, partitions)


def test_checkpoint_has_single_coordinator_writer_and_resume_skips_success(tmp_path):
    plan = _parallel_plan(5)
    checkpoint = CoordinatorCheckpoint(tmp_path / "parallel.json")
    coordinator_thread = threading.get_ident()
    first_client = ConcurrencyTransport()
    first = DynamicAcquirer(first_client, checkpoint=checkpoint).run(
        plan,
        budget=ResourceBudget(max_api_calls=2, max_parallel_requests=2),
    )
    assert first.status is PartitionStatus.BUDGET_EXHAUSTED
    assert checkpoint.writer_threads
    assert set(checkpoint.writer_threads) == {coordinator_thread}

    second_client = ConcurrencyTransport(reverse_delay=True)
    second = DynamicAcquirer(second_client, checkpoint=checkpoint).run(
        plan, budget=ResourceBudget(max_parallel_requests=3)
    )
    assert second.status is PartitionStatus.SUCCESS
    assert second.skipped_completed == 2
    assert second_client.calls == 3
    assert first.api_calls_used == 2
    assert second.api_calls_used == 5
    assert sum(item.api_calls_used for item in second.partitions) == 5
