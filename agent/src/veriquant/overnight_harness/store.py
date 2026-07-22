from __future__ import annotations

from hashlib import sha256
import json
import os
from pathlib import Path
import tempfile
from typing import Any, Mapping

from .models import canonical_json


def text_hash(value: str) -> str:
    return "sha256:" + sha256(value.encode("utf-8")).hexdigest()


def file_hash(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


class HarnessStore:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.phase_root = self.root / "phase_results"
        self.root.mkdir(parents=True, exist_ok=True)
        self.phase_root.mkdir(parents=True, exist_ok=True)

    def atomic_json(self, name: str, payload: Mapping[str, Any]) -> None:
        self.atomic_text(name, canonical_json(dict(payload)) + "\n")

    def atomic_text(self, name: str, content: str) -> None:
        target = (self.root / name).resolve()
        if self.root != target and self.root not in target.parents:
            raise ValueError("artifact path escapes harness root")
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            json.loads(Path(temporary).read_text(encoding="utf-8")) if target.suffix == ".json" else None
            os.replace(temporary, target)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def append_jsonl(self, name: str, payload: Mapping[str, Any]) -> None:
        target = (self.root / name).resolve()
        if self.root != target and self.root not in target.parents:
            raise ValueError("artifact path escapes harness root")
        target.parent.mkdir(parents=True, exist_ok=True)
        line = canonical_json(dict(payload)) + "\n"
        with target.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())

    def load_json(self, name: str) -> dict[str, Any] | None:
        path = self.root / name
        if not path.exists():
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError(f"{name} must contain an object")
        return value

    def phase_result_path(self, phase: str) -> Path:
        return self.phase_root / f"{phase}.json"

    def artifact_hashes(self) -> dict[str, str]:
        excluded = {"artifact_hashes.json", "acceptance_verdict.json", "acceptance_report.md"}
        result: dict[str, str] = {}
        for path in sorted(self.root.rglob("*")):
            if not path.is_file() or path.name in excluded or path.suffix == ".tmp":
                continue
            result[path.relative_to(self.root).as_posix()] = file_hash(path)
        return result
