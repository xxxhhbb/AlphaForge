"""Explicitly enabled, local-only pyqlib runtime availability boundary."""

from __future__ import annotations

import importlib.util
import json
import os
import platform
import subprocess
from importlib import metadata
from pathlib import Path
from typing import Mapping

from .models import (
    FormulaExecutionFailure,
    FormulaExecutionRequest,
    QlibRuntimeManifest,
    RuntimeBackend,
    RuntimeFailureCode,
    RuntimeResult,
)
from .protocols import CertifiedExpressionIdentity, FrozenFeatureProvider


ENABLE_ENV = "VERIQUANT_ENABLE_PYQLIB_EXPRESSION_RUNTIME"
PYTHON_ENV = "VERIQUANT_QLIB_PYTHON"
_PROBE_SCRIPT = (
    "import importlib.metadata as m,json,platform;"
    "names=('pyqlib','numpy','pandas','scipy');"
    "versions={n:m.version(n) for n in names};"
    "print(json.dumps({'python':platform.python_version(),'versions':versions},sort_keys=True))"
)


class OptionalPyQlibExpressionRuntime:
    """Probe-only boundary until a repository-owned fixed operator worker is wired.

    Merely installing pyqlib never enables execution.  This class does not
    initialize Qlib, download data, parse expression strings, or register
    operators.  A future integration package can replace the unavailable
    terminal with a fixed, reviewed operator-object worker.
    """

    def __init__(
        self,
        *,
        operator_policy_hash: str,
        code_hash: str,
        python_executable: str | Path | None = None,
    ) -> None:
        self._operator_policy_hash = operator_policy_hash
        self._code_hash = code_hash
        configured = python_executable or os.environ.get(PYTHON_ENV)
        self._python_executable = Path(configured).resolve() if configured else None

    @staticmethod
    def _enabled() -> bool:
        return os.environ.get(ENABLE_ENV) == "1"

    @staticmethod
    def _active_environment_versions() -> tuple[str, Mapping[str, str]] | None:
        if importlib.util.find_spec("qlib") is None:
            return None
        try:
            versions = {
                name: metadata.version(name)
                for name in ("pyqlib", "numpy", "pandas", "scipy")
            }
        except metadata.PackageNotFoundError:
            return None
        return platform.python_version(), versions

    def _configured_environment_versions(self) -> tuple[str, Mapping[str, str]] | None:
        executable = self._python_executable
        if executable is None:
            return self._active_environment_versions()
        if not executable.is_absolute() or not executable.is_file():
            return None
        environment: dict[str, str] = {
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUTF8": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONNOUSERSITE": "1",
        }
        for name in ("SystemRoot", "WINDIR", "PATH", "TEMP", "TMP"):
            value = os.environ.get(name)
            if value:
                environment[name] = value
        try:
            completed = subprocess.run(
                [str(executable), "-I", "-c", _PROBE_SCRIPT],
                shell=False,
                check=False,
                capture_output=True,
                timeout=10.0,
                env=environment,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if completed.returncode != 0 or len(completed.stdout) > 16_384:
            return None
        try:
            payload = json.loads(completed.stdout.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict) or set(payload) != {"python", "versions"}:
            return None
        python_version = payload["python"]
        versions = payload["versions"]
        if (
            not isinstance(python_version, str)
            or not isinstance(versions, dict)
            or "pyqlib" not in versions
            or any(not isinstance(key, str) or not isinstance(value, str) for key, value in versions.items())
        ):
            return None
        return python_version, dict(sorted(versions.items()))

    @property
    def manifest(self) -> QlibRuntimeManifest:
        enabled = self._enabled()
        probe = self._configured_environment_versions() if enabled else None
        python_version, dependencies = probe if probe is not None else (platform.python_version(), {})
        return QlibRuntimeManifest(
            runtime_id="certified-optional-pyqlib-v1",
            backend=RuntimeBackend.OPTIONAL_PYQLIB,
            implementation="veriquant-optional-pyqlib-boundary",
            implementation_version="1.0.0",
            python_version=python_version,
            dependency_versions=dependencies,
            operator_policy_hash=self._operator_policy_hash,
            code_hash=self._code_hash,
            enabled=enabled and probe is not None,
        )

    def execute(
        self,
        *,
        request: FormulaExecutionRequest,
        program: CertifiedExpressionIdentity,
        provider: FrozenFeatureProvider,
    ) -> RuntimeResult:
        del program, provider
        manifest = self.manifest
        if not self._enabled():
            code = RuntimeFailureCode.RUNTIME_DISABLED
            message = f"optional pyqlib runtime is disabled; set {ENABLE_ENV}=1 explicitly"
        elif not manifest.enabled:
            code = RuntimeFailureCode.RUNTIME_UNAVAILABLE
            message = "pyqlib is unavailable in the configured local Python environment"
        elif request.runtime_hash != manifest.artifact_hash:
            code = RuntimeFailureCode.RUNTIME_MISMATCH
            message = "optional pyqlib runtime identity mismatch"
        else:
            code = RuntimeFailureCode.RUNTIME_UNAVAILABLE
            message = "fixed reviewed pyqlib operator worker is not configured"
        return FormulaExecutionFailure(
            request_hash=request.artifact_hash,
            runtime_hash=manifest.artifact_hash,
            code=code,
            message=message,
            retryable=False,
        )
