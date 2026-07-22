"""Content-addressed read-only handles with no caller-controlled paths."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any

from .contracts import ReadOnlyArtifactHandle


_HANDLE = re.compile(r"^sha256:[0-9a-f]{64}$")


class ContentAddressedHandleStore:
    def __init__(self, root: str | Path) -> None:
        self._root = Path(root).resolve()
        self._root.mkdir(parents=True, exist_ok=True)

    def put_bytes(self, payload: bytes, *, media_type: str = "application/octet-stream") -> ReadOnlyArtifactHandle:
        if not isinstance(payload, bytes):
            raise TypeError("payload must be bytes")
        handle_id = "sha256:" + hashlib.sha256(payload).hexdigest()
        target = self._target(handle_id)
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary_handle = tempfile.NamedTemporaryFile(delete=False, dir=target.parent)
            temporary = Path(temporary_handle.name)
            try:
                temporary_handle.write(payload)
                temporary_handle.flush()
                os.fsync(temporary_handle.fileno())
                temporary_handle.close()
                os.replace(temporary, target)
            finally:
                if not temporary_handle.closed:
                    temporary_handle.close()
                if temporary.exists():
                    temporary.unlink()
        return ReadOnlyArtifactHandle(handle_id=handle_id, media_type=media_type, size_bytes=len(payload))

    def put_json(self, value: Any) -> ReadOnlyArtifactHandle:
        payload = json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return self.put_bytes(payload, media_type="application/json")

    def read_bytes(self, handle: ReadOnlyArtifactHandle | str) -> bytes:
        handle_id = handle.handle_id if isinstance(handle, ReadOnlyArtifactHandle) else handle
        target = self._target(handle_id)
        try:
            payload = target.read_bytes()
        except FileNotFoundError as exc:
            raise ValueError("unknown content handle") from exc
        actual = "sha256:" + hashlib.sha256(payload).hexdigest()
        if actual != handle_id:
            raise ValueError("content handle failed integrity verification")
        if isinstance(handle, ReadOnlyArtifactHandle) and len(payload) != handle.size_bytes:
            raise ValueError("content handle size mismatch")
        return payload

    def read_json(self, handle: ReadOnlyArtifactHandle | str) -> Any:
        return json.loads(self.read_bytes(handle).decode("utf-8"))

    def _target(self, handle_id: str) -> Path:
        if not isinstance(handle_id, str) or not _HANDLE.fullmatch(handle_id):
            raise ValueError("invalid content handle; filesystem paths are forbidden")
        digest = handle_id[7:]
        return self._root / digest[:2] / digest[2:]


__all__ = ["ContentAddressedHandleStore"]
