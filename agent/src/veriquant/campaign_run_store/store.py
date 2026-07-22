"""Append-only operational store for Campaign automation runs."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from threading import RLock
from types import MappingProxyType
from typing import Any, ClassVar, Mapping

from ..contracts import CanonicalContract, ContractValidationError, FailureCode
from ..contracts.base import canonicalize, fail, require_aware_datetime, require_hash, require_identifier, require_text
from ..research_campaign.runtime import CampaignCommittedStage, CampaignRuntimeManifest, CampaignRuntimeRecoveryState


CAMPAIGN_RUN_STORE_SCHEMA_VERSION = "alphaforge.campaign-run-store/v1"
GENESIS_EVENT_HASH = "sha256:" + "0" * 64
DEFAULT_EVENT_TIME = datetime(2026, 7, 19, 12, 0, tzinfo=timezone.utc)


class CampaignRunStoreStatus(str, Enum):
    CREATED = "CREATED"
    RUNNING = "RUNNING"
    AWAITING_CHECKPOINT = "AWAITING_CHECKPOINT"
    COMPLETED = "COMPLETED"
    PARTIAL = "PARTIAL"
    CANCELLED = "CANCELLED"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"


class CampaignRunEventType(str, Enum):
    RUN_CREATED = "RUN_CREATED"
    DATA_BOUND = "DATA_BOUND"
    STAGE_COMMITTED = "STAGE_COMMITTED"
    RUNTIME_MANIFEST_COMMITTED = "RUNTIME_MANIFEST_COMMITTED"
    CHECKPOINT_REACHED = "CHECKPOINT_REACHED"
    CANCEL_REQUESTED = "CANCEL_REQUESTED"
    REPORT_PUBLISHED = "REPORT_PUBLISHED"
    RUN_BLOCKED = "RUN_BLOCKED"


@dataclass(frozen=True, kw_only=True)
class CampaignRunStoreFailure(CanonicalContract):
    SCHEMA_VERSION: ClassVar[str] = CAMPAIGN_RUN_STORE_SCHEMA_VERSION

    failure_code: FailureCode
    message: str

    def __post_init__(self) -> None:
        if not isinstance(self.failure_code, FailureCode):
            object.__setattr__(self, "failure_code", FailureCode(self.failure_code))
        require_text(self.message, "message")


@dataclass(frozen=True, kw_only=True)
class CampaignCheckpoint(CanonicalContract):
    SCHEMA_VERSION: ClassVar[str] = CAMPAIGN_RUN_STORE_SCHEMA_VERSION

    run_id: str
    committed_stage_keys: tuple[str, ...] = ()
    runtime_manifest_hash: str | None = None
    cancelled: bool = False

    def __post_init__(self) -> None:
        require_identifier(self.run_id, "run_id")
        for key in self.committed_stage_keys:
            require_identifier(key, "committed_stage_key")
        if len(set(self.committed_stage_keys)) != len(self.committed_stage_keys):
            fail(FailureCode.INVALID_STATE, "duplicate committed stage keys")
        if self.runtime_manifest_hash is not None:
            require_hash(self.runtime_manifest_hash, "runtime_manifest_hash")
        if not isinstance(self.cancelled, bool):
            fail(FailureCode.INVALID_VALUE, "cancelled must be boolean")

    def to_recovery_state(self, stages: Mapping[str, CampaignCommittedStage]) -> CampaignRuntimeRecoveryState:
        committed = []
        for key in self.committed_stage_keys:
            stage = stages.get(key)
            if stage is None:
                fail(FailureCode.INVALID_STATE, "checkpoint references missing stage", stage_key=key)
            committed.append(stage)
        return CampaignRuntimeRecoveryState(committed_stages=tuple(committed))


@dataclass(frozen=True, kw_only=True)
class CampaignRunRecord(CanonicalContract):
    SCHEMA_VERSION: ClassVar[str] = CAMPAIGN_RUN_STORE_SCHEMA_VERSION

    run_id: str
    request_hash: str
    campaign_hash: str
    route_hash: str
    protocol_plan_hash: str
    status: CampaignRunStoreStatus
    committed_stage_keys: tuple[str, ...] = ()
    data_binding_hash: str | None = None
    runtime_manifest_hash: str | None = None
    report_hash: str | None = None
    event_log_hash: str = GENESIS_EVENT_HASH
    cancelled: bool = False
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if isinstance(self.run_id, str) and any(token in self.run_id for token in ("..", "/", "\\", ":")):
            fail(FailureCode.UNAUTHORIZED_CAPABILITY, "run_id must be a single safe path segment")
        require_identifier(self.run_id, "run_id")
        for name in ("request_hash", "campaign_hash", "route_hash", "protocol_plan_hash", "event_log_hash"):
            require_hash(getattr(self, name), name)
        if not isinstance(self.status, CampaignRunStoreStatus):
            object.__setattr__(self, "status", CampaignRunStoreStatus(self.status))
        for key in self.committed_stage_keys:
            require_identifier(key, "committed_stage_key")
        if len(set(self.committed_stage_keys)) != len(self.committed_stage_keys):
            fail(FailureCode.INVALID_STATE, "duplicate committed stage keys")
        for name in ("data_binding_hash", "runtime_manifest_hash", "report_hash"):
            value = getattr(self, name)
            if value is not None:
                require_hash(value, name)
        if not isinstance(self.cancelled, bool):
            fail(FailureCode.INVALID_VALUE, "cancelled must be boolean")
        object.__setattr__(self, "metadata", MappingProxyType(dict(sorted(self.metadata.items()))))

    def checkpoint(self) -> CampaignCheckpoint:
        return CampaignCheckpoint(
            run_id=self.run_id,
            committed_stage_keys=self.committed_stage_keys,
            runtime_manifest_hash=self.runtime_manifest_hash,
            cancelled=self.cancelled,
        )


@dataclass(frozen=True, kw_only=True)
class CampaignRunEvent(CanonicalContract):
    SCHEMA_VERSION: ClassVar[str] = CAMPAIGN_RUN_STORE_SCHEMA_VERSION

    run_id: str
    sequence: int
    event_type: CampaignRunEventType
    event_time: datetime
    previous_event_hash: str
    payload_hash: str
    payload: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        require_identifier(self.run_id, "run_id")
        if isinstance(self.sequence, bool) or not isinstance(self.sequence, int) or self.sequence < 1:
            fail(FailureCode.INVALID_VALUE, "sequence must be a positive integer")
        if not isinstance(self.event_type, CampaignRunEventType):
            object.__setattr__(self, "event_type", CampaignRunEventType(self.event_type))
        require_aware_datetime(self.event_time, "event_time")
        require_hash(self.previous_event_hash, "previous_event_hash")
        require_hash(self.payload_hash, "payload_hash")
        rendered_hash = _payload_hash(self.payload)
        if rendered_hash != self.payload_hash:
            fail(FailureCode.LEDGER_CORRUPTION, "event payload hash mismatch")
        object.__setattr__(self, "payload", MappingProxyType(dict(sorted(self.payload.items()))))


class InMemoryCampaignRunStore:
    """Deterministic in-memory store used by focused tests."""

    def __init__(self) -> None:
        self.records: dict[str, CampaignRunRecord] = {}
        self.events: dict[str, list[CampaignRunEvent]] = {}
        self.stages: dict[str, dict[str, CampaignCommittedStage]] = {}

    def create_run(self, record: CampaignRunRecord) -> CampaignRunRecord:
        existing = self.records.get(record.run_id)
        if existing is not None:
            if not _same_run_identity(existing, record):
                fail(FailureCode.ARTIFACT_COLLISION, "run_id already exists with different identity")
            return existing
        self.records[record.run_id] = record
        self.events[record.run_id] = []
        self.stages[record.run_id] = {}
        self.append_event(
            record.run_id,
            CampaignRunEventType.RUN_CREATED,
            {"record_hash": record.artifact_hash},
        )
        return self.records[record.run_id]

    def append_event(
        self,
        run_id: str,
        event_type: CampaignRunEventType,
        payload: Mapping[str, Any],
        *,
        event_time: datetime = DEFAULT_EVENT_TIME,
    ) -> CampaignRunEvent:
        require_identifier(run_id, "run_id")
        previous = self.events.get(run_id, [])[-1].artifact_hash if self.events.get(run_id) else GENESIS_EVENT_HASH
        event = CampaignRunEvent(
            run_id=run_id,
            sequence=len(self.events.get(run_id, [])) + 1,
            event_type=event_type,
            event_time=event_time,
            previous_event_hash=previous,
            payload_hash=_payload_hash(payload),
            payload=payload,
        )
        self.events.setdefault(run_id, []).append(event)
        self._update_event_hash(run_id)
        return event

    def commit_stage(self, run_id: str, stage: CampaignCommittedStage) -> None:
        require_identifier(run_id, "run_id")
        existing = self.stages.setdefault(run_id, {}).get(stage.stage_key)
        if existing is not None and existing.artifact_hash != stage.artifact_hash:
            fail(FailureCode.ARTIFACT_COLLISION, "stage already committed with different result")
        self.stages[run_id][stage.stage_key] = stage
        record = self.records[run_id]
        keys = tuple(sorted({*record.committed_stage_keys, stage.stage_key}))
        self.records[run_id] = _replace_record(record, committed_stage_keys=keys, status=CampaignRunStoreStatus.RUNNING)
        self.append_event(run_id, CampaignRunEventType.STAGE_COMMITTED, {"stage_hash": stage.artifact_hash})

    def commit_runtime_manifest(self, run_id: str, manifest: CampaignRuntimeManifest) -> None:
        record = self.records[run_id]
        status = CampaignRunStoreStatus(manifest.status.value)
        self.records[run_id] = _replace_record(
            record,
            runtime_manifest_hash=manifest.artifact_hash,
            status=status,
            cancelled=manifest.cancelled,
        )
        self.append_event(
            run_id,
            CampaignRunEventType.RUNTIME_MANIFEST_COMMITTED,
            {"runtime_manifest_hash": manifest.artifact_hash},
        )

    def load_checkpoint(self, run_id: str) -> CampaignCheckpoint:
        return self.records[run_id].checkpoint()

    def replay(self, run_id: str) -> CampaignRunRecord:
        self._validate_event_chain(run_id)
        return self.records[run_id]

    def _update_event_hash(self, run_id: str) -> None:
        record = self.records.get(run_id)
        if record is None:
            return
        event_hash = self.events[run_id][-1].artifact_hash if self.events[run_id] else GENESIS_EVENT_HASH
        self.records[run_id] = _replace_record(record, event_log_hash=event_hash)

    def _validate_event_chain(self, run_id: str) -> None:
        previous = GENESIS_EVENT_HASH
        for expected, event in enumerate(self.events.get(run_id, ()), start=1):
            if event.sequence != expected or event.previous_event_hash != previous:
                fail(FailureCode.LEDGER_CORRUPTION, "campaign event chain is corrupt")
            previous = event.artifact_hash
        record = self.records.get(run_id)
        if record is not None and record.event_log_hash != previous:
            fail(FailureCode.LEDGER_CORRUPTION, "campaign event log hash does not match run materialization")


class FileCampaignRunStore(InMemoryCampaignRunStore):
    """File-backed store with atomic run materializations and append-only events."""

    def __init__(self, root: Path | str) -> None:
        super().__init__()
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()

    def create_run(self, record: CampaignRunRecord) -> CampaignRunRecord:
        with self._lock:
            _safe_segment(record.run_id, "run_id")
            run_dir = self._run_dir(record.run_id)
            run_dir.mkdir(parents=True, exist_ok=True)
            existing_path = run_dir / "run.json"
            if existing_path.exists():
                existing = CampaignRunRecord.from_payload(json.loads(existing_path.read_text(encoding="utf-8")))
                if not _same_run_identity(existing, record):
                    fail(FailureCode.ARTIFACT_COLLISION, "run_id already exists with different identity")
                self.records[record.run_id] = existing
                self._load_events(record.run_id)
                return existing
            self.records[record.run_id] = record
            self.events[record.run_id] = []
            self.stages[record.run_id] = {}
            self._atomic_write_json(existing_path, record.to_dict())
            self.append_event(
                record.run_id,
                CampaignRunEventType.RUN_CREATED,
                {"record_hash": record.artifact_hash},
            )
            return self.records[record.run_id]

    def append_event(
        self,
        run_id: str,
        event_type: CampaignRunEventType,
        payload: Mapping[str, Any],
        *,
        event_time: datetime = DEFAULT_EVENT_TIME,
    ) -> CampaignRunEvent:
        with self._lock:
            _safe_segment(run_id, "run_id")
            if run_id not in self.records:
                self._load_record(run_id)
            event = super().append_event(run_id, event_type, payload, event_time=event_time)
            events_path = self._run_dir(run_id) / "events.jsonl"
            with events_path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(event.canonical_json() + "\n")
            self._atomic_write_json(self._run_dir(run_id) / "run.json", self.records[run_id].to_dict())
            return event

    def commit_stage(self, run_id: str, stage: CampaignCommittedStage) -> None:
        with self._lock:
            _safe_segment(run_id, "run_id")
            if run_id not in self.records:
                self._load_record(run_id)
            super().commit_stage(run_id, stage)
            stage_path = self._run_dir(run_id) / "stages" / f"{stage.stage_key.replace(':', '__')}.json"
            self._atomic_write_json(stage_path, stage.to_dict())
            self._atomic_write_json(self._run_dir(run_id) / "run.json", self.records[run_id].to_dict())

    def commit_runtime_manifest(self, run_id: str, manifest: CampaignRuntimeManifest) -> None:
        with self._lock:
            if run_id not in self.records:
                self._load_record(run_id)
            super().commit_runtime_manifest(run_id, manifest)
            self._atomic_write_json(self._run_dir(run_id) / "runtime_manifest.json", manifest.to_dict())
            self._atomic_write_json(self._run_dir(run_id) / "run.json", self.records[run_id].to_dict())

    def load_checkpoint(self, run_id: str) -> CampaignCheckpoint:
        with self._lock:
            if run_id not in self.records:
                self._load_record(run_id)
            self._load_stages(run_id)
            return self.records[run_id].checkpoint()

    def replay(self, run_id: str) -> CampaignRunRecord:
        with self._lock:
            self._load_record(run_id)
            self._load_events(run_id)
            self._validate_event_chain(run_id)
            return self.records[run_id]

    def _run_dir(self, run_id: str) -> Path:
        _safe_segment(run_id, "run_id")
        candidate = (self.root / run_id).resolve()
        if self.root not in (candidate, *candidate.parents):
            fail(FailureCode.UNAUTHORIZED_CAPABILITY, "run path escapes store root")
        return candidate

    def _load_record(self, run_id: str) -> None:
        path = self._run_dir(run_id) / "run.json"
        if not path.exists():
            fail(FailureCode.INVALID_STATE, "run does not exist")
        self.records[run_id] = CampaignRunRecord.from_payload(json.loads(path.read_text(encoding="utf-8")))

    def _load_events(self, run_id: str) -> None:
        path = self._run_dir(run_id) / "events.jsonl"
        events: list[CampaignRunEvent] = []
        if path.exists():
            for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
                if not line.strip():
                    fail(FailureCode.LEDGER_CORRUPTION, "blank campaign event line", line=line_no)
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ContractValidationError(
                        FailureCode.LEDGER_CORRUPTION,
                        "campaign event log is not parseable JSONL",
                        details={"line": line_no},
                    ) from exc
                events.append(CampaignRunEvent.from_payload(payload))
        self.events[run_id] = events
        self._validate_event_chain(run_id)

    def _load_stages(self, run_id: str) -> None:
        stage_dir = self._run_dir(run_id) / "stages"
        stages: dict[str, CampaignCommittedStage] = {}
        if stage_dir.exists():
            for path in sorted(stage_dir.glob("*.json")):
                stage = CampaignCommittedStage.from_payload(json.loads(path.read_text(encoding="utf-8")))
                stages[stage.stage_key] = stage
        self.stages[run_id] = stages

    @staticmethod
    def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        rendered = json.dumps(canonicalize(payload), sort_keys=True, indent=2, ensure_ascii=False)
        tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        tmp.write_text(rendered + "\n", encoding="utf-8")
        os.replace(tmp, path)


def _replace_record(record: CampaignRunRecord, **changes: Any) -> CampaignRunRecord:
    payload = record.to_dict()
    payload.pop("contract_type")
    payload.pop("schema_version")
    payload.update(changes)
    return CampaignRunRecord(**payload)


def _same_run_identity(left: CampaignRunRecord, right: CampaignRunRecord) -> bool:
    return (
        left.run_id == right.run_id
        and left.request_hash == right.request_hash
        and left.campaign_hash == right.campaign_hash
        and left.route_hash == right.route_hash
        and left.protocol_plan_hash == right.protocol_plan_hash
    )


def _payload_hash(payload: Mapping[str, Any]) -> str:
    rendered = json.dumps(canonicalize(payload), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    import hashlib

    return "sha256:" + hashlib.sha256(rendered.encode("utf-8")).hexdigest()


def _safe_segment(value: str, field: str) -> None:
    require_identifier(value, field)
    if any(token in value for token in ("..", "/", "\\", ":")):
        fail(FailureCode.UNAUTHORIZED_CAPABILITY, f"{field} must be a single safe path segment")


__all__ = [
    "CAMPAIGN_RUN_STORE_SCHEMA_VERSION",
    "CampaignCheckpoint",
    "CampaignRunEvent",
    "CampaignRunEventType",
    "CampaignRunRecord",
    "CampaignRunStoreFailure",
    "CampaignRunStoreStatus",
    "FileCampaignRunStore",
    "GENESIS_EVENT_HASH",
    "InMemoryCampaignRunStore",
]
