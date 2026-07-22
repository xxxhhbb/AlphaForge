"""Frozen SSE/SZSE trading-calendar contracts and content-addressed storage."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from ...contracts import CanonicalContract, FailureCode
from ...contracts.base import (
    canonicalize,
    fail,
    require_aware_datetime,
    require_hash,
    require_identifier,
    require_text,
)
from ..bitemporal import canonical_content_hash


SHANGHAI = ZoneInfo("Asia/Shanghai")


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


def _hash_segment(value: str) -> str:
    require_hash(value, "content_hash")
    return value.removeprefix("sha256:")


def _atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("wb") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _parse_date(value: str | date, field_name: str) -> date:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError as exc:
            fail(FailureCode.INVALID_TIMESTAMP, f"{field_name} must be YYYY-MM-DD")
    fail(FailureCode.INVALID_TIMESTAMP, f"{field_name} must be a date")


def calendar_session_hash(session_dates: tuple[str, ...]) -> str:
    return canonical_content_hash(
        {
            "schema_version": "veriquant.trading-calendar-sessions/v1",
            "session_dates": session_dates,
        }
    )


@dataclass(frozen=True, kw_only=True)
class FrozenTradingCalendar(CanonicalContract):
    """A bounded, frozen session calendar selected before production PIT use."""

    calendar_id: str
    venue_codes: tuple[str, ...]
    source: str
    source_calendar: str
    source_version: str
    policy_version: str
    session_start: str
    session_end: str
    session_dates: tuple[str, ...]
    session_content_hash: str

    def __post_init__(self) -> None:
        require_identifier(self.calendar_id, "calendar_id")
        if not isinstance(self.venue_codes, tuple) or self.venue_codes != ("SSE", "SZSE"):
            fail(FailureCode.INVALID_VALUE, "A-share calendar must bind exactly SSE and SZSE")
        for value in self.venue_codes:
            require_identifier(value, "venue_codes")
        for name in ("source", "source_calendar", "source_version", "policy_version"):
            require_text(getattr(self, name), name)
        start = _parse_date(self.session_start, "session_start")
        end = _parse_date(self.session_end, "session_end")
        if start > end:
            fail(FailureCode.INVALID_TIME_ORDER, "calendar start follows end")
        if not isinstance(self.session_dates, tuple) or not self.session_dates:
            fail(FailureCode.INVALID_VALUE, "calendar requires at least one session")
        parsed = tuple(_parse_date(item, "session_dates") for item in self.session_dates)
        if tuple(sorted(parsed)) != parsed or len(set(parsed)) != len(parsed):
            fail(FailureCode.INVALID_VALUE, "calendar sessions must be sorted and unique")
        if parsed[0] < start or parsed[-1] > end:
            fail(FailureCode.INVALID_TIME_ORDER, "calendar sessions exceed frozen range")
        require_hash(self.session_content_hash, "session_content_hash")
        expected = calendar_session_hash(self.session_dates)
        if expected != self.session_content_hash:
            fail(FailureCode.INVALID_HASH, "calendar session hash mismatch")

    def next_session_open(self, event_time: datetime, *, hour: int, minute: int) -> datetime:
        require_aware_datetime(event_time, "event_time")
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            fail(FailureCode.INVALID_VALUE, "next-session clock is invalid")
        local_date = event_time.astimezone(SHANGHAI).date()
        for session_text in self.session_dates:
            session_date = date.fromisoformat(session_text)
            if session_date > local_date:
                return datetime(
                    session_date.year,
                    session_date.month,
                    session_date.day,
                    hour,
                    minute,
                    tzinfo=SHANGHAI,
                )
        fail(
            FailureCode.INCOMPLETE_RESULT,
            "frozen calendar does not cover the next trading session",
        )


@dataclass(frozen=True, kw_only=True)
class CalendarPersistResult(CanonicalContract):
    calendar_hash: str
    directory: str
    manifest_hash: str
    session_content_hash: str
    reused: bool

    def __post_init__(self) -> None:
        require_hash(self.calendar_hash, "calendar_hash")
        require_hash(self.manifest_hash, "manifest_hash")
        require_hash(self.session_content_hash, "session_content_hash")
        require_text(self.directory, "directory")
        if type(self.reused) is not bool:
            fail(FailureCode.INVALID_VALUE, "reused must be boolean")


class FrozenTradingCalendarStore:
    def __init__(self, root: str | Path | None = None) -> None:
        repo_root = Path(__file__).resolve().parents[5]
        self.root = (
            Path(root).resolve()
            if root is not None
            else repo_root / ".autobuild" / "veriquant" / "calendars"
        )

    @staticmethod
    def _manifest_core(calendar: FrozenTradingCalendar) -> dict[str, object]:
        return {
            "schema_version": "veriquant.trading-calendar-manifest/v1",
            "calendar": calendar,
            "calendar_hash": calendar.artifact_hash,
            "session_content_hash": calendar.session_content_hash,
            "sessions_path": "sessions.json",
        }

    def persist(self, calendar: FrozenTradingCalendar) -> CalendarPersistResult:
        if not isinstance(calendar, FrozenTradingCalendar):
            fail(FailureCode.INVALID_VALUE, "typed FrozenTradingCalendar is required")
        directory = self.root / calendar.calendar_id / _hash_segment(calendar.artifact_hash)
        sessions_path = directory / "sessions.json"
        manifest_path = directory / "manifest.json"
        sessions_payload = {
            "schema_version": "veriquant.trading-calendar-sessions/v1",
            "calendar_hash": calendar.artifact_hash,
            "session_content_hash": calendar.session_content_hash,
            "session_dates": calendar.session_dates,
        }
        manifest_core = self._manifest_core(calendar)
        manifest_hash = canonical_content_hash(manifest_core)
        manifest_payload = {**manifest_core, "manifest_hash": manifest_hash}
        if directory.exists():
            loaded = self.load_manifest(manifest_path)
            if loaded.artifact_hash != calendar.artifact_hash:
                fail(FailureCode.ARTIFACT_COLLISION, "calendar path owns different content")
            return CalendarPersistResult(
                calendar_hash=calendar.artifact_hash,
                directory=str(directory),
                manifest_hash=manifest_hash,
                session_content_hash=calendar.session_content_hash,
                reused=True,
            )
        directory.mkdir(parents=True, exist_ok=False)
        _atomic_write(sessions_path, _json_bytes(sessions_payload))
        _atomic_write(manifest_path, _json_bytes(manifest_payload))
        return CalendarPersistResult(
            calendar_hash=calendar.artifact_hash,
            directory=str(directory),
            manifest_hash=manifest_hash,
            session_content_hash=calendar.session_content_hash,
            reused=False,
        )

    def load_manifest(self, manifest_path: str | Path) -> FrozenTradingCalendar:
        path = Path(manifest_path).resolve()
        if not path.is_file():
            fail(FailureCode.ADAPTER_UNAVAILABLE, "frozen calendar manifest is missing")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError("frozen calendar manifest is unreadable") from exc
        manifest_hash = payload.pop("manifest_hash", None)
        if not isinstance(manifest_hash, str):
            fail(FailureCode.MISSING_REQUIRED_FIELD, "calendar manifest_hash is required")
        expected_manifest_hash = canonical_content_hash(payload)
        if manifest_hash != expected_manifest_hash:
            fail(FailureCode.INVALID_HASH, "calendar manifest hash mismatch")
        if payload.get("schema_version") != "veriquant.trading-calendar-manifest/v1":
            fail(FailureCode.INVALID_SCHEMA_VERSION, "unsupported calendar manifest schema")
        raw_calendar = payload.get("calendar")
        if not isinstance(raw_calendar, dict):
            fail(FailureCode.MISSING_REQUIRED_FIELD, "calendar manifest requires calendar")
        calendar = FrozenTradingCalendar.from_payload(raw_calendar)
        if payload.get("calendar_hash") != calendar.artifact_hash:
            fail(FailureCode.INVALID_HASH, "calendar hash mismatch")
        if payload.get("session_content_hash") != calendar.session_content_hash:
            fail(FailureCode.INVALID_HASH, "calendar session hash mismatch")
        sessions_path = path.parent / "sessions.json"
        if not sessions_path.is_file():
            fail(FailureCode.INCOMPLETE_RESULT, "calendar sessions file is missing")
        try:
            sessions_payload = json.loads(sessions_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError("frozen calendar sessions are unreadable") from exc
        if sessions_payload.get("calendar_hash") != calendar.artifact_hash:
            fail(FailureCode.INVALID_HASH, "calendar sessions calendar_hash mismatch")
        if tuple(sessions_payload.get("session_dates", ())) != calendar.session_dates:
            fail(FailureCode.INVALID_HASH, "calendar sessions payload mismatch")
        if calendar_session_hash(calendar.session_dates) != calendar.session_content_hash:
            fail(FailureCode.INVALID_HASH, "calendar sessions content hash mismatch")
        return calendar


def freeze_sse_szse_calendar(
    *,
    start_date: str | date,
    end_date: str | date,
    source_calendar: str = "XSHG",
) -> FrozenTradingCalendar:
    """Freeze mainland A-share sessions from an already installed calendar package."""

    start = _parse_date(start_date, "start_date")
    end = _parse_date(end_date, "end_date")
    if start > end:
        fail(FailureCode.INVALID_TIME_ORDER, "calendar start_date follows end_date")
    try:
        import exchange_calendars as xc

        calendar = xc.get_calendar(source_calendar)
        sessions = calendar.sessions_in_range(start.isoformat(), end.isoformat())
        source_version = getattr(xc, "__version__", "unknown")
    except Exception as exc:
        return fail(
            FailureCode.ADAPTER_UNAVAILABLE,
            f"exchange_calendars source unavailable: {type(exc).__name__}",
        )
    session_dates = tuple(item.date().isoformat() for item in sessions)
    if not session_dates:
        fail(FailureCode.INCOMPLETE_RESULT, "calendar source returned no sessions")
    return FrozenTradingCalendar(
        calendar_id="cn-ashare-sse-szse",
        venue_codes=("SSE", "SZSE"),
        source="exchange_calendars",
        source_calendar=source_calendar,
        source_version=str(source_version),
        policy_version="sse-szse-shared-session-calendar/v1",
        session_start=start.isoformat(),
        session_end=end.isoformat(),
        session_dates=session_dates,
        session_content_hash=calendar_session_hash(session_dates),
    )
