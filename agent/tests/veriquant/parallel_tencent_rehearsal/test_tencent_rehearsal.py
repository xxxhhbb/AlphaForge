from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from src.veriquant.data.ashare.calendar import (
    FrozenTradingCalendar,
    FrozenTradingCalendarStore,
    calendar_session_hash,
)
from src.veriquant.tencent_rehearsal.api import (
    AdjustmentSafetyVerdict,
    DataAvailabilityStatus,
    LegacyTencentLoaderTransport,
    TencentBar,
    TencentBoundedRequest,
    TencentFetchPayload,
    TencentMarketVintageProvider,
    TencentRehearsalError,
    TencentRehearsalFailureCode,
    TencentRehearsalSnapshotProvider,
)


SHANGHAI = ZoneInfo("Asia/Shanghai")


class FixedClock:
    def __init__(self) -> None:
        self._value = datetime(2024, 5, 1, 9, 0, tzinfo=SHANGHAI)

    def __call__(self) -> datetime:
        value = self._value
        self._value += timedelta(seconds=1)
        return value


class FakeTransport:
    name = "offline-fixture-transport"
    requires_network = False

    def __init__(
        self,
        records: tuple[TencentBar, ...],
        *,
        adjustment: AdjustmentSafetyVerdict = AdjustmentSafetyVerdict.QFQ_ONLY,
        raw: bytes = b'{"fixture":"prices"}',
        availability_verified: bool = False,
    ) -> None:
        self.records = records
        self.adjustment = adjustment
        self.raw = raw
        self.availability_verified = availability_verified

    def fetch(self, request: TencentBoundedRequest) -> TencentFetchPayload:
        return TencentFetchPayload(
            raw_bytes=self.raw,
            records=self.records,
            adjustment_verdict=self.adjustment,
            raw_capture_kind="OFFLINE_TEST_FIXTURE",
            availability_semantics_verified=self.availability_verified,
            adjustment_attestation_hash=(
                "sha256:" + "a" * 64
                if self.adjustment is AdjustmentSafetyVerdict.RAW_VERIFIED
                else None
            ),
        )


def _calendar(calendar_root: Path) -> tuple[str, str]:
    sessions = ("2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05", "2024-01-08", "2024-01-09", "2024-01-10")
    calendar = FrozenTradingCalendar(
        calendar_id="cn-ashare-sse-szse",
        venue_codes=("SSE", "SZSE"),
        source="test-frozen-calendar",
        source_calendar="XSHG",
        source_version="test-v1",
        policy_version="sse-szse-shared-session-calendar/v1",
        session_start="2024-01-01",
        session_end="2024-01-10",
        session_dates=sessions,
        session_content_hash=calendar_session_hash(sessions),
    )
    FrozenTradingCalendarStore(calendar_root).persist(calendar)
    return calendar.calendar_id, calendar.artifact_hash


def _request(calendar_root: Path, **overrides) -> TencentBoundedRequest:
    calendar_id, calendar_hash = _calendar(calendar_root)
    values = {
        "dataset_id": "tencent-rehearsal-test",
        "request_id": "bounded-202401",
        "codes": ("000001.SZ", "600000.SH"),
        "start_date": "2024-01-02",
        "end_date": "2024-01-10",
        "calendar_id": calendar_id,
        "calendar_hash": calendar_hash,
    }
    values.update(overrides)
    return TencentBoundedRequest(**values)


def _bar(code: str, day: int, close: float = 10.0) -> TencentBar:
    return TencentBar(
        code=code,
        trade_date=f"2024-01-{day:02d}",
        available_at=datetime(2024, 1, day, 18, 0, tzinfo=SHANGHAI),
        open=close,
        high=close + 1.0,
        low=close - 1.0,
        close=close,
        volume=1000.0,
    )


def _base_records() -> tuple[TencentBar, ...]:
    return (
        _bar("000001.SZ", 2, 10.0),
        _bar("000001.SZ", 3, 11.0),
        _bar("600000.SH", 2, 20.0),
        _bar("600000.SH", 3, 21.0),
    )


def _manifest(handle) -> dict:
    return json.loads(Path(handle.manifest_path).read_text(encoding="utf-8"))["manifest"]


