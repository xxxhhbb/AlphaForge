"""Integration-owned publication into Agent F's read-only Workbench repository."""

from __future__ import annotations

import json
import os
import re
import tempfile
from hashlib import sha256
from pathlib import Path
from typing import Any, Mapping

from src.api.veriquant_workbench_routes import (
    ARTIFACT_NAMES,
    RUN_STATUS_VALUES,
    WorkbenchArtifactRepository,
    redact_for_workbench,
)


_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


class WorkbenchPublicationError(ValueError):
    """Fail-closed Workbench publication error."""


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _hash(value: Any) -> str:
    return "sha256:" + sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = _canonical_json(value) + "\n"
    handle = tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        newline="\n",
        delete=False,
        dir=str(path.parent),
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    tmp_name = handle.name
    try:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
        handle.close()
        os.replace(tmp_name, path)
    finally:
        if not handle.closed:
            handle.close()
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)


def _read_index(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"schema_version": "veriquant-workbench-index/v1", "runs": []}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise WorkbenchPublicationError("existing Workbench index is corrupt") from exc
    if not isinstance(value, dict) or not isinstance(value.get("runs"), list):
        raise WorkbenchPublicationError("existing Workbench index has an invalid schema")
    return value


def publish_workbench_run(
    *,
    root: str | Path,
    run_id: str,
    status: str,
    updated_at: str,
    artifacts: Mapping[str, Any],
) -> dict[str, Any]:
    """Atomically publish a sealed-redacted A-E artifact projection.

    Only Agent F's fixed artifact names are accepted. Re-publishing the same
    ``run_id`` replaces its derived read model, while upstream ledgers remain
    append-only and outside this repository.
    """

    if not _SAFE_ID.fullmatch(run_id) or ".." in run_id:
        raise WorkbenchPublicationError("invalid run_id")
    normalized_status = status.upper()
    if normalized_status not in RUN_STATUS_VALUES:
        raise WorkbenchPublicationError("invalid Workbench status")
    unknown = set(artifacts) - set(ARTIFACT_NAMES)
    if unknown:
        raise WorkbenchPublicationError(f"unknown Workbench artifacts: {sorted(unknown)}")

    repository_root = Path(root).resolve()
    run_root = repository_root / "runs" / run_id
    hashes: dict[str, str] = {}
    for name, filename in sorted(ARTIFACT_NAMES.items()):
        raw = artifacts.get(name, {})
        projected = redact_for_workbench(raw)
        target = run_root / filename
        _atomic_write_json(target, projected)
        hashes[name] = _hash(projected)

    index_path = repository_root / "index.json"
    index = _read_index(index_path)
    rows = [row for row in index["runs"] if isinstance(row, dict) and row.get("run_id") != run_id]
    rows.append(
        {
            "run_id": run_id,
            "status": normalized_status,
            "updated_at": updated_at,
            "overview_hash": hashes["overview"],
        }
    )
    rows.sort(key=lambda row: (str(row.get("updated_at", "")), str(row.get("run_id", ""))), reverse=True)
    final_index = {"schema_version": "veriquant-workbench-index/v1", "runs": rows}
    _atomic_write_json(index_path, final_index)

    repository = WorkbenchArtifactRepository(repository_root)
    detail = repository.get_run_detail(run_id)
    return {
        "run_id": run_id,
        "status": normalized_status,
        "artifact_hashes": hashes,
        "index_hash": _hash(final_index),
        "detail_hash": _hash(detail),
        "artifact_count": len(hashes),
    }


__all__ = ["WorkbenchPublicationError", "publish_workbench_run"]
