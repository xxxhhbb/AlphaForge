"""Atomic manifests and an append-only lifecycle event log for research jobs."""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Any, Mapping, TypeVar

from veriquant.contracts import FailureCode
from veriquant.contracts.base import canonicalize, fail
from veriquant.data import canonical_content_hash

from .models import (
    JobEventType,
    ResearchJobDraft,
    ResearchJobEvent,
    ResearchJobManifest,
    ResearchReportReference,
    ZERO_HASH,
    require_safe_segment,
)


T = TypeVar("T")


class ResearchJobStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()

    def run_dir(self, run_id: str) -> Path:
        require_safe_segment(run_id, "run_id")
        target = (self.root / "jobs" / run_id).resolve()
        if self.root not in target.parents:
            fail(FailureCode.UNAUTHORIZED_CAPABILITY, "job path escapes configured root")
        return target

    def save_draft(self, draft: ResearchJobDraft) -> ResearchJobDraft:
        path = self.run_dir(draft.run_id) / "draft.json"
        with self._lock:
            if path.exists():
                existing = self._read_contract(path, ResearchJobDraft)
                if existing.artifact_hash != draft.artifact_hash:
                    fail(FailureCode.ARTIFACT_COLLISION, "run_id already has a different draft")
                return existing
            self._write_new(path, draft.canonical_bytes() + b"\n")
            self.append_event(draft.run_id, JobEventType.DRAFT_CREATED, {"draft_hash": draft.artifact_hash})
            return draft

    def load_draft(self, run_id: str) -> ResearchJobDraft:
        return self._read_contract(self.run_dir(run_id) / "draft.json", ResearchJobDraft)

    def save_manifest(self, manifest: ResearchJobManifest) -> ResearchJobManifest:
        path = self.run_dir(manifest.run_id) / "manifest.json"
        with self._lock:
            if path.exists():
                existing = self._read_contract(path, ResearchJobManifest)
                if existing.artifact_hash != manifest.artifact_hash:
                    fail(FailureCode.ARTIFACT_COLLISION, "run_id already has a different confirmed manifest")
                return existing
            self._write_new(path, manifest.canonical_bytes() + b"\n")
            self.append_event(
                manifest.run_id,
                JobEventType.JOB_CONFIRMED,
                {"manifest_hash": manifest.artifact_hash, "authority": manifest.confirmation_authority.value},
            )
            return manifest

    def load_manifest(self, run_id: str) -> ResearchJobManifest:
        return self._read_contract(self.run_dir(run_id) / "manifest.json", ResearchJobManifest)

    def try_load_manifest(self, run_id: str) -> ResearchJobManifest | None:
        path = self.run_dir(run_id) / "manifest.json"
        return self._read_contract(path, ResearchJobManifest) if path.exists() else None

    def save_report_reference(self, reference: ResearchReportReference) -> ResearchReportReference:
        path = self.run_dir(reference.run_id) / "report_reference.json"
        with self._lock:
            if path.exists():
                existing = self._read_contract(path, ResearchReportReference)
                if existing.artifact_hash != reference.artifact_hash:
                    fail(FailureCode.ARTIFACT_COLLISION, "report reference cannot be overwritten")
                return existing
            self._write_new(path, reference.canonical_bytes() + b"\n")
            self.append_event(reference.run_id, JobEventType.REPORT_ATTACHED, {"reference_hash": reference.artifact_hash})
            return reference

    def try_load_report_reference(self, run_id: str) -> ResearchReportReference | None:
        path = self.run_dir(run_id) / "report_reference.json"
        return self._read_contract(path, ResearchReportReference) if path.exists() else None

    def append_event(
        self,
        run_id: str,
        event_type: JobEventType,
        payload: Mapping[str, Any],
        *,
        occurred_at: datetime | None = None,
    ) -> ResearchJobEvent:
        with self._lock:
            events = self.read_events(run_id)
            event = ResearchJobEvent(
                run_id=run_id,
                sequence=len(events) + 1,
                event_type=event_type,
                occurred_at=occurred_at or datetime.now(timezone.utc),
                previous_event_hash=events[-1].artifact_hash if events else ZERO_HASH,
                payload_hash=canonical_content_hash(payload),
                payload=payload,
            )
            path = self.run_dir(run_id) / "events.jsonl"
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("ab") as handle:
                handle.write(event.canonical_bytes() + b"\n")
                handle.flush()
                os.fsync(handle.fileno())
            return event

    def read_events(self, run_id: str) -> tuple[ResearchJobEvent, ...]:
        path = self.run_dir(run_id) / "events.jsonl"
        if not path.exists():
            return ()
        events: list[ResearchJobEvent] = []
        previous = ZERO_HASH
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except Exception as exc:
            fail(FailureCode.LEDGER_CORRUPTION, "job event log is unreadable", error=type(exc).__name__)
        for sequence, line in enumerate(lines, start=1):
            if not line.strip():
                fail(FailureCode.LEDGER_CORRUPTION, "job event log contains a blank line")
            try:
                event = ResearchJobEvent.from_payload(json.loads(line))
            except Exception as exc:
                fail(FailureCode.LEDGER_CORRUPTION, "job event log is corrupt", line=sequence, error=type(exc).__name__)
            if event.sequence != sequence or event.previous_event_hash != previous:
                fail(FailureCode.LEDGER_CORRUPTION, "job event hash chain is corrupt", line=sequence)
            previous = event.artifact_hash
            events.append(event)
        return tuple(events)

    @staticmethod
    def _read_contract(path: Path, contract_type: type[T]) -> T:
        if not path.exists():
            fail(FailureCode.MISSING_REQUIRED_FIELD, f"job artifact does not exist: {path.name}")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            return contract_type.from_payload(payload)  # type: ignore[attr-defined, no-any-return]
        except Exception as exc:
            fail(FailureCode.LEDGER_CORRUPTION, f"job artifact is corrupt: {path.name}", error=type(exc).__name__)

    @staticmethod
    def _write_new(path: Path, content: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = tempfile.NamedTemporaryFile("wb", delete=False, dir=str(path.parent))
        tmp = Path(handle.name)
        try:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
            handle.close()
            if path.exists():
                fail(FailureCode.ARTIFACT_COLLISION, f"artifact already exists: {path.name}")
            os.replace(tmp, path)
        finally:
            if not handle.closed:
                handle.close()
            if tmp.exists():
                tmp.unlink()


__all__ = ["ResearchJobStore"]
