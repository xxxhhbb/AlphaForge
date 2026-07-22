"""Docker-enforced runner for mechanically reviewed research programs.

The Docker daemon is an operator-provided capability.  This module never
pulls or builds an image: it resolves an explicitly named local image to an
expected immutable image id and then starts a one-shot, non-root container.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

from .contracts import (
    SCHEMA_VERSION,
    DatasetAssessmentTable,
    DiagnosticTable,
    EventTable,
    RegressionTable,
    RegimeComparisonTable,
    ScoreTable,
)
from .process_runner import (
    ProcessFailureCode,
    ProcessIsolationLevel,
    ProcessRunStatus,
    RESULT_PREFIX,
    ResearchProcessReceipt,
    ResearchProcessRequest,
    _validate_feature_payload,
    _validate_result_payload,
    canonical_hash,
)


_IMAGE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/:@-]{0,254}$")
_IMAGE_ID = re.compile(r"^sha256:[0-9a-f]{64}$")
_SAFE_EXCEPTION_CLASS = re.compile(
    rb"(?m)^([A-Za-z_][A-Za-z0-9_]*(?:Error|Exception)):",
)
_SAFE_PROGRAM_LINE = re.compile(
    rb'File "/sandbox/reviewed_program\.py", line ([1-9][0-9]{0,5})',
)
_CONTAINER_PREFIX = "alphaforge-research-"
_CONTRACT_TYPES = {
    cls.__name__: cls
    for cls in (
        ScoreTable,
        EventTable,
        RegressionTable,
        RegimeComparisonTable,
        DiagnosticTable,
        DatasetAssessmentTable,
    )
}


def _aware_time(value: str, name: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be an ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
    except ValueError as exc:
        raise ValueError(f"{name} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return parsed


@dataclass(frozen=True, slots=True)
class DockerTrainingObservation:
    """A historical outcome already available before prediction begins."""

    symbol: str
    decision_time: str
    source_available_at: str
    outcome_available_at: str
    target_value: float
    features: Mapping[str, Any]

    def __post_init__(self) -> None:
        if not isinstance(self.symbol, str) or not self.symbol:
            raise ValueError("training symbol is required")
        decision = _aware_time(self.decision_time, "decision_time")
        source = _aware_time(self.source_available_at, "source_available_at")
        outcome = _aware_time(self.outcome_available_at, "outcome_available_at")
        if source > decision or outcome < decision:
            raise ValueError("training observation clocks are invalid")
        if isinstance(self.target_value, bool) or not isinstance(self.target_value, (int, float)):
            raise ValueError("target_value must be numeric")
        _validate_feature_payload((self.features,))

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "decision_time": self.decision_time,
            "source_available_at": self.source_available_at,
            "outcome_available_at": self.outcome_available_at,
            "target_value": float(self.target_value),
            "features": dict(self.features),
        }


@dataclass(frozen=True, slots=True)
class DockerResearchRequest:
    """Versioned train/predict request; prediction rows remain label-free."""

    process_request: ResearchProcessRequest
    training_rows: tuple[DockerTrainingObservation, ...]
    training_cutoff: str
    entrypoint_version: str = "TRAIN_PREDICT_V2"

    def __post_init__(self) -> None:
        if not isinstance(self.process_request, ResearchProcessRequest):
            raise ValueError("process_request must be typed")
        if self.entrypoint_version != "TRAIN_PREDICT_V2":
            raise ValueError("unsupported Docker research entrypoint version")
        if not self.training_rows or not all(isinstance(row, DockerTrainingObservation) for row in self.training_rows):
            raise ValueError("training_rows must be a non-empty typed tuple")
        cutoff = _aware_time(self.training_cutoff, "training_cutoff")
        prediction_start = min(
            _aware_time(str(row["decision_time"]), "prediction decision_time")
            for row in self.process_request.feature_rows
        )
        if cutoff > prediction_start:
            raise ValueError("training_cutoff cannot be after prediction start")
        for row in self.training_rows:
            if _aware_time(row.outcome_available_at, "outcome_available_at") > cutoff:
                raise ValueError("training outcome was unavailable at training_cutoff")

    @property
    def prediction_input_hash(self) -> str:
        return canonical_hash({"feature_rows": self.process_request.feature_rows})

    @property
    def training_input_hash(self) -> str:
        return canonical_hash(
            {
                "training_cutoff": self.training_cutoff,
                "training_rows": tuple(row.to_dict() for row in self.training_rows),
            }
        )

    @property
    def input_hash(self) -> str:
        return canonical_hash(
            {
                "base_request_hash": self.process_request.input_hash,
                "entrypoint_version": self.entrypoint_version,
                "prediction_input_hash": self.prediction_input_hash,
                "training_input_hash": self.training_input_hash,
            }
        )


@dataclass(frozen=True, slots=True)
class DockerResearchProcessReceipt(ResearchProcessReceipt):
    """Receipt binding separated historical-training and prediction inputs."""

    training_input_hash: str = "sha256:" + "0" * 64
    prediction_input_hash: str = "sha256:" + "0" * 64
    image_id: str = "sha256:" + "0" * 64
    entrypoint_version: str = "FEATURE_ONLY_V1"

    @property
    def artifact_hash(self) -> str:
        return canonical_hash(
            {
                "base_receipt_hash": ResearchProcessReceipt.artifact_hash.fget(self),
                "training_input_hash": self.training_input_hash,
                "prediction_input_hash": self.prediction_input_hash,
                "image_id": self.image_id,
                "entrypoint_version": self.entrypoint_version,
            }
        )


@dataclass(frozen=True, slots=True)
class DockerResearchConfig:
    """Pinned local-image and resource policy for one-shot execution."""

    sandbox_root: Path
    image_name: str
    expected_image_id: str
    docker_path: str = "docker"
    timeout_seconds: float = 30.0
    memory_limit: str = "256m"
    cpu_limit: float = 0.5
    pids_limit: int = 32
    tmpfs_size: str = "16m"
    max_input_bytes: int = 8 * 1024 * 1024
    max_output_bytes: int = 8 * 1024 * 1024
    max_diagnostic_bytes: int = 4096

    def __post_init__(self) -> None:
        root = Path(self.sandbox_root).resolve()
        if not root.is_absolute():
            raise ValueError("sandbox_root must be absolute")
        if not isinstance(self.image_name, str) or _IMAGE_NAME.fullmatch(self.image_name) is None:
            raise ValueError("image_name must be an explicit safe local image reference")
        if not isinstance(self.expected_image_id, str) or _IMAGE_ID.fullmatch(self.expected_image_id) is None:
            raise ValueError("expected_image_id must pin sha256:<64 lowercase hex>")
        docker_path = str(self.docker_path)
        if os.path.isabs(docker_path):
            resolved = Path(docker_path).resolve(strict=True)
            if not resolved.is_file():
                raise ValueError("docker_path must resolve to a file")
            docker_path = str(resolved)
        elif docker_path != "docker":
            raise ValueError("docker_path must be 'docker' or an absolute executable path")
        if isinstance(self.timeout_seconds, bool) or not 0.05 <= float(self.timeout_seconds) <= 900.0:
            raise ValueError("timeout_seconds must be from 0.05 to 900")
        if isinstance(self.cpu_limit, bool) or not 0.1 <= float(self.cpu_limit) <= 4.0:
            raise ValueError("cpu_limit must be from 0.1 to 4")
        if isinstance(self.pids_limit, bool) or not 8 <= self.pids_limit <= 256:
            raise ValueError("pids_limit must be from 8 to 256")
        if re.fullmatch(r"[1-9][0-9]{0,4}[mk]", self.memory_limit) is None:
            raise ValueError("memory_limit must be a bounded Docker m/k value")
        if re.fullmatch(r"[1-9][0-9]{0,4}[mk]", self.tmpfs_size) is None:
            raise ValueError("tmpfs_size must be a bounded Docker m/k value")
        for value in (self.max_input_bytes, self.max_output_bytes, self.max_diagnostic_bytes):
            if isinstance(value, bool) or not isinstance(value, int) or value < 128:
                raise ValueError("byte limits must be positive bounded integers")
        object.__setattr__(self, "sandbox_root", root)
        object.__setattr__(self, "docker_path", docker_path)


class DockerResearchRunner:
    """Run reviewed code in a locked-down, uniquely named Docker container."""

    def __init__(self, config: DockerResearchConfig) -> None:
        self.config = config

    def available(self) -> bool:
        """Return true only if daemon and the pinned *local* image are ready."""

        return self._resolve_local_image() == self.config.expected_image_id

    def execute(
        self, request: ResearchProcessRequest | DockerResearchRequest
    ) -> DockerResearchProcessReceipt:
        base_request = request.process_request if isinstance(request, DockerResearchRequest) else request
        effective_input_hash = request.input_hash if isinstance(request, DockerResearchRequest) else request.input_hash
        prediction_input_hash = (
            request.prediction_input_hash
            if isinstance(request, DockerResearchRequest)
            else canonical_hash({"feature_rows": request.feature_rows})
        )
        training_input_hash = (
            request.training_input_hash
            if isinstance(request, DockerResearchRequest)
            else canonical_hash({"training_rows": ()})
        )
        entrypoint_version = request.entrypoint_version if isinstance(request, DockerResearchRequest) else "FEATURE_ONLY_V1"
        encoded_rows = json.dumps(
            base_request.feature_rows,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        encoded_training = json.dumps(
            tuple(row.to_dict() for row in request.training_rows)
            if isinstance(request, DockerResearchRequest)
            else (),
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        if len(encoded_rows.encode("utf-8")) + len(encoded_training.encode("utf-8")) > self.config.max_input_bytes:
            return self._failure(request, ProcessRunStatus.BLOCKED, ProcessFailureCode.INPUT_TOO_LARGE)

        # ``image inspect`` never pulls.  A mutable tag is not accepted unless
        # it still resolves to the identity frozen in the run configuration.
        if self._resolve_local_image() != self.config.expected_image_id:
            return self._failure(
                request,
                ProcessRunStatus.BLOCKED,
                ProcessFailureCode.OS_SANDBOX_UNAVAILABLE,
            )

        source = _build_docker_program(base_request, encoded_rows, encoded_training, entrypoint_version)
        self.config.sandbox_root.mkdir(parents=True, exist_ok=True)
        container_name = _CONTAINER_PREFIX + uuid.uuid4().hex
        try:
            with tempfile.TemporaryDirectory(prefix=container_name + "-", dir=self.config.sandbox_root) as directory:
                mount_root = Path(directory).resolve()
                program = mount_root / "reviewed_program.py"
                program.write_text(source, encoding="utf-8", newline="\n")
                command = self._docker_command(container_name, mount_root)
                creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
                process = subprocess.Popen(
                    command,
                    shell=False,
                    cwd=str(self.config.sandbox_root),
                    env=self._docker_client_environment(),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    creationflags=creationflags,
                )
                try:
                    stdout, stderr = process.communicate(timeout=self.config.timeout_seconds)
                except subprocess.TimeoutExpired:
                    self._force_remove(container_name)
                    process.kill()
                    process.communicate()
                    return self._failure(request, ProcessRunStatus.TIMEOUT, ProcessFailureCode.TIMEOUT)
        except OSError:
            self._force_remove(container_name)
            return self._failure(request, ProcessRunStatus.FAILED, ProcessFailureCode.LAUNCH_FAILED)

        # --rm should remove completed containers; force-remove is idempotent
        # and closes the race where the client exits before daemon cleanup.
        self._force_remove(container_name)
        if len(stdout) > self.config.max_output_bytes or len(stderr) > self.config.max_diagnostic_bytes:
            return self._failure(request, ProcessRunStatus.BLOCKED, ProcessFailureCode.OUTPUT_TOO_LARGE)
        if process.returncode != 0:
            return self._failure(
                request,
                ProcessRunStatus.FAILED,
                ProcessFailureCode.PROTOCOL_FAILED,
                diagnostic=_safe_protocol_diagnostic(stderr),
            )

        try:
            output = self._parse_contract(stdout, base_request)
            _validate_result_payload(output)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError):
            return self._failure(request, ProcessRunStatus.FAILED, ProcessFailureCode.OUTPUT_INVALID)
        output_hash = canonical_hash(
            {"input_hash": effective_input_hash, "output_kind": base_request.output_kind, "output": output}
        )
        return DockerResearchProcessReceipt(
            run_id=base_request.run_id,
            experiment_id=base_request.experiment_id,
            status=ProcessRunStatus.COMPLETED_AUXILIARY,
            isolation_level=ProcessIsolationLevel.OS_SANDBOXED,
            input_hash=effective_input_hash,
            code_hash=base_request.code_hash,
            output_hash=output_hash,
            output_kind=base_request.output_kind,
            output=output,
            failure_code=None,
            diagnostic=None,
            amendment_hash=base_request.amendment_hash,
            # These contracts can be independently recomputed by the unified
            # evaluator from frozen primitive inputs. Summary-only contracts
            # stay useful AUXILIARY evidence without acquiring official status.
            official_evidence_eligible=base_request.output_kind in {
                ScoreTable.__name__, "EventTable", "DatasetAssessmentTable",
            },
            training_input_hash=training_input_hash,
            prediction_input_hash=prediction_input_hash,
            image_id=self.config.expected_image_id,
            entrypoint_version=entrypoint_version,
        )

    def _docker_command(self, container_name: str, mount_root: Path) -> tuple[str, ...]:
        mount = f"type=bind,source={mount_root},target=/sandbox,readonly"
        return (
            self.config.docker_path,
            "run",
            "--rm",
            "--pull",
            "never",
            "--name",
            container_name,
            "--network",
            "none",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges:true",
            "--user",
            "65534:65534",
            "--pids-limit",
            str(self.config.pids_limit),
            "--memory",
            self.config.memory_limit,
            "--cpus",
            str(self.config.cpu_limit),
            "--ipc",
            "none",
            "--tmpfs",
            f"/tmp:rw,noexec,nosuid,nodev,size={self.config.tmpfs_size}",
            "--ulimit",
            "nofile=64:64",
            "--ulimit",
            f"nproc={self.config.pids_limit}:{self.config.pids_limit}",
            "--no-healthcheck",
            "--mount",
            mount,
            "--workdir",
            "/tmp",
            "--env",
            "HOME=/tmp",
            "--env",
            "PYTHONHASHSEED=0",
            "--env",
            "PYTHONNOUSERSITE=1",
            "--env",
            "PYTHONDONTWRITEBYTECODE=1",
            "--entrypoint",
            "python",
            # Execute the immutable image id, never the mutable tag.
            self.config.expected_image_id,
            "-I",
            "-B",
            "/sandbox/reviewed_program.py",
        )

    def _resolve_local_image(self) -> str | None:
        if self.config.docker_path == "docker" and shutil.which("docker") is None:
            return None
        try:
            result = subprocess.run(
                (
                    self.config.docker_path,
                    "image",
                    "inspect",
                    self.config.image_name,
                    "--format",
                    "{{.Id}}",
                ),
                shell=False,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=min(10.0, self.config.timeout_seconds),
                check=False,
                env=self._docker_client_environment(),
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if result.returncode != 0:
            return None
        candidate = result.stdout.decode("ascii", errors="ignore").strip()
        return candidate if _IMAGE_ID.fullmatch(candidate) else None

    def _force_remove(self, container_name: str) -> None:
        try:
            subprocess.run(
                (self.config.docker_path, "rm", "-f", container_name),
                shell=False,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10.0,
                check=False,
                env=self._docker_client_environment(),
            )
        except (OSError, subprocess.TimeoutExpired):
            # The returned execution receipt is already fail-closed.  A unique
            # name plus --rm makes a later operator cleanup unambiguous.
            return

    @staticmethod
    def _docker_client_environment() -> dict[str, str]:
        # Docker Desktop needs only connection/runtime variables.  Provider
        # keys and the host environment never enter the container.
        allowed = ("PATH", "DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CERT_PATH", "DOCKER_TLS_VERIFY")
        return {key: os.environ[key] for key in allowed if key in os.environ}

    @staticmethod
    def _parse_contract(stdout: bytes, request: ResearchProcessRequest) -> dict[str, Any]:
        decoded = stdout.decode("utf-8")
        lines = [line for line in decoded.splitlines() if line.startswith(RESULT_PREFIX)]
        other = [line for line in decoded.splitlines() if line and not line.startswith(RESULT_PREFIX)]
        if len(lines) != 1 or other:
            raise ValueError("sandbox output protocol violation")
        output = json.loads(lines[0][len(RESULT_PREFIX) :])
        contract_type = _CONTRACT_TYPES.get(request.output_kind)
        if contract_type is None or not isinstance(output, dict):
            raise ValueError("unknown or malformed research output contract")
        parsed = contract_type.from_payload(output)
        if (
            parsed.experiment_id != request.experiment_id
            or parsed.code_identity_hash != request.code_identity_hash
            or parsed.data_identity_hash != request.data_identity_hash
        ):
            raise ValueError("research output identity mismatch")
        return parsed.to_dict()

    def _failure(
        self,
        request: ResearchProcessRequest | DockerResearchRequest,
        status: ProcessRunStatus,
        code: ProcessFailureCode,
        *,
        diagnostic: str | None = None,
    ) -> DockerResearchProcessReceipt:
        base_request = request.process_request if isinstance(request, DockerResearchRequest) else request
        effective_input_hash = request.input_hash
        return DockerResearchProcessReceipt(
            run_id=base_request.run_id,
            experiment_id=base_request.experiment_id,
            status=status,
            isolation_level=ProcessIsolationLevel.OS_SANDBOXED,
            input_hash=effective_input_hash,
            code_hash=base_request.code_hash,
            output_hash=None,
            output_kind=base_request.output_kind,
            output=None,
            failure_code=code,
            diagnostic=diagnostic or code.value,
            amendment_hash=base_request.amendment_hash,
            official_evidence_eligible=False,
            training_input_hash=(
                request.training_input_hash
                if isinstance(request, DockerResearchRequest)
                else canonical_hash({"training_rows": ()})
            ),
            prediction_input_hash=(
                request.prediction_input_hash
                if isinstance(request, DockerResearchRequest)
                else canonical_hash({"feature_rows": base_request.feature_rows})
            ),
            image_id=self.config.expected_image_id,
            entrypoint_version=(
                request.entrypoint_version if isinstance(request, DockerResearchRequest) else "FEATURE_ONLY_V1"
            ),
        )


def _safe_protocol_diagnostic(stderr: bytes) -> str:
    """Return only exception class and sandbox line, never text or values."""

    matches = _SAFE_EXCEPTION_CLASS.findall(stderr)
    if not matches:
        return ProcessFailureCode.PROTOCOL_FAILED.value
    diagnostic = f"{ProcessFailureCode.PROTOCOL_FAILED.value}:{matches[-1].decode('ascii')}"
    lines = _SAFE_PROGRAM_LINE.findall(stderr)
    return diagnostic if not lines else f"{diagnostic}:line{lines[-1].decode('ascii')}"


__all__ = [
    "DockerResearchConfig",
    "DockerResearchProcessReceipt",
    "DockerResearchRequest",
    "DockerResearchRunner",
    "DockerTrainingObservation",
]


def _build_docker_program(
    request: ResearchProcessRequest,
    encoded_rows: str,
    encoded_training: str,
    entrypoint_version: str,
) -> str:
    """Wrap untrusted output with identities that untrusted code cannot forge.

    The reviewed function returns only contract-specific fields (normally
    ``rows``).  The wrapper supplies the frozen experiment/code/data identity.
    This avoids a circular dependency between source hash and code identity.
    """

    output_class = (
        "DRAFT_RESEARCH_RESULT"
        if request.output_kind in {"ScoreTable", "EventTable"}
        else "AUXILIARY_RESEARCH_EVIDENCE"
    )
    row_contract_type = {
        "ScoreTable": "ScoreRow",
        "EventTable": "EventRow",
        "RegressionTable": "RegressionRow",
        "RegimeComparisonTable": "RegimeComparisonRow",
        "DiagnosticTable": "DiagnosticRow",
        "DatasetAssessmentTable": "DatasetAssessmentRow",
    }.get(request.output_kind)
    metadata = {
        "contract_type": request.output_kind,
        "schema_version": SCHEMA_VERSION,
        "experiment_id": request.experiment_id,
        "code_identity_hash": request.code_identity_hash,
        "data_identity_hash": request.data_identity_hash,
        "output_class": output_class,
    }
    header = "import json as _alphaforge_json\n"
    footer = f'''

if "run_research" not in globals() or not callable(run_research):
    raise RuntimeError("reviewed program must define run_research(rows, seed)")
_alphaforge_rows = _alphaforge_json.loads({encoded_rows!r})
_alphaforge_training = _alphaforge_json.loads({encoded_training!r})
_alphaforge_body = (run_research(_alphaforge_training, _alphaforge_rows, {request.seed})
    if {entrypoint_version!r} == "TRAIN_PREDICT_V2" else run_research(_alphaforge_rows, {request.seed}))
if not isinstance(_alphaforge_body, dict):
    raise RuntimeError("research output body must be an object")
_alphaforge_rows_body = _alphaforge_body.get("rows")
if {row_contract_type!r} is not None:
    if not isinstance(_alphaforge_rows_body, list) or not all(isinstance(_row, dict) for _row in _alphaforge_rows_body):
        raise RuntimeError("research output rows must be an array of objects")
    for _row in _alphaforge_rows_body:
        # Row contract identity is runner-owned just like table identity.  A
        # generated draft supplies business values only and cannot forge or
        # alter the schema identity used by the parser.
        _row["contract_type"] = {row_contract_type!r}
        _row["schema_version"] = {SCHEMA_VERSION!r}
_alphaforge_reserved = {{"contract_type", "schema_version", "experiment_id", "code_identity_hash", "data_identity_hash", "output_class"}}
if _alphaforge_reserved.intersection(_alphaforge_body):
    raise RuntimeError("research code cannot write runner-owned identity fields")
_alphaforge_payload = {metadata!r}
_alphaforge_payload.update(_alphaforge_body)
print({RESULT_PREFIX!r} + _alphaforge_json.dumps(_alphaforge_payload, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")))
'''
    return header + request.code_text.rstrip() + "\n" + footer.lstrip()