def test_default_is_offline_and_does_not_call_network(tmp_path: Path) -> None:
    request = _request(tmp_path / "calendar")
    provider = TencentRehearsalSnapshotProvider(
        artifact_root=tmp_path / "artifacts",
        calendar_root=tmp_path / "calendar",
        transport=LegacyTencentLoaderTransport(),
    )
    report = provider.availability(request)
    assert report.status is DataAvailabilityStatus.OFFLINE_DISABLED
    with pytest.raises(TencentRehearsalError) as caught:
        provider.freeze(request)
    assert caught.value.code is TencentRehearsalFailureCode.NETWORK_DISABLED


def test_request_rejects_path_traversal_and_unbounded_inputs(tmp_path: Path) -> None:
    with pytest.raises(TencentRehearsalError) as caught:
        _request(tmp_path / "calendar-a", dataset_id="../escape")
    assert caught.value.code is TencentRehearsalFailureCode.PATH_TRAVERSAL
    with pytest.raises(TencentRehearsalError):
        _request(tmp_path / "calendar-b", codes=tuple(f"{index:06d}.SZ" for index in range(11)))
    with pytest.raises(TencentRehearsalError):
        _request(tmp_path / "calendar-c", start_date="2022-01-01")


def test_same_frozen_input_and_clock_reproduce_hashes(tmp_path: Path) -> None:
    request = _request(tmp_path / "calendar")
    first = TencentRehearsalSnapshotProvider(
        artifact_root=tmp_path / "one",
        calendar_root=tmp_path / "calendar",
        transport=FakeTransport(_base_records()),
        clock=FixedClock(),
    ).freeze(request)
    second = TencentRehearsalSnapshotProvider(
        artifact_root=tmp_path / "two",
        calendar_root=tmp_path / "calendar",
        transport=FakeTransport(_base_records()),
        clock=FixedClock(),
    ).freeze(request)
    assert first.manifest_hash == second.manifest_hash
    assert first.raw_content_hash == second.raw_content_hash
    assert first.normalized_content_hash == second.normalized_content_hash


def test_future_perturbation_does_not_change_past_vintage(tmp_path: Path) -> None:
    request = _request(tmp_path / "calendar")
    first_records = _base_records() + (_bar("000001.SZ", 10, 12.0), _bar("600000.SH", 10, 22.0))
    second_records = _base_records() + (_bar("000001.SZ", 10, 99.0), _bar("600000.SH", 10, 88.0))
    first = TencentRehearsalSnapshotProvider(
        artifact_root=tmp_path / "artifacts",
        calendar_root=tmp_path / "calendar",
        transport=FakeTransport(first_records, raw=b'{"revision":1}'),
        clock=FixedClock(),
    ).freeze(request)
    second = TencentRehearsalSnapshotProvider(
        artifact_root=tmp_path / "artifacts",
        calendar_root=tmp_path / "calendar",
        transport=FakeTransport(second_records, raw=b'{"revision":2}'),
        clock=FixedClock(),
    ).freeze(request)
    reader = TencentMarketVintageProvider(artifact_root=tmp_path / "artifacts")
    cutoff = datetime(2024, 1, 4, 9, 0, tzinfo=SHANGHAI)
    vintage_one = reader.as_of(dataset_id=request.dataset_id, snapshot_id=first.snapshot_id, decision_time=cutoff)
    vintage_two = reader.as_of(dataset_id=request.dataset_id, snapshot_id=second.snapshot_id, decision_time=cutoff)
    assert vintage_one.rows == vintage_two.rows
    assert vintage_one.vintage_hash == vintage_two.vintage_hash
    assert vintage_one.source_snapshot_hash != vintage_two.source_snapshot_hash


@pytest.mark.parametrize(
    "verdict",
    [AdjustmentSafetyVerdict.QFQ_ONLY, AdjustmentSafetyVerdict.UNKNOWN],
)
def test_adjusted_or_unknown_data_is_auxiliary_and_never_promotable(tmp_path: Path, verdict) -> None:
    request = _request(tmp_path / "calendar")
    handle = TencentRehearsalSnapshotProvider(
        artifact_root=tmp_path / "artifacts",
        calendar_root=tmp_path / "calendar",
        transport=FakeTransport(_base_records(), adjustment=verdict),
        clock=FixedClock(),
    ).freeze(request)
    manifest = _manifest(handle)
    assert manifest["authority_class"] == "AUXILIARY_RESEARCH_EVIDENCE"
    assert manifest["pit_eligible"] is False
    assert manifest["promotion_eligible"] is False
    assert manifest["official_v1_tushare_smoke"] is False
    assert manifest["historical_universe"] is False
    assert manifest["universe_policy"] == "FIXED_OPERATIONAL_SAMPLE"


