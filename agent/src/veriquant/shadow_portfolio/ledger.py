"""Append-only in-memory shadow ledger with idempotent run recording."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import Any, Mapping

from ..contracts import ContractValidationError, FailureCode
from ..contracts.base import canonicalize, require_hash
from .models import ShadowLedgerEvent, ShadowRunManifest


def _hash(value: Any) -> str:
    import hashlib
    import json

    rendered = json.dumps(
        canonicalize(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    return "sha256:" + hashlib.sha256(rendered.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class LedgerSnapshot:
    events: tuple[ShadowLedgerEvent, ...]
    payloads: tuple[Any, ...]
    manifests: Mapping[str, ShadowRunManifest]


class ShadowLedger:
    """Operational shadow ledger; append-only and deterministic for fixtures."""

    def __init__(self) -> None:
        self._events: list[ShadowLedgerEvent] = []
        self._payloads: list[Any] = []
        self._manifests: dict[str, ShadowRunManifest] = {}
        self._run_request_hashes: dict[str, str] = {}

    @property
    def tip_hash(self) -> str:
        if not self._events:
            return _hash({"empty_shadow_ledger": True})
        last = self._events[-1]
        assert last.event_hash is not None
        return last.event_hash

    @property
    def event_count(self) -> int:
        return len(self._events)

    def get_manifest(self, run_id: str, request_hash: str) -> ShadowRunManifest | None:
        existing_hash = self._run_request_hashes.get(run_id)
        if existing_hash is None:
            return None
        if existing_hash != request_hash:
            raise ContractValidationError(
                FailureCode.DUPLICATE_FILL_CONFLICT,
                "run_id already committed with a different frozen request",
                details={"run_id": run_id},
            )
        return self._manifests[run_id]

    def append_payload(
        self,
        *,
        run_id: str,
        event_type: str,
        event_time: datetime,
        payload: Any,
    ) -> ShadowLedgerEvent:
        payload_hash = _hash(payload)
        previous_hash = self.tip_hash if self._events else None
        sequence = len(self._events) + 1
        body = {
            "sequence": sequence,
            "run_id": run_id,
            "event_type": event_type,
            "event_time": event_time,
            "previous_hash": previous_hash,
            "payload_hash": payload_hash,
        }
        event = ShadowLedgerEvent(
            sequence=sequence,
            run_id=run_id,
            event_type=event_type,
            event_time=event_time,
            previous_hash=previous_hash,
            payload_hash=payload_hash,
            event_hash=_hash(body),
        )
        self._events.append(event)
        self._payloads.append(payload)
        return event

    def commit_manifest(self, *, request_hash: str, manifest: ShadowRunManifest) -> ShadowRunManifest:
        existing = self.get_manifest(manifest.run_id, request_hash)
        if existing is not None:
            return existing
        require_hash(request_hash, "request_hash")
        self._run_request_hashes[manifest.run_id] = request_hash
        self._manifests[manifest.run_id] = manifest
        return manifest

    def snapshot(self) -> LedgerSnapshot:
        return LedgerSnapshot(
            events=tuple(self._events),
            payloads=tuple(self._payloads),
            manifests=MappingProxyType(dict(self._manifests)),
        )
