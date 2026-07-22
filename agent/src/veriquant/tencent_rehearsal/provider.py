"""Fail-closed Tencent operational rehearsal snapshot provider."""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Protocol
from zoneinfo import ZoneInfo

from ..contracts.base import canonicalize
from ..data.ashare.calendar import FrozenTradingCalendarStore
from ..data.bitemporal import canonical_content_hash
from .models import (
    AdjustmentSafetyVerdict,
    DataAvailabilityStatus,
    SOURCE_MARKER,
    TencentBar,
    TencentBoundedRequest,
    TencentDataAvailability,
    TencentFetchPayload,
    TencentMarketVintage,
    TencentRehearsalError,
    TencentRehearsalFailureCode,
    TencentSnapshotHandle,
    TencentSnapshotManifest,
    rehearsal_fail,
    require_safe_id,
)


SHANGHAI = ZoneInfo("Asia/Shanghai")
_HASH = re.compile(r"^sha256:[0-9a-f]{64}$")
_SECRET_MARKERS = (b"authorization", b"bearer ", b"api_key", b"api-key", b"access_token")


def _sha256(content: bytes) -> str:
    return "sha256:" + hashlib.sha256(content).hexdigest()


def _json_bytes(value: object) -> bytes:
    return (
        json.dumps(
            canonicalize(value),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("wb") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _safe_snapshot_path(root: Path, dataset_id: str, snapshot_id: str) -> Path:
    require_safe_id(dataset_id, "dataset_id")
    if re.fullmatch(r"[0-9a-f]{64}", snapshot_id) is None:
        rehearsal_fail(TencentRehearsalFailureCode.PATH_TRAVERSAL, "snapshot_id is invalid")
    resolved_root = root.resolve()
    resolved = (resolved_root / dataset_id / snapshot_id).resolve()
    try:
        resolved.relative_to(resolved_root)
    except ValueError as exc:
        raise TencentRehearsalError(
            TencentRehearsalFailureCode.PATH_TRAVERSAL,
            "snapshot path escapes the configured artifact root",
        ) from exc
    return resolved


class TencentFetchTransport(Protocol):
    """Minimal typed port; implementations cannot receive arbitrary paths or code."""

    name: str
    requires_network: bool

    def fetch(self, request: TencentBoundedRequest) -> TencentFetchPayload: ...


class LegacyTencentLoaderTransport:
    """Explicit network adapter around the existing qfq Tencent loader.

    The existing loader does not preserve wire bytes or prove raw prices, so
    this adapter records its canonical loader output and always reports
    ``QFQ_ONLY``.  It can never make a PIT or promotion claim.
    """

    name = "vibe-backtest-tencent-loader"
    requires_network = True

    def fetch(self, request: TencentBoundedRequest) -> TencentFetchPayload:
        from backtest.loaders.tencent_loader import DataLoader

        frames = DataLoader().fetch(
            list(request.codes),
            request.start_date,
            request.end_date,
            interval=request.interval,
            fields=["open", "high", "low", "close", "volume"],
        )
        records: list[TencentBar] = []
        raw_rows: dict[str, list[dict[str, object]]] = {}
        for code in request.codes:
            frame = frames.get(code)
            if frame is None or frame.empty:
                continue
            code_rows: list[dict[str, object]] = []
            for index, row in frame.sort_index().iterrows():
                trade_date = index.date().isoformat()
                available_at = datetime.combine(index.date() + timedelta(days=1), time.min, SHANGHAI)
                payload = {
                    "trade_date": trade_date,
                    "open": float(row["open"]),
                    "high": float(row["high"]),
                    "low": float(row["low"]),
                    "close": float(row["close"]),
                    "volume": float(row["volume"]),
                }
                code_rows.append(payload)
                records.append(TencentBar(code=code, available_at=available_at, **payload))
            raw_rows[code] = code_rows
        raw_bytes = _json_bytes(
            {
                "schema_version": "veriquant.tencent-loader-canonical-output/v1",
                "source": SOURCE_MARKER,
                "adjustment": "qfq",
                "rows": raw_rows,
            }
        )
        return TencentFetchPayload(
            raw_bytes=raw_bytes,
            records=tuple(records),
            adjustment_verdict=AdjustmentSafetyVerdict.QFQ_ONLY,
            raw_capture_kind="LEGACY_LOADER_CANONICAL_OUTPUT",
            availability_semantics_verified=False,
        )


class TencentRehearsalSnapshotProvider:
    """Freeze a bounded Tencent payload as a content-addressed auxiliary artifact."""

    def __init__(
        self,
        *,
        artifact_root: str | Path | None = None,
        calendar_root: str | Path | None = None,
        network_enabled: bool = False,
        transport: TencentFetchTransport | None = None,
        clock=None,
    ) -> None:
        repo_root = Path(__file__).resolve().parents[4]
        self.artifact_root = (
            Path(artifact_root).resolve()
            if artifact_root is not None
            else repo_root / ".autobuild" / "veriquant" / "tencent-rehearsal"
        )
        self.calendar_root = (
            Path(calendar_root).resolve()
            if calendar_root is not None
            else repo_root / ".autobuild" / "veriquant" / "calendars"
        )
        self.network_enabled = network_enabled
        self.transport = transport
        self._clock = clock or (lambda: datetime.now(SHANGHAI))

    def availability(self, request: TencentBoundedRequest) -> TencentDataAvailability:
        if self.transport is None:
            return TencentDataAvailability(
                status=DataAvailabilityStatus.UNAVAILABLE,
                network_enabled=self.network_enabled,
                transport_available=False,
                message="typed Tencent transport is unavailable",
                request_hash=request.artifact_hash,
                failure_code=TencentRehearsalFailureCode.TRANSPORT_UNAVAILABLE,
            )
        if self.transport.requires_network and not self.network_enabled:
            return TencentDataAvailability(
                status=DataAvailabilityStatus.OFFLINE_DISABLED,
                network_enabled=False,
                transport_available=True,
                message="network remains disabled by default",
                request_hash=request.artifact_hash,
                failure_code=TencentRehearsalFailureCode.NETWORK_DISABLED,
            )
        return TencentDataAvailability(
            status=DataAvailabilityStatus.AVAILABLE,
            network_enabled=self.network_enabled,
            transport_available=True,
            message="bounded rehearsal transport is available",
            request_hash=request.artifact_hash,
        )

    def freeze(self, request: TencentBoundedRequest) -> TencentSnapshotHandle:
        if not isinstance(request, TencentBoundedRequest):
            rehearsal_fail(TencentRehearsalFailureCode.INVALID_REQUEST, "typed request is required")
        self._verify_calendar(request)
        availability = self.availability(request)
        if availability.status is not DataAvailabilityStatus.AVAILABLE:
            rehearsal_fail(
                availability.failure_code or TencentRehearsalFailureCode.TRANSPORT_UNAVAILABLE,
                availability.message,
            )
        assert self.transport is not None
        try:
            payload = self.transport.fetch(request)
        except TencentRehearsalError:
            raise
        except Exception as exc:
            raise TencentRehearsalError(
                TencentRehearsalFailureCode.FETCH_FAILED,
                "Tencent transport failed without exposing provider payload",
                details={"error_type": type(exc).__name__},
            ) from exc
        if not isinstance(payload, TencentFetchPayload):
            rehearsal_fail(TencentRehearsalFailureCode.INVALID_RECORD, "transport payload is not typed")
        self._reject_secret_material(payload.raw_bytes)
        records = self._validate_records(request, payload.records)
        normalized_bytes = _json_bytes(
            {
                "schema_version": "veriquant.tencent-normalized-bars/v1",
                "source": SOURCE_MARKER,
                "records": records,
            }
        )
        raw_hash = _sha256(payload.raw_bytes)
        normalized_hash = _sha256(normalized_bytes)
        fetched_at = self._clock()
        frozen_at = self._clock()
        counts = {code: sum(1 for record in records if record.code == code) for code in request.codes}
        manifest = TencentSnapshotManifest(
            request=request,
            request_hash=request.artifact_hash,
            fetched_at=fetched_at,
            frozen_at=frozen_at,
            raw_content_hash=raw_hash,
            normalized_content_hash=normalized_hash,
            calendar_hash=request.calendar_hash,
            row_count=len(records),
            rows_by_code=counts,
            adjustment_verdict=payload.adjustment_verdict,
            adjustment_attestation_hash=payload.adjustment_attestation_hash,
            raw_capture_kind=payload.raw_capture_kind,
            availability_semantics_verified=payload.availability_semantics_verified,
            pit_eligible=(
                payload.adjustment_verdict is AdjustmentSafetyVerdict.RAW_VERIFIED
                and payload.availability_semantics_verified
                and payload.adjustment_attestation_hash is not None
            ),
        )
        manifest_hash = manifest.artifact_hash
        snapshot_id = manifest_hash.removeprefix("sha256:")
        directory = _safe_snapshot_path(self.artifact_root, request.dataset_id, snapshot_id)
        manifest_bytes = _json_bytes(
            {
                "schema_version": "veriquant.tencent-snapshot-envelope/v1",
                "manifest": manifest,
                "manifest_hash": manifest_hash,
            }
        )
        if directory.exists():
            existing = directory / "manifest.json"
            if not existing.is_file() or existing.read_bytes() != manifest_bytes:
                rehearsal_fail(
                    TencentRehearsalFailureCode.SNAPSHOT_CORRUPT,
                    "content-addressed snapshot path contains different or incomplete content",
                )
        else:
            directory.mkdir(parents=True, exist_ok=False)
            _atomic_write(directory / "raw.bin", payload.raw_bytes)
            _atomic_write(directory / "records.json", normalized_bytes)
            _atomic_write(directory / "manifest.json", manifest_bytes)
        return TencentSnapshotHandle(
            dataset_id=request.dataset_id,
            snapshot_id=snapshot_id,
            manifest_hash=manifest_hash,
            raw_content_hash=raw_hash,
            normalized_content_hash=normalized_hash,
            calendar_hash=request.calendar_hash,
            manifest_path=str(directory / "manifest.json"),
        )

    def _verify_calendar(self, request: TencentBoundedRequest) -> None:
        calendar_path = (
            self.calendar_root
            / request.calendar_id
            / request.calendar_hash.removeprefix("sha256:")
            / "manifest.json"
        )
        try:
            calendar = FrozenTradingCalendarStore(self.calendar_root).load_manifest(calendar_path)
        except Exception as exc:
            raise TencentRehearsalError(
                TencentRehearsalFailureCode.CALENDAR_MISSING,
                "frozen SSE/SZSE calendar is missing or invalid",
                details={"error_type": type(exc).__name__},
            ) from exc
        if calendar.artifact_hash != request.calendar_hash:
            rehearsal_fail(TencentRehearsalFailureCode.CALENDAR_INVALID, "calendar hash mismatch")
        if calendar.session_start > request.start_date or calendar.session_end < request.end_date:
            rehearsal_fail(TencentRehearsalFailureCode.CALENDAR_INVALID, "calendar does not cover request")

    @staticmethod
    def _reject_secret_material(raw_bytes: bytes) -> None:
        lowered = raw_bytes.lower()
        if any(marker in lowered for marker in _SECRET_MARKERS):
            rehearsal_fail(
                TencentRehearsalFailureCode.SECRET_MATERIAL,
                "raw provider artifact contains forbidden secret-like material",
            )

    @staticmethod
    def _validate_records(
        request: TencentBoundedRequest,
        records: tuple[TencentBar, ...],
    ) -> tuple[TencentBar, ...]:
        ordered = tuple(sorted(records, key=lambda row: (row.code, row.trade_date, row.available_at)))
        if len(ordered) > request.max_total_rows:
            rehearsal_fail(TencentRehearsalFailureCode.ROW_LIMIT_EXCEEDED, "total row bound exceeded")
        seen: set[tuple[str, str]] = set()
        counts = {code: 0 for code in request.codes}
        for row in ordered:
            if not isinstance(row, TencentBar) or row.code not in counts:
                rehearsal_fail(TencentRehearsalFailureCode.INVALID_RECORD, "record lies outside request codes")
            if not request.start_date <= row.trade_date <= request.end_date:
                rehearsal_fail(TencentRehearsalFailureCode.INVALID_RECORD, "record lies outside request dates")
            identity = (row.code, row.trade_date)
            if identity in seen:
                rehearsal_fail(TencentRehearsalFailureCode.INVALID_RECORD, "duplicate code/date record")
            seen.add(identity)
            counts[row.code] += 1
            if counts[row.code] > request.max_rows_per_code:
                rehearsal_fail(TencentRehearsalFailureCode.ROW_LIMIT_EXCEEDED, "per-code row bound exceeded")
        if not ordered or any(value == 0 for value in counts.values()):
            rehearsal_fail(
                TencentRehearsalFailureCode.EMPTY_RESULT,
                "every fixed-sample symbol must have at least one row",
            )
        return ordered


class TencentMarketVintageProvider:
    """Read verified snapshot bytes and derive an as-of auxiliary vintage."""

    def __init__(self, *, artifact_root: str | Path) -> None:
        self.artifact_root = Path(artifact_root).resolve()

    def as_of(
        self,
        *,
        dataset_id: str,
        snapshot_id: str,
        decision_time: datetime,
    ) -> TencentMarketVintage:
        directory = _safe_snapshot_path(self.artifact_root, dataset_id, snapshot_id)
        manifest_path = directory / "manifest.json"
        raw_path = directory / "raw.bin"
        records_path = directory / "records.json"
        try:
            envelope = json.loads(manifest_path.read_text(encoding="utf-8"))
            records_payload = json.loads(records_path.read_text(encoding="utf-8"))
            raw_bytes = raw_path.read_bytes()
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise TencentRehearsalError(
                TencentRehearsalFailureCode.SNAPSHOT_CORRUPT,
                "snapshot files are missing or unreadable",
                details={"error_type": type(exc).__name__},
            ) from exc
        if envelope.get("schema_version") != "veriquant.tencent-snapshot-envelope/v1":
            rehearsal_fail(TencentRehearsalFailureCode.SNAPSHOT_CORRUPT, "snapshot schema is unsupported")
        manifest_payload = envelope.get("manifest")
        if not isinstance(manifest_payload, dict):
            rehearsal_fail(TencentRehearsalFailureCode.SNAPSHOT_CORRUPT, "typed manifest is missing")
        try:
            manifest = TencentSnapshotManifest.from_payload(manifest_payload)
        except Exception as exc:
            raise TencentRehearsalError(
                TencentRehearsalFailureCode.SNAPSHOT_CORRUPT,
                "snapshot manifest failed contract validation",
                details={"error_type": type(exc).__name__},
            ) from exc
        if envelope.get("manifest_hash") != manifest.artifact_hash or snapshot_id != manifest.artifact_hash.removeprefix("sha256:"):
            rehearsal_fail(TencentRehearsalFailureCode.HASH_MISMATCH, "manifest hash mismatch")
        if _sha256(raw_bytes) != manifest.raw_content_hash or _sha256(records_path.read_bytes()) != manifest.normalized_content_hash:
            rehearsal_fail(TencentRehearsalFailureCode.HASH_MISMATCH, "snapshot content hash mismatch")
        raw_records = records_payload.get("records") if isinstance(records_payload, dict) else None
        if not isinstance(raw_records, list):
            rehearsal_fail(TencentRehearsalFailureCode.SNAPSHOT_CORRUPT, "normalized records are missing")
        try:
            records = tuple(TencentBar.from_payload(value) for value in raw_records)
        except Exception as exc:
            raise TencentRehearsalError(
                TencentRehearsalFailureCode.SNAPSHOT_CORRUPT,
                "normalized records failed validation",
                details={"error_type": type(exc).__name__},
            ) from exc
        visible = tuple(
            row
            for row in records
            if row.available_at <= decision_time and row.trade_date <= decision_time.astimezone(SHANGHAI).date().isoformat()
        )
        if not visible:
            rehearsal_fail(TencentRehearsalFailureCode.EMPTY_RESULT, "no rows are visible as of decision_time")
        vintage_hash = canonical_content_hash(
            {
                "schema_version": "veriquant.tencent-market-vintage-content/v1",
                "dataset_id": dataset_id,
                "decision_time": decision_time,
                "calendar_hash": manifest.calendar_hash,
                "adjustment_verdict": manifest.adjustment_verdict,
                "pit_eligible": manifest.pit_eligible,
                "rows": visible,
            }
        )
        return TencentMarketVintage(
            dataset_id=dataset_id,
            source_snapshot_hash=manifest.artifact_hash,
            decision_time=decision_time,
            calendar_hash=manifest.calendar_hash,
            adjustment_verdict=manifest.adjustment_verdict,
            pit_eligible=manifest.pit_eligible,
            rows=visible,
            vintage_hash=vintage_hash,
        )
