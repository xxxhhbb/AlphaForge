"""Versioned, stable models for the local VeriQuant operator security gate."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping


SCHEMA_VERSION = "veriquant.v1-operator-security/v1"


class SecurityStatus(str, Enum):
    PASSED = "PASSED"
    FAILED = "FAILED"


class RouteAuthority(str, Enum):
    READ_ONLY_STATUS = "READ_ONLY_STATUS"
    READ_ONLY_WORKBENCH = "READ_ONLY_WORKBENCH"
    READ_ONLY_OPERATOR_CATALOG = "READ_ONLY_OPERATOR_CATALOG"
    OFFLINE_JOB_CREATE = "OFFLINE_JOB_CREATE"
    OFFLINE_JOB_STATUS = "OFFLINE_JOB_STATUS"
    OFFLINE_JOB_EVENTS = "OFFLINE_JOB_EVENTS"
    OFFLINE_JOB_CONTROL = "OFFLINE_JOB_CONTROL"


def _canonical(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Mapping):
        return {str(key): _canonical(value[key]) for key in sorted(value)}
    if isinstance(value, (tuple, list)):
        return [_canonical(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted(_canonical(item) for item in value)
    return value


def canonical_hash(value: Any) -> str:
    rendered = json.dumps(
        _canonical(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    return "sha256:" + hashlib.sha256(rendered.encode("utf-8")).hexdigest()


@dataclass(frozen=True, kw_only=True)
class RouteRecord:
    method: str
    path: str
    authority: RouteAuthority | str
    financial_authority: bool = False
    operator_auth_required: bool = False

    def __post_init__(self) -> None:
        method = str(self.method).strip().upper()
        path = str(self.path).strip()
        if method not in {"GET", "POST"}:
            raise ValueError("operator route method must be GET or POST")
        if not path.startswith("/") or "?" in path or "#" in path or "\\" in path:
            raise ValueError("operator route path must be canonical")
        authority = self.authority if isinstance(self.authority, RouteAuthority) else RouteAuthority(self.authority)
        if self.financial_authority:
            raise ValueError("local operator routes cannot have financial authority")
        if method == "POST" and not self.operator_auth_required:
            raise ValueError("unsafe operator routes must require operator authentication")
        if method == "GET" and self.operator_auth_required:
            raise ValueError("read-only routes do not require unsafe-method authentication")
        object.__setattr__(self, "method", method)
        object.__setattr__(self, "path", path)
        object.__setattr__(self, "authority", authority)

    @property
    def key(self) -> str:
        return f"{self.method} {self.path}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "method": self.method,
            "path": self.path,
            "authority": self.authority.value,
            "financial_authority": self.financial_authority,
            "operator_auth_required": self.operator_auth_required,
        }


@dataclass(frozen=True, kw_only=True)
class RouteAuthorityManifest:
    profile_id: str
    routes: tuple[RouteRecord, ...]
    bind_host: str
    allowed_origins: tuple[str, ...]
    imported_modules: tuple[str, ...]
    offline_only: bool = True
    provider_keys_required: bool = False
    real_broker_enabled: bool = False
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        routes = tuple(self.routes)
        keys = tuple(route.key for route in routes)
        if len(keys) != len(set(keys)):
            raise ValueError("route manifest contains duplicate method/path keys")
        object.__setattr__(self, "routes", routes)
        object.__setattr__(self, "allowed_origins", tuple(sorted(str(item) for item in self.allowed_origins)))
        object.__setattr__(self, "imported_modules", tuple(sorted(str(item) for item in self.imported_modules)))

    @property
    def artifact_hash(self) -> str:
        return canonical_hash(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "profile_id": self.profile_id,
            "bind_host": self.bind_host,
            "allowed_origins": self.allowed_origins,
            "offline_only": self.offline_only,
            "provider_keys_required": self.provider_keys_required,
            "real_broker_enabled": self.real_broker_enabled,
            "imported_modules": self.imported_modules,
            "routes": [route.to_dict() for route in sorted(self.routes, key=lambda item: item.key)],
        }


@dataclass(frozen=True, kw_only=True)
class SecurityFinding:
    check_id: str
    status: SecurityStatus | str
    message: str
    evidence_hash: str
    details: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.check_id or not self.message:
            raise ValueError("security finding requires an id and message")
        if not str(self.evidence_hash).startswith("sha256:"):
            raise ValueError("security finding evidence must be hashed")
        status = self.status if isinstance(self.status, SecurityStatus) else SecurityStatus(self.status)
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "details", MappingProxyType(dict(self.details)))

    @property
    def passed(self) -> bool:
        return self.status is SecurityStatus.PASSED

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "check_id": self.check_id,
            "status": self.status.value,
            "message": self.message,
            "evidence_hash": self.evidence_hash,
            "details": _canonical(dict(self.details)),
        }


@dataclass(frozen=True, kw_only=True)
class SecurityProbeReport:
    profile_id: str
    findings: tuple[SecurityFinding, ...]
    schema_version: str = SCHEMA_VERSION

    @property
    def passed(self) -> bool:
        return bool(self.findings) and all(finding.passed for finding in self.findings)

    @property
    def artifact_hash(self) -> str:
        return canonical_hash(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "profile_id": self.profile_id,
            "passed": self.passed,
            "findings": [finding.to_dict() for finding in self.findings],
        }


def finding(*, check_id: str, passed: bool, message: str, evidence: Any, details: Mapping[str, Any] | None = None) -> SecurityFinding:
    return SecurityFinding(
        check_id=check_id,
        status=SecurityStatus.PASSED if passed else SecurityStatus.FAILED,
        message=message,
        evidence_hash=canonical_hash(evidence),
        details=details or {},
    )

