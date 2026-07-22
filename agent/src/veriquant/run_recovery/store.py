"""Append-only event store plus atomic materialized checkpoint state."""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterable

from veriquant.contracts import ContractValidationError, FailureCode
from veriquant.contracts.base import canonicalize, fail, require_hash, require_identifier
from veriquant.data import canonical_content_hash

from .models import (
    LeaseToken,
    LeaseUnavailable,
    MaterializedRunState,
    RecoveryError,
    RecoveryEvent,
    RecoveryEventType,
    RecoveryStatus,
    StageCheckpoint,
    StageIdentity,
    ZERO_HASH,
)


Clock = Callable[[], datetime]


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


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


def _atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
    with temporary.open("wb") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RecoveryError(
            FailureCode.LEDGER_CORRUPTION,
            f"checkpoint JSON is corrupt: {path.name}",
            details={"path": str(path)},
        ) from exc


class RunCheckpointStore:
    """Durable checkpoint store for generic A-F stage orchestration."""

    def __init__(self, root: str | Path, *, clock: Clock | None = None) -> None:
        self.root = Path(root).resolve()
        self.clock = clock or utc_now

    def run_dir(self, run_id: str) -> Path:
        require_identifier(run_id, "run_id")
        return self.root / "runs" / run_id

    def events_path(self, run_id: str) -> Path:
        return self.run_dir(run_id) / "events.jsonl"

    def state_path(self, run_id: str) -> Path:
        return self.run_dir(run_id) / "state.json"

    def lease_path(self, run_id: str) -> Path:
        return self.run_dir(run_id) / "lease.json"

    def read_events(self, run_id: str) -> tuple[RecoveryEvent, ...]:
        path = self.events_path(run_id)
        if not path.exists():
            return ()
        events: list[RecoveryEvent] = []
        previous_hash = ZERO_HASH
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except Exception as exc:
            raise RecoveryError(FailureCode.LEDGER_CORRUPTION, "events file is unreadable") from exc
        for index, line in enumerate(lines, start=1):
            if not line.strip():
                raise RecoveryError(FailureCode.LEDGER_CORRUPTION, "events file contains a blank line")
            try:
                event = RecoveryEvent.from_payload(json.loads(line))
            except Exception as exc:
                raise RecoveryError(
                    FailureCode.LEDGER_CORRUPTION,
                    "events file contains a corrupt event",
                    details={"line": index},
                ) from exc
            if event.sequence != index:
                raise RecoveryError(FailureCode.LEDGER_CORRUPTION, "event sequence is non-contiguous")
            if event.previous_event_hash != previous_hash:
                raise RecoveryError(FailureCode.LEDGER_CORRUPTION, "event hash chain is broken")
            previous_hash = event.artifact_hash
            events.append(event)
        return tuple(events)

    def append_event(
        self,
        *,
        run_id: str,
        event_type: RecoveryEventType,
        stage_identity_hash: str | None = None,
        lease_token_id: str | None = None,
        status: RecoveryStatus | None = None,
        stage_artifact_hash: str | None = None,
        payload_hash: str | None = None,
        failure_code: FailureCode | None = None,
        message: str = "",
    ) -> RecoveryEvent:
        events = self.read_events(run_id)
        previous_hash = events[-1].artifact_hash if events else ZERO_HASH
        event = RecoveryEvent(
            run_id=run_id,
            sequence=len(events) + 1,
            event_type=event_type,
            occurred_at=self.clock(),
            previous_event_hash=previous_hash,
            stage_identity_hash=stage_identity_hash,
            lease_token_id=lease_token_id,
            status=status,
            stage_artifact_hash=stage_artifact_hash,
            payload_hash=payload_hash,
            failure_code=failure_code,
            message=message,
        )
        path = self.events_path(run_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("ab") as handle:
            handle.write(event.canonical_bytes() + b"\n")
            handle.flush()
            os.fsync(handle.fileno())
        return event

    def load_state(self, run_id: str) -> MaterializedRunState:
        path = self.state_path(run_id)
        if not path.exists():
            return MaterializedRunState(run_id=run_id)
        raw = _read_json(path)
        if not isinstance(raw, dict) or "state_hash" not in raw:
            raise RecoveryError(FailureCode.LEDGER_CORRUPTION, "checkpoint state envelope is invalid")
        expected = raw.pop("state_hash")
        require_hash(expected, "state_hash")
        actual = canonical_content_hash(raw)
        if actual != expected:
            raise RecoveryError(FailureCode.LEDGER_CORRUPTION, "checkpoint state hash mismatch")
        try:
            return MaterializedRunState.from_payload(raw)
        except (ContractValidationError, ValueError, TypeError) as exc:
            raise RecoveryError(FailureCode.LEDGER_CORRUPTION, "checkpoint state payload is invalid") from exc

    def write_state(self, state: MaterializedRunState) -> None:
        payload = state.to_dict()
        state_hash = canonical_content_hash(payload)
        _atomic_write(self.state_path(state.run_id), _json_bytes({**payload, "state_hash": state_hash}))

    def replay_state(self, run_id: str) -> MaterializedRunState:
        committed: dict[str, StageCheckpoint] = {}
        failed: dict[str, StageCheckpoint] = {}
        blocked: dict[str, StageCheckpoint] = {}
        cancelled = False
        for event in self.read_events(run_id):
            if event.event_type is RecoveryEventType.RUN_CANCEL_REQUESTED:
                cancelled = True
            if event.stage_identity_hash is None:
                continue
            # Replay cannot reconstruct full StageIdentity from an event alone.
            # It is used here for integrity checks and sequence recovery; the
            # materialized state remains the authoritative read model for typed
            # checkpoints.
        state = self.load_state(run_id)
        if state.last_event_sequence > len(self.read_events(run_id)):
            raise RecoveryError(FailureCode.LEDGER_CORRUPTION, "state references missing future events")
        return MaterializedRunState(
            run_id=run_id,
            committed=tuple(committed.values()) or state.committed,
            failed=tuple(failed.values()) or state.failed,
            blocked=tuple(blocked.values()) or state.blocked,
            cancelled=cancelled or state.cancelled,
            last_event_sequence=max(state.last_event_sequence, len(self.read_events(run_id))),
        )

    def _upsert_checkpoint(
        self,
        *,
        run_id: str,
        checkpoint: StageCheckpoint,
        bucket: str,
    ) -> MaterializedRunState:
        state = self.load_state(run_id)

        def replace(items: Iterable[StageCheckpoint]) -> tuple[StageCheckpoint, ...]:
            by_hash = {item.identity.artifact_hash: item for item in items}
            by_hash[checkpoint.identity.artifact_hash] = checkpoint
            return tuple(sorted(by_hash.values(), key=lambda item: (item.identity.ordinal, item.identity.branch_id, item.identity.stage_id)))

        committed = state.committed
        failed = state.failed
        blocked = state.blocked
        if bucket == "committed":
            committed = replace(committed)
        elif bucket == "failed":
            failed = replace(failed)
        elif bucket == "blocked":
            blocked = replace(blocked)
        else:
            fail(FailureCode.INVALID_VALUE, "unknown checkpoint bucket")
        updated = MaterializedRunState(
            run_id=run_id,
            committed=committed,
            failed=failed,
            blocked=blocked,
            cancelled=state.cancelled,
            last_event_sequence=checkpoint.event_sequence,
        )
        self.write_state(updated)
        return updated

    def commit_stage(
        self,
        *,
        token: LeaseToken,
        identity: StageIdentity,
        artifact_hash: str,
        payload_hash: str,
    ) -> StageCheckpoint:
        self.assert_lease(token)
        state = self.load_state(identity.run_id)
        existing = state.committed_by_hash.get(identity.artifact_hash)
        if existing is not None:
            return existing
        if artifact_hash != identity.expected_artifact_hash:
            raise RecoveryError(FailureCode.INVALID_HASH, "provider artifact hash does not match StageIdentity")
        event = self.append_event(
            run_id=identity.run_id,
            event_type=RecoveryEventType.STAGE_COMMITTED,
            stage_identity_hash=identity.artifact_hash,
            lease_token_id=token.token_id,
            status=RecoveryStatus.COMMITTED,
            stage_artifact_hash=artifact_hash,
            payload_hash=payload_hash,
        )
        checkpoint = StageCheckpoint(
            identity=identity,
            status=RecoveryStatus.COMMITTED,
            committed_artifact_hash=artifact_hash,
            payload_hash=payload_hash,
            event_sequence=event.sequence,
            committed_at=event.occurred_at,
        )
        self._upsert_checkpoint(run_id=identity.run_id, checkpoint=checkpoint, bucket="committed")
        return checkpoint

    def fail_stage(
        self,
        *,
        token: LeaseToken,
        identity: StageIdentity,
        failure_code: FailureCode,
        message: str,
        blocked: bool = False,
    ) -> StageCheckpoint:
        self.assert_lease(token)
        event_type = RecoveryEventType.STAGE_BLOCKED if blocked else RecoveryEventType.STAGE_FAILED
        status = RecoveryStatus.BLOCKED if blocked else RecoveryStatus.FAILED
        event = self.append_event(
            run_id=identity.run_id,
            event_type=event_type,
            stage_identity_hash=identity.artifact_hash,
            lease_token_id=token.token_id,
            status=status,
            stage_artifact_hash=identity.expected_artifact_hash,
            payload_hash=ZERO_HASH,
            failure_code=failure_code,
            message=message,
        )
        checkpoint = StageCheckpoint(
            identity=identity,
            status=status,
            committed_artifact_hash=identity.expected_artifact_hash,
            payload_hash=ZERO_HASH,
            event_sequence=event.sequence,
            committed_at=event.occurred_at,
            failure_code=failure_code,
            message=message,
        )
        self._upsert_checkpoint(
            run_id=identity.run_id,
            checkpoint=checkpoint,
            bucket="blocked" if blocked else "failed",
        )
        return checkpoint

    def request_cancel(self, run_id: str, *, holder_id: str = "operator") -> None:
        require_identifier(holder_id, "holder_id")
        state = self.load_state(run_id)
        event = self.append_event(
            run_id=run_id,
            event_type=RecoveryEventType.RUN_CANCEL_REQUESTED,
            status=RecoveryStatus.CANCELLED,
            message=f"cancel requested by {holder_id}",
        )
        self.write_state(
            MaterializedRunState(
                run_id=run_id,
                committed=state.committed,
                failed=state.failed,
                blocked=state.blocked,
                cancelled=True,
                last_event_sequence=event.sequence,
            )
        )

    def acquire_lease(
        self,
        run_id: str,
        *,
        holder_id: str,
        ttl_seconds: int = 60,
    ) -> LeaseToken:
        require_identifier(holder_id, "holder_id")
        if isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, int) or ttl_seconds < 1:
            fail(FailureCode.INVALID_VALUE, "ttl_seconds must be positive")
        directory = self.run_dir(run_id)
        directory.mkdir(parents=True, exist_ok=True)
        lease_path = self.lease_path(run_id)
        now = self.clock()
        existing = self._read_lease_if_present(run_id)
        if existing is not None and existing.is_valid_at(now):
            raise LeaseUnavailable(
                FailureCode.CONSTRAINT_VIOLATION,
                "another recovery worker holds the active lease",
                details={"holder_id": existing.holder_id, "run_id": run_id},
            )
        if existing is not None:
            try:
                lease_path.unlink()
            except FileNotFoundError:
                _ = lease_path
        token = LeaseToken(
            run_id=run_id,
            holder_id=holder_id,
            token_id=f"lease-{uuid.uuid4().hex}",
            fencing_token=len(self.read_events(run_id)) + 1,
            acquired_at=now,
            expires_at=now + timedelta(seconds=ttl_seconds),
        )
        content = token.canonical_bytes() + b"\n"
        try:
            descriptor = os.open(str(lease_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError as exc:
            raise LeaseUnavailable(FailureCode.CONSTRAINT_VIOLATION, "lease acquisition lost a race") from exc
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        self.append_event(
            run_id=run_id,
            event_type=RecoveryEventType.LEASE_ACQUIRED,
            lease_token_id=token.token_id,
            message=f"holder={holder_id}",
        )
        return token

    def _read_lease_if_present(self, run_id: str) -> LeaseToken | None:
        path = self.lease_path(run_id)
        if not path.exists():
            return None
        raw = _read_json(path)
        try:
            return LeaseToken.from_payload(raw)
        except Exception as exc:
            raise RecoveryError(FailureCode.LEDGER_CORRUPTION, "lease file is corrupt") from exc

    def assert_lease(self, token: LeaseToken) -> None:
        if not isinstance(token, LeaseToken):
            fail(FailureCode.INVALID_VALUE, "token must be LeaseToken")
        current = self._read_lease_if_present(token.run_id)
        if current is None:
            raise RecoveryError(FailureCode.CONSTRAINT_VIOLATION, "lease has been released or lost")
        if current.token_id != token.token_id or current.fencing_token != token.fencing_token:
            raise RecoveryError(FailureCode.CONSTRAINT_VIOLATION, "stale fenced lease token")
        if not current.is_valid_at(self.clock()):
            raise RecoveryError(FailureCode.CONSTRAINT_VIOLATION, "lease token expired")

    def release_lease(self, token: LeaseToken) -> None:
        self.assert_lease(token)
        path = self.lease_path(token.run_id)
        try:
            path.unlink()
        except FileNotFoundError:
            _ = path
        self.append_event(
            run_id=token.run_id,
            event_type=RecoveryEventType.LEASE_RELEASED,
            lease_token_id=token.token_id,
            message=f"holder={token.holder_id}",
        )
