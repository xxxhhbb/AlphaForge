"""Fail-closed DTOs for the standalone VeriQuant operator API.

The operator API is a control plane for offline, deterministic VeriQuant jobs.
It deliberately carries no financial truth: no factor payloads, provider
configuration, credentials, promotion verdicts, portfolio targets, orders, or
arbitrary executable input may cross this boundary.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from types import MappingProxyType
from typing import Any, ClassVar, Mapping

from veriquant.contracts import FailureCode
from veriquant.contracts.base import (
    CanonicalContract,
    fail,
    freeze_mapping,
    require_aware_datetime,
    require_identifier,
    require_text,
)


OPERATOR_API_SCHEMA_VERSION = "veriquant.operator-api/v1"
DEFAULT_MAX_STEPS = 20
DEFAULT_MAX_EVENTS = 1_000
DEFAULT_MAX_WALL_CLOCK_SECONDS = 600
SAFE_PUBLIC_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
IDEMPOTENCY_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
TOKEN_LIKE_RE = re.compile(
    r"(?i)(bearer\s+[a-z0-9._~-]{8,}|sk-[a-z0-9_-]{8,}|api[_-]?key|secret|password|private[_ -]?key|tushare[_ -]?token)"
)
URL_RE = re.compile(r"(?i)\b(?:https?|ftp|file)://")


FORBIDDEN_CREATE_FIELDS = frozenset(
    {
        "url",
        "uri",
        "path",
        "file",
        "filepath",
        "file_path",
        "local_path",
        "artifact_path",
        "token",
        "api_key",
        "apikey",
        "authorization",
        "cookie",
        "secret",
        "password",
        "tushare_token",
        "tushare_api_key",
        "python",
        "python_code",
        "code",
        "shell",
        "command",
        "cmd",
        "subprocess",
        "provider",
        "adapter",
        "promotion",
        "promotion_verdict",
        "verdict",
    }
)


class OperatorJobStatus(str, Enum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    CANCELLED = "CANCELLED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class OperatorEventType(str, Enum):
    JOB_CREATED = "JOB_CREATED"
    JOB_RESUMED = "JOB_RESUMED"
    JOB_CANCELLED = "JOB_CANCELLED"
    JOB_COMPLETED = "JOB_COMPLETED"
    JOB_FAILED = "JOB_FAILED"


class OperatorApiError(RuntimeError):
    """Machine-readable error returned by package-local routes."""

    def __init__(self, code: FailureCode, message: str, *, details: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.details = MappingProxyType(dict(details or {}))

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code.value,
            "message": str(self),
            "details": {key: _redact_value(value) for key, value in self.details.items()},
        }


def _fail(code: FailureCode, message: str, *, details: Mapping[str, Any] | None = None) -> None:
    raise OperatorApiError(code, message, details=details)


def _require_public_id(value: str, field_name: str) -> None:
    if not isinstance(value, str) or SAFE_PUBLIC_ID_RE.fullmatch(value) is None:
        _fail(FailureCode.INVALID_IDENTIFIER, f"{field_name} must be a registered public identifier")
    if any(token in value for token in ("..", "/", "\\", ":", "\x00")):
        _fail(FailureCode.INVALID_IDENTIFIER, f"{field_name} must not be a path or URL-like value")


def _require_idempotency_key(value: str) -> None:
    if not isinstance(value, str) or IDEMPOTENCY_KEY_RE.fullmatch(value) is None:
        _fail(FailureCode.INVALID_IDENTIFIER, "idempotency_key must be a stable bounded identifier")
    if _is_forbidden_text(value):
        _fail(FailureCode.FORBIDDEN_PROPOSAL_PAYLOAD, "idempotency_key contains forbidden content")


def _require_positive_bounded_int(value: int, field_name: str, *, max_value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1 or value > max_value:
        _fail(FailureCode.INVALID_VALUE, f"{field_name} must be an integer between 1 and {max_value}")


def _is_forbidden_text(value: str) -> bool:
    return bool(
        URL_RE.search(value)
        or TOKEN_LIKE_RE.search(value)
        or ".." in value
        or "\x00" in value
        or value.strip().lower().startswith(("python ", "python3 ", "cmd ", "powershell ", "bash ", "sh "))
    )


def _redact_value(value: Any) -> Any:
    if isinstance(value, str) and _is_forbidden_text(value):
        return "[REDACTED]"
    if isinstance(value, Mapping):
        return {key: _redact_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact_value(item) for item in value]
    return value


def _scan_forbidden_payload(value: Any, *, path: str = "$") -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            key_text = str(key)
            key_normalized = key_text.strip().lower().replace("-", "_")
            if key_normalized in FORBIDDEN_CREATE_FIELDS:
                _fail(
                    FailureCode.FORBIDDEN_PROPOSAL_PAYLOAD,
                    "operator job payload contains forbidden field",
                    details={"field": key_text, "path": path},
                )
            _scan_forbidden_payload(item, path=f"{path}.{key_text}")
        return
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _scan_forbidden_payload(item, path=f"{path}[{index}]")
        return
    if isinstance(value, str) and _is_forbidden_text(value):
        _fail(
            FailureCode.FORBIDDEN_PROPOSAL_PAYLOAD,
            "operator job payload contains forbidden value",
            details={"path": path, "value": "[REDACTED]"},
        )


@dataclass(frozen=True, kw_only=True)
class OperatorBudget(CanonicalContract):
    SCHEMA_VERSION: ClassVar[str] = OPERATOR_API_SCHEMA_VERSION

    max_steps: int = DEFAULT_MAX_STEPS
    max_events: int = DEFAULT_MAX_EVENTS
    max_wall_clock_seconds: int = DEFAULT_MAX_WALL_CLOCK_SECONDS

    def __post_init__(self) -> None:
        _require_positive_bounded_int(self.max_steps, "budget.max_steps", max_value=DEFAULT_MAX_STEPS)
        _require_positive_bounded_int(self.max_events, "budget.max_events", max_value=DEFAULT_MAX_EVENTS)
        _require_positive_bounded_int(
            self.max_wall_clock_seconds,
            "budget.max_wall_clock_seconds",
            max_value=DEFAULT_MAX_WALL_CLOCK_SECONDS,
        )

    @classmethod
    def from_api_payload(cls, payload: Mapping[str, Any] | None) -> "OperatorBudget":
        if payload is None:
            return cls()
        if not isinstance(payload, Mapping):
            _fail(FailureCode.INVALID_VALUE, "budget must be an object")
        allowed = {"max_steps", "max_events", "max_wall_clock_seconds"}
        unknown = set(payload) - allowed
        if unknown:
            _fail(FailureCode.UNKNOWN_FIELD, "budget contains unknown fields", details={"fields": tuple(sorted(unknown))})
        _scan_forbidden_payload(payload)
        return cls(
            max_steps=payload.get("max_steps", DEFAULT_MAX_STEPS),
            max_events=payload.get("max_events", DEFAULT_MAX_EVENTS),
            max_wall_clock_seconds=payload.get("max_wall_clock_seconds", DEFAULT_MAX_WALL_CLOCK_SECONDS),
        )


@dataclass(frozen=True, kw_only=True)
class OperatorJobCreateRequest(CanonicalContract):
    SCHEMA_VERSION: ClassVar[str] = OPERATOR_API_SCHEMA_VERSION

    snapshot_id: str
    profile_id: str
    seed: int
    budget: OperatorBudget = field(default_factory=OperatorBudget)

    def __post_init__(self) -> None:
        _require_public_id(self.snapshot_id, "snapshot_id")
        _require_public_id(self.profile_id, "profile_id")
        if isinstance(self.seed, bool) or not isinstance(self.seed, int) or self.seed < 0 or self.seed > 2**32 - 1:
            _fail(FailureCode.INVALID_VALUE, "seed must be an integer between 0 and 2**32-1")
        if not isinstance(self.budget, OperatorBudget):
            _fail(FailureCode.INVALID_VALUE, "budget must be OperatorBudget")

    @classmethod
    def from_api_payload(cls, payload: Mapping[str, Any]) -> "OperatorJobCreateRequest":
        if not isinstance(payload, Mapping):
            _fail(FailureCode.INVALID_VALUE, "create job payload must be an object")
        allowed = {"snapshot_id", "profile_id", "seed", "budget"}
        unknown = set(payload) - allowed
        _scan_forbidden_payload(payload)
        if unknown:
            _fail(FailureCode.UNKNOWN_FIELD, "create job payload contains unknown fields", details={"fields": tuple(sorted(unknown))})
        missing = allowed - set(payload)
        missing.discard("budget")
        if missing:
            _fail(FailureCode.MISSING_REQUIRED_FIELD, "create job payload is missing fields", details={"fields": tuple(sorted(missing))})
        return cls(
            snapshot_id=payload["snapshot_id"],
            profile_id=payload["profile_id"],
            seed=payload["seed"],
            budget=OperatorBudget.from_api_payload(payload.get("budget")),
        )

    @staticmethod
    def public_schema() -> dict[str, Any]:
        """Schema fragment for UI/API wiring.

        The schema intentionally exposes no Tushare token, provider, path, URL,
        Python, shell, or promotion-verdict fields.
        """

        return {
            "type": "object",
            "additionalProperties": False,
            "required": ["snapshot_id", "profile_id", "seed"],
            "properties": {
                "snapshot_id": {"type": "string", "description": "Registered frozen snapshot id only."},
                "profile_id": {"type": "string", "description": "Registered offline profile id only."},
                "seed": {"type": "integer", "minimum": 0, "maximum": 2**32 - 1},
                "budget": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "max_steps": {"type": "integer", "minimum": 1, "maximum": DEFAULT_MAX_STEPS},
                        "max_events": {"type": "integer", "minimum": 1, "maximum": DEFAULT_MAX_EVENTS},
                        "max_wall_clock_seconds": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": DEFAULT_MAX_WALL_CLOCK_SECONDS,
                        },
                    },
                },
            },
            "x-veriquant-ui-policy": {
                "tokens": "UI MUST NOT read, render, store, or submit Tushare tokens or other credentials.",
                "network": "offline-only; do not accept URLs, local paths, providers, Python, or shell input.",
            },
        }


@dataclass(frozen=True, kw_only=True)
class RegisteredSnapshot(CanonicalContract):
    SCHEMA_VERSION: ClassVar[str] = OPERATOR_API_SCHEMA_VERSION

    snapshot_id: str
    content_hash: str
    description: str = ""

    def __post_init__(self) -> None:
        _require_public_id(self.snapshot_id, "snapshot_id")
        require_text(self.content_hash, "content_hash")
        if self.description:
            require_text(self.description, "description")


@dataclass(frozen=True, kw_only=True)
class RegisteredProfile(CanonicalContract):
    SCHEMA_VERSION: ClassVar[str] = OPERATOR_API_SCHEMA_VERSION

    profile_id: str
    max_budget: OperatorBudget = field(default_factory=OperatorBudget)
    description: str = ""

    def __post_init__(self) -> None:
        _require_public_id(self.profile_id, "profile_id")
        if not isinstance(self.max_budget, OperatorBudget):
            _fail(FailureCode.INVALID_VALUE, "max_budget must be OperatorBudget")
        if self.description:
            require_text(self.description, "description")


@dataclass(frozen=True, kw_only=True)
class OperatorJob(CanonicalContract):
    SCHEMA_VERSION: ClassVar[str] = OPERATOR_API_SCHEMA_VERSION

    job_id: str
    status: OperatorJobStatus
    snapshot_id: str
    profile_id: str
    seed: int
    budget: OperatorBudget
    created_at: datetime
    updated_at: datetime
    checkpoint_ref: str = ""

    def __post_init__(self) -> None:
        require_identifier(self.job_id, "job_id")
        if not isinstance(self.status, OperatorJobStatus):
            _fail(FailureCode.INVALID_STATE, "status must be OperatorJobStatus")
        _require_public_id(self.snapshot_id, "snapshot_id")
        _require_public_id(self.profile_id, "profile_id")
        if isinstance(self.seed, bool) or not isinstance(self.seed, int):
            _fail(FailureCode.INVALID_VALUE, "seed must be integer")
        if not isinstance(self.budget, OperatorBudget):
            _fail(FailureCode.INVALID_VALUE, "budget must be OperatorBudget")
        require_aware_datetime(self.created_at, "created_at")
        require_aware_datetime(self.updated_at, "updated_at")
        if self.created_at > self.updated_at:
            _fail(FailureCode.INVALID_TIME_ORDER, "created_at must not follow updated_at")
        if self.checkpoint_ref:
            require_identifier(self.checkpoint_ref, "checkpoint_ref")


@dataclass(frozen=True, kw_only=True)
class OperatorJobEvent(CanonicalContract):
    SCHEMA_VERSION: ClassVar[str] = OPERATOR_API_SCHEMA_VERSION

    job_id: str
    sequence: int
    event_type: OperatorEventType
    status: OperatorJobStatus
    occurred_at: datetime
    cursor: str
    message: str = ""
    metadata: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        require_identifier(self.job_id, "job_id")
        if isinstance(self.sequence, bool) or not isinstance(self.sequence, int) or self.sequence < 1:
            _fail(FailureCode.INVALID_VALUE, "sequence must be a positive integer")
        if not isinstance(self.event_type, OperatorEventType):
            _fail(FailureCode.INVALID_VALUE, "event_type must be OperatorEventType")
        if not isinstance(self.status, OperatorJobStatus):
            _fail(FailureCode.INVALID_STATE, "status must be OperatorJobStatus")
        require_aware_datetime(self.occurred_at, "occurred_at")
        require_identifier(self.cursor, "cursor")
        if self.message:
            require_text(self.message, "message")
        redacted = {key: str(_redact_value(value)) for key, value in dict(self.metadata).items()}
        if any(_is_forbidden_text(value) for value in redacted.values()):
            _fail(FailureCode.FORBIDDEN_PROPOSAL_PAYLOAD, "event metadata contains forbidden secret-like value")
        object.__setattr__(self, "metadata", freeze_mapping(redacted, "metadata"))


def utc_now() -> datetime:
    return datetime.now(timezone.utc)

