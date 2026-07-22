"""Branch checkpoint, cancellation, and resume contracts."""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Callable

from ..contracts import FailureCode
from ..contracts.base import fail
from ..llm_desks.safety import canonical_hash
from .contracts import ResearchAgentOutcome, ResearchAgentTask


class ResearchCancellationToken:
    """Cooperative cancellation shared by scheduler and injected AgentLoops."""

    def __init__(self) -> None:
        self._event = threading.Event()
        self._lock = threading.Lock()
        self._callbacks: list[Callable[[], None]] = []

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def register(self, callback: Callable[[], None]) -> None:
        with self._lock:
            if self._event.is_set():
                callback()
                return
            self._callbacks.append(callback)

    def cancel(self) -> None:
        self._event.set()
        with self._lock:
            callbacks = tuple(self._callbacks)
        for callback in callbacks:
            try:
                callback()
            except Exception:
                continue


class BranchCheckpointStore:
    """Content-addressed terminal branch store; never a Trial Ledger."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()
        self._lock = threading.RLock()

    def _path(self, task: ResearchAgentTask) -> Path:
        target = (self.root / task.run_id / "branches" / task.task_id / "outcome.json").resolve()
        try:
            target.relative_to(self.root)
        except ValueError:
            fail(FailureCode.UNAUTHORIZED_CAPABILITY, "branch checkpoint path escapes root")
        return target

    def load(self, task: ResearchAgentTask) -> ResearchAgentOutcome | None:
        path = self._path(task)
        with self._lock:
            if not path.exists():
                return None
            try:
                envelope = json.loads(path.read_text(encoding="utf-8"))
                payload = envelope["payload"]
                if envelope["payload_hash"] != canonical_hash(payload):
                    raise ValueError("hash mismatch")
                outcome = ResearchAgentOutcome.from_payload(payload)
            except Exception:
                fail(FailureCode.LEDGER_CORRUPTION, "branch checkpoint is corrupt")
            if outcome.task_hash != task.artifact_hash:
                fail(FailureCode.ARTIFACT_COLLISION, "branch checkpoint belongs to another task identity")
            return outcome

    def save(
        self,
        task: ResearchAgentTask,
        outcome: ResearchAgentOutcome,
        *,
        resume_cancelled: bool = False,
    ) -> ResearchAgentOutcome:
        if outcome.task_id != task.task_id or outcome.task_hash != task.artifact_hash:
            fail(FailureCode.INVALID_HASH, "branch outcome does not match task")
        path = self._path(task)
        payload = outcome.to_dict()
        envelope = {"payload": payload, "payload_hash": canonical_hash(payload)}
        encoded = json.dumps(envelope, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
        with self._lock:
            existing = self.load(task)
            if existing is not None:
                if resume_cancelled and existing.status.value == "CANCELLED":
                    pass
                elif existing.artifact_hash != outcome.artifact_hash:
                    fail(FailureCode.ARTIFACT_COLLISION, "terminal branch checkpoint cannot be overwritten")
                else:
                    return existing
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix(".tmp")
            with temporary.open("w", encoding="utf-8", newline="\n") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            json.loads(temporary.read_text(encoding="utf-8"))
            os.replace(temporary, path)
        return outcome


__all__ = ["BranchCheckpointStore", "ResearchCancellationToken"]
