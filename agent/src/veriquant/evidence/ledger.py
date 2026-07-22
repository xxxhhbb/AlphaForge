"""Append-only, hash-chained SQLite Trial Ledger with atomic family budgets."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Callable

from ..contracts import ContractValidationError, FailureCode
from ..contracts.base import require_aware_datetime, require_identifier
from ..data import canonical_content_hash
from .models import ExperimentIdentity


LEDGER_SCHEMA_VERSION = "veriquant-trial-ledger/v1"
ZERO_HASH = "sha256:" + "0" * 64


class LedgerEventType(str, Enum):
    RESERVE = "RESERVE"
    START = "START"
    TERMINAL = "TERMINAL"
    RELEASE = "RELEASE"


class TrialOutcome(str, Enum):
    SUCCESS = "SUCCESS"
    PARSE_REJECTION = "PARSE_REJECTION"
    PIT_REJECTION = "PIT_REJECTION"
    TIMEOUT = "TIMEOUT"
    EVALUATION_REJECTION = "EVALUATION_REJECTION"
    STATISTICAL_REJECTION = "STATISTICAL_REJECTION"
    SEALED_REJECTION = "SEALED_REJECTION"
    EXCEPTION = "EXCEPTION"
    CANCELLATION = "CANCELLATION"


class TrialStatus(str, Enum):
    RESERVED = "RESERVED"
    STARTED = "STARTED"
    TERMINAL = "TERMINAL"
    RELEASED = "RELEASED"


class LedgerIntegrityError(ContractValidationError):
    def __init__(self, message: str, **details) -> None:
        super().__init__(FailureCode.INVALID_HASH, message, details=details)


@dataclass(frozen=True, kw_only=True)
class LedgerEvent:
    sequence: int
    event_id: str
    event_type: LedgerEventType
    family_id: str
    trial_id: str
    identity_hash: str
    proposer_id: str
    units: int
    outcome: TrialOutcome | None
    failure_code: FailureCode | None
    event_time: datetime
    prev_hash: str
    event_hash: str


@dataclass(frozen=True, kw_only=True)
class TrialReplayState:
    trial_id: str
    family_id: str
    identity_hash: str
    proposer_id: str
    units: int
    status: TrialStatus
    outcome: TrialOutcome | None
    failure_code: FailureCode | None


@dataclass(frozen=True, kw_only=True)
class FamilyReplayState:
    family_id: str
    budget_limit: int
    consumed: int
    reserved_trials: int
    terminal_trials: int
    released_trials: int


@dataclass(frozen=True, kw_only=True)
class LedgerReplay:
    last_sequence: int
    last_hash: str
    families: dict[str, FamilyReplayState]
    trials: dict[str, TrialReplayState]


class TrialLedger:
    """All state derives from immutable rows; UPDATE and DELETE are denied."""

    def __init__(
        self,
        path: str | Path,
        *,
        clock: Callable[[], datetime] | None = None,
        timeout_seconds: float = 30.0,
    ) -> None:
        self.path = Path(path)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.timeout_seconds = timeout_seconds
        if not self.path.parent.exists():
            raise ValueError("Trial Ledger parent directory must already exist")
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.path,
            timeout=self.timeout_seconds,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA synchronous = FULL")
        return connection

    def _initialize(self) -> None:
        try:
            with self._connect() as connection:
                connection.execute("PRAGMA journal_mode = WAL")
                connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS meta (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS families (
                        family_id TEXT PRIMARY KEY,
                        budget_limit INTEGER NOT NULL CHECK (budget_limit >= 0)
                    );
                    CREATE TABLE IF NOT EXISTS identities (
                        identity_hash TEXT PRIMARY KEY,
                        identity_json TEXT NOT NULL UNIQUE
                    );
                    CREATE TABLE IF NOT EXISTS events (
                        sequence INTEGER PRIMARY KEY,
                        event_id TEXT NOT NULL UNIQUE,
                        event_type TEXT NOT NULL,
                        family_id TEXT NOT NULL,
                        trial_id TEXT NOT NULL,
                        identity_hash TEXT NOT NULL,
                        proposer_id TEXT NOT NULL,
                        units INTEGER NOT NULL CHECK (units >= 0),
                        outcome TEXT,
                        failure_code TEXT,
                        event_time TEXT NOT NULL,
                        prev_hash TEXT NOT NULL,
                        event_hash TEXT NOT NULL UNIQUE,
                        FOREIGN KEY (family_id) REFERENCES families(family_id),
                        FOREIGN KEY (identity_hash) REFERENCES identities(identity_hash)
                    );
                    CREATE INDEX IF NOT EXISTS idx_events_family ON events(family_id, sequence);
                    CREATE INDEX IF NOT EXISTS idx_events_trial ON events(trial_id, sequence);
                    CREATE INDEX IF NOT EXISTS idx_events_identity ON events(identity_hash, sequence);
                    CREATE TRIGGER IF NOT EXISTS no_update_events BEFORE UPDATE ON events
                    BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;
                    CREATE TRIGGER IF NOT EXISTS no_delete_events BEFORE DELETE ON events
                    BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;
                    CREATE TRIGGER IF NOT EXISTS no_update_families BEFORE UPDATE ON families
                    BEGIN SELECT RAISE(ABORT, 'families are immutable'); END;
                    CREATE TRIGGER IF NOT EXISTS no_delete_families BEFORE DELETE ON families
                    BEGIN SELECT RAISE(ABORT, 'families are immutable'); END;
                    CREATE TRIGGER IF NOT EXISTS no_update_identities BEFORE UPDATE ON identities
                    BEGIN SELECT RAISE(ABORT, 'identities are immutable'); END;
                    CREATE TRIGGER IF NOT EXISTS no_delete_identities BEFORE DELETE ON identities
                    BEGIN SELECT RAISE(ABORT, 'identities are immutable'); END;
                    CREATE TRIGGER IF NOT EXISTS no_update_meta BEFORE UPDATE ON meta
                    BEGIN SELECT RAISE(ABORT, 'ledger metadata is immutable'); END;
                    CREATE TRIGGER IF NOT EXISTS no_delete_meta BEFORE DELETE ON meta
                    BEGIN SELECT RAISE(ABORT, 'ledger metadata is immutable'); END;
                    """
                )
                connection.execute(
                    "INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', ?)",
                    (LEDGER_SCHEMA_VERSION,),
                )
                row = connection.execute(
                    "SELECT value FROM meta WHERE key = 'schema_version'"
                ).fetchone()
                if row is None or row["value"] != LEDGER_SCHEMA_VERSION:
                    raise LedgerIntegrityError("unsupported Trial Ledger schema")
        except sqlite3.DatabaseError as exc:
            raise LedgerIntegrityError("cannot initialize Trial Ledger", error=str(exc)) from exc

    def create_family(self, family_id: str, budget_limit: int) -> None:
        require_identifier(family_id, "family_id")
        if isinstance(budget_limit, bool) or not isinstance(budget_limit, int) or budget_limit < 0:
            raise ValueError("budget_limit must be a non-negative integer")
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT budget_limit FROM families WHERE family_id = ?", (family_id,)
                ).fetchone()
                if row is None:
                    connection.execute(
                        "INSERT INTO families(family_id, budget_limit) VALUES (?, ?)",
                        (family_id, budget_limit),
                    )
                elif row["budget_limit"] != budget_limit:
                    raise ContractValidationError(
                        FailureCode.INVALID_VALUE,
                        "family already exists with a different budget",
                    )
                connection.commit()
        except sqlite3.DatabaseError as exc:
            raise LedgerIntegrityError("family registration failed", error=str(exc)) from exc

    def register_identity(self, identity: ExperimentIdentity) -> str:
        if not isinstance(identity, ExperimentIdentity):
            raise TypeError("identity must be ExperimentIdentity")
        identity_json = identity.canonical_json()
        identity_hash = identity.identity_hash
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT identity_json FROM identities WHERE identity_hash = ?",
                    (identity_hash,),
                ).fetchone()
                if row is None:
                    connection.execute(
                        "INSERT INTO identities(identity_hash, identity_json) VALUES (?, ?)",
                        (identity_hash, identity_json),
                    )
                elif row["identity_json"] != identity_json:
                    raise LedgerIntegrityError("identity hash collision or tamper")
                connection.commit()
            return identity_hash
        except sqlite3.DatabaseError as exc:
            raise LedgerIntegrityError("identity registration failed", error=str(exc)) from exc

    def reserve_attempt(
        self,
        *,
        family_id: str,
        trial_id: str,
        identity_hash: str,
        proposer_id: str,
        units: int = 1,
    ) -> LedgerEvent:
        for value, name in (
            (family_id, "family_id"),
            (trial_id, "trial_id"),
            (proposer_id, "proposer_id"),
        ):
            require_identifier(value, name)
        if isinstance(units, bool) or not isinstance(units, int) or units < 1:
            raise ValueError("reservation units must be positive")
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                family = connection.execute(
                    "SELECT budget_limit FROM families WHERE family_id = ?", (family_id,)
                ).fetchone()
                identity = connection.execute(
                    "SELECT 1 FROM identities WHERE identity_hash = ?", (identity_hash,)
                ).fetchone()
                if family is None or identity is None:
                    raise ContractValidationError(
                        FailureCode.INVALID_IDENTIFIER,
                        "family and registered identity are required before reservation",
                    )
                existing_identity = connection.execute(
                    "SELECT * FROM events WHERE identity_hash = ? AND event_type = 'RESERVE' ORDER BY sequence LIMIT 1",
                    (identity_hash,),
                ).fetchone()
                if existing_identity is not None:
                    connection.commit()
                    return self._row_to_event(existing_identity)
                existing_trial = connection.execute(
                    "SELECT * FROM events WHERE trial_id = ? ORDER BY sequence LIMIT 1",
                    (trial_id,),
                ).fetchone()
                if existing_trial is not None:
                    raise ContractValidationError(
                        FailureCode.INVALID_IDENTIFIER,
                        "trial_id already belongs to another attempt",
                    )
                consumed = connection.execute(
                    """
                    SELECT COALESCE(SUM(
                        CASE event_type
                            WHEN 'RESERVE' THEN units
                            WHEN 'RELEASE' THEN -units
                            ELSE 0
                        END
                    ), 0) AS consumed
                    FROM events WHERE family_id = ?
                    """,
                    (family_id,),
                ).fetchone()["consumed"]
                if consumed + units > family["budget_limit"]:
                    raise ContractValidationError(
                        FailureCode.INVALID_VALUE,
                        "family budget is exhausted",
                        details={
                            "family_id": family_id,
                            "budget_limit": family["budget_limit"],
                            "consumed": consumed,
                            "requested": units,
                        },
                    )
                event = self._append_event(
                    connection,
                    event_type=LedgerEventType.RESERVE,
                    family_id=family_id,
                    trial_id=trial_id,
                    identity_hash=identity_hash,
                    proposer_id=proposer_id,
                    units=units,
                )
                connection.commit()
                return event
        except sqlite3.DatabaseError as exc:
            raise LedgerIntegrityError("reservation transaction failed", error=str(exc)) from exc

    def start_attempt(self, trial_id: str) -> LedgerEvent:
        return self._transition(trial_id, LedgerEventType.START)

    def commit_outcome(
        self,
        trial_id: str,
        *,
        outcome: TrialOutcome,
        failure_code: FailureCode | None = None,
    ) -> LedgerEvent:
        if not isinstance(outcome, TrialOutcome):
            raise TypeError("outcome must be TrialOutcome")
        if outcome is TrialOutcome.SUCCESS and failure_code is not None:
            raise ValueError("successful outcomes cannot carry a failure code")
        if outcome is not TrialOutcome.SUCCESS and failure_code is None:
            raise ValueError("failed outcomes require a typed failure code")
        return self._transition(
            trial_id,
            LedgerEventType.TERMINAL,
            outcome=outcome,
            failure_code=failure_code,
        )

    def release_reservation(self, trial_id: str) -> LedgerEvent:
        return self._transition(trial_id, LedgerEventType.RELEASE)

    def _transition(
        self,
        trial_id: str,
        event_type: LedgerEventType,
        *,
        outcome: TrialOutcome | None = None,
        failure_code: FailureCode | None = None,
    ) -> LedgerEvent:
        require_identifier(trial_id, "trial_id")
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                rows = connection.execute(
                    "SELECT * FROM events WHERE trial_id = ? ORDER BY sequence", (trial_id,)
                ).fetchall()
                if not rows:
                    raise ContractValidationError(
                        FailureCode.INVALID_IDENTIFIER, "trial has no reservation"
                    )
                current_types = [LedgerEventType(row["event_type"]) for row in rows]
                reserve = self._row_to_event(rows[0])
                if event_type is LedgerEventType.START:
                    if current_types == [LedgerEventType.RESERVE]:
                        pass
                    elif current_types == [LedgerEventType.RESERVE, LedgerEventType.START]:
                        connection.commit()
                        return self._row_to_event(rows[-1])
                    else:
                        raise ContractValidationError(
                            FailureCode.INVALID_STATE_TRANSITION,
                            "only a reserved attempt can start",
                        )
                    units = 0
                elif event_type is LedgerEventType.TERMINAL:
                    if current_types != [LedgerEventType.RESERVE, LedgerEventType.START]:
                        raise ContractValidationError(
                            FailureCode.INVALID_STATE_TRANSITION,
                            "only a started attempt can commit a terminal outcome",
                        )
                    units = 0
                elif event_type is LedgerEventType.RELEASE:
                    if current_types != [LedgerEventType.RESERVE]:
                        raise ContractValidationError(
                            FailureCode.INVALID_STATE_TRANSITION,
                            "only a truly unstarted reservation can be released",
                        )
                    units = reserve.units
                else:
                    raise AssertionError("unsupported transition")
                event = self._append_event(
                    connection,
                    event_type=event_type,
                    family_id=reserve.family_id,
                    trial_id=reserve.trial_id,
                    identity_hash=reserve.identity_hash,
                    proposer_id=reserve.proposer_id,
                    units=units,
                    outcome=outcome,
                    failure_code=failure_code,
                )
                connection.commit()
                return event
        except sqlite3.DatabaseError as exc:
            raise LedgerIntegrityError("trial transition failed", error=str(exc)) from exc

    def _append_event(
        self,
        connection: sqlite3.Connection,
        *,
        event_type: LedgerEventType,
        family_id: str,
        trial_id: str,
        identity_hash: str,
        proposer_id: str,
        units: int,
        outcome: TrialOutcome | None = None,
        failure_code: FailureCode | None = None,
    ) -> LedgerEvent:
        previous = connection.execute(
            "SELECT sequence, event_hash FROM events ORDER BY sequence DESC LIMIT 1"
        ).fetchone()
        sequence = 1 if previous is None else previous["sequence"] + 1
        prev_hash = ZERO_HASH if previous is None else previous["event_hash"]
        event_time = self.clock()
        require_aware_datetime(event_time, "event_time")
        event_id = f"event-{sequence:012d}"
        body = self._event_body(
            sequence=sequence,
            event_id=event_id,
            event_type=event_type,
            family_id=family_id,
            trial_id=trial_id,
            identity_hash=identity_hash,
            proposer_id=proposer_id,
            units=units,
            outcome=outcome,
            failure_code=failure_code,
            event_time=event_time,
            prev_hash=prev_hash,
        )
        event_hash = canonical_content_hash(body)
        connection.execute(
            """
            INSERT INTO events(
                sequence, event_id, event_type, family_id, trial_id,
                identity_hash, proposer_id, units, outcome, failure_code,
                event_time, prev_hash, event_hash
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                sequence,
                event_id,
                event_type.value,
                family_id,
                trial_id,
                identity_hash,
                proposer_id,
                units,
                None if outcome is None else outcome.value,
                None if failure_code is None else failure_code.value,
                event_time.isoformat(),
                prev_hash,
                event_hash,
            ),
        )
        return LedgerEvent(event_hash=event_hash, **body)

    @staticmethod
    def _event_body(
        *,
        sequence: int,
        event_id: str,
        event_type: LedgerEventType,
        family_id: str,
        trial_id: str,
        identity_hash: str,
        proposer_id: str,
        units: int,
        outcome: TrialOutcome | None,
        failure_code: FailureCode | None,
        event_time: datetime,
        prev_hash: str,
    ) -> dict:
        return {
            "sequence": sequence,
            "event_id": event_id,
            "event_type": event_type,
            "family_id": family_id,
            "trial_id": trial_id,
            "identity_hash": identity_hash,
            "proposer_id": proposer_id,
            "units": units,
            "outcome": outcome,
            "failure_code": failure_code,
            "event_time": event_time,
            "prev_hash": prev_hash,
        }

    @staticmethod
    def _row_to_event(row: sqlite3.Row) -> LedgerEvent:
        return LedgerEvent(
            sequence=row["sequence"],
            event_id=row["event_id"],
            event_type=LedgerEventType(row["event_type"]),
            family_id=row["family_id"],
            trial_id=row["trial_id"],
            identity_hash=row["identity_hash"],
            proposer_id=row["proposer_id"],
            units=row["units"],
            outcome=None if row["outcome"] is None else TrialOutcome(row["outcome"]),
            failure_code=None
            if row["failure_code"] is None
            else FailureCode(row["failure_code"]),
            event_time=datetime.fromisoformat(row["event_time"]),
            prev_hash=row["prev_hash"],
            event_hash=row["event_hash"],
        )

    def replay(self) -> LedgerReplay:
        try:
            header = self.path.read_bytes()[:16]
        except OSError as exc:
            raise LedgerIntegrityError("Trial Ledger file is unreadable", error=str(exc)) from exc
        if header != b"SQLite format 3\x00":
            raise LedgerIntegrityError("Trial Ledger file header is corrupt")
        try:
            with self._connect() as connection:
                check = connection.execute("PRAGMA quick_check").fetchone()[0]
                if check != "ok":
                    raise LedgerIntegrityError("SQLite integrity check failed", result=check)
                meta = connection.execute(
                    "SELECT value FROM meta WHERE key = 'schema_version'"
                ).fetchone()
                if meta is None or meta["value"] != LEDGER_SCHEMA_VERSION:
                    raise LedgerIntegrityError("Trial Ledger schema metadata is invalid")
                family_rows = connection.execute(
                    "SELECT family_id, budget_limit FROM families ORDER BY family_id"
                ).fetchall()
                identity_rows = connection.execute(
                    "SELECT identity_hash, identity_json FROM identities ORDER BY identity_hash"
                ).fetchall()
                event_rows = connection.execute(
                    "SELECT * FROM events ORDER BY sequence"
                ).fetchall()
        except LedgerIntegrityError:
            raise
        except (sqlite3.DatabaseError, OSError) as exc:
            raise LedgerIntegrityError("Trial Ledger is corrupt or unreadable", error=str(exc)) from exc

        identities: set[str] = set()
        for row in identity_rows:
            try:
                parsed = json.loads(row["identity_json"])
                canonical = json.dumps(
                    parsed,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                    allow_nan=False,
                )
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise LedgerIntegrityError("identity JSON is corrupt") from exc
            actual = "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()
            if actual != row["identity_hash"]:
                raise LedgerIntegrityError("identity hash verification failed")
            identities.add(row["identity_hash"])

        family_limits = {row["family_id"]: row["budget_limit"] for row in family_rows}
        consumed = {family_id: 0 for family_id in family_limits}
        trials: dict[str, TrialReplayState] = {}
        previous_hash = ZERO_HASH
        expected_sequence = 1
        for row in event_rows:
            try:
                event = self._row_to_event(row)
            except (ValueError, TypeError) as exc:
                raise LedgerIntegrityError("event enum or timestamp is corrupt") from exc
            if event.sequence != expected_sequence or event.prev_hash != previous_hash:
                raise LedgerIntegrityError("event sequence/hash chain is broken")
            body = self._event_body(
                sequence=event.sequence,
                event_id=event.event_id,
                event_type=event.event_type,
                family_id=event.family_id,
                trial_id=event.trial_id,
                identity_hash=event.identity_hash,
                proposer_id=event.proposer_id,
                units=event.units,
                outcome=event.outcome,
                failure_code=event.failure_code,
                event_time=event.event_time,
                prev_hash=event.prev_hash,
            )
            if canonical_content_hash(body) != event.event_hash:
                raise LedgerIntegrityError("event content hash verification failed")
            if event.family_id not in family_limits or event.identity_hash not in identities:
                raise LedgerIntegrityError("event references unknown immutable state")

            current = trials.get(event.trial_id)
            if event.event_type is LedgerEventType.RESERVE:
                if current is not None or event.units < 1:
                    raise LedgerIntegrityError("invalid RESERVE event")
                consumed[event.family_id] += event.units
                if consumed[event.family_id] > family_limits[event.family_id]:
                    raise LedgerIntegrityError("family budget overspent in event replay")
                trials[event.trial_id] = TrialReplayState(
                    trial_id=event.trial_id,
                    family_id=event.family_id,
                    identity_hash=event.identity_hash,
                    proposer_id=event.proposer_id,
                    units=event.units,
                    status=TrialStatus.RESERVED,
                    outcome=None,
                    failure_code=None,
                )
            elif current is None:
                raise LedgerIntegrityError("trial event appears before reservation")
            elif event.event_type is LedgerEventType.START:
                if current.status is not TrialStatus.RESERVED or event.units != 0:
                    raise LedgerIntegrityError("invalid START event")
                trials[event.trial_id] = TrialReplayState(
                    **{**current.__dict__, "status": TrialStatus.STARTED}
                )
            elif event.event_type is LedgerEventType.TERMINAL:
                if current.status is not TrialStatus.STARTED or event.outcome is None:
                    raise LedgerIntegrityError("invalid TERMINAL event")
                if event.outcome is TrialOutcome.SUCCESS and event.failure_code is not None:
                    raise LedgerIntegrityError("successful terminal event has failure code")
                if event.outcome is not TrialOutcome.SUCCESS and event.failure_code is None:
                    raise LedgerIntegrityError("failed terminal event lacks failure code")
                trials[event.trial_id] = TrialReplayState(
                    **{
                        **current.__dict__,
                        "status": TrialStatus.TERMINAL,
                        "outcome": event.outcome,
                        "failure_code": event.failure_code,
                    }
                )
            elif event.event_type is LedgerEventType.RELEASE:
                if current.status is not TrialStatus.RESERVED or event.units != current.units:
                    raise LedgerIntegrityError("invalid RELEASE event")
                consumed[event.family_id] -= event.units
                trials[event.trial_id] = TrialReplayState(
                    **{**current.__dict__, "status": TrialStatus.RELEASED}
                )
            previous_hash = event.event_hash
            expected_sequence += 1

        families: dict[str, FamilyReplayState] = {}
        for family_id, limit in family_limits.items():
            family_trials = [state for state in trials.values() if state.family_id == family_id]
            families[family_id] = FamilyReplayState(
                family_id=family_id,
                budget_limit=limit,
                consumed=consumed[family_id],
                reserved_trials=sum(state.status is TrialStatus.RESERVED for state in family_trials),
                terminal_trials=sum(state.status is TrialStatus.TERMINAL for state in family_trials),
                released_trials=sum(state.status is TrialStatus.RELEASED for state in family_trials),
            )
        return LedgerReplay(
            last_sequence=expected_sequence - 1,
            last_hash=previous_hash,
            families=families,
            trials=trials,
        )

