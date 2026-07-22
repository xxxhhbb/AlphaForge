"""Atomic, content-addressed storage for the quality/delivery Harness."""

from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import tempfile
from typing import Any, Iterable, Mapping


def normalize_json(value: Any) -> Any:
    """Return a deterministic JSON value without inventing missing numbers."""

    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, Mapping):
        return {str(key): normalize_json(item) for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))}
    if isinstance(value, (list, tuple)):
        return [normalize_json(item) for item in value]
    return value


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        normalize_json(value), ensure_ascii=False, allow_nan=False,
        sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")


def canonical_hash(value: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical_bytes(value)).hexdigest()


def content_hash(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return "sha256:" + digest.hexdigest()


def with_artifact_hash(value: Mapping[str, Any]) -> dict[str, Any]:
    result = normalize_json(dict(value))
    result.pop("artifact_hash", None)
    result["artifact_hash"] = canonical_hash(result)
    return result


def atomic_json(path: str | Path, value: Mapping[str, Any]) -> dict[str, Any]:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    result = with_artifact_hash(value)
    encoded = json.dumps(
        result, ensure_ascii=False, allow_nan=False, indent=2,
        sort_keys=True,
    ).encode("utf-8") + b"\n"
    descriptor, temporary = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return result


def atomic_text(path: str | Path, value: str) -> str:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def atomic_jsonl(path: str | Path, rows: Iterable[Mapping[str, Any]]) -> str:
    payload = "".join(
        json.dumps(normalize_json(row), ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")) + "\n"
        for row in rows
    )
    return atomic_text(path, payload)


def load_hashed(path: str | Path) -> dict[str, Any]:
    source = Path(path)
    value = json.loads(source.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON object required: {source}")
    claimed = value.pop("artifact_hash", None)
    actual = canonical_hash(value)
    if claimed != actual:
        raise ValueError(f"artifact hash mismatch: {source}")
    value["artifact_hash"] = claimed
    return value


__all__ = [
    "atomic_json", "atomic_jsonl", "atomic_text", "canonical_bytes",
    "canonical_hash", "content_hash", "load_hashed", "normalize_json",
    "with_artifact_hash",
]
