"""Stable security acceptance models for the VeriQuant-only V1 profile."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping


SCHEMA_VERSION = "veriquant.v1_security/v1"


class SecurityStatus(str, Enum):
    PASSED = "PASSED"
    FAILED = "FAILED"


class SecuritySeverity(str, Enum):
    INFO = "INFO"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"


def _canonical(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Mapping):
        return {str(key): _canonical(value[key]) for key in sorted(value)}
    if isinstance(value, (tuple, list)):
        return [_canonical(item) for item in value]
    if isinstance(value, set):
        return sorted(_canonical(item) for item in value)
    return value


def canonical_hash(value: Any) -> str:
    rendered = json.dumps(_canonical(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(rendered.encode("utf-8")).hexdigest()


def _require_text(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")


def _require_status(value: SecurityStatus | str) -> SecurityStatus:
    if isinstance(value, SecurityStatus):
        return value
    return SecurityStatus(value)


def _require_severity(value: SecuritySeverity | str) -> SecuritySeverity:
    if isinstance(value, SecuritySeverity):
        return value
    return SecuritySeverity(value)


@dataclass(frozen=True, kw_only=True)
class SecurityFinding:
    """One machine-checkable security finding.

    Findings never contain raw secrets or sealed payloads. The evidence hash is
    derived from a redacted, canonical summary of the checked input.
    """

    check_id: str
    status: SecurityStatus | str
    severity: SecuritySeverity | str
    message: str
    evidence_hash: str
    details: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require_text(self.check_id, "check_id")
        _require_text(self.message, "message")
        if not isinstance(self.evidence_hash, str) or not self.evidence_hash.startswith("sha256:"):
            raise ValueError("evidence_hash must be a sha256 hash")
        object.__setattr__(self, "status", _require_status(self.status))
        object.__setattr__(self, "severity", _require_severity(self.severity))
        object.__setattr__(self, "details", MappingProxyType(dict(self.details)))

    @property
    def passed(self) -> bool:
        return self.status is SecurityStatus.PASSED

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "check_id": self.check_id,
            "status": self.status.value,
            "severity": self.severity.value,
            "message": self.message,
            "evidence_hash": self.evidence_hash,
            "details": _canonical(dict(self.details)),
        }


@dataclass(frozen=True, kw_only=True)
class RouteRecord:
    path: str
    methods: tuple[str, ...] = ("GET",)
    authority: str = "READ_ONLY_WORKBENCH"
    handler: str = ""

    def __post_init__(self) -> None:
        _require_text(self.path, "path")
        methods = tuple(str(method).upper() for method in self.methods)
        if not methods:
            raise ValueError("route methods cannot be empty")
        for method in methods:
            _require_text(method, "route method")
        _require_text(self.authority, "authority")
        object.__setattr__(self, "methods", methods)
        object.__setattr__(self, "authority", str(self.authority).upper())
        object.__setattr__(self, "handler", str(self.handler or ""))

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "methods": self.methods,
            "authority": self.authority,
            "handler": self.handler,
        }


@dataclass(frozen=True, kw_only=True)
class ProbeReport:
    profile_id: str
    findings: tuple[SecurityFinding, ...]

    @property
    def passed(self) -> bool:
        return all(finding.passed for finding in self.findings)

    @property
    def artifact_hash(self) -> str:
        return canonical_hash(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "profile_id": self.profile_id,
            "status": SecurityStatus.PASSED.value if self.passed else SecurityStatus.FAILED.value,
            "findings": [finding.to_dict() for finding in self.findings],
        }


def finding(
    *,
    check_id: str,
    passed: bool,
    message: str,
    evidence: Any,
    severity: SecuritySeverity = SecuritySeverity.CRITICAL,
    details: Mapping[str, Any] | None = None,
) -> SecurityFinding:
    return SecurityFinding(
        check_id=check_id,
        status=SecurityStatus.PASSED if passed else SecurityStatus.FAILED,
        severity=severity,
        message=message,
        evidence_hash=canonical_hash(evidence),
        details=details or {},
    )
