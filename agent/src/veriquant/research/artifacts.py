"""Isolated atomic branch artifacts and append-only operational events."""

from __future__ import annotations

import json
import os
import re
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from ..contracts import ContractValidationError, FailureCode
from ..contracts.base import canonicalize, require_aware_datetime
from ..data import canonical_content_hash


BRANCH_JSON_ARTIFACTS: tuple[str, ...] = (
    "proposal.json",
    "factor_spec.json",
    "experiment_manifest.json",
    "evidence_pack.json",
    "critic_findings.json",
)
BRANCH_ARTIFACTS: tuple[str, ...] = (*BRANCH_JSON_ARTIFACTS, "events.jsonl")
_SAFE_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,80}$")


def _fail(code: FailureCode, message: str, **details: Any) -> None:
    raise ContractValidationError(code, message, details=details)


def _safe_segment(value: str, field: str) -> None:
    if not isinstance(value, str) or _SAFE_SEGMENT.fullmatch(value) is None:
        _fail(FailureCode.INVALID_IDENTIFIER, f"{field} is not a safe path segment")


class BranchArtifactStore:
    """One store owns exactly one run/branch namespace."""

    def __init__(
        self,
        *,
        artifact_root: str | Path,
        run_id: str,
        branch_id: str,
        event_time: datetime,
    ) -> None:
        _safe_segment(run_id, "run_id")
        _safe_segment(branch_id, "branch_id")
        require_aware_datetime(event_time, "event_time")
        root = Path(artifact_root).resolve()
        self.run_id = run_id
        self.branch_id = branch_id
        self.branch_dir = (root / run_id / "branches" / branch_id).resolve()
        expected_parent = (root / run_id / "branches").resolve()
        if self.branch_dir.parent != expected_parent:
            _fail(FailureCode.INVALID_IDENTIFIER, "branch namespace escaped artifact root")
        try:
            self.branch_dir.mkdir(parents=True, exist_ok=False)
        except FileExistsError as exc:
            raise ContractValidationError(
                FailureCode.ARTIFACT_COLLISION,
                "branch artifact namespace already exists",
                details={"branch": branch_id},
            ) from exc
        self._lock = threading.Lock()
        self._sequence = 0
        self._last_event_hash = "sha256:" + "0" * 64
        self._produced: set[str] = set()
        for name in BRANCH_JSON_ARTIFACTS:
            self._atomic_write(
                name,
                {
                    "artifact_status": "NOT_PRODUCED",
                    "artifact_name": name,
                    "branch_id": branch_id,
                    "content": None,
                    "failure": None,
                },
            )
        events_path = self.branch_dir / "events.jsonl"
        with events_path.open("x", encoding="utf-8", newline="\n") as handle:
            handle.flush()
            os.fsync(handle.fileno())
        self.append_event(
            stage="branch",
            status="INITIALIZED",
            event_time=event_time,
        )

    def path(self, name: str) -> Path:
        if name not in BRANCH_ARTIFACTS:
            _fail(FailureCode.INVALID_IDENTIFIER, "unknown branch artifact name", artifact=name)
        return self.branch_dir / name

    def _atomic_write(self, name: str, payload: Any) -> None:
        path = self.path(name)
        temp = path.with_name(path.name + ".tmp")
        rendered = json.dumps(
            canonicalize(payload),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
        with temp.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(rendered)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)

    def write_artifact(self, name: str, content: Any) -> str:
        if name not in BRANCH_JSON_ARTIFACTS:
            _fail(FailureCode.INVALID_IDENTIFIER, "artifact is not a branch JSON artifact")
        content_hash = canonical_content_hash(content)
        with self._lock:
            if name in self._produced:
                _fail(
                    FailureCode.ARTIFACT_COLLISION,
                    "branch artifact cannot be produced twice",
                    artifact=name,
                    branch=self.branch_id,
                )
            self._atomic_write(
                name,
                {
                    "artifact_status": "PRODUCED",
                    "artifact_name": name,
                    "branch_id": self.branch_id,
                    "content_hash": content_hash,
                    "content": content,
                    "failure": None,
                },
            )
            self._produced.add(name)
        return content_hash

    def mark_unproduced(self, *, failure_code: str, message: str) -> None:
        """Record absence honestly; never writes a success-shaped placeholder."""

        with self._lock:
            for name in BRANCH_JSON_ARTIFACTS:
                if name in self._produced:
                    continue
                self._atomic_write(
                    name,
                    {
                        "artifact_status": "NOT_PRODUCED",
                        "artifact_name": name,
                        "branch_id": self.branch_id,
                        "content": None,
                        "failure": {"code": failure_code, "message": message},
                    },
                )

    def append_event(self, *, stage: str, status: str, event_time: datetime) -> str:
        require_aware_datetime(event_time, "event_time")
        with self._lock:
            body = {
                "sequence": self._sequence,
                "run_id": self.run_id,
                "branch_id": self.branch_id,
                "stage": stage,
                "status": status,
                "event_time": event_time,
                "previous_hash": self._last_event_hash,
            }
            event_hash = canonical_content_hash(body)
            event = {**body, "event_hash": event_hash}
            rendered = json.dumps(
                canonicalize(event),
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            )
            with self.path("events.jsonl").open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(rendered + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            self._sequence += 1
            self._last_event_hash = event_hash
            return event_hash
