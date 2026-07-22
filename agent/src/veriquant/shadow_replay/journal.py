"""Append-only JSONL journal for multi-day shadow replay."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

from ..contracts import ContractValidationError, FailureCode
from ..contracts.base import canonicalize, require_hash
from .models import DailyReplayRecord

JOURNAL_SCHEMA_VERSION = "veriquant-shadow-replay-journal/v1"


def stable_hash(value: Any) -> str:
    import hashlib

    rendered = json.dumps(
        canonicalize(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    return "sha256:" + hashlib.sha256(rendered.encode("utf-8")).hexdigest()


def _fail(code: FailureCode, message: str, **details: Any) -> None:
    raise ContractValidationError(code, message, details=details)


@dataclass(frozen=True)
class JournalSnapshot:
    records: tuple[DailyReplayRecord, ...]
    tip_hash: str
    by_identity: Mapping[str, DailyReplayRecord]


class ShadowReplayJournal:
    """Persistent append-only replay journal with hash-chain verification."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def snapshot(self) -> JournalSnapshot:
        entries = self._read_verified_entries()
        records = tuple(entry["record_obj"] for entry in entries)
        by_identity: dict[str, DailyReplayRecord] = {}
        for record in records:
            existing = by_identity.get(record.identity_hash)
            if existing is not None and existing.artifact_hash != record.artifact_hash:
                _fail(
                    FailureCode.LEDGER_CORRUPTION,
                    "journal contains conflicting records for one replay identity",
                    identity_hash=record.identity_hash,
                )
            by_identity[record.identity_hash] = record
        tip_hash = entries[-1]["entry_hash"] if entries else stable_hash({"empty_shadow_replay_journal": True})
        return JournalSnapshot(
            records=records,
            tip_hash=tip_hash,
            by_identity=MappingProxyType(by_identity),
        )

    def append_or_get(self, record: DailyReplayRecord) -> DailyReplayRecord:
        snapshot = self.snapshot()
        existing = snapshot.by_identity.get(record.identity_hash)
        if existing is not None:
            if existing.artifact_hash != record.artifact_hash:
                _fail(
                    FailureCode.ARTIFACT_COLLISION,
                    "same replay identity produced different daily record",
                    identity_hash=record.identity_hash,
                )
            return existing
        self.path.parent.mkdir(parents=True, exist_ok=True)
        previous_hash = snapshot.tip_hash if snapshot.records else None
        body = {
            "schema_version": JOURNAL_SCHEMA_VERSION,
            "sequence": len(snapshot.records) + 1,
            "previous_hash": previous_hash,
            "record_hash": record.artifact_hash,
            "record": record.to_dict(),
        }
        entry = {**body, "entry_hash": stable_hash(body)}
        with self.path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(entry, sort_keys=True, separators=(",", ":"), ensure_ascii=False))
            handle.write("\n")
        return record

    def _read_verified_entries(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        entries: list[dict[str, Any]] = []
        previous_hash: str | None = None
        with self.path.open("r", encoding="utf-8") as handle:
            for line_number, raw_line in enumerate(handle, start=1):
                line = raw_line.rstrip("\n")
                if not line:
                    _fail(FailureCode.LEDGER_CORRUPTION, "blank line in replay journal", line=line_number)
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ContractValidationError(
                        FailureCode.LEDGER_CORRUPTION,
                        "replay journal line is not valid JSON",
                        details={"line": line_number},
                    ) from exc
                self._verify_entry(entry, expected_sequence=line_number, expected_previous_hash=previous_hash)
                record = DailyReplayRecord.from_payload(entry["record"])
                if record.artifact_hash != entry["record_hash"]:
                    _fail(FailureCode.LEDGER_CORRUPTION, "daily record hash mismatch", line=line_number)
                entry_hash = entry["entry_hash"]
                entries.append({**entry, "record_obj": record})
                previous_hash = entry_hash
        return entries

    @staticmethod
    def _verify_entry(entry: Mapping[str, Any], *, expected_sequence: int, expected_previous_hash: str | None) -> None:
        required = {"schema_version", "sequence", "previous_hash", "record_hash", "record", "entry_hash"}
        unknown = set(entry) - required
        missing = required - set(entry)
        if missing or unknown:
            _fail(
                FailureCode.LEDGER_CORRUPTION,
                "replay journal entry fields are invalid",
                missing=tuple(sorted(missing)),
                unknown=tuple(sorted(unknown)),
            )
        if entry["schema_version"] != JOURNAL_SCHEMA_VERSION:
            _fail(FailureCode.INVALID_SCHEMA_VERSION, "unsupported replay journal schema")
        if entry["sequence"] != expected_sequence:
            _fail(FailureCode.LEDGER_CORRUPTION, "replay journal sequence gap")
        if entry["previous_hash"] != expected_previous_hash:
            _fail(FailureCode.LEDGER_CORRUPTION, "replay journal hash chain is broken")
        require_hash(entry["record_hash"], "record_hash")
        require_hash(entry["entry_hash"], "entry_hash")
        body = {
            "schema_version": entry["schema_version"],
            "sequence": entry["sequence"],
            "previous_hash": entry["previous_hash"],
            "record_hash": entry["record_hash"],
            "record": entry["record"],
        }
        if stable_hash(body) != entry["entry_hash"]:
            _fail(FailureCode.LEDGER_CORRUPTION, "replay journal entry hash mismatch")