def test_raw_verified_requires_attestation_and_availability_proof(tmp_path: Path) -> None:
    request = _request(tmp_path / "calendar")
    handle = TencentRehearsalSnapshotProvider(
        artifact_root=tmp_path / "artifacts",
        calendar_root=tmp_path / "calendar",
        transport=FakeTransport(
            _base_records(),
            adjustment=AdjustmentSafetyVerdict.RAW_VERIFIED,
            availability_verified=True,
        ),
        clock=FixedClock(),
    ).freeze(request)
    manifest = _manifest(handle)
    assert manifest["pit_eligible"] is True
    assert manifest["promotion_eligible"] is False
    assert manifest["authority_class"] == "AUXILIARY_RESEARCH_EVIDENCE"


def test_missing_calendar_and_missing_transport_fail_closed(tmp_path: Path) -> None:
    request = _request(tmp_path / "existing-calendar")
    missing_calendar = TencentRehearsalSnapshotProvider(
        artifact_root=tmp_path / "artifacts",
        calendar_root=tmp_path / "missing-calendar",
        transport=FakeTransport(_base_records()),
    )
    with pytest.raises(TencentRehearsalError) as caught:
        missing_calendar.freeze(request)
    assert caught.value.code is TencentRehearsalFailureCode.CALENDAR_MISSING
    no_transport = TencentRehearsalSnapshotProvider(
        artifact_root=tmp_path / "artifacts",
        calendar_root=tmp_path / "existing-calendar",
    )
    with pytest.raises(TencentRehearsalError) as caught:
        no_transport.freeze(request)
    assert caught.value.code is TencentRehearsalFailureCode.TRANSPORT_UNAVAILABLE


def test_tampered_or_missing_snapshot_content_fails_closed(tmp_path: Path) -> None:
    request = _request(tmp_path / "calendar")
    handle = TencentRehearsalSnapshotProvider(
        artifact_root=tmp_path / "artifacts",
        calendar_root=tmp_path / "calendar",
        transport=FakeTransport(_base_records()),
        clock=FixedClock(),
    ).freeze(request)
    Path(handle.manifest_path).with_name("raw.bin").write_bytes(b"tampered")
    with pytest.raises(TencentRehearsalError) as caught:
        TencentMarketVintageProvider(artifact_root=tmp_path / "artifacts").as_of(
            dataset_id=request.dataset_id,
            snapshot_id=handle.snapshot_id,
            decision_time=datetime(2024, 1, 4, 9, 0, tzinfo=SHANGHAI),
        )
    assert caught.value.code is TencentRehearsalFailureCode.HASH_MISMATCH


def test_secret_like_raw_payload_is_rejected_without_echo(tmp_path: Path) -> None:
    request = _request(tmp_path / "calendar")
    provider = TencentRehearsalSnapshotProvider(
        artifact_root=tmp_path / "artifacts",
        calendar_root=tmp_path / "calendar",
        transport=FakeTransport(_base_records(), raw=b'{"access_token":"DO_NOT_ECHO"}'),
    )
    with pytest.raises(TencentRehearsalError) as caught:
        provider.freeze(request)
    assert caught.value.code is TencentRehearsalFailureCode.SECRET_MATERIAL
    assert "DO_NOT_ECHO" not in str(caught.value)
    assert "DO_NOT_ECHO" not in repr(dict(caught.value.details))


def test_row_and_scope_bounds_fail_closed(tmp_path: Path) -> None:
    request = _request(tmp_path / "calendar", max_rows_per_code=1)
    provider = TencentRehearsalSnapshotProvider(
        artifact_root=tmp_path / "artifacts",
        calendar_root=tmp_path / "calendar",
        transport=FakeTransport(_base_records()),
    )
    with pytest.raises(TencentRehearsalError) as caught:
        provider.freeze(request)
    assert caught.value.code is TencentRehearsalFailureCode.ROW_LIMIT_EXCEEDED


def test_snapshot_locator_rejects_arbitrary_paths(tmp_path: Path) -> None:
    reader = TencentMarketVintageProvider(artifact_root=tmp_path / "artifacts")
    with pytest.raises(TencentRehearsalError) as caught:
        reader.as_of(
            dataset_id="../../outside",
            snapshot_id="a" * 64,
            decision_time=datetime(2024, 1, 4, 9, 0, tzinfo=SHANGHAI),
        )
    assert caught.value.code is TencentRehearsalFailureCode.PATH_TRAVERSAL
