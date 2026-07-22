"""Immutable content-addressed store for Tushare market-state captures."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from ..contracts.base import canonicalize
from ..data.bitemporal import canonical_content_hash
from .models import (
    FrozenMarketStateBundle,
    FrozenMarketStateHandle,
    FrozenMarketStateManifest,
    MarketStateCapture,
    MarketStateError,
    MarketStateFailureCode,
    NormalizedMarketStateRow,
    market_state_fail,
    require_dataset_id,
)
from .service import MarketStateSdk, acquire_and_normalize
from .models import MarketStateRequest


_HASH_SEGMENT = re.compile(r"^[0-9a-f]{64}$")


def _json_bytes(value: Any) -> bytes:
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


def _atomic_create_or_verify(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        try:
            current = path.read_bytes()
        except OSError as exc:
            raise MarketStateError(
                MarketStateFailureCode.SNAPSHOT_CORRUPT,
                "existing frozen artifact is unreadable",
                details={"error_type": type(exc).__name__},
            ) from exc
        if current != content:
            market_state_fail(
                MarketStateFailureCode.HASH_MISMATCH,
                "content-addressed artifact differs from existing bytes",
            )
        return
    temporary = path.with_name(path.name + ".tmp")
    try:
        with temporary.open("xb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _directory(root: Path, dataset_id: str, vintage_hash: str) -> Path:
    require_dataset_id(dataset_id)
    segment = vintage_hash.removeprefix("sha256:")
    if _HASH_SEGMENT.fullmatch(segment) is None:
        market_state_fail(MarketStateFailureCode.PATH_TRAVERSAL, "vintage hash is not a safe path segment")
    resolved_root = root.resolve()
    candidate = (resolved_root / dataset_id / segment).resolve()
    try:
        candidate.relative_to(resolved_root)
    except ValueError as exc:
        raise MarketStateError(
            MarketStateFailureCode.PATH_TRAVERSAL,
            "market-state artifact path escapes its root",
        ) from exc
    return candidate


class FrozenMarketStateStore:
    """Write-once store; revisions always create a distinct vintage directory."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()

    def freeze(self, request: MarketStateRequest, *, sdk: MarketStateSdk) -> FrozenMarketStateBundle:
        capture, rows, manifest = acquire_and_normalize(request, sdk=sdk)
        directory = _directory(self.root, request.dataset_id, manifest.vintage_hash)
        capture_bytes = _json_bytes({"capture": capture.to_dict()})
        records_bytes = _json_bytes({"records": [row.to_dict() for row in rows]})
        if canonical_content_hash(rows) != manifest.records_hash:
            market_state_fail(MarketStateFailureCode.HASH_MISMATCH, "normalized records hash changed")
        envelope = {
            "schema_version": "veriquant.tushare-market-state-envelope/v1",
            "manifest": manifest.to_dict(),
            "manifest_hash": manifest.artifact_hash,
            "capture_file_hash": "sha256:" + __import__("hashlib").sha256(capture_bytes).hexdigest(),
            "records_file_hash": "sha256:" + __import__("hashlib").sha256(records_bytes).hexdigest(),
        }
        manifest_bytes = _json_bytes(envelope)
        _atomic_create_or_verify(directory / "capture.json", capture_bytes)
        _atomic_create_or_verify(directory / "records.json", records_bytes)
        _atomic_create_or_verify(directory / "manifest.json", manifest_bytes)
        handle = FrozenMarketStateHandle(
            dataset_id=request.dataset_id,
            vintage_hash=manifest.vintage_hash,
            manifest_hash=manifest.artifact_hash,
            provider_capture_hash=manifest.provider_capture_hash,
            records_hash=manifest.records_hash,
            manifest_path=str(directory / "manifest.json"),
        )
        return FrozenMarketStateBundle(manifest=manifest, capture=capture, rows=rows, handle=handle)

    def load(self, *, dataset_id: str, vintage_hash: str) -> FrozenMarketStateBundle:
        directory = _directory(self.root, dataset_id, vintage_hash)
        try:
            manifest_bytes = (directory / "manifest.json").read_bytes()
            capture_bytes = (directory / "capture.json").read_bytes()
            records_bytes = (directory / "records.json").read_bytes()
            envelope = json.loads(manifest_bytes)
            capture_payload = json.loads(capture_bytes)
            records_payload = json.loads(records_bytes)
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise MarketStateError(
                MarketStateFailureCode.SNAPSHOT_CORRUPT,
                "market-state artifact is missing or unreadable",
                details={"error_type": type(exc).__name__},
            ) from exc
        if envelope.get("schema_version") != "veriquant.tushare-market-state-envelope/v1":
            market_state_fail(MarketStateFailureCode.SNAPSHOT_CORRUPT, "market-state envelope schema is unsupported")
        try:
            manifest = FrozenMarketStateManifest.from_payload(envelope["manifest"])
            capture = MarketStateCapture.from_payload(capture_payload["capture"])
            rows = tuple(NormalizedMarketStateRow.from_payload(row) for row in records_payload["records"])
        except Exception as exc:
            raise MarketStateError(
                MarketStateFailureCode.SNAPSHOT_CORRUPT,
                "market-state typed payload failed validation",
                details={"error_type": type(exc).__name__},
            ) from exc
        import hashlib

        if (
            envelope.get("manifest_hash") != manifest.artifact_hash
            or envelope.get("capture_file_hash") != "sha256:" + hashlib.sha256(capture_bytes).hexdigest()
            or envelope.get("records_file_hash") != "sha256:" + hashlib.sha256(records_bytes).hexdigest()
            or capture.artifact_hash != manifest.provider_capture_hash
            or canonical_content_hash(rows) != manifest.records_hash
            or manifest.vintage_hash != vintage_hash
        ):
            market_state_fail(MarketStateFailureCode.HASH_MISMATCH, "market-state artifact hash mismatch")
        handle = FrozenMarketStateHandle(
            dataset_id=dataset_id,
            vintage_hash=manifest.vintage_hash,
            manifest_hash=manifest.artifact_hash,
            provider_capture_hash=manifest.provider_capture_hash,
            records_hash=manifest.records_hash,
            manifest_path=str(directory / "manifest.json"),
        )
        return FrozenMarketStateBundle(manifest=manifest, capture=capture, rows=rows, handle=handle)
