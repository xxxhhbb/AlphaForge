"""Atomic, content-addressed checkpoint persistence."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Mapping

from .models import PartitionResult, PartitionStatus, stable_content_hash


class CheckpointCorruptionError(ValueError):
    """A checkpoint exists but cannot be trusted for resume."""


class CheckpointStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def load(self, plan_hash: str) -> dict[str, PartitionResult]:
        if not self.path.exists():
            return {}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError) as exc:
            raise CheckpointCorruptionError("checkpoint is unreadable") from exc
        claimed_hash = payload.pop("artifact_hash", None)
        if claimed_hash != stable_content_hash(payload):
            raise CheckpointCorruptionError("checkpoint hash mismatch")
        if payload.get("plan_hash") != plan_hash:
            raise CheckpointCorruptionError("checkpoint belongs to another plan")
        output = {}
        for item in payload.get("partitions", []):
            try:
                result = PartitionResult(
                    task_id=str(item["task_id"]),
                    status=PartitionStatus(str(item["status"])),
                    row_count=int(item["row_count"]),
                    content_hash=item.get("content_hash"),
                    failure_type=item.get("failure_type"),
                    content_bytes=int(item.get("content_bytes", 0)),
                    api_calls_used=int(item.get("api_calls_used", 0)),
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise CheckpointCorruptionError(
                    "checkpoint contains an invalid partition"
                ) from exc
            if result.task_id in output:
                raise CheckpointCorruptionError(
                    "checkpoint contains duplicate partitions"
                )
            output[result.task_id] = result
        return output

    def save(
        self,
        plan_hash: str,
        partitions: Mapping[str, PartitionResult],
    ) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": "veriquant.dynamic-acquisition-checkpoint/v1",
            "plan_hash": plan_hash,
            "partitions": [
                {
                    "task_id": item.task_id,
                    "status": item.status.value,
                    "row_count": item.row_count,
                    "content_hash": item.content_hash,
                    "failure_type": item.failure_type,
                    "content_bytes": item.content_bytes,
                    "api_calls_used": item.api_calls_used,
                }
                for item in sorted(partitions.values(), key=lambda value: value.task_id)
            ],
        }
        payload["artifact_hash"] = stable_content_hash(payload)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            encoding="utf-8",
        )
        temporary.replace(self.path)


__all__ = ["CheckpointCorruptionError", "CheckpointStore"]
