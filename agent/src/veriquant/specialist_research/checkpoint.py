"""Atomic, checksummed specialist checkpoint and terminal outcome storage."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Mapping

from ..contracts import FailureCode
from ..contracts.base import fail
from ..llm_desks.safety import canonical_hash
from .models import SpecialistAgentCheckpoint, SpecialistAgentOutcome


_PATH_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,254}$")


class SpecialistCheckpointStore:
    def __init__(self, artifact_root: Path, *, run_id: str, specialist_id: str) -> None:
        # Materialize the owned root before resolving descendants.  On Windows,
        # resolving a not-yet-created parent concurrently with another branch
        # can yield inconsistent canonical forms and a false path-escape result.
        artifact_root.mkdir(parents=True, exist_ok=True)
        root = artifact_root.resolve(strict=True)
        if (
            not isinstance(run_id, str)
            or not isinstance(specialist_id, str)
            or _PATH_SEGMENT.fullmatch(run_id) is None
            or _PATH_SEGMENT.fullmatch(specialist_id) is None
            or run_id in {".", ".."}
            or specialist_id in {".", ".."}
        ):
            fail(FailureCode.UNAUTHORIZED_CAPABILITY, "specialist artifact path escapes root")
        lexical_target = root / run_id / "specialists" / specialist_id
        lexical_target.mkdir(parents=True, exist_ok=True)
        target = lexical_target.resolve(strict=True)
        try:
            root_key = os.path.normcase(os.path.normpath(str(root)))
            target_key = os.path.normcase(os.path.normpath(str(target)))
            contained = os.path.commonpath((root_key, target_key)) == root_key
        except ValueError:
            contained = False
        if not contained:
            fail(FailureCode.UNAUTHORIZED_CAPABILITY, "specialist artifact path escapes root")
        self.directory = target
        self.checkpoint_path = self.directory / "checkpoint.json"
        self.outcome_path = self.directory / "outcome.json"

    def save_checkpoint(self, checkpoint: SpecialistAgentCheckpoint) -> None:
        self._write(self.checkpoint_path, checkpoint.to_dict())

    def load_checkpoint(self) -> SpecialistAgentCheckpoint | None:
        payload = self._read(self.checkpoint_path)
        return None if payload is None else SpecialistAgentCheckpoint.from_payload(payload)

    def save_outcome(self, outcome: SpecialistAgentOutcome) -> None:
        self._write(self.outcome_path, outcome.to_dict())
        if outcome.plan is not None:
            self._write(self.directory / "exploration_plan.json", outcome.plan.to_dict())
        self._write(
            self.directory / "candidate_drafts.json",
            {"items": [item.to_dict() for item in outcome.candidates]},
        )
        self._write(
            self.directory / "preflight_results.json",
            {"items": [item.to_dict() for item in outcome.preflight_results]},
        )
        self._write(
            self.directory / "capability_requests.json",
            {"items": [item.to_dict() for item in outcome.capability_requests]},
        )
        self._write(
            self.directory / "draft_issues.json",
            {"items": [item.to_dict() for item in outcome.draft_issues]},
        )
        self._write_jsonl(
            self.directory / "investigation_findings.jsonl",
            tuple(item.to_dict() for item in outcome.findings),
        )
        self._write_jsonl(
            self.directory / "events.jsonl",
            tuple(
                {
                    "sequence": index,
                    "stage": stage.value,
                    "request_hash": outcome.request_hash,
                    "specialist_id": outcome.specialist_id,
                }
                for index, stage in enumerate(outcome.stage_history)
            ),
        )

    def load_outcome(self) -> SpecialistAgentOutcome | None:
        payload = self._read(self.outcome_path)
        return None if payload is None else SpecialistAgentOutcome.from_payload(payload)

    @staticmethod
    def _write(path: Path, payload: Mapping[str, Any]) -> None:
        envelope = {"payload": payload, "payload_hash": canonical_hash(payload)}
        rendered = json.dumps(envelope, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        temporary = path.with_suffix(path.suffix + ".tmp")
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(rendered)
            handle.flush()
            os.fsync(handle.fileno())
        json.loads(temporary.read_text(encoding="utf-8"))
        os.replace(temporary, path)

    @staticmethod
    def _read(path: Path) -> Mapping[str, Any] | None:
        if not path.exists():
            return None
        try:
            envelope = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            fail(FailureCode.INVALID_STATE, "specialist checkpoint is corrupt", error=type(exc).__name__)
        if not isinstance(envelope, Mapping) or set(envelope) != {"payload", "payload_hash"}:
            fail(FailureCode.INVALID_STATE, "specialist checkpoint envelope is invalid")
        payload = envelope["payload"]
        if not isinstance(payload, Mapping) or envelope["payload_hash"] != canonical_hash(payload):
            fail(FailureCode.INVALID_HASH, "specialist checkpoint hash mismatch")
        return payload

    @staticmethod
    def _write_jsonl(path: Path, rows: tuple[Mapping[str, Any], ...]) -> None:
        temporary = path.with_suffix(path.suffix + ".tmp")
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            for row in rows:
                handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
                handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        for line in temporary.read_text(encoding="utf-8").splitlines():
            if line:
                json.loads(line)
        os.replace(temporary, path)
