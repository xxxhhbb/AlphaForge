"""Content-addressed, atomic storage for A-share snapshots and typed failures."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from ...contracts import CanonicalContract, FailureCode
from ...contracts.base import canonicalize, fail, require_hash
from ..bitemporal import canonical_content_hash
from .models import AShareDataRequest, AShareFailure, FrozenAShareSnapshot


@dataclass(frozen=True, kw_only=True)
class SnapshotPersistResult(CanonicalContract):
    snapshot_hash: str
    directory: str
    manifest_hash: str
    raw_content_hash: str
    reused: bool

    def __post_init__(self) -> None:
        require_hash(self.snapshot_hash, "snapshot_hash")
        require_hash(self.manifest_hash, "manifest_hash")
        require_hash(self.raw_content_hash, "raw_content_hash")


def _json_bytes(value) -> bytes:
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


class FrozenSnapshotStore:
    def __init__(self, root: str | Path | None = None) -> None:
        repo_root = Path(__file__).resolve().parents[5]
        self.root = Path(root).resolve() if root is not None else repo_root / ".autobuild" / "veriquant" / "datasets"

    def persist(self, snapshot: FrozenAShareSnapshot) -> SnapshotPersistResult:
        if not isinstance(snapshot, FrozenAShareSnapshot):
            fail(FailureCode.INVALID_VALUE, "typed FrozenAShareSnapshot is required")
        directory = self.root / snapshot.request.dataset_id / _hash_segment(snapshot.snapshot_hash)
        raw_path = directory / "raw_response.json"
        manifest_path = directory / "manifest.json"
        raw_payload = {"schema_version": "ashare-raw/v1", "rows": snapshot.rows}
        raw_content = _json_bytes(raw_payload)
        raw_hash = canonical_content_hash(raw_payload)
        manifest_payload = {
            "schema_version": "ashare-snapshot-manifest/v1",
            "snapshot_hash": snapshot.snapshot_hash,
            "request": snapshot.request,
            "request_hash": snapshot.request.artifact_hash,
            "fetch_timestamp": snapshot.fetched_at,
            "provider": snapshot.provider,
            "raw_schema": "ashare-raw/v1",
            "raw_content_hash": raw_hash,
            "availability_policy": snapshot.availability_policy,
            "trading_calendar": snapshot.trading_calendar,
            "historical_universe": snapshot.historical_universe,
            "coverage": snapshot.coverage_by_field,
            "excluded_rows": snapshot.excluded_rows,
            "dataset_card": snapshot.dataset_card,
            "errors": [],
        }
        manifest_hash = canonical_content_hash(manifest_payload)
        manifest_content = _json_bytes({**manifest_payload, "manifest_hash": manifest_hash})
        if directory.exists():
            if not raw_path.is_file() or not manifest_path.is_file():
                fail(FailureCode.ARTIFACT_COLLISION, "snapshot directory is incomplete")
            existing = json.loads(manifest_path.read_text(encoding="utf-8"))
            if existing.get("snapshot_hash") != snapshot.snapshot_hash or existing.get("raw_content_hash") != raw_hash:
                fail(FailureCode.ARTIFACT_COLLISION, "snapshot path owns different frozen content")
            return SnapshotPersistResult(
                snapshot_hash=snapshot.snapshot_hash,
                directory=str(directory),
                manifest_hash=str(existing["manifest_hash"]),
                raw_content_hash=raw_hash,
                reused=True,
            )
        directory.mkdir(parents=True, exist_ok=False)
        _atomic_write(raw_path, raw_content)
        _atomic_write(manifest_path, manifest_content)
        return SnapshotPersistResult(
            snapshot_hash=snapshot.snapshot_hash,
            directory=str(directory),
            manifest_hash=manifest_hash,
            raw_content_hash=raw_hash,
            reused=False,
        )

    def record_failure(
        self,
        *,
        request: AShareDataRequest,
        failure: AShareFailure,
        fetched_at: datetime,
    ) -> Path:
        payload = {
            "schema_version": "ashare-fetch-failure/v1",
            "request": request,
            "request_hash": request.artifact_hash,
            "fetched_at": fetched_at,
            "failure": failure,
        }
        failure_hash = canonical_content_hash(payload)
        directory = (
            self.root
            / request.dataset_id
            / "failures"
            / _hash_segment(request.artifact_hash)
            / _hash_segment(failure_hash)
        )
        path = directory / "failure.json"
        content = _json_bytes({**payload, "failure_hash": failure_hash})
        if path.exists():
            if path.read_bytes() != content:
                fail(FailureCode.ARTIFACT_COLLISION, "failure audit path owns different content")
            return path
        directory.mkdir(parents=True, exist_ok=False)
        _atomic_write(path, content)
        return path

