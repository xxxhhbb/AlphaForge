"""Bounded launcher for the fixed certified-Qlib subprocess worker."""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path
from typing import Mapping

from ..qlib_expression.api import QlibExpressionPolicy
from ..qlib_operator_worker.api import QlibOperatorExecutionRequest
from .models import (
    QlibSubprocessConfig,
    QlibSubprocessFailure,
    QlibSubprocessFailureCode,
    QlibSubprocessResult,
    sha256_file,
)
from .wire import WireFormatError, dumps, loads, request_to_wire, result_from_wire


PINNED_WORKER_PATH = Path(__file__).with_name("_worker_main.py").resolve(strict=True)
PINNED_WORKER_SOURCE_HASH = "sha256:c9ce95f34f8d0141fa5e91f617ea82ece7e9ca0a97c376f8c8a6e514d02067f6"


def build_worker_environment(isolated_home: Path, source: Mapping[str, str] | None = None) -> dict[str, str]:
    """Return an allowlist environment; provider credentials are never copied."""

    parent = os.environ if source is None else source
    environment = {
        "PYTHONHASHSEED": "0",
        "PYTHONNOUSERSITE": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONUTF8": "1",
        "USERPROFILE": str(isolated_home),
        "HOME": str(isolated_home),
        "TEMP": str(isolated_home),
        "TMP": str(isolated_home),
    }
    for system_name in ("SystemRoot", "WINDIR", "SYSTEMDRIVE"):
        value = parent.get(system_name)
        if value:
            environment[system_name] = value
    return environment


def _failure(
    request: QlibOperatorExecutionRequest,
    code: QlibSubprocessFailureCode,
    message: str,
    *,
    retryable: bool = False,
) -> QlibSubprocessFailure:
    return QlibSubprocessFailure(
        code=code,
        message=message[:512],
        program_hash=request.program.expression_hash,
        input_hash=request.input_hash,
        retryable=retryable,
    )


class QlibOperatorSubprocessRunner:
    """Execute exactly one typed request in exactly one fresh process."""

    def __init__(self, config: QlibSubprocessConfig) -> None:
        if type(config) is not QlibSubprocessConfig:
            raise TypeError("exact QlibSubprocessConfig required")
        self._config = config

    def execute(
        self,
        request: QlibOperatorExecutionRequest,
        *,
        policy: QlibExpressionPolicy,
    ) -> QlibSubprocessResult:
        if type(request) is not QlibOperatorExecutionRequest or type(policy) is not QlibExpressionPolicy:
            raise TypeError("runner accepts only typed request and policy; expression strings are prohibited")
        config = self._config
        if sha256_file(config.python_path) != config.python_sha256:
            return _failure(request, QlibSubprocessFailureCode.PYTHON_NOT_PINNED, "pinned Python executable identity changed")
        if PINNED_WORKER_PATH != Path(__file__).with_name("_worker_main.py").resolve(strict=True) or sha256_file(PINNED_WORKER_PATH) != PINNED_WORKER_SOURCE_HASH:
            return _failure(request, QlibSubprocessFailureCode.WORKER_IDENTITY_MISMATCH, "fixed worker source identity changed")
        try:
            payload = dumps(request_to_wire(request, policy))
        except Exception:
            return _failure(request, QlibSubprocessFailureCode.INVALID_REQUEST, "request serialization failed closed")
        if len(payload) > config.max_input_bytes:
            return _failure(request, QlibSubprocessFailureCode.INPUT_TOO_LARGE, "structured request exceeds IPC input limit")

        config.sandbox_root.mkdir(parents=True, exist_ok=True)
        try:
            with tempfile.TemporaryDirectory(prefix="vq-qlib-", dir=config.sandbox_root) as temporary:
                isolated_home = Path(temporary).resolve()
                command = (str(config.python_path), "-I", "-B", str(PINNED_WORKER_PATH))
                creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
                process = subprocess.Popen(
                    command,
                    shell=False,
                    cwd=str(isolated_home),
                    env=build_worker_environment(isolated_home),
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    creationflags=creationflags,
                )
                try:
                    stdout, stderr = process.communicate(payload, timeout=config.timeout_seconds)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.communicate()
                    return _failure(request, QlibSubprocessFailureCode.TIMEOUT, "certified Qlib subprocess timed out", retryable=True)
        except OSError:
            return _failure(request, QlibSubprocessFailureCode.LAUNCH_FAILED, "certified Qlib subprocess could not be launched")

        if len(stdout) > config.max_output_bytes or len(stderr) > config.max_diagnostic_bytes:
            return _failure(request, QlibSubprocessFailureCode.OUTPUT_TOO_LARGE, "worker output or diagnostic exceeds IPC limit")
        if process.returncode != 0:
            return _failure(request, QlibSubprocessFailureCode.WORKER_PROTOCOL_FAILED, "fixed worker exited without a valid result")
        try:
            return result_from_wire(
                loads(stdout),
                expected_program_hash=request.program.expression_hash,
                expected_input_hash=request.input_hash,
            )
        except Exception:
            return _failure(request, QlibSubprocessFailureCode.OUTPUT_INVALID, "worker output failed identity validation")


__all__ = [
    "PINNED_WORKER_PATH",
    "PINNED_WORKER_SOURCE_HASH",
    "QlibOperatorSubprocessRunner",
    "build_worker_environment",
]
