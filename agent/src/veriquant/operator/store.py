"""Hash-chained event storage and atomic job materialization."""

from __future__ import annotations

import json
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .models import (
    EventType,
    JobManifest,
    JobState,
    JobStatus,
    OperatorError,
    OperatorEvent,
    OperatorFailureCode,
    SCHEMA_VERSION,
    ZERO_HASH,
    canonical_hash,
)


SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


def _atomic_write(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    content = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")
    with temporary.open("wb") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


class OperatorStore:
    def __init__(self, root: str | Path, *, clock: Callable[[], datetime] | None = None) -> None:
        self.root = Path(root).resolve()
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def _run_dir(self, job_id: str) -> Path:
        self._require_id(job_id)
        return self.root / "runs" / job_id

    def write_manifest(self, manifest: JobManifest) -> None:
        path = self._run_dir(manifest.job_id) / "manifest.json"
        if path.exists():
            current = self.read_manifest(manifest.job_id)
            if current.to_dict() != manifest.to_dict():
                raise OperatorError(OperatorFailureCode.IDEMPOTENCY_CONFLICT, "job manifest conflict")
            return
        body = manifest.to_dict()
        _atomic_write(path, {**body, "manifest_hash": canonical_hash(body)})

    def read_manifest(self, job_id: str) -> JobManifest:
        raw = self._read_envelope(self._run_dir(job_id) / "manifest.json", "manifest_hash")
        try:
            return JobManifest(
                job_id=raw["job_id"],
                profile_id=raw["profile_id"],
                provider_id=raw["provider_id"],
                seed=raw["seed"],
                budget=raw["budget"],
                idempotency_key_hash=raw["idempotency_key_hash"],
                plan_hash=raw["plan_hash"],
                stage_ids=tuple(raw["stage_ids"]),
                request_hash=raw["request_hash"],
            )
        except Exception as exc:
            raise OperatorError(OperatorFailureCode.CORRUPT_STATE, "invalid job manifest") from exc

    def write_state(self, state: JobState) -> None:
        body = state.to_dict()
        _atomic_write(self._run_dir(state.job_id) / "state.json", {**body, "state_hash": canonical_hash(body)})

    def read_state(self, job_id: str) -> JobState:
        path = self._run_dir(job_id) / "state.json"
        if not path.exists():
            raise OperatorError(OperatorFailureCode.NOT_FOUND, "job not found")
        raw = self._read_envelope(path, "state_hash")
        try:
            return JobState(
                job_id=raw["job_id"],
                status=JobStatus(raw["status"]),
                completed_stages=tuple(raw["completed_stages"]),
                artifact_hashes=dict(raw["artifact_hashes"]),
                last_event_sequence=raw["last_event_sequence"],
                last_event_hash=raw["last_event_hash"],
                failure_code=OperatorFailureCode(raw["failure_code"]) if raw.get("failure_code") else None,
            )
        except Exception as exc:
            raise OperatorError(OperatorFailureCode.CORRUPT_STATE, "invalid job state") from exc

    def append_event(
        self,
        *,
        job_id: str,
        event_type: EventType,
        status: JobStatus,
        stage_id: str | None = None,
        artifact_hash: str | None = None,
        failure_code: OperatorFailureCode | None = None,
    ) -> OperatorEvent:
        events = self.read_events(job_id)
        event = OperatorEvent(
            job_id=job_id,
            sequence=len(events) + 1,
            event_type=event_type,
            status=status,
            occurred_at=self.clock().astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
            previous_event_hash=events[-1].event_hash if events else ZERO_HASH,
            stage_id=stage_id,
            artifact_hash=artifact_hash,
            failure_code=failure_code,
        )
        path = self._run_dir(job_id) / "events.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("ab") as handle:
            handle.write(json.dumps(event.to_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8") + b"\n")
            handle.flush()
            os.fsync(handle.fileno())
        return event

    def read_events(self, job_id: str, *, after: int = 0, limit: int = 200) -> tuple[OperatorEvent, ...]:
        self._require_id(job_id)
        path = self._run_dir(job_id) / "events.jsonl"
        if not path.exists():
            return ()
        previous = ZERO_HASH
        parsed: list[OperatorEvent] = []
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
            for sequence, line in enumerate(lines, 1):
                raw = json.loads(line)
                if raw.get("schema_version") != SCHEMA_VERSION:
                    raise ValueError("schema")
                event = OperatorEvent(
                    job_id=raw["job_id"],
                    sequence=raw["sequence"],
                    event_type=EventType(raw["event_type"]),
                    status=JobStatus(raw["status"]),
                    occurred_at=raw["occurred_at"],
                    previous_event_hash=raw["previous_event_hash"],
                    stage_id=raw.get("stage_id"),
                    artifact_hash=raw.get("artifact_hash"),
                    failure_code=OperatorFailureCode(raw["failure_code"]) if raw.get("failure_code") else None,
                )
                if event.job_id != job_id or event.sequence != sequence or event.previous_event_hash != previous:
                    raise ValueError("chain")
                previous = event.event_hash
                parsed.append(event)
        except Exception as exc:
            raise OperatorError(OperatorFailureCode.CORRUPT_STATE, "operator event chain is corrupt") from exc
        return tuple(event for event in parsed if event.sequence > after)[: max(0, min(limit, 200))]

    def read_idempotency(self, key_hash: str) -> dict[str, object] | None:
        path = self.root / "idempotency" / (key_hash.removeprefix("sha256:") + ".json")
        if not path.exists():
            return None
        return self._read_envelope(path, "record_hash")

    def write_idempotency(self, key_hash: str, *, job_id: str, request_hash: str) -> None:
        body: dict[str, object] = {"schema_version": SCHEMA_VERSION, "job_id": job_id, "request_hash": request_hash}
        _atomic_write(
            self.root / "idempotency" / (key_hash.removeprefix("sha256:") + ".json"),
            {**body, "record_hash": canonical_hash(body)},
        )

    @staticmethod
    def _require_id(value: str) -> None:
        if not isinstance(value, str) or not SAFE_ID.fullmatch(value) or ".." in value:
            raise OperatorError(OperatorFailureCode.INVALID_REQUEST, "invalid identifier")

    @staticmethod
    def _read_envelope(path: Path, hash_field: str) -> dict[str, object]:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            expected = raw.pop(hash_field)
            if expected != canonical_hash(raw):
                raise ValueError("hash")
            return raw
        except FileNotFoundError as exc:
            raise OperatorError(OperatorFailureCode.NOT_FOUND, "operator artifact not found") from exc
        except Exception as exc:
            raise OperatorError(OperatorFailureCode.CORRUPT_STATE, "operator artifact is corrupt") from exc
