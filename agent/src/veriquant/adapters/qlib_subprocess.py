"""Isolated subprocess boundary for the opt-in visible-only pyqlib worker."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Mapping

from ..contracts import ContractValidationError, FailureCode
from ..factors import CompiledFactorDAG
from .qlib import AdapterFailure, QlibVisibleRow, VisibleQlibManifest


_MAX_CAPTURE_BYTES = 64 * 1024
_MAX_OUTPUT_BYTES = 256 * 1024
_FORBIDDEN = ("sealed", "test", "official", "promotion", "token", "secret")


def _has_forbidden_reference(value: object) -> bool:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                return True
            normalized = key.lower()
            if any(token in normalized for token in _FORBIDDEN):
                return True
            if _has_forbidden_reference(item):
                return True
        return False
    if isinstance(value, (list, tuple)):
        return any(_has_forbidden_reference(item) for item in value)
    if isinstance(value, str):
        normalized = value.lower()
        return normalized in _FORBIDDEN or any(normalized.startswith(token + ":") for token in _FORBIDDEN)
    return False

class SubprocessLocalQlibVisibleRunner:
    """Run only the repository-owned worker with an explicit isolated Python."""

    def __init__(
        self,
        *,
        python_executable: str | Path | None = None,
        worker_path: str | Path | None = None,
        timeout_seconds: float = 30.0,
    ) -> None:
        configured = python_executable or os.environ.get("QLIB_PYTHON", "")
        self.python_executable = Path(configured)
        default_worker = Path(__file__).with_name("qlib_worker.py")
        self.worker_path = Path(worker_path).resolve() if worker_path else default_worker.resolve()
        self.timeout_seconds = float(timeout_seconds)

    def _preflight(self, manifest_hash: str) -> AdapterFailure | None:
        if not self.python_executable.is_absolute() or not self.python_executable.is_file():
            return AdapterFailure(
                adapter="local-pyqlib-subprocess",
                code=FailureCode.ADAPTER_UNAVAILABLE,
                message="QLIB_PYTHON must name an existing absolute executable",
                manifest_hash=manifest_hash,
            )
        if self.worker_path != Path(__file__).with_name("qlib_worker.py").resolve():
            return AdapterFailure(
                adapter="local-pyqlib-subprocess",
                code=FailureCode.UNAUTHORIZED_CAPABILITY,
                message="only the repository-owned fixed Qlib worker is permitted",
                manifest_hash=manifest_hash,
            )
        if not self.worker_path.is_file() or self.timeout_seconds <= 0:
            return AdapterFailure(
                adapter="local-pyqlib-subprocess",
                code=FailureCode.ADAPTER_UNAVAILABLE,
                message="fixed Qlib worker or timeout configuration is unavailable",
                manifest_hash=manifest_hash,
            )
        return None

    @staticmethod
    def _environment() -> dict[str, str]:
        qlib_home = Path(__file__).resolve().parents[4] / ".autobuild" / "veriquant" / "qlib_home"
        qlib_home.mkdir(parents=True, exist_ok=True)
        environment = {
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUTF8": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONNOUSERSITE": "1",
            "USERPROFILE": str(qlib_home),
        }
        for name in ("SystemRoot", "WINDIR", "PATH", "TEMP", "TMP"):
            value = os.environ.get(name)
            if value:
                environment[name] = value
        return environment

    @staticmethod
    def _failure(
        manifest_hash: str,
        code: FailureCode,
        message: str,
        *,
        retryable: bool = False,
    ) -> AdapterFailure:
        return AdapterFailure(
            adapter="local-pyqlib-subprocess",
            code=code,
            message=message,
            manifest_hash=manifest_hash,
            retryable=retryable,
        )

    def pyqlib_available(self, manifest_hash: str) -> bool:
        if self._preflight(manifest_hash) is not None:
            return False
        try:
            completed = subprocess.run(
                [str(self.python_executable), str(self.worker_path), "--probe"],
                shell=False,
                check=False,
                capture_output=True,
                timeout=min(self.timeout_seconds, 10.0),
                env=self._environment(),
            )
        except (OSError, subprocess.SubprocessError):
            return False
        return completed.returncode == 0 and completed.stdout.strip() == b'{"pyqlib":true}'

    def run_visible(
        self,
        *,
        manifest: VisibleQlibManifest,
        candidate: CompiledFactorDAG,
        rows: tuple[QlibVisibleRow, ...],
    ) -> Mapping[str, Mapping[str, float]] | AdapterFailure:
        if not isinstance(manifest, VisibleQlibManifest):
            raise ContractValidationError(
                FailureCode.UNAUTHORIZED_CAPABILITY,
                "subprocess runner requires the visible-only manifest",
            )
        if not isinstance(candidate, CompiledFactorDAG):
            raise ContractValidationError(
                FailureCode.INVALID_FACTOR_DAG,
                "subprocess runner requires a compiled restricted FactorDAG",
            )
        preflight = self._preflight(manifest.manifest_hash)
        if preflight is not None:
            return preflight
        payload = {
            "schema_version": "veriquant.qlib-worker-input/v1",
            "manifest": manifest.to_dict(),
            "candidate_hash": candidate.artifact_hash,
            "rows": [row.to_dict() for row in rows],
        }
        encoded = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
        if _has_forbidden_reference(payload):
            return self._failure(
                manifest.manifest_hash,
                FailureCode.UNAUTHORIZED_CAPABILITY,
                "visible Qlib input contains a forbidden authority or partition token",
            )
        with tempfile.TemporaryDirectory(prefix="veriquant-qlib-") as temporary:
            directory = Path(temporary)
            input_path = directory / "input.json"
            output_path = directory / "output.json"
            input_path.write_bytes(encoded)
            command = [
                str(self.python_executable),
                str(self.worker_path),
                "--input",
                str(input_path),
                "--output",
                str(output_path),
            ]
            try:
                completed = subprocess.run(
                    command,
                    shell=False,
                    check=False,
                    capture_output=True,
                    timeout=self.timeout_seconds,
                    env=self._environment(),
                )
            except subprocess.TimeoutExpired:
                return self._failure(
                    manifest.manifest_hash,
                    FailureCode.ADAPTER_EXECUTION_FAILED,
                    "local Qlib worker timed out",
                    retryable=True,
                )
            except OSError as exc:
                return self._failure(
                    manifest.manifest_hash,
                    FailureCode.ADAPTER_UNAVAILABLE,
                    f"local Qlib worker could not start: {type(exc).__name__}",
                )
            if len(completed.stdout) + len(completed.stderr) > _MAX_CAPTURE_BYTES:
                return self._failure(
                    manifest.manifest_hash,
                    FailureCode.ADAPTER_EXECUTION_FAILED,
                    "local Qlib worker exceeded the diagnostic output limit",
                )
            if completed.returncode != 0:
                return self._failure(
                    manifest.manifest_hash,
                    FailureCode.ADAPTER_EXECUTION_FAILED,
                    "local Qlib worker returned a non-zero status",
                )
            if not output_path.is_file() or output_path.stat().st_size > _MAX_OUTPUT_BYTES:
                return self._failure(
                    manifest.manifest_hash,
                    FailureCode.ADAPTER_EXECUTION_FAILED,
                    "local Qlib worker output is missing or oversized",
                )
            try:
                result = json.loads(output_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                return self._failure(
                    manifest.manifest_hash,
                    FailureCode.ADAPTER_EXECUTION_FAILED,
                    "local Qlib worker output is invalid JSON",
                )
            if not isinstance(result, dict) or set(result) != {"train", "validation"}:
                return self._failure(
                    manifest.manifest_hash,
                    FailureCode.ADAPTER_EXECUTION_FAILED,
                    "local Qlib worker output must contain exactly visible partitions",
                )
            for partition, metrics in result.items():
                if not isinstance(metrics, dict) or not metrics:
                    return self._failure(
                        manifest.manifest_hash,
                        FailureCode.ADAPTER_EXECUTION_FAILED,
                        "local Qlib worker returned invalid metrics",
                    )
                if any(
                    not isinstance(name, str)
                    or any(token in name.lower() for token in _FORBIDDEN)
                    or isinstance(value, bool)
                    or not isinstance(value, (int, float))
                    for name, value in metrics.items()
                ):
                    return self._failure(
                        manifest.manifest_hash,
                        FailureCode.UNAUTHORIZED_CAPABILITY,
                        f"local Qlib worker returned forbidden {partition} metrics",
                    )
            return result


