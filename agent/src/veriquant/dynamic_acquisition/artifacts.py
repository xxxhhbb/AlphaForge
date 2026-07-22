"""Content-addressed, atomic raw-partition storage.

This store is intentionally inert: it persists allowlisted provider rows and
query identity, but performs no financial calculation and never accepts
credentials or endpoint configuration.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
from typing import Any, Mapping

from .models import QueryTask, canonical_rows_hash, stable_content_hash


class ContentAddressedRowStore:
    """Persist each completed partition under its deterministic task id."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def write(
        self,
        task: QueryTask,
        rows: tuple[Mapping[str, Any], ...],
    ) -> str:
        task_name = _safe_task_name(task.task_id)
        row_hash = canonical_rows_hash(rows)
        payload = {
            "schema_version": "veriquant.dynamic-acquisition-partition/v1",
            "task": {
                "task_id": task.task_id,
                "api_name": task.api_name,
                "fields": list(task.fields),
                "params": dict(task.params),
                "date_start": task.date_start,
                "date_end": task.date_end,
                "symbol_shard": list(task.symbol_shard),
            },
            "row_count": len(rows),
            "rows_hash": row_hash,
            "rows": [dict(row) for row in rows],
        }
        payload["artifact_hash"] = stable_content_hash(payload)
        _atomic_json(self.root / f"{task_name}.json", payload)
        return row_hash

    def load(self, task_id: str) -> tuple[dict[str, Any], ...]:
        path = self.root / f"{_safe_task_name(task_id)}.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        claimed_artifact = payload.pop("artifact_hash", None)
        if claimed_artifact != stable_content_hash(payload):
            raise ValueError("partition artifact hash mismatch")
        rows = payload.get("rows")
        if not isinstance(rows, list) or any(
            not isinstance(row, Mapping) for row in rows
        ):
            raise ValueError("partition rows must be objects")
        normalized = tuple(dict(row) for row in rows)
        if payload.get("rows_hash") != canonical_rows_hash(normalized):
            raise ValueError("partition rows hash mismatch")
        return normalized

    def load_all(self) -> tuple[dict[str, Any], ...]:
        rows: list[dict[str, Any]] = []
        for path in sorted(self.root.glob("sha256_*.json")):
            payload = json.loads(path.read_text(encoding="utf-8"))
            task_id = str(payload.get("task", {}).get("task_id") or "")
            rows.extend(self.load(task_id))
        return tuple(rows)


def _safe_task_name(task_id: str) -> str:
    if (
        not isinstance(task_id, str)
        or not task_id.startswith("sha256:")
        or len(task_id) != 71
        or any(char not in "0123456789abcdef" for char in task_id[7:])
    ):
        raise ValueError("invalid content-addressed task id")
    return task_id.replace(":", "_", 1)


def _atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    handle = tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        newline="\n",
        delete=False,
        dir=path.parent,
    )
    temporary = Path(handle.name)
    try:
        json.dump(
            payload,
            handle,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
        handle.close()
        os.replace(temporary, path)
    finally:
        if not handle.closed:
            handle.close()
        if temporary.exists():
            temporary.unlink()


__all__ = ["ContentAddressedRowStore"]
