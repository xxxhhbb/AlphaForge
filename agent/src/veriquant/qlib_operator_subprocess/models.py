"""Typed contracts for one-request-per-process certified Qlib execution."""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from ..qlib_operator_worker.api import QlibFactorScoreArtifact, QlibOperatorWorkerFailureCode


SUBPROCESS_SCHEMA_VERSION = "veriquant.qlib-operator-subprocess/v1"
MAX_PROTOCOL_INPUT_BYTES = 64 * 1024 * 1024
MAX_PROTOCOL_OUTPUT_BYTES = 64 * 1024 * 1024
MAX_PROTOCOL_DIAGNOSTIC_BYTES = 4_096


class QlibSubprocessFailureCode(str, Enum):
    INVALID_REQUEST = "INVALID_REQUEST"
    PYTHON_NOT_PINNED = "PYTHON_NOT_PINNED"
    WORKER_IDENTITY_MISMATCH = "WORKER_IDENTITY_MISMATCH"
    INPUT_TOO_LARGE = "INPUT_TOO_LARGE"
    OUTPUT_TOO_LARGE = "OUTPUT_TOO_LARGE"
    TIMEOUT = "TIMEOUT"
    LAUNCH_FAILED = "LAUNCH_FAILED"
    WORKER_PROTOCOL_FAILED = "WORKER_PROTOCOL_FAILED"
    WORKER_EXECUTION_FAILED = "WORKER_EXECUTION_FAILED"
    OUTPUT_INVALID = "OUTPUT_INVALID"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _is_hash(value: str) -> bool:
    return isinstance(value, str) and value.startswith("sha256:") and len(value) == 71


@dataclass(frozen=True, slots=True)
class QlibSubprocessConfig:
    """Pinned executable and bounded IPC policy.

    ``pin`` captures the executable identity before the first launch.  Every
    execution re-hashes it, so replacing the configured path fails closed.
    The worker script is deliberately not caller-configurable.
    """

    python_path: Path
    python_sha256: str
    sandbox_root: Path
    timeout_seconds: float = 60.0
    max_input_bytes: int = 16 * 1024 * 1024
    max_output_bytes: int = 16 * 1024 * 1024
    max_diagnostic_bytes: int = MAX_PROTOCOL_DIAGNOSTIC_BYTES

    def __post_init__(self) -> None:
        python_path = Path(self.python_path)
        sandbox_root = Path(self.sandbox_root)
        if not python_path.is_absolute() or not python_path.is_file():
            raise ValueError("python_path must be an existing absolute file")
        if python_path.resolve() != python_path:
            raise ValueError("python_path must be resolved and cannot contain path indirection")
        if not _is_hash(self.python_sha256):
            raise ValueError("python_sha256 must be a SHA-256 identity")
        if not sandbox_root.is_absolute():
            raise ValueError("sandbox_root must be absolute")
        if isinstance(self.timeout_seconds, bool) or not isinstance(self.timeout_seconds, (int, float)):
            raise ValueError("timeout_seconds must be numeric")
        if not math.isfinite(float(self.timeout_seconds)) or not 0.05 <= float(self.timeout_seconds) <= 300.0:
            raise ValueError("timeout_seconds must be from 0.05 to 300 seconds")
        bounds = (
            ("max_input_bytes", self.max_input_bytes, MAX_PROTOCOL_INPUT_BYTES),
            ("max_output_bytes", self.max_output_bytes, MAX_PROTOCOL_OUTPUT_BYTES),
            ("max_diagnostic_bytes", self.max_diagnostic_bytes, MAX_PROTOCOL_DIAGNOSTIC_BYTES),
        )
        for name, value, maximum in bounds:
            if isinstance(value, bool) or not isinstance(value, int) or not 128 <= value <= maximum:
                raise ValueError(f"{name} must be from 128 to {maximum}")
        object.__setattr__(self, "python_path", python_path)
        object.__setattr__(self, "sandbox_root", sandbox_root.resolve())

    @classmethod
    def pin(
        cls,
        *,
        python_path: str | Path,
        sandbox_root: str | Path,
        **limits: object,
    ) -> "QlibSubprocessConfig":
        resolved_python = Path(python_path).resolve(strict=True)
        resolved_root = Path(sandbox_root).resolve()
        return cls(
            python_path=resolved_python,
            python_sha256=sha256_file(resolved_python),
            sandbox_root=resolved_root,
            **limits,
        )


@dataclass(frozen=True, slots=True)
class QlibSubprocessFailure:
    code: QlibSubprocessFailureCode
    message: str
    program_hash: str
    input_hash: str
    retryable: bool = False
    worker_failure_code: QlibOperatorWorkerFailureCode | None = None

    def __post_init__(self) -> None:
        if not _is_hash(self.program_hash) or not _is_hash(self.input_hash):
            raise ValueError("subprocess failure must bind program and input hashes")
        if not isinstance(self.message, str) or not self.message or len(self.message.encode("utf-8")) > MAX_PROTOCOL_DIAGNOSTIC_BYTES:
            raise ValueError("subprocess failure diagnostic is invalid")
        if not isinstance(self.retryable, bool):
            raise ValueError("retryable must be boolean")
        if self.worker_failure_code is not None and not isinstance(self.worker_failure_code, QlibOperatorWorkerFailureCode):
            raise ValueError("worker_failure_code is invalid")


QlibSubprocessResult = QlibFactorScoreArtifact | QlibSubprocessFailure


__all__ = [
    "MAX_PROTOCOL_DIAGNOSTIC_BYTES",
    "MAX_PROTOCOL_INPUT_BYTES",
    "MAX_PROTOCOL_OUTPUT_BYTES",
    "QlibSubprocessConfig",
    "QlibSubprocessFailure",
    "QlibSubprocessFailureCode",
    "QlibSubprocessResult",
    "SUBPROCESS_SCHEMA_VERSION",
    "sha256_file",
]
