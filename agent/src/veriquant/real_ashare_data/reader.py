"""Fail-closed reader for frozen real A-share snapshots.

This package is an Agent A handoff module.  It wraps the existing Prompt 10
A-share adapter/store/calendar contracts and does not reimplement provider
fetching, PIT construction, Qlib staging, evidence, promotion, portfolio, or
ledger logic.
"""

from __future__ import annotations

import importlib.util
import json
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

from ..contracts import CanonicalContract, ContractValidationError, FailureCode
from ..contracts.base import (
    fail,
    freeze_mapping,
    require_aware_datetime,
    require_hash,
    require_identifier,
    require_text,
)
from ..data.ashare import (
    AShareDataRequest,
    AShareFailure,
    AShareSnapshotRow,
    AvailabilityPolicy,
    FrozenAShareSnapshot,
    FrozenSnapshotStore,
    FrozenTradingCalendar,
    FrozenTradingCalendarStore,
    RepositoryMarketDataProvider,
    TushareHistoricalUniverseProvider,
    VibeAshareLoader,
    VibeAshareSnapshotAdapter,
)
from ..contracts import DatasetCard
from ..data.bitemporal import canonical_content_hash
from ..data.ashare.models import SHANGHAI, freeze_universe_mapping


SNAPSHOT_MANIFEST_SCHEMA = "ashare-snapshot-manifest/v1"
RAW_SCHEMA = "ashare-raw/v1"


