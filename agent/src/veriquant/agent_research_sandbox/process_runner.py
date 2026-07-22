"""One-shot process runner for reviewed auxiliary research programs.

This module deliberately distinguishes a scrubbed child process from an
OS-enforced sandbox.  ``PROCESS_FENCED`` runs are useful for development and
auxiliary canaries, but are never sufficient for official Evidence admission.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Mapping, Sequence

from .contracts import ExperimentReviewDecision, MechanicalReviewStatus


AMENDMENT_VERSION = "research-sandbox-amendment/v1"
RESULT_PREFIX = "__ALPHAFORGE_RESULT__"
_FORBIDDEN_INPUT_PARTS = (
    "label",
    "future",
    "sealed",
    "official",
    "promotion",
    "challenger",
    "target_weight",
    "order",
    "ledger",
    "secret",
    "token",
    "authorization",
)


def canonical_hash(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
        default=lambda item: item.value if isinstance(item, Enum) else item,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


class ProcessIsolationLevel(str, Enum):
    PROCESS_FENCED = "PROCESS_FENCED"
    OS_SANDBOXED = "OS_SANDBOXED"


class ProcessRunStatus(str, Enum):
    COMPLETED_AUXILIARY = "COMPLETED_AUXILIARY"
    BLOCKED = "BLOCKED"
    TIMEOUT = "TIMEOUT"
    FAILED = "FAILED"


class ProcessFailureCode(str, Enum):
    CODE_HASH_MISMATCH = "CODE_HASH_MISMATCH"
    AMENDMENT_MISMATCH = "AMENDMENT_MISMATCH"
    FORBIDDEN_INPUT = "FORBIDDEN_INPUT"
    INPUT_TOO_LARGE = "INPUT_TOO_LARGE"
    OUTPUT_TOO_LARGE = "OUTPUT_TOO_LARGE"
    TIMEOUT = "TIMEOUT"
    LAUNCH_FAILED = "LAUNCH_FAILED"
    PROTOCOL_FAILED = "PROTOCOL_FAILED"
    OUTPUT_INVALID = "OUTPUT_INVALID"
    OS_SANDBOX_UNAVAILABLE = "OS_SANDBOX_UNAVAILABLE"
    REVIEW_NOT_ACCEPTED = "REVIEW_NOT_ACCEPTED"
    REVIEW_IDENTITY_MISMATCH = "REVIEW_IDENTITY_MISMATCH"


@dataclass(frozen=True, slots=True)
class ResearchProcessConfig:
    python_path: Path
    sandbox_root: Path
    timeout_seconds: float = 10.0
    max_input_bytes: int = 8 * 1024 * 1024
    max_output_bytes: int = 8 * 1024 * 1024
    max_diagnostic_bytes: int = 4096
    require_os_sandbox: bool = False

    def __post_init__(self) -> None:
        python = Path(self.python_path).resolve(strict=True)
        root = Path(self.sandbox_root).resolve()
        if not python.is_file() or not root.is_absolute():
            raise ValueError("resolved Python and absolute sandbox_root are required")
        if isinstance(self.timeout_seconds, bool) or not 0.05 <= float(self.timeout_seconds) <= 300.0:
            raise ValueError("timeout_seconds must be from 0.05 to 300")
        for value in (self.max_input_bytes, self.max_output_bytes, self.max_diagnostic_bytes):
            if isinstance(value, bool) or not isinstance(value, int) or value < 128:
                raise ValueError("process byte limits must be positive bounded integers")
        object.__setattr__(self, "python_path", python)
        object.__setattr__(self, "sandbox_root", root)


@dataclass(frozen=True, slots=True)
class ResearchProcessRequest:
    run_id: str
    experiment_id: str
    code_text: str
    code_hash: str
    code_identity_hash: str
    data_identity_hash: str
    review_decision: ExperimentReviewDecision
    feature_rows: tuple[Mapping[str, Any], ...]
    output_kind: str
    seed: int
    amendment_hash: str
    amendment_version: str = AMENDMENT_VERSION

    def __post_init__(self) -> None:
        for name in ("run_id", "experiment_id", "output_kind"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value or any(token in value for token in ("/", "\\", "..")):
                raise ValueError(f"{name} must be a safe identifier")
        if not isinstance(self.code_text, str) or not self.code_text.strip():
            raise ValueError("code_text is required")
        if self.code_hash != _source_hash(self.code_text):
            raise ValueError(ProcessFailureCode.CODE_HASH_MISMATCH.value)
        if not _is_sha256(self.code_identity_hash) or not _is_sha256(self.data_identity_hash):
            raise ValueError("code and data identity hashes are required")
        if not isinstance(self.review_decision, ExperimentReviewDecision):
            raise ValueError(ProcessFailureCode.REVIEW_IDENTITY_MISMATCH.value)
        if self.review_decision.status is not MechanicalReviewStatus.ACCEPTED:
            raise ValueError(ProcessFailureCode.REVIEW_NOT_ACCEPTED.value)
        if (
            self.review_decision.experiment_id != self.experiment_id
            or self.review_decision.source_hash != self.code_hash
            or self.review_decision.amendment_hash != self.amendment_hash
        ):
            raise ValueError(ProcessFailureCode.REVIEW_IDENTITY_MISMATCH.value)
        if not _is_sha256(self.amendment_hash) or self.amendment_version != AMENDMENT_VERSION:
            raise ValueError(ProcessFailureCode.AMENDMENT_MISMATCH.value)
        if isinstance(self.seed, bool) or not isinstance(self.seed, int) or self.seed < 0:
            raise ValueError("seed must be a non-negative integer")
        if not isinstance(self.feature_rows, tuple) or not self.feature_rows:
            raise ValueError("feature_rows must be a non-empty tuple")
        _validate_feature_payload(self.feature_rows)

    @property
    def input_hash(self) -> str:
        return canonical_hash(
            {
                "run_id": self.run_id,
                "experiment_id": self.experiment_id,
                "code_hash": self.code_hash,
                "code_identity_hash": self.code_identity_hash,
                "data_identity_hash": self.data_identity_hash,
                "review_decision_hash": self.review_decision.artifact_hash,
                "feature_rows": self.feature_rows,
                "output_kind": self.output_kind,
                "seed": self.seed,
                "amendment_hash": self.amendment_hash,
                "amendment_version": self.amendment_version,
            }
        )


@dataclass(frozen=True, slots=True)
class ResearchProcessReceipt:
    run_id: str
    experiment_id: str
    status: ProcessRunStatus
    isolation_level: ProcessIsolationLevel
    input_hash: str
    code_hash: str
    output_hash: str | None
    output_kind: str
    output: Any | None
    failure_code: ProcessFailureCode | None
    diagnostic: str | None
    amendment_hash: str
    official_evidence_eligible: bool = False

    @property
    def artifact_hash(self) -> str:
        return canonical_hash(
            {
                "run_id": self.run_id,
                "experiment_id": self.experiment_id,
                "status": self.status,
                "isolation_level": self.isolation_level,
                "input_hash": self.input_hash,
                "code_hash": self.code_hash,
                "output_hash": self.output_hash,
                "output_kind": self.output_kind,
                "output": self.output,
                "failure_code": self.failure_code,
                "diagnostic": self.diagnostic,
                "amendment_hash": self.amendment_hash,
                "official_evidence_eligible": self.official_evidence_eligible,
            }
        )


class ProcessFencedResearchRunner:
    """Execute reviewed code in a scrubbed one-shot child process.

    This is not an AppContainer, VM, or container.  Receipts remain auxiliary
    and explicitly fail when the caller requires OS-enforced isolation.
    """

    def __init__(self, config: ResearchProcessConfig) -> None:
        self.config = config

    def execute(self, request: ResearchProcessRequest) -> ResearchProcessReceipt:
        if self.config.require_os_sandbox:
            return self._failure(request, ProcessRunStatus.BLOCKED, ProcessFailureCode.OS_SANDBOX_UNAVAILABLE)
        encoded_rows = json.dumps(
            request.feature_rows,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        if len(encoded_rows.encode("utf-8")) > self.config.max_input_bytes:
            return self._failure(request, ProcessRunStatus.BLOCKED, ProcessFailureCode.INPUT_TOO_LARGE)
        source = _build_program(request.code_text, encoded_rows, request.seed)
        self.config.sandbox_root.mkdir(parents=True, exist_ok=True)
        try:
            with tempfile.TemporaryDirectory(prefix="alphaforge-research-", dir=self.config.sandbox_root) as directory:
                home = Path(directory).resolve()
                program = home / "reviewed_program.py"
                program.write_text(source, encoding="utf-8", newline="\n")
                creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
                process = subprocess.Popen(
                    (str(self.config.python_path), "-I", "-B", str(program)),
                    shell=False,
                    cwd=str(home),
                    env=_minimal_environment(home),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    creationflags=creationflags,
                )
                try:
                    stdout, stderr = process.communicate(timeout=self.config.timeout_seconds)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.communicate()
                    return self._failure(request, ProcessRunStatus.TIMEOUT, ProcessFailureCode.TIMEOUT)
        except OSError:
            return self._failure(request, ProcessRunStatus.FAILED, ProcessFailureCode.LAUNCH_FAILED)

        if len(stdout) > self.config.max_output_bytes or len(stderr) > self.config.max_diagnostic_bytes:
            return self._failure(request, ProcessRunStatus.BLOCKED, ProcessFailureCode.OUTPUT_TOO_LARGE)
        if process.returncode != 0:
            return self._failure(request, ProcessRunStatus.FAILED, ProcessFailureCode.PROTOCOL_FAILED)
        try:
            decoded = stdout.decode("utf-8")
            lines = [line for line in decoded.splitlines() if line.startswith(RESULT_PREFIX)]
            if len(lines) != 1:
                raise ValueError("result marker missing or duplicated")
            other = [line for line in decoded.splitlines() if line and not line.startswith(RESULT_PREFIX)]
            if other:
                raise ValueError("uncontracted stdout is forbidden")
            output = json.loads(lines[0][len(RESULT_PREFIX) :])
            _validate_result_payload(output)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError):
            return self._failure(request, ProcessRunStatus.FAILED, ProcessFailureCode.OUTPUT_INVALID)
        output_hash = canonical_hash(
            {
                "input_hash": request.input_hash,
                "output_kind": request.output_kind,
                "output": output,
            }
        )
        return ResearchProcessReceipt(
            run_id=request.run_id,
            experiment_id=request.experiment_id,
            status=ProcessRunStatus.COMPLETED_AUXILIARY,
            isolation_level=ProcessIsolationLevel.PROCESS_FENCED,
            input_hash=request.input_hash,
            code_hash=request.code_hash,
            output_hash=output_hash,
            output_kind=request.output_kind,
            output=output,
            failure_code=None,
            diagnostic=None,
            amendment_hash=request.amendment_hash,
            official_evidence_eligible=False,
        )

    @staticmethod
    def _failure(
        request: ResearchProcessRequest,
        status: ProcessRunStatus,
        code: ProcessFailureCode,
    ) -> ResearchProcessReceipt:
        return ResearchProcessReceipt(
            run_id=request.run_id,
            experiment_id=request.experiment_id,
            status=status,
            isolation_level=ProcessIsolationLevel.PROCESS_FENCED,
            input_hash=request.input_hash,
            code_hash=request.code_hash,
            output_hash=None,
            output_kind=request.output_kind,
            output=None,
            failure_code=code,
            diagnostic=code.value,
            amendment_hash=request.amendment_hash,
            official_evidence_eligible=False,
        )


def _build_program(code_text: str, encoded_rows: str, seed: int) -> str:
    header = "import json as _alphaforge_json\n"
    footer = f'''

if "run_research" not in globals() or not callable(run_research):
    raise RuntimeError("reviewed program must define run_research(rows, seed)")
__alphaforge_rows = _alphaforge_json.loads({encoded_rows!r})
__alphaforge_output = run_research(__alphaforge_rows, {seed})
print({RESULT_PREFIX!r} + _alphaforge_json.dumps(__alphaforge_output, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")))
'''
    return header + code_text.rstrip() + "\n" + footer.lstrip()


def _minimal_environment(home: Path) -> dict[str, str]:
    environment = {
        "PYTHONHASHSEED": "0",
        "PYTHONNOUSERSITE": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONUTF8": "1",
        "USERPROFILE": str(home),
        "HOME": str(home),
        "TEMP": str(home),
        "TMP": str(home),
    }
    for key in ("SystemRoot", "WINDIR", "SYSTEMDRIVE"):
        if os.environ.get(key):
            environment[key] = os.environ[key]
    return environment


def _validate_feature_payload(rows: Sequence[Mapping[str, Any]]) -> None:
    for row in rows:
        if not isinstance(row, Mapping):
            raise ValueError("feature rows must be mappings")
        for key, value in row.items():
            lowered = str(key).lower()
            if any(part in lowered for part in _FORBIDDEN_INPUT_PARTS):
                raise ValueError(ProcessFailureCode.FORBIDDEN_INPUT.value)
            if isinstance(value, float) and not math.isfinite(value):
                raise ValueError("feature rows cannot contain NaN or Inf")
            if not (value is None or type(value) in {str, bool, int, float}):
                raise ValueError("feature rows contain an unsupported value")


def _validate_result_payload(value: Any) -> None:
    encoded = json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))
    lowered = encoded.lower()
    if any(part in lowered for part in ("sealed", "official_metrics", "promotion", "target_weights", "orders", "ledger")):
        raise ValueError("result requests forbidden authority")


def _is_sha256(value: str) -> bool:
    return isinstance(value, str) and value.startswith("sha256:") and len(value) == 71


def _source_hash(source: str) -> str:
    return "sha256:" + hashlib.sha256(source.encode("utf-8")).hexdigest()


__all__ = [
    "AMENDMENT_VERSION",
    "ProcessFailureCode",
    "ProcessFencedResearchRunner",
    "ProcessIsolationLevel",
    "ProcessRunStatus",
    "ResearchProcessConfig",
    "ResearchProcessReceipt",
    "ResearchProcessRequest",
    "canonical_hash",
]
