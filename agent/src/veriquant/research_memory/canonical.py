from __future__ import annotations

import dataclasses
import hashlib
import json
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any


HASH_PREFIX = "sha256:"


def parse_datetime(value: str | datetime, *, field_name: str) -> datetime:
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            raise ValueError(f"{field_name} is required")
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(text)
        except ValueError as exc:
            raise ValueError(f"{field_name} must be ISO-8601 datetime") from exc
    else:
        raise ValueError(f"{field_name} must be datetime or ISO-8601 string")
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError(f"{field_name} must include timezone")
    return dt.astimezone(timezone.utc)


def isoformat_utc(value: str | datetime, *, field_name: str) -> str:
    return parse_datetime(value, field_name=field_name).isoformat().replace("+00:00", "Z")


def normalize_for_json(value: Any) -> Any:
    if dataclasses.is_dataclass(value):
        return normalize_for_json(dataclasses.asdict(value))
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(k): normalize_for_json(value[k]) for k in sorted(value)}
    if isinstance(value, (list, tuple)):
        return [normalize_for_json(item) for item in value]
    return value


def canonical_json(value: Any) -> str:
    return json.dumps(
        normalize_for_json(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def artifact_hash(value: Any) -> str:
    return HASH_PREFIX + hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def content_hash(text: str) -> str:
    return HASH_PREFIX + hashlib.sha256(text.encode("utf-8")).hexdigest()


def bytes_hash(data: bytes) -> str:
    return HASH_PREFIX + hashlib.sha256(data).hexdigest()
