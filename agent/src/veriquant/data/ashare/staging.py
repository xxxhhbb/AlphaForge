"""Deterministic visible-only CSV staging for an existing local Qlib runtime."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

from ...contracts import CanonicalContract, FailureCode
from ...contracts.base import (
    canonicalize,
    fail,
    freeze_mapping,
    require_aware_datetime,
    require_hash,
    require_text,
)
from ..bitemporal import canonical_content_hash
from .models import FORBIDDEN_NAMES, FrozenAShareSnapshot, OHLCV_FIELDS


_DEFAULT_FIELD_MAPPING = MappingProxyType(
    {
        "open": "open",
        "high": "high",
        "low": "low",
        "close": "close",
        "volume": "volume",
        "amount": "amount",
    }
)


def _file_hash(content: bytes) -> str:
    return "sha256:" + hashlib.sha256(content).hexdigest()


def _hash_segment(value: str) -> str:
    require_hash(value, "content_hash")
    return value.removeprefix("sha256:")


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


@dataclass(frozen=True, kw_only=True)
class VisibleStagingSplit(CanonicalContract):
    train_start: datetime
    train_end: datetime
    validation_start: datetime
    validation_end: datetime

    def __post_init__(self) -> None:
        for name in (
            "train_start",
            "train_end",
            "validation_start",
            "validation_end",
        ):
            require_aware_datetime(getattr(self, name), name)
        if not (
            self.train_start <= self.train_end < self.validation_start <= self.validation_end
        ):
            fail(
                FailureCode.INVALID_TIME_ORDER,
                "visible staging requires disjoint ordered train and validation windows",
            )

    def partition(self, event_time: datetime) -> str | None:
        if self.train_start <= event_time <= self.train_end:
            return "train"
        if self.validation_start <= event_time <= self.validation_end:
            return "validation"
        return None


@dataclass(frozen=True, kw_only=True)
class QlibStagingResult(CanonicalContract):
    staging_hash: str
    source_snapshot_hash: str
    directory: str
    manifest_hash: str
    file_hashes: Mapping[str, str]
    train_rows: int
    validation_rows: int
    reused: bool

    def __post_init__(self) -> None:
        for name in ("staging_hash", "source_snapshot_hash", "manifest_hash"):
            require_hash(getattr(self, name), name)
        require_text(self.directory, "directory")
        hashes = freeze_mapping(self.file_hashes, "file_hashes")
        for name, value in hashes.items():
            if any(token in name.lower() for token in FORBIDDEN_NAMES):
                fail(FailureCode.UNAUTHORIZED_CAPABILITY, "staging has a forbidden file name")
            require_hash(value, f"file_hashes.{name}")
        object.__setattr__(self, "file_hashes", hashes)
        if self.train_rows <= 0 or self.validation_rows <= 0:
            fail(FailureCode.INCOMPLETE_RESULT, "both visible partitions require rows")
        if type(self.reused) is not bool:
            fail(FailureCode.INVALID_VALUE, "reused must be boolean")


def _csv_content(rows: list[dict[str, object]], columns: tuple[str, ...]) -> bytes:
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=list(columns), lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({name: row.get(name, "") for name in columns})
    return buffer.getvalue().encode("utf-8")


class QlibCsvStager:
    """Stage frozen PIT rows; the output contains train/validation only."""

    def __init__(self, output_root: str | Path) -> None:
        self.output_root = Path(output_root).resolve()

    def stage(
        self,
        *,
        snapshot: FrozenAShareSnapshot,
        split: VisibleStagingSplit,
        field_mapping: Mapping[str, str] | None = None,
    ) -> QlibStagingResult:
        if not isinstance(snapshot, FrozenAShareSnapshot):
            fail(FailureCode.INVALID_VALUE, "Qlib staging requires FrozenAShareSnapshot")
        if not isinstance(split, VisibleStagingSplit):
            fail(FailureCode.INVALID_VALUE, "Qlib staging requires VisibleStagingSplit")
        mapping = dict(_DEFAULT_FIELD_MAPPING if field_mapping is None else field_mapping)
        if not mapping or set(mapping) - OHLCV_FIELDS:
            fail(FailureCode.UNKNOWN_FIELD, "Qlib field mapping contains unknown source fields")
        if len(set(mapping.values())) != len(mapping):
            fail(FailureCode.INVALID_VALUE, "Qlib field mapping targets must be unique")
        for source_name, target_name in mapping.items():
            require_text(target_name, f"field_mapping.{source_name}")
            if any(token in target_name.lower() for token in FORBIDDEN_NAMES):
                fail(FailureCode.UNAUTHORIZED_CAPABILITY, "forbidden Qlib field mapping")

        visible_rows = [row for row in snapshot.rows if split.partition(row.event_time)]
        train_rows = sum(split.partition(row.event_time) == "train" for row in visible_rows)
        validation_rows = sum(
            split.partition(row.event_time) == "validation" for row in visible_rows
        )
        if train_rows == 0 or validation_rows == 0:
            fail(FailureCode.INCOMPLETE_RESULT, "snapshot does not cover both visible partitions")

        files: dict[str, bytes] = {}
        csv_columns = ("datetime", "instrument", "partition", *tuple(mapping.values()))
        symbols = sorted({row.symbol for row in visible_rows})
        for symbol in symbols:
            normalized: list[dict[str, object]] = []
            for row in sorted(
                (item for item in visible_rows if item.symbol == symbol),
                key=lambda item: item.event_time,
            ):
                record: dict[str, object] = {
                    "datetime": row.event_time.date().isoformat(),
                    "instrument": row.symbol,
                    "partition": split.partition(row.event_time),
                }
                for source_name, target_name in mapping.items():
                    value = row.values.get(source_name)
                    record[target_name] = "" if value is None else value
                normalized.append(record)
            safe_symbol = symbol.replace("/", "_").replace("\\", "_")
            files[f"csv/{safe_symbol}.csv"] = _csv_content(normalized, csv_columns)

        calendar = "\n".join(
            sorted({row.event_time.date().isoformat() for row in visible_rows})
        ) + "\n"
        instruments = "\n".join(symbols) + "\n"
        files["calendars/day.txt"] = calendar.encode("utf-8")
        files["instruments/all.txt"] = instruments.encode("utf-8")
        file_hashes = {name: _file_hash(content) for name, content in sorted(files.items())}
        staging_identity = {
            "schema_version": "veriquant.qlib-visible-staging/v1",
            "source_snapshot_hash": snapshot.snapshot_hash,
            "request_hash": snapshot.request.artifact_hash,
            "historical_universe_hash": canonical_content_hash(snapshot.historical_universe),
            "adjustment": snapshot.request.adjustment,
            "availability_policy": snapshot.availability_policy,
            "trading_calendar": snapshot.trading_calendar,
            "trading_calendar_hash": None if snapshot.trading_calendar is None else snapshot.trading_calendar.artifact_hash,
            "field_mapping": mapping,
            "split": split,
            "file_hashes": file_hashes,
        }
        staging_hash = canonical_content_hash(staging_identity)
        manifest_core = {
            **staging_identity,
            "staging_hash": staging_hash,
            "train_rows": train_rows,
            "validation_rows": validation_rows,
            "partitions": ("train", "validation"),
            "source_dataset_card_hash": snapshot.dataset_card.artifact_hash,
        }
        manifest_hash = canonical_content_hash(manifest_core)
        manifest_content = _json_bytes({**manifest_core, "manifest_hash": manifest_hash})
        directory = self.output_root / _hash_segment(staging_hash)
        manifest_path = directory / "manifest.json"
        if directory.exists():
            if not manifest_path.is_file():
                fail(FailureCode.ARTIFACT_COLLISION, "staging directory is incomplete")
            existing = json.loads(manifest_path.read_text(encoding="utf-8"))
            if existing.get("manifest_hash") != manifest_hash:
                fail(FailureCode.ARTIFACT_COLLISION, "staging path owns different content")
            for name, expected in file_hashes.items():
                path = directory / name
                if not path.is_file() or _file_hash(path.read_bytes()) != expected:
                    fail(FailureCode.ARTIFACT_COLLISION, "staged file hash mismatch")
            return QlibStagingResult(
                staging_hash=staging_hash,
                source_snapshot_hash=snapshot.snapshot_hash,
                directory=str(directory),
                manifest_hash=manifest_hash,
                file_hashes=file_hashes,
                train_rows=train_rows,
                validation_rows=validation_rows,
                reused=True,
            )
        directory.mkdir(parents=True, exist_ok=False)
        for name, content in files.items():
            _atomic_write(directory / name, content)
        _atomic_write(manifest_path, manifest_content)
        return QlibStagingResult(
            staging_hash=staging_hash,
            source_snapshot_hash=snapshot.snapshot_hash,
            directory=str(directory),
            manifest_hash=manifest_hash,
            file_hashes=file_hashes,
            train_rows=train_rows,
            validation_rows=validation_rows,
            reused=False,
        )
