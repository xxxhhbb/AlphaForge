"""Rebuildable lineage memory, intentionally separate from the Trial Ledger."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from threading import RLock

from ..adapters import DraftProposal
from ..contracts import ContractValidationError, EvolutionLineage, FailureCode
from ..data import canonical_content_hash


LINEAGE_SCHEMA_VERSION = "veriquant-lineage-memory/v1"
ZERO_HASH = "sha256:" + "0" * 64


class LineageIntegrityError(ContractValidationError):
    def __init__(self, message: str, **details) -> None:
        super().__init__(FailureCode.LINEAGE_CORRUPTION, message, details=details)


@dataclass(frozen=True, kw_only=True)
class StoredLineage:
    lineage: EvolutionLineage
    proposal: DraftProposal | None
    event_hash: str


class LineageStore:
    """Append-only operational lineage journal; never budget or promotion truth."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        if not self.path.parent.exists():
            raise ValueError("lineage store parent directory must already exist")
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30.0, isolation_level=None)
        connection.row_factory = sqlite3.Row
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
                    CREATE TABLE IF NOT EXISTS lineage_events (
                        sequence INTEGER PRIMARY KEY,
                        event_id TEXT NOT NULL UNIQUE,
                        child_id TEXT NOT NULL UNIQUE,
                        lineage_json TEXT NOT NULL,
                        proposal_json TEXT,
                        prev_hash TEXT NOT NULL,
                        event_hash TEXT NOT NULL UNIQUE
                    );
                    CREATE TRIGGER IF NOT EXISTS no_update_lineage
                    BEFORE UPDATE ON lineage_events
                    BEGIN SELECT RAISE(ABORT, 'lineage events are append-only'); END;
                    CREATE TRIGGER IF NOT EXISTS no_delete_lineage
                    BEFORE DELETE ON lineage_events
                    BEGIN SELECT RAISE(ABORT, 'lineage events are append-only'); END;
                    CREATE TRIGGER IF NOT EXISTS no_update_lineage_meta
                    BEFORE UPDATE ON meta
                    BEGIN SELECT RAISE(ABORT, 'lineage metadata is immutable'); END;
                    CREATE TRIGGER IF NOT EXISTS no_delete_lineage_meta
                    BEFORE DELETE ON meta
                    BEGIN SELECT RAISE(ABORT, 'lineage metadata is immutable'); END;
                    """
                )
                connection.execute(
                    "INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', ?)",
                    (LINEAGE_SCHEMA_VERSION,),
                )
                row = connection.execute(
                    "SELECT value FROM meta WHERE key = 'schema_version'"
                ).fetchone()
                if row is None or row["value"] != LINEAGE_SCHEMA_VERSION:
                    raise LineageIntegrityError("unsupported lineage schema")
        except sqlite3.DatabaseError as exc:
            raise LineageIntegrityError("cannot initialize lineage store", error=str(exc)) from exc

    @staticmethod
    def _body(
        *,
        sequence: int,
        event_id: str,
        child_id: str,
        lineage_json: str,
        proposal_json: str | None,
        prev_hash: str,
    ) -> dict[str, object]:
        return {
            "sequence": sequence,
            "event_id": event_id,
            "child_id": child_id,
            "lineage_json": lineage_json,
            "proposal_json": proposal_json,
            "prev_hash": prev_hash,
        }

    def append(
        self, lineage: EvolutionLineage, proposal: DraftProposal | None
    ) -> StoredLineage:
        if not isinstance(lineage, EvolutionLineage):
            raise TypeError("lineage must be EvolutionLineage")
        if proposal is not None and not isinstance(proposal, DraftProposal):
            raise TypeError("proposal must be DraftProposal or None")
        if lineage.outcome == "COMMITTED_DRAFT":
            if proposal is None or proposal.artifact_hash != lineage.candidate_hash:
                raise LineageIntegrityError("committed lineage must bind its DRAFT artifact")
        elif proposal is not None:
            raise LineageIntegrityError("failed lineage cannot store a success proposal")

        lineage_json = lineage.canonical_json()
        proposal_json = None if proposal is None else proposal.canonical_json()
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                existing = connection.execute(
                    "SELECT * FROM lineage_events WHERE child_id = ?",
                    (lineage.candidate_id,),
                ).fetchone()
                if existing is not None:
                    if (
                        existing["lineage_json"] != lineage_json
                        or existing["proposal_json"] != proposal_json
                    ):
                        raise LineageIntegrityError("child lineage conflicts with prior memory")
                    connection.commit()
                    return StoredLineage(
                        lineage=lineage,
                        proposal=proposal,
                        event_hash=existing["event_hash"],
                    )
                previous = connection.execute(
                    "SELECT sequence, event_hash FROM lineage_events "
                    "ORDER BY sequence DESC LIMIT 1"
                ).fetchone()
                sequence = 1 if previous is None else int(previous["sequence"]) + 1
                prev_hash = ZERO_HASH if previous is None else previous["event_hash"]
                event_id = f"lineage-{sequence:012d}"
                body = self._body(
                    sequence=sequence,
                    event_id=event_id,
                    child_id=lineage.candidate_id,
                    lineage_json=lineage_json,
                    proposal_json=proposal_json,
                    prev_hash=prev_hash,
                )
                event_hash = canonical_content_hash(body)
                connection.execute(
                    """
                    INSERT INTO lineage_events(
                        sequence, event_id, child_id, lineage_json,
                        proposal_json, prev_hash, event_hash
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        sequence,
                        event_id,
                        lineage.candidate_id,
                        lineage_json,
                        proposal_json,
                        prev_hash,
                        event_hash,
                    ),
                )
                connection.commit()
                return StoredLineage(
                    lineage=lineage,
                    proposal=proposal,
                    event_hash=event_hash,
                )
        except sqlite3.DatabaseError as exc:
            raise LineageIntegrityError("lineage append failed", error=str(exc)) from exc

    def replay(self) -> dict[str, StoredLineage]:
        try:
            if self.path.read_bytes()[:16] != b"SQLite format 3\x00":
                raise LineageIntegrityError("invalid lineage SQLite header")
            with self._connect() as connection:
                check = connection.execute("PRAGMA quick_check").fetchone()
                if check is None or check[0] != "ok":
                    raise LineageIntegrityError("lineage SQLite quick_check failed")
                meta = connection.execute(
                    "SELECT value FROM meta WHERE key = 'schema_version'"
                ).fetchone()
                if meta is None or meta["value"] != LINEAGE_SCHEMA_VERSION:
                    raise LineageIntegrityError("lineage schema mismatch")
                rows = connection.execute(
                    "SELECT * FROM lineage_events ORDER BY sequence"
                ).fetchall()
        except sqlite3.DatabaseError as exc:
            raise LineageIntegrityError("cannot replay lineage store", error=str(exc)) from exc
        except OSError as exc:
            raise LineageIntegrityError("cannot read lineage store", error=str(exc)) from exc

        rebuilt: dict[str, StoredLineage] = {}
        previous_hash = ZERO_HASH
        for expected_sequence, row in enumerate(rows, 1):
            if row["sequence"] != expected_sequence or row["prev_hash"] != previous_hash:
                raise LineageIntegrityError("lineage sequence or hash chain is broken")
            body = self._body(
                sequence=row["sequence"],
                event_id=row["event_id"],
                child_id=row["child_id"],
                lineage_json=row["lineage_json"],
                proposal_json=row["proposal_json"],
                prev_hash=row["prev_hash"],
            )
            if canonical_content_hash(body) != row["event_hash"]:
                raise LineageIntegrityError("lineage event hash mismatch")
            try:
                lineage = EvolutionLineage.from_payload(json.loads(row["lineage_json"]))
                proposal = (
                    None
                    if row["proposal_json"] is None
                    else DraftProposal.from_payload(json.loads(row["proposal_json"]))
                )
            except (ValueError, TypeError, json.JSONDecodeError) as exc:
                raise LineageIntegrityError("invalid typed lineage payload") from exc
            if lineage.candidate_id != row["child_id"]:
                raise LineageIntegrityError("lineage child id mismatch")
            if lineage.outcome == "COMMITTED_DRAFT":
                if proposal is None or proposal.artifact_hash != lineage.candidate_hash:
                    raise LineageIntegrityError("lineage proposal artifact mismatch")
            elif proposal is not None:
                raise LineageIntegrityError("failed lineage contains a proposal")
            if lineage.candidate_id in rebuilt:
                raise LineageIntegrityError("duplicate lineage child")
            rebuilt[lineage.candidate_id] = StoredLineage(
                lineage=lineage,
                proposal=proposal,
                event_hash=row["event_hash"],
            )
            previous_hash = row["event_hash"]
        return rebuilt


class ProposalMemory:
    """Clearable materialization. Rebuild always comes from LineageStore."""

    def __init__(self, store: LineageStore) -> None:
        self.store = store
        self._lock = RLock()
        self._items: dict[str, StoredLineage] = {}
        self.rebuild()

    def rebuild(self) -> tuple[EvolutionLineage, ...]:
        replayed = self.store.replay()
        with self._lock:
            self._items = replayed
            return tuple(item.lineage for item in self._items.values())

    def clear(self) -> None:
        with self._lock:
            self._items.clear()

    def record(
        self, lineage: EvolutionLineage, proposal: DraftProposal | None
    ) -> StoredLineage:
        stored = self.store.append(lineage, proposal)
        with self._lock:
            self._items[lineage.candidate_id] = stored
        return stored

    def get(self, candidate_id: str) -> StoredLineage | None:
        with self._lock:
            return self._items.get(candidate_id)

    def records(self) -> tuple[EvolutionLineage, ...]:
        with self._lock:
            return tuple(item.lineage for item in self._items.values())

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)
