"""Content-addressed publication of immutable registered context snapshots."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile

from .models import RegisteredResearchContext


_HASH = re.compile(r"^sha256:[0-9a-f]{64}$")


@dataclass(frozen=True, kw_only=True)
class ReadOnlyContextHandle:
    handle_id: str
    size_bytes: int


class ContentAddressedRegisteredContextStore:
    def __init__(self, root: str | Path) -> None:
        self._root = Path(root).resolve()
        self._root.mkdir(parents=True, exist_ok=True)

    def publish(self, context: RegisteredResearchContext) -> ReadOnlyContextHandle:
        if not isinstance(context, RegisteredResearchContext):
            raise TypeError("context must be RegisteredResearchContext")
        payload = context.canonical_bytes()
        target = self._target(context.artifact_hash)
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            descriptor = tempfile.NamedTemporaryFile(delete=False, dir=target.parent)
            temporary = Path(descriptor.name)
            try:
                descriptor.write(payload)
                descriptor.flush()
                os.fsync(descriptor.fileno())
                descriptor.close()
                os.replace(temporary, target)
            finally:
                if not descriptor.closed:
                    descriptor.close()
                if temporary.exists():
                    temporary.unlink()
        return ReadOnlyContextHandle(handle_id=context.artifact_hash, size_bytes=len(payload))

    def read(self, handle: ReadOnlyContextHandle | str) -> RegisteredResearchContext:
        handle_id = handle.handle_id if isinstance(handle, ReadOnlyContextHandle) else handle
        payload = self._target(handle_id).read_bytes()
        if "sha256:" + hashlib.sha256(payload).hexdigest() != handle_id:
            raise ValueError("registered context failed integrity verification")
        if isinstance(handle, ReadOnlyContextHandle) and len(payload) != handle.size_bytes:
            raise ValueError("registered context size mismatch")
        return RegisteredResearchContext.from_payload(json.loads(payload))

    def _target(self, handle_id: str) -> Path:
        if not isinstance(handle_id, str) or not _HASH.fullmatch(handle_id):
            raise ValueError("invalid context handle; filesystem paths are forbidden")
        digest = handle_id[7:]
        return self._root / digest[:2] / digest[2:]


__all__ = ["ContentAddressedRegisteredContextStore", "ReadOnlyContextHandle"]
