"""Fail-closed auxiliary research sandbox contracts and fake runner."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Any, ClassVar, Mapping

from ..contracts import AuxiliaryEvidence, CanonicalContract, Component, FailureCode
from ..contracts.base import (
    canonicalize,
    fail,
    freeze_mapping,
    require_aware_datetime,
    require_hash,
    require_identifier,
    require_text,
)
from ..research_os import AnswerLevel, AnswerSection


RESEARCH_SANDBOX_SCHEMA_VERSION = "alphaforge.research-sandbox/v1"
_FIXED_CLOCK = datetime(2026, 7, 19, 9, 30, tzinfo=timezone.utc)
_FACTOR_FILE = "factor" + ".py"


class StrEnum(str, Enum):
    """Python 3.10-compatible stable string enum."""


class ResearchSandboxRunStatus(StrEnum):
    COMPLETED_AUXILIARY = "COMPLETED_AUXILIARY"
    BLOCKED = "BLOCKED"
    TIMEOUT = "TIMEOUT"
    RESOURCE_BUDGET_EXCEEDED = "RESOURCE_BUDGET_EXCEEDED"


class ResearchSandboxFailureCode(StrEnum):
    UNSAFE_PAYLOAD = "UNSAFE_PAYLOAD"
    PATH_TRAVERSAL = "PATH_TRAVERSAL"
    NETWORK_FORBIDDEN = "NETWORK_FORBIDDEN"
    SECRET_MATERIAL = "SECRET_MATERIAL"
    DOWNSTREAM_AUTHORITY_FORBIDDEN = "DOWNSTREAM_AUTHORITY_FORBIDDEN"
    CODE_EXECUTION_FORBIDDEN = "CODE_EXECUTION_FORBIDDEN"
    RESOURCE_BUDGET_EXCEEDED = "RESOURCE_BUDGET_EXCEEDED"
    ARTIFACT_WRITE_FAILED = "ARTIFACT_WRITE_FAILED"


_DOWNSTREAM_KEYS = frozenset(
    {
        "official_metrics",
        "official_evidence",
        "promotion",
        "promotion_decision",
        "risk_approval",
        "target_weights",
        "orders",
        "fills",
        "ledger",
        "ledger_entries",
        "broker",
    }
)
_SECRET_KEYS = frozenset({"api_key", "apikey", "authorization", "access_token", "token", "secret", "cookie", "password"})
_SEALED_KEYS = frozenset({"sealed", "sealed_metric", "sealed_metrics", "sealed_rank", "raw_sealed", "test_metrics"})
_NETWORK_TOKENS = ("http://", "https://", "socket.", "requests.", "urllib", "aiohttp")
_SHELL_TOKENS = ("sub" + "process", "power" + "shell", "cmd" + ".exe", "bash", " sh ", "shell", "os.system", "popen")
_CODE_TOKENS = ("__import__", "import ", "from ", "importlib", "open(", "read_text(", "write_text(", _FACTOR_FILE)
_PATH_RE = re.compile(r"(?i)([a-z]:[\\/]|\\\\|(?:^|[\s])\.\.[\\/]|[\\/]\.\.[\\/]|~[\\/])")
_DYNAMIC_CALL_RE = re.compile(r"(?i)\b(?:eval|exec)\s*\(")


def _coerce_enum(value: Any, enum_type: type[Enum], field_name: str) -> Enum:
    if isinstance(value, enum_type):
        return value
    try:
        return enum_type(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} has invalid enum value") from exc


def _text_tuple(
    value: tuple[str, ...],
    field_name: str,
    *,
    required: bool = True,
    check_unsafe: bool = True,
) -> tuple[str, ...]:
    if not isinstance(value, tuple) or (required and not value):
        fail(FailureCode.INVALID_VALUE, f"{field_name} must be a tuple", field=field_name)
    for item in value:
        require_text(item, field_name)
        if check_unsafe:
            _reject_unsafe_text(item, field_name)
    if len(value) != len(set(value)):
        fail(FailureCode.INVALID_VALUE, f"{field_name} contains duplicates", field=field_name)
    return value


def _safe_segment(value: str, field_name: str) -> None:
    require_identifier(value, field_name)
    if ".." in value or "/" in value or "\\" in value:
        fail(
            FailureCode.UNAUTHORIZED_CAPABILITY,
            f"{field_name} must be a safe artifact segment",
            sandbox_failure=ResearchSandboxFailureCode.PATH_TRAVERSAL.value,
        )


def _stable_hash(value: Any) -> str:
    encoded = json.dumps(canonicalize(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _reject_unsafe_text(value: str, field_name: str) -> None:
    lowered = value.lower()
    if _PATH_RE.search(value):
        fail(
            FailureCode.UNAUTHORIZED_CAPABILITY,
            "path-bearing text is not accepted by the research sandbox",
            field=field_name,
            sandbox_failure=ResearchSandboxFailureCode.PATH_TRAVERSAL.value,
        )
    if any(token in lowered for token in _NETWORK_TOKENS):
        fail(
            FailureCode.UNAUTHORIZED_CAPABILITY,
            "network material is not accepted by the research sandbox",
            field=field_name,
            sandbox_failure=ResearchSandboxFailureCode.NETWORK_FORBIDDEN.value,
        )
    if any(token in lowered for token in _SHELL_TOKENS) or _DYNAMIC_CALL_RE.search(value) or any(token in lowered for token in _CODE_TOKENS):
        fail(
            FailureCode.UNAUTHORIZED_CAPABILITY,
            "executable payload is not accepted by the research sandbox",
            field=field_name,
            sandbox_failure=ResearchSandboxFailureCode.CODE_EXECUTION_FORBIDDEN.value,
        )
    if any(token in lowered for token in _SECRET_KEYS):
        fail(
            FailureCode.UNAUTHORIZED_CAPABILITY,
            "secret-like text is not accepted by the research sandbox",
            field=field_name,
            sandbox_failure=ResearchSandboxFailureCode.SECRET_MATERIAL.value,
        )
    if any(token in lowered for token in _SEALED_KEYS) or any(token in lowered for token in _DOWNSTREAM_KEYS):
        fail(
            FailureCode.UNAUTHORIZED_CAPABILITY,
            "sealed or downstream authority text is not accepted by the research sandbox",
            field=field_name,
            sandbox_failure=ResearchSandboxFailureCode.DOWNSTREAM_AUTHORITY_FORBIDDEN.value,
        )


def _validate_payload(value: Any, field_name: str = "research_payload") -> Any:
    if isinstance(value, Mapping):
        out: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str) or not key:
                fail(FailureCode.INVALID_VALUE, "payload keys must be non-empty strings", field=field_name)
            lowered = key.lower()
            if lowered in _DOWNSTREAM_KEYS or lowered in _SEALED_KEYS:
                fail(
                    FailureCode.UNAUTHORIZED_CAPABILITY,
                    "sandbox payload requests forbidden authority",
                    field=f"{field_name}.{key}",
                    sandbox_failure=ResearchSandboxFailureCode.DOWNSTREAM_AUTHORITY_FORBIDDEN.value,
                )
            if lowered in _SECRET_KEYS:
                fail(
                    FailureCode.UNAUTHORIZED_CAPABILITY,
                    "sandbox payload contains secret-like fields",
                    field=f"{field_name}.{key}",
                    sandbox_failure=ResearchSandboxFailureCode.SECRET_MATERIAL.value,
                )
            _reject_unsafe_text(key, f"{field_name}.key")
            out[key] = _validate_payload(item, f"{field_name}.{key}")
        return MappingProxyType(dict(sorted(out.items())))
    if isinstance(value, (tuple, list)):
        return tuple(_validate_payload(item, f"{field_name}[{index}]") for index, item in enumerate(value))
    if isinstance(value, str):
        _reject_unsafe_text(value, field_name)
        return value
    if value is None or type(value) in {bool, int, float}:
        return value
    fail(FailureCode.INVALID_VALUE, f"unsupported sandbox payload type: {type(value).__name__}", field=field_name)


@dataclass(frozen=True, kw_only=True)
class SandboxResourceBudget(CanonicalContract):
    SCHEMA_VERSION: ClassVar[str] = RESEARCH_SANDBOX_SCHEMA_VERSION

    max_runtime_ms: int = 1000
    max_input_bytes: int = 8192
    max_output_bytes: int = 8192
    max_artifacts: int = 3

    def __post_init__(self) -> None:
        for name in ("max_runtime_ms", "max_input_bytes", "max_output_bytes", "max_artifacts"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                fail(FailureCode.INVALID_VALUE, f"{name} must be a positive integer", field=name)
        if self.max_artifacts > 10:
            fail(FailureCode.INVALID_VALUE, "max_artifacts is too broad", field="max_artifacts")


@dataclass(frozen=True, kw_only=True)
class ResearchSandboxProtocol(CanonicalContract):
    SCHEMA_VERSION: ClassVar[str] = RESEARCH_SANDBOX_SCHEMA_VERSION

    protocol_id: str
    campaign_hash: str
    group_id: str
    purpose: str
    input_hashes: tuple[str, ...]
    budget: SandboxResourceBudget = field(default_factory=SandboxResourceBudget)
    offline: bool = True
    allow_network: bool = False
    allow_secret_access: bool = False
    allow_broker_access: bool = False
    trusted_path_enabled: bool = False

    def __post_init__(self) -> None:
        _safe_segment(self.protocol_id, "protocol_id")
        require_hash(self.campaign_hash, "campaign_hash")
        require_identifier(self.group_id, "group_id")
        require_text(self.purpose, "purpose")
        _reject_unsafe_text(self.purpose, "purpose")
        if not isinstance(self.budget, SandboxResourceBudget):
            fail(FailureCode.INVALID_VALUE, "budget must be SandboxResourceBudget", field="budget")
        for value in self.input_hashes:
            require_hash(value, "input_hashes")
        if self.offline is not True:
            fail(FailureCode.UNAUTHORIZED_CAPABILITY, "research sandbox is offline-only")
        for name, failure in (
            ("allow_network", ResearchSandboxFailureCode.NETWORK_FORBIDDEN),
            ("allow_secret_access", ResearchSandboxFailureCode.SECRET_MATERIAL),
            ("allow_broker_access", ResearchSandboxFailureCode.DOWNSTREAM_AUTHORITY_FORBIDDEN),
            ("trusted_path_enabled", ResearchSandboxFailureCode.DOWNSTREAM_AUTHORITY_FORBIDDEN),
        ):
            if getattr(self, name) is not False:
                fail(
                    FailureCode.UNAUTHORIZED_CAPABILITY,
                    f"{name} must be false",
                    field=name,
                    sandbox_failure=failure.value,
                )


@dataclass(frozen=True, kw_only=True)
class ResearchSandboxArtifactRef(CanonicalContract):
    SCHEMA_VERSION: ClassVar[str] = RESEARCH_SANDBOX_SCHEMA_VERSION

    artifact_id: str
    relative_path: str
    content_hash: str
    byte_size: int
    media_type: str

    def __post_init__(self) -> None:
        _safe_segment(self.artifact_id, "artifact_id")
        require_text(self.relative_path, "relative_path")
        if self.relative_path.startswith(("/", "\\")) or ".." in self.relative_path or "\\" in self.relative_path:
            fail(
                FailureCode.UNAUTHORIZED_CAPABILITY,
                "artifact relative path is unsafe",
                sandbox_failure=ResearchSandboxFailureCode.PATH_TRAVERSAL.value,
            )
        require_hash(self.content_hash, "content_hash")
        if isinstance(self.byte_size, bool) or not isinstance(self.byte_size, int) or self.byte_size <= 0:
            fail(FailureCode.INVALID_VALUE, "byte_size must be positive", field="byte_size")
        require_identifier(self.media_type, "media_type")


@dataclass(frozen=True, kw_only=True)
class ResearchSandboxRunRequest(CanonicalContract):
    SCHEMA_VERSION: ClassVar[str] = RESEARCH_SANDBOX_SCHEMA_VERSION

    run_id: str
    protocol: ResearchSandboxProtocol
    prompt_hash: str
    research_payload: Mapping[str, Any]
    requested_outputs: tuple[str, ...] = ("auxiliary_summary",)
    generated_code_text: str | None = None

    def __post_init__(self) -> None:
        _safe_segment(self.run_id, "run_id")
        if not isinstance(self.protocol, ResearchSandboxProtocol):
            fail(FailureCode.INVALID_VALUE, "protocol must be ResearchSandboxProtocol", field="protocol")
        require_hash(self.prompt_hash, "prompt_hash")
        object.__setattr__(self, "research_payload", _validate_payload(self.research_payload))
        _text_tuple(self.requested_outputs, "requested_outputs")
        forbidden_outputs = {"official_evidence", "promotion", "portfolio", "risk", "orders", "ledger"}
        if any(item.lower() in forbidden_outputs for item in self.requested_outputs):
            fail(
                FailureCode.UNAUTHORIZED_CAPABILITY,
                "sandbox cannot request downstream outputs",
                sandbox_failure=ResearchSandboxFailureCode.DOWNSTREAM_AUTHORITY_FORBIDDEN.value,
            )
        if self.generated_code_text is not None:
            require_text(self.generated_code_text, "generated_code_text")
            _reject_unsafe_text(self.generated_code_text, "generated_code_text")


@dataclass(frozen=True, kw_only=True)
class ResearchSandboxRunResult(CanonicalContract):
    SCHEMA_VERSION: ClassVar[str] = RESEARCH_SANDBOX_SCHEMA_VERSION

    run_id: str
    status: ResearchSandboxRunStatus
    protocol_hash: str
    output_hash: str
    answer_section: AnswerSection | None
    auxiliary_evidence: AuxiliaryEvidence | None
    artifact_refs: tuple[ResearchSandboxArtifactRef, ...] = ()
    limitations: tuple[str, ...] = ()
    failure_code: ResearchSandboxFailureCode | None = None
    completed_at: datetime = _FIXED_CLOCK

    def __post_init__(self) -> None:
        _safe_segment(self.run_id, "run_id")
        object.__setattr__(self, "status", _coerce_enum(self.status, ResearchSandboxRunStatus, "status"))
        require_hash(self.protocol_hash, "protocol_hash")
        require_hash(self.output_hash, "output_hash")
        if not isinstance(self.artifact_refs, tuple):
            fail(FailureCode.INVALID_VALUE, "artifact_refs must be a tuple", field="artifact_refs")
        for ref in self.artifact_refs:
            if not isinstance(ref, ResearchSandboxArtifactRef):
                fail(FailureCode.INVALID_VALUE, "artifact_refs must contain ResearchSandboxArtifactRef")
        _text_tuple(self.limitations, "limitations", check_unsafe=False)
        if self.failure_code is not None:
            object.__setattr__(self, "failure_code", _coerce_enum(self.failure_code, ResearchSandboxFailureCode, "failure_code"))
        require_aware_datetime(self.completed_at, "completed_at")
        if self.status is ResearchSandboxRunStatus.COMPLETED_AUXILIARY:
            if self.answer_section is None or self.auxiliary_evidence is None:
                fail(FailureCode.INCOMPLETE_RESULT, "completed sandbox result requires auxiliary artifacts")
            if self.answer_section.level is not AnswerLevel.AUXILIARY_RESEARCH:
                fail(FailureCode.AUXILIARY_MASQUERADE, "sandbox answer must remain auxiliary")
            if self.failure_code is not None:
                fail(FailureCode.INVALID_STATE, "completed sandbox result cannot carry failure")
        elif self.failure_code is None:
            fail(FailureCode.INVALID_STATE, "non-completed sandbox result requires failure_code")


class FakeAuxiliaryResearchSandboxRunner:
    """Deterministic offline runner that never executes submitted code."""

    def __init__(self, *, artifact_root: Path, clock: datetime = _FIXED_CLOCK) -> None:
        require_aware_datetime(clock, "clock")
        self.artifact_root = Path(artifact_root).resolve()
        self.clock = clock

    def run(self, request: ResearchSandboxRunRequest) -> ResearchSandboxRunResult:
        encoded_request = request.canonical_bytes()
        if len(encoded_request) > request.protocol.budget.max_input_bytes:
            return self._blocked(request, ResearchSandboxRunStatus.RESOURCE_BUDGET_EXCEEDED, ResearchSandboxFailureCode.RESOURCE_BUDGET_EXCEEDED)
        if request.protocol.budget.max_runtime_ms < 10:
            return self._blocked(request, ResearchSandboxRunStatus.TIMEOUT, ResearchSandboxFailureCode.RESOURCE_BUDGET_EXCEEDED)
        payload_hash = _stable_hash(request.research_payload)
        code_hash = _stable_hash({"generated_code_text": request.generated_code_text}) if request.generated_code_text is not None else None
        limitations = (
            "This sandbox result is AUXILIARY_RESEARCH only.",
            "No generated code was executed or admitted to the trusted FactorDAG path.",
            "No network, secret, broker, order, promotion, risk, or ledger authority was available.",
        )
        summary = {
            "schema_version": RESEARCH_SANDBOX_SCHEMA_VERSION,
            "run_id": request.run_id,
            "authority_class": "AUXILIARY_RESEARCH_EVIDENCE",
            "campaign_hash": request.protocol.campaign_hash,
            "group_id": request.protocol.group_id,
            "payload_hash": payload_hash,
            "prompt_hash": request.prompt_hash,
            "generated_code_hash": code_hash,
            "code_disposition": "TEXT_ONLY_NOT_EXECUTED" if code_hash else "NO_CODE",
            "limitations": limitations,
        }
        output_hash = _stable_hash(summary)
        if len(json.dumps(summary, ensure_ascii=False, sort_keys=True).encode("utf-8")) > request.protocol.budget.max_output_bytes:
            return self._blocked(request, ResearchSandboxRunStatus.RESOURCE_BUDGET_EXCEEDED, ResearchSandboxFailureCode.RESOURCE_BUDGET_EXCEEDED)
        run_dir = self._run_dir(request.run_id)
        json_path = run_dir / "sandbox_summary.json"
        md_path = run_dir / "sandbox_summary.md"
        try:
            _atomic_json(json_path, summary)
            _atomic_text(md_path, self._markdown(summary))
        except OSError:
            return self._blocked(request, ResearchSandboxRunStatus.BLOCKED, ResearchSandboxFailureCode.ARTIFACT_WRITE_FAILED)
        refs = (
            self._artifact_ref("summary_json", json_path, "application/json"),
            self._artifact_ref("summary_md", md_path, "text/markdown"),
        )
        if len(refs) > request.protocol.budget.max_artifacts:
            return self._blocked(request, ResearchSandboxRunStatus.RESOURCE_BUDGET_EXCEEDED, ResearchSandboxFailureCode.RESOURCE_BUDGET_EXCEEDED)
        answer = AnswerSection(
            title="沙箱辅助研究摘要",
            level=AnswerLevel.AUXILIARY_RESEARCH,
            authority="isolated_sandbox",
            content="已完成隔离沙箱推演；结果仅可作为候选研究线索，不能替代机器证据。",
            artifact_hashes=tuple(ref.content_hash for ref in refs),
            limitations=limitations,
        )
        auxiliary = AuxiliaryEvidence(
            evidence_id=f"aux.{request.run_id}",
            source_component=Component.RESEARCH_DESK,
            experiment_identity_hash=request.protocol.artifact_hash,
            metrics={
                "sandbox_inputs": float(len(request.protocol.input_hashes)),
                "payload_bytes": float(len(encoded_request)),
                "generated_code_text_present": 1.0 if request.generated_code_text is not None else 0.0,
            },
            artifact_hashes=(answer.artifact_hash,),
            event_time=self.clock,
            published_at=self.clock,
            available_at=self.clock,
        )
        result = ResearchSandboxRunResult(
            run_id=request.run_id,
            status=ResearchSandboxRunStatus.COMPLETED_AUXILIARY,
            protocol_hash=request.protocol.artifact_hash,
            output_hash=output_hash,
            answer_section=answer,
            auxiliary_evidence=auxiliary,
            artifact_refs=refs,
            limitations=limitations,
            completed_at=self.clock,
        )
        _atomic_json(run_dir / "sandbox_result.json", result.to_dict())
        return result

    def _blocked(
        self,
        request: ResearchSandboxRunRequest,
        status: ResearchSandboxRunStatus,
        failure_code: ResearchSandboxFailureCode,
    ) -> ResearchSandboxRunResult:
        limitations = (
            "Sandbox failed closed before producing auxiliary research.",
            "No official evidence, promotion, portfolio, risk, order, broker, or ledger action occurred.",
        )
        return ResearchSandboxRunResult(
            run_id=request.run_id,
            status=status,
            protocol_hash=request.protocol.artifact_hash,
            output_hash=_stable_hash({"run_id": request.run_id, "status": status.value, "failure": failure_code.value}),
            answer_section=None,
            auxiliary_evidence=None,
            artifact_refs=(),
            limitations=limitations,
            failure_code=failure_code,
            completed_at=self.clock,
        )

    def _run_dir(self, run_id: str) -> Path:
        _safe_segment(run_id, "run_id")
        target = (self.artifact_root / run_id).resolve()
        try:
            target.relative_to(self.artifact_root)
        except ValueError as exc:
            raise ValueError("sandbox artifact traversal rejected") from exc
        return target

    def _artifact_ref(self, artifact_id: str, path: Path, media_type: str) -> ResearchSandboxArtifactRef:
        relative = path.resolve().relative_to(self.artifact_root).as_posix()
        return ResearchSandboxArtifactRef(
            artifact_id=artifact_id,
            relative_path=relative,
            content_hash=_file_hash(path),
            byte_size=path.stat().st_size,
            media_type=media_type,
        )

    @staticmethod
    def _markdown(summary: Mapping[str, Any]) -> str:
        return "\n".join(
            [
                "# AlphaForge 沙箱辅助研究",
                "",
                f"- 运行 ID: `{summary['run_id']}`",
                "- 证据级别: `AUXILIARY_RESEARCH_EVIDENCE`",
                f"- 研究小组: `{summary['group_id']}`",
                f"- Payload hash: `{summary['payload_hash']}`",
                f"- Code disposition: `{summary['code_disposition']}`",
                "",
                "本结果只用于形成研究线索，不能进入正式机器证据、晋级、组合、风控、下单或账本路径。",
            ]
        )


def _file_hash(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    rendered = json.dumps(canonicalize(payload), ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n"
    _atomic_text(path, rendered)


def _atomic_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile("w", encoding="utf-8", newline="\n", delete=False, dir=str(path.parent))
    tmp_name = handle.name
    try:
        handle.write(text.rstrip() + "\n")
        handle.flush()
        os.fsync(handle.fileno())
        handle.close()
        os.replace(tmp_name, path)
    finally:
        if not handle.closed:
            handle.close()
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