class SnapshotIntegrityError(ValueError):
    """Typed fail-closed reader error for frozen snapshot integrity failures."""

    def __init__(
        self,
        code: FailureCode,
        message: str,
        *,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.details = MappingProxyType(dict(details or {}))


def _integrity_fail(
    code: FailureCode,
    message: str,
    **details: Any,
) -> None:
    raise SnapshotIntegrityError(code, message, details=details)


def _hash_segment(value: str) -> str:
    require_hash(value, "hash")
    return value.removeprefix("sha256:")


def _read_json(path: Path, *, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        _integrity_fail(
            FailureCode.ADAPTER_UNAVAILABLE,
            f"{label} is missing or unreadable",
            path=str(path),
            error_type=type(exc).__name__,
        )
    if not isinstance(payload, dict):
        _integrity_fail(FailureCode.INVALID_VALUE, f"{label} must be a JSON object")
    return payload


def _safe_resolve(root: Path, value: str | Path, *, expected_name: str) -> Path:
    root = root.resolve()
    raw = str(value)
    if not raw.strip() or "\x00" in raw:
        _integrity_fail(FailureCode.INVALID_VALUE, "path must be non-empty and contain no NUL")
    candidate = Path(raw)
    resolved = candidate.resolve() if candidate.is_absolute() else (root / candidate).resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise SnapshotIntegrityError(
            FailureCode.UNAUTHORIZED_CAPABILITY,
            "snapshot path escapes the configured content root",
            details={"path": raw, "root": str(root)},
        ) from exc
    if resolved.name != expected_name:
        _integrity_fail(
            FailureCode.INVALID_VALUE,
            f"path must identify {expected_name}",
            path=str(resolved),
        )
    if not resolved.is_file():
        _integrity_fail(
            FailureCode.ADAPTER_UNAVAILABLE,
            f"{expected_name} is not a file",
            path=str(resolved),
        )
    return resolved


def _verify_embedded_manifest_hash(payload: Mapping[str, Any]) -> str:
    manifest_hash = payload.get("manifest_hash")
    if not isinstance(manifest_hash, str):
        _integrity_fail(FailureCode.MISSING_REQUIRED_FIELD, "snapshot manifest_hash is required")
    core = dict(payload)
    core.pop("manifest_hash", None)
    expected = canonical_content_hash(core)
    if manifest_hash != expected:
        _integrity_fail(
            FailureCode.INVALID_HASH,
            "snapshot manifest hash mismatch",
            expected=expected,
            actual=manifest_hash,
        )
    return manifest_hash


def _contract_from_payload(contract_type: type, payload: Any, field_name: str):
    if not isinstance(payload, Mapping):
        _integrity_fail(FailureCode.MISSING_REQUIRED_FIELD, f"{field_name} is required")
    try:
        return contract_type.from_payload(payload)
    except ContractValidationError as exc:
        raise SnapshotIntegrityError(
            exc.code,
            f"{field_name} failed contract validation",
            details={"field": field_name, **dict(exc.details)},
        ) from exc


def _coerce_historical_universe(
    value: Mapping[str, Any],
    field_name: str = "historical_universe",
) -> Mapping[str, tuple[str, ...]]:
    if not isinstance(value, Mapping):
        _integrity_fail(FailureCode.MISSING_REQUIRED_FIELD, f"{field_name} is required")
    converted: dict[str, tuple[str, ...]] = {}
    for date_text, members in value.items():
        if isinstance(members, tuple):
            converted[date_text] = members
        elif isinstance(members, list):
            converted[date_text] = tuple(members)
        else:
            _integrity_fail(
                FailureCode.INVALID_VALUE,
                f"{field_name} entries must be arrays of symbols",
                date=str(date_text),
            )
    try:
        return freeze_universe_mapping(converted, field_name)
    except ContractValidationError as exc:
        raise SnapshotIntegrityError(
            exc.code,
            f"{field_name} failed contract validation",
            details=dict(exc.details),
        ) from exc


@dataclass(frozen=True, kw_only=True)
class FrozenSnapshotHandle(CanonicalContract):
    """Content-addressed pointer to a verified frozen A-share snapshot."""

    dataset_id: str
    snapshot_hash: str
    snapshot_dir: str
    manifest_path: str
    raw_path: str
    manifest_hash: str
    raw_content_hash: str
    universe_hash: str
    calendar_hash: str
    calendar_manifest_path: str

    def __post_init__(self) -> None:
        require_identifier(self.dataset_id, "dataset_id")
        for name in (
            "snapshot_hash",
            "manifest_hash",
            "raw_content_hash",
            "universe_hash",
            "calendar_hash",
        ):
            require_hash(getattr(self, name), name)
        for name in ("snapshot_dir", "manifest_path", "raw_path", "calendar_manifest_path"):
            require_text(getattr(self, name), name)


@dataclass(frozen=True, kw_only=True)
class HistoricalUniverseSnapshot(CanonicalContract):
    """Historical index membership visible as of a decision time."""

    dataset_id: str
    index_code: str
    decision_time: datetime
    members_by_date: Mapping[str, tuple[str, ...]]
    latest_members: tuple[str, ...]
    universe_hash: str
    source_snapshot_hash: str

    def __post_init__(self) -> None:
        require_identifier(self.dataset_id, "dataset_id")
        require_identifier(self.index_code, "index_code")
        require_aware_datetime(self.decision_time, "decision_time")
        object.__setattr__(self, "members_by_date", _coerce_historical_universe(self.members_by_date, "members_by_date"))
        if not isinstance(self.latest_members, tuple):
            fail(FailureCode.INVALID_VALUE, "latest_members must be a tuple")
        for symbol in self.latest_members:
            require_identifier(symbol, "latest_members")
        require_hash(self.universe_hash, "universe_hash")
        require_hash(self.source_snapshot_hash, "source_snapshot_hash")


@dataclass(frozen=True, kw_only=True)
class MarketStateRow(CanonicalContract):
    """A PIT-visible row plus the date's historical universe context."""

    symbol: str
    event_time: datetime
    published_at: datetime
    available_at: datetime
    calendar_session: str
    historical_universe: tuple[str, ...]
    values: Mapping[str, int | float | None]

    def __post_init__(self) -> None:
        require_identifier(self.symbol, "symbol")
        for name in ("event_time", "published_at", "available_at"):
            require_aware_datetime(getattr(self, name), name)
        require_text(self.calendar_session, "calendar_session")
        if self.symbol not in self.historical_universe:
            fail(FailureCode.INVALID_VALUE, "row symbol is not in the historical universe")
        object.__setattr__(self, "values", freeze_mapping(self.values, "values"))


@dataclass(frozen=True, kw_only=True)
class MarketVintage(CanonicalContract):
    """Daily market vintage visible to research as of a decision time."""

    dataset_id: str
    snapshot_hash: str
    manifest_hash: str
    raw_content_hash: str
    calendar_hash: str
    universe_hash: str
    decision_time: datetime
    rows: tuple[MarketStateRow, ...]
    historical_universe: HistoricalUniverseSnapshot
    coverage_by_field: Mapping[str, float]
    market_state_hash: str

    def __post_init__(self) -> None:
        require_identifier(self.dataset_id, "dataset_id")
        for name in (
            "snapshot_hash",
            "manifest_hash",
            "raw_content_hash",
            "calendar_hash",
            "universe_hash",
            "market_state_hash",
        ):
            require_hash(getattr(self, name), name)
        require_aware_datetime(self.decision_time, "decision_time")
        if not isinstance(self.rows, tuple) or not self.rows:
            fail(FailureCode.INCOMPLETE_RESULT, "market vintage requires at least one PIT-visible row")
        if not isinstance(self.historical_universe, HistoricalUniverseSnapshot):
            fail(FailureCode.INVALID_VALUE, "historical_universe must be typed")
        object.__setattr__(
            self,
            "coverage_by_field",
            freeze_mapping(self.coverage_by_field, "coverage_by_field", numeric_values=True),
        )


@dataclass(frozen=True, kw_only=True)
class RealDataAvailability(CanonicalContract):
    """Boolean-only availability report; never includes credentials."""

    status: str
    failure_code: str | None
    message: str
    network_enabled: bool
    tushare_module_available: bool
    tushare_credential_configured: bool
    calendar_manifest_available: bool
    provider: str = "vibe-market-data"
    request_hash: str | None = None
    snapshot_handle_hash: str | None = None

    def __post_init__(self) -> None:
        if self.status not in {"AVAILABLE", "UNAVAILABLE"}:
            fail(FailureCode.INVALID_VALUE, "availability status must be AVAILABLE or UNAVAILABLE")
        if self.failure_code is not None:
            try:
                FailureCode(self.failure_code)
            except ValueError as exc:
                raise ContractValidationError(
                    FailureCode.INVALID_VALUE,
                    "failure_code must be a typed VeriQuant failure code",
                    details={"field": "failure_code"},
                ) from exc
        require_text(self.message, "message")
        for name in (
            "network_enabled",
            "tushare_module_available",
            "tushare_credential_configured",
            "calendar_manifest_available",
        ):
            if type(getattr(self, name)) is not bool:
                fail(FailureCode.INVALID_VALUE, f"{name} must be boolean")
        require_identifier(self.provider, "provider")
        if self.request_hash is not None:
            require_hash(self.request_hash, "request_hash")
        if self.snapshot_handle_hash is not None:
            require_hash(self.snapshot_handle_hash, "snapshot_handle_hash")


@dataclass(frozen=True, kw_only=True)
class _LoadedSnapshot:
    handle: FrozenSnapshotHandle
    snapshot: FrozenAShareSnapshot
    manifest_payload: Mapping[str, Any]
    raw_payload: Mapping[str, Any]


class FrozenAShareSnapshotReader:
    """Read-only content-addressed frozen snapshot reader."""

    def __init__(
        self,
        *,
        snapshot_root: str | Path | None = None,
        calendar_root: str | Path | None = None,
    ) -> None:
        repo_root = Path(__file__).resolve().parents[5]
        self.snapshot_root = (
            Path(snapshot_root).resolve()
            if snapshot_root is not None
            else repo_root / ".autobuild" / "veriquant" / "datasets"
        )
        self.calendar_root = (
            Path(calendar_root).resolve()
            if calendar_root is not None
            else repo_root / ".autobuild" / "veriquant" / "calendars"
        )

    def open(self, manifest_path: str | Path) -> FrozenSnapshotHandle:
        """Validate a snapshot manifest and return a stable handle."""

        return self._load(manifest_path).handle

    def read_snapshot(self, handle: FrozenSnapshotHandle) -> FrozenAShareSnapshot:
        """Reload and revalidate a previously opened handle."""

        if not isinstance(handle, FrozenSnapshotHandle):
            _integrity_fail(FailureCode.INVALID_VALUE, "FrozenSnapshotHandle is required")
        loaded = self._load(handle.manifest_path)
        if loaded.handle.artifact_hash != handle.artifact_hash:
            _integrity_fail(FailureCode.INVALID_HASH, "snapshot handle no longer matches disk")
        return loaded.snapshot

    def historical_universe_snapshot(
        self,
        handle: FrozenSnapshotHandle,
        *,
        decision_time: datetime,
    ) -> HistoricalUniverseSnapshot:
        """Return only historical universe entries visible by decision_time."""

        snapshot = self.read_snapshot(handle)
        require_aware_datetime(decision_time, "decision_time")
        if decision_time > snapshot.request.decision_time:
            _integrity_fail(
                FailureCode.INVALID_TIME_ORDER,
                "decision_time exceeds frozen snapshot decision_time",
            )
        cutoff_date = decision_time.astimezone(SHANGHAI).date().isoformat()
        visible = {
            date_text: members
            for date_text, members in snapshot.historical_universe.items()
            if date_text <= cutoff_date
        }
        if not visible:
            _integrity_fail(
                FailureCode.INCOMPLETE_RESULT,
                "no historical universe entries are visible at decision_time",
            )
        latest_date = sorted(visible)[-1]
        universe_hash = canonical_content_hash(visible)
        return HistoricalUniverseSnapshot(
            dataset_id=snapshot.request.dataset_id,
            index_code=snapshot.request.index_code,
            decision_time=decision_time,
            members_by_date=visible,
            latest_members=visible[latest_date],
            universe_hash=universe_hash,
            source_snapshot_hash=snapshot.snapshot_hash,
        )

    def market_vintage(
        self,
        handle: FrozenSnapshotHandle,
        *,
        decision_time: datetime,
    ) -> MarketVintage:
        """Build a PIT-visible daily MarketVintage from frozen rows."""

        snapshot = self.read_snapshot(handle)
        universe = self.historical_universe_snapshot(handle, decision_time=decision_time)
        selected: dict[tuple[str, datetime], AShareSnapshotRow] = {}
        for row in snapshot.rows:
            session = row.event_time.astimezone(SHANGHAI).date().isoformat()
            members = universe.members_by_date.get(session)
            if (
                row.event_time > decision_time
                or row.available_at > decision_time
                or members is None
                or row.symbol not in members
            ):
                continue
            key = (row.symbol, row.event_time)
            current = selected.get(key)
            if current is None or (row.available_at, row.published_at, row.artifact_hash) > (
                current.available_at,
                current.published_at,
                current.artifact_hash,
            ):
                selected[key] = row
        if not selected:
            _integrity_fail(
                FailureCode.INCOMPLETE_RESULT,
                "no PIT-visible market rows at decision_time",
            )
        rows = tuple(
            MarketStateRow(
                symbol=row.symbol,
                event_time=row.event_time,
                published_at=row.published_at,
                available_at=row.available_at,
                calendar_session=row.event_time.astimezone(SHANGHAI).date().isoformat(),
                historical_universe=universe.members_by_date[
                    row.event_time.astimezone(SHANGHAI).date().isoformat()
                ],
                values=dict(row.values),
            )
            for row in sorted(selected.values(), key=lambda item: (item.event_time, item.symbol))
        )
        fields = snapshot.request.fields
        coverage = {
            field: sum(row.values.get(field) is not None for row in rows) / len(rows)
            for field in fields
        }
        state_hash = canonical_content_hash(
            {
                "schema_version": "real-ashare-market-vintage/v1",
                "snapshot_hash": handle.snapshot_hash,
                "decision_time": decision_time,
                "rows": rows,
                "historical_universe_hash": universe.universe_hash,
                "coverage": coverage,
            }
        )
        return MarketVintage(
            dataset_id=snapshot.request.dataset_id,
            snapshot_hash=handle.snapshot_hash,
            manifest_hash=handle.manifest_hash,
            raw_content_hash=handle.raw_content_hash,
            calendar_hash=handle.calendar_hash,
            universe_hash=universe.universe_hash,
            decision_time=decision_time,
            rows=rows,
            historical_universe=universe,
            coverage_by_field=coverage,
            market_state_hash=state_hash,
        )

    def real_data_availability(
        self,
        *,
        request: AShareDataRequest | None = None,
        calendar_manifest_path: str | Path | None = None,
    ) -> RealDataAvailability:
        """Probe real-data readiness without printing or returning credentials."""

        request_hash = request.artifact_hash if isinstance(request, AShareDataRequest) else None
        network_enabled = os.environ.get("ENABLE_ASHARE_NETWORK") == "1"
        module_available = importlib.util.find_spec("tushare") is not None
        credential_configured = _tushare_credential_configured()
        calendar_available = False
        if calendar_manifest_path is not None:
            try:
                self._load_calendar_manifest(calendar_manifest_path)
                calendar_available = True
            except SnapshotIntegrityError:
                calendar_available = False
        if not network_enabled:
            return RealDataAvailability(
                status="UNAVAILABLE",
                failure_code=FailureCode.ADAPTER_UNAVAILABLE.value,
                message="A-share network adapter is disabled; set ENABLE_ASHARE_NETWORK=1 explicitly",
                network_enabled=False,
                tushare_module_available=module_available,
                tushare_credential_configured=credential_configured,
                calendar_manifest_available=calendar_available,
                request_hash=request_hash,
            )
        if not module_available:
            return RealDataAvailability(
                status="UNAVAILABLE",
                failure_code=FailureCode.ADAPTER_UNAVAILABLE.value,
                message="Tushare dependency is unavailable; fallback providers are not permitted",
                network_enabled=True,
                tushare_module_available=False,
                tushare_credential_configured=credential_configured,
                calendar_manifest_available=calendar_available,
                request_hash=request_hash,
            )
        if not credential_configured:
            return RealDataAvailability(
                status="UNAVAILABLE",
                failure_code=FailureCode.ADAPTER_UNAVAILABLE.value,
                message="Tushare credentials are not configured in EnvConfig",
                network_enabled=True,
                tushare_module_available=True,
                tushare_credential_configured=False,
                calendar_manifest_available=calendar_available,
                request_hash=request_hash,
            )
        if calendar_manifest_path is None or not calendar_available:
            return RealDataAvailability(
                status="UNAVAILABLE",
                failure_code=FailureCode.INCOMPLETE_RESULT.value,
                message="A frozen SSE/SZSE trading-calendar manifest is required",
                network_enabled=True,
                tushare_module_available=True,
                tushare_credential_configured=True,
                calendar_manifest_available=False,
                request_hash=request_hash,
            )
        return RealDataAvailability(
            status="AVAILABLE",
            failure_code=None,
            message="Real A-share provider preflight is configured; fetching still requires explicit bounded request",
            network_enabled=True,
            tushare_module_available=True,
            tushare_credential_configured=True,
            calendar_manifest_available=True,
            request_hash=request_hash,
        )

    def fetch_and_freeze(
        self,
        *,
        request: AShareDataRequest,
        calendar_manifest_path: str | Path,
    ) -> FrozenSnapshotHandle | RealDataAvailability:
        """Bounded real Tushare smoke wrapper around the existing Prompt 10 adapter."""

        if not isinstance(request, AShareDataRequest):
            _integrity_fail(FailureCode.INVALID_VALUE, "typed AShareDataRequest is required")
        availability = self.real_data_availability(
            request=request,
            calendar_manifest_path=calendar_manifest_path,
        )
        if availability.status != "AVAILABLE":
            return availability
        calendar = self._load_calendar_manifest(calendar_manifest_path)
        store = FrozenSnapshotStore(self.snapshot_root)
        built = VibeAshareSnapshotAdapter(
            loader=VibeAshareLoader(
                provider=RepositoryMarketDataProvider(),
                trading_calendar=calendar,
            ),
            universe_provider=TushareHistoricalUniverseProvider(),
            store=store,
            trading_calendar=calendar,
        ).build(request)
        if isinstance(built, AShareFailure):
            return RealDataAvailability(
                status="UNAVAILABLE",
                failure_code=built.code.value,
                message=built.message,
                network_enabled=True,
                tushare_module_available=True,
                tushare_credential_configured=True,
                calendar_manifest_available=True,
                provider=built.provider,
                request_hash=request.artifact_hash,
            )
        manifest_path = (
            self.snapshot_root
            / request.dataset_id
            / _hash_segment(built.snapshot_hash)
            / "manifest.json"
        )
        return self.open(manifest_path)

    def _load(self, manifest_path: str | Path) -> _LoadedSnapshot:
        path = _safe_resolve(self.snapshot_root, manifest_path, expected_name="manifest.json")
        directory = path.parent
        snapshot_hash_from_dir = "sha256:" + directory.name
        try:
            require_hash(snapshot_hash_from_dir, "snapshot directory hash")
        except ContractValidationError as exc:
            raise SnapshotIntegrityError(
                exc.code,
                "snapshot directory is not content-addressed by sha256 hex",
                details={"directory": str(directory)},
            ) from exc

        manifest = _read_json(path, label="snapshot manifest")
        manifest_hash = _verify_embedded_manifest_hash(manifest)
        if manifest.get("schema_version") != SNAPSHOT_MANIFEST_SCHEMA:
            _integrity_fail(FailureCode.INVALID_SCHEMA_VERSION, "unsupported snapshot manifest schema")
        if manifest.get("snapshot_hash") != snapshot_hash_from_dir:
            _integrity_fail(
                FailureCode.INVALID_HASH,
                "snapshot manifest hash does not match content-addressed directory",
            )

        raw_path = directory / "raw_response.json"
        raw_payload = _read_json(raw_path, label="raw response")
        if raw_payload.get("schema_version") != RAW_SCHEMA:
            _integrity_fail(FailureCode.INVALID_SCHEMA_VERSION, "unsupported raw response schema")
        raw_hash = canonical_content_hash(raw_payload)
        if raw_hash != manifest.get("raw_content_hash"):
            _integrity_fail(
                FailureCode.INVALID_HASH,
                "raw response content hash mismatch",
                expected=manifest.get("raw_content_hash"),
                actual=raw_hash,
            )

        request = _contract_from_payload(AShareDataRequest, manifest.get("request"), "request")
        dataset_card = _contract_from_payload(DatasetCard, manifest.get("dataset_card"), "dataset_card")
        availability_policy = _contract_from_payload(
            AvailabilityPolicy,
            manifest.get("availability_policy"),
            "availability_policy",
        )
        raw_rows = raw_payload.get("rows")
        if not isinstance(raw_rows, list) or not raw_rows:
            _integrity_fail(FailureCode.INCOMPLETE_RESULT, "raw response rows are required")
        rows = tuple(
            _contract_from_payload(AShareSnapshotRow, row, f"rows[{index}]")
            for index, row in enumerate(raw_rows)
        )
        historical_universe = manifest.get("historical_universe")
        if not isinstance(historical_universe, Mapping):
            _integrity_fail(FailureCode.MISSING_REQUIRED_FIELD, "historical_universe is required")
        universe = _coerce_historical_universe(historical_universe)
        universe_hash = canonical_content_hash(universe)

        embedded_calendar = manifest.get("trading_calendar")
        if not isinstance(embedded_calendar, Mapping):
            _integrity_fail(
                FailureCode.MISSING_REQUIRED_FIELD,
                "snapshot manifest requires embedded frozen trading_calendar",
            )
        embedded = _contract_from_payload(
            FrozenTradingCalendar,
            embedded_calendar,
            "trading_calendar",
        )
        calendar_manifest = self._discover_calendar_manifest(embedded)
        loaded_calendar = self._load_calendar_manifest(calendar_manifest)
        if loaded_calendar.artifact_hash != embedded.artifact_hash:
            _integrity_fail(FailureCode.INVALID_HASH, "embedded calendar does not match calendar store")

        coverage = manifest.get("coverage")
        if not isinstance(coverage, Mapping):
            _integrity_fail(FailureCode.MISSING_REQUIRED_FIELD, "coverage is required")
        excluded_rows = manifest.get("excluded_rows", 0)
        if isinstance(excluded_rows, bool) or not isinstance(excluded_rows, int) or excluded_rows < 0:
            _integrity_fail(FailureCode.INVALID_VALUE, "excluded_rows must be a non-negative integer")
        provider = manifest.get("provider")
        if not isinstance(provider, str):
            _integrity_fail(FailureCode.MISSING_REQUIRED_FIELD, "provider is required")
        fetched_at = _parse_manifest_datetime(manifest.get("fetch_timestamp"), "fetch_timestamp")

        material = {
            "request_hash": request.artifact_hash,
            "rows": rows,
            "historical_universe": universe,
            "coverage": coverage,
            "availability_policy": availability_policy,
            "provider": provider,
            "trading_calendar": loaded_calendar,
        }
        recomputed_snapshot_hash = canonical_content_hash(material)
        if recomputed_snapshot_hash != manifest.get("snapshot_hash"):
            _integrity_fail(
                FailureCode.INVALID_HASH,
                "snapshot hash does not match reconstructed content",
                expected=manifest.get("snapshot_hash"),
                actual=recomputed_snapshot_hash,
            )
        try:
            snapshot = FrozenAShareSnapshot(
                request=request,
                dataset_card=dataset_card,
                snapshot_hash=str(manifest["snapshot_hash"]),
                rows=rows,
                historical_universe=universe,
                coverage_by_field=coverage,
                availability_policy=availability_policy,
                provider=provider,
                fetched_at=fetched_at,
                excluded_rows=excluded_rows,
                trading_calendar=loaded_calendar,
            )
        except ContractValidationError as exc:
            raise SnapshotIntegrityError(
                exc.code,
                "FrozenAShareSnapshot reconstruction failed",
                details=dict(exc.details),
            ) from exc
        handle = FrozenSnapshotHandle(
            dataset_id=request.dataset_id,
            snapshot_hash=snapshot.snapshot_hash,
            snapshot_dir=str(directory),
            manifest_path=str(path),
            raw_path=str(raw_path),
            manifest_hash=manifest_hash,
            raw_content_hash=raw_hash,
            universe_hash=universe_hash,
            calendar_hash=loaded_calendar.artifact_hash,
            calendar_manifest_path=str(calendar_manifest),
        )
        return _LoadedSnapshot(
            handle=handle,
            snapshot=snapshot,
            manifest_payload=MappingProxyType(dict(manifest)),
            raw_payload=MappingProxyType(dict(raw_payload)),
        )

    def _discover_calendar_manifest(self, calendar: FrozenTradingCalendar) -> Path:
        path = (
            self.calendar_root
            / calendar.calendar_id
            / _hash_segment(calendar.artifact_hash)
            / "manifest.json"
        )
        if not path.is_file():
            _integrity_fail(
                FailureCode.ADAPTER_UNAVAILABLE,
                "frozen SSE/SZSE trading-calendar manifest is missing",
                path=str(path),
            )
        return path

    def _load_calendar_manifest(self, manifest_path: str | Path) -> FrozenTradingCalendar:
        path = _safe_resolve(self.calendar_root, manifest_path, expected_name="manifest.json")
        try:
            return FrozenTradingCalendarStore(self.calendar_root).load_manifest(path)
        except ContractValidationError as exc:
            raise SnapshotIntegrityError(
                exc.code,
                "frozen trading-calendar manifest failed validation",
                details=dict(exc.details),
            ) from exc
        except ValueError as exc:
            raise SnapshotIntegrityError(
                FailureCode.INVALID_VALUE,
                "frozen trading-calendar manifest is unreadable",
                details={"error_type": type(exc).__name__},
            ) from exc


def _parse_manifest_datetime(value: Any, field_name: str) -> datetime:
    if isinstance(value, str):
        candidate = value[:-1] + "+00:00" if value.endswith("Z") else value
        try:
            parsed = datetime.fromisoformat(candidate)
        except ValueError as exc:
            raise SnapshotIntegrityError(
                FailureCode.INVALID_TIMESTAMP,
                f"{field_name} must be an ISO-8601 timestamp",
            ) from exc
        require_aware_datetime(parsed, field_name)
        return parsed
    if isinstance(value, datetime):
        require_aware_datetime(value, field_name)
        return value
    _integrity_fail(FailureCode.INVALID_TIMESTAMP, f"{field_name} must be an ISO-8601 timestamp")


def _tushare_credential_configured() -> bool:
    """Return a boolean only; never return or log the credential."""

    try:
        from src.config.accessor import get_env_config

        token = get_env_config().data.tushare_token.strip()
    except Exception:
        return False
    return bool(token and token != "your-tushare-token")


__all__ = [
    "FrozenAShareSnapshotReader",
    "FrozenSnapshotHandle",
    "HistoricalUniverseSnapshot",
    "MarketVintage",
    "RealDataAvailability",
    "SnapshotIntegrityError",
]
