"""Safe publication contracts for AlphaForge Research OS artifacts."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Any, ClassVar, Mapping

from veriquant.contracts import CanonicalContract, FailureCode
from veriquant.contracts.base import fail, freeze_mapping, require_hash, require_identifier, require_text


RESEARCH_OS_PUBLICATION_SCHEMA_VERSION = "alphaforge.research-os-publication/v1"

_SAFE_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_SAFE_RELATIVE_FILE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_FORBIDDEN_PUBLIC_TERMS = (
    "Desk",
    "desk",
    "Router",
    "router",
    "Swarm",
    "swarm",
    "pipeline",
    "Pipeline",
)
_FORBIDDEN_SENSITIVE_TERMS = (
    "sealed",
    "api_key",
    "authorization",
    "bearer ",
    "token=",
    "token:",
    "secret",
    "OFFICIAL_TUSHARE",
)
_FORBIDDEN_PATH_SEGMENTS = frozenset({"live", "broker", "brokers", "order", "orders"})


class StrEnum(str, Enum):
    """Python 3.10-compatible stable string enum."""


class PublicationStatus(StrEnum):
    PUBLISHED = "PUBLISHED"


class PublicationArtifactKind(StrEnum):
    MACHINE_JSON = "MACHINE_JSON"
    CHINESE_MARKDOWN = "CHINESE_MARKDOWN"
    ARTIFACT_MANIFEST = "ARTIFACT_MANIFEST"
    PUBLICATION_RECEIPT = "PUBLICATION_RECEIPT"
    LATEST_POINTER = "LATEST_POINTER"


def require_safe_run_id(run_id: str, field_name: str = "run_id") -> None:
    """Require a run identifier that cannot escape the publication root."""

    require_identifier(run_id, field_name)
    if _SAFE_RUN_ID.fullmatch(run_id) is None or ".." in run_id:
        fail(FailureCode.INVALID_IDENTIFIER, f"{field_name} is not safe for artifact publication")


def require_safe_relative_file(relative_path: str, field_name: str = "relative_path") -> None:
    """Require a single safe file name under the run publication directory."""

    require_text(relative_path, field_name)
    normalized = relative_path.replace("\\", "/")
    if (
        normalized.startswith("/")
        or "/" in normalized
        or ".." in normalized
        or _SAFE_RELATIVE_FILE.fullmatch(normalized) is None
    ):
        fail(FailureCode.INVALID_IDENTIFIER, f"{field_name} is not a safe publication file name")
    stem_parts = re.split(r"[_.-]+", normalized.lower())
    if any(part in _FORBIDDEN_PATH_SEGMENTS for part in stem_parts):
        fail(FailureCode.UNAUTHORIZED_CAPABILITY, "publication artifact path uses a forbidden live/trading segment")


def assert_publication_payload_safe(value: Any, field_name: str = "payload") -> None:
    """Reject sensitive data and engineering implementation vocabulary in public artifacts."""

    if isinstance(value, str):
        lowered = value.lower()
        for token in _FORBIDDEN_PUBLIC_TERMS:
            if token in value:
                fail(FailureCode.UNAUTHORIZED_CAPABILITY, "publication exposes internal engineering vocabulary", field=field_name)
        for token in _FORBIDDEN_SENSITIVE_TERMS:
            if token.lower() in lowered:
                fail(FailureCode.UNAUTHORIZED_CAPABILITY, "publication exposes sealed or secret material", field=field_name)
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            assert_publication_payload_safe(item, f"{field_name}.{key}")
        return
    if isinstance(value, (tuple, list)):
        for index, item in enumerate(value):
            assert_publication_payload_safe(item, f"{field_name}[{index}]")


def _hash_tuple(value: tuple[str, ...], field_name: str, *, nonempty: bool = False) -> None:
    if not isinstance(value, tuple) or (nonempty and not value):
        fail(FailureCode.INVALID_VALUE, f"{field_name} must be a tuple")
    for item in value:
        require_hash(item, field_name)
    if len(value) != len(set(value)):
        fail(FailureCode.INVALID_VALUE, f"{field_name} contains duplicate hashes")


@dataclass(frozen=True, kw_only=True)
class PublicationArtifactRef(CanonicalContract):
    SCHEMA_VERSION: ClassVar[str] = RESEARCH_OS_PUBLICATION_SCHEMA_VERSION

    kind: PublicationArtifactKind
    relative_path: str
    content_hash: str
    content_type: str
    byte_size: int

    def __post_init__(self) -> None:
        if not isinstance(self.kind, PublicationArtifactKind):
            object.__setattr__(self, "kind", PublicationArtifactKind(self.kind))
        require_safe_relative_file(self.relative_path)
        require_hash(self.content_hash, "content_hash")
        require_identifier(self.content_type, "content_type")
        if isinstance(self.byte_size, bool) or not isinstance(self.byte_size, int) or self.byte_size <= 0:
            fail(FailureCode.INVALID_VALUE, "byte_size must be a positive integer")


@dataclass(frozen=True, kw_only=True)
class ResearchOSArtifactManifest(CanonicalContract):
    SCHEMA_VERSION: ClassVar[str] = RESEARCH_OS_PUBLICATION_SCHEMA_VERSION

    run_id: str
    report_hash: str
    acceptance_report_hash: str
    run_evidence_hash: str
    artifacts: Mapping[str, PublicationArtifactRef] = field(default_factory=dict)

    def __post_init__(self) -> None:
        require_safe_run_id(self.run_id)
        for name in ("report_hash", "acceptance_report_hash", "run_evidence_hash"):
            require_hash(getattr(self, name), name)
        raw = freeze_mapping(self.artifacts, "artifacts")
        normalized: dict[str, PublicationArtifactRef] = {}
        for key, value in raw.items():
            require_identifier(key, "artifacts.key")
            if not isinstance(value, PublicationArtifactRef):
                fail(FailureCode.INVALID_VALUE, "artifacts must contain PublicationArtifactRef values")
            normalized[key] = value
        object.__setattr__(self, "artifacts", MappingProxyType(dict(sorted(normalized.items()))))


@dataclass(frozen=True, kw_only=True)
class ResearchOSPublicationReceipt(CanonicalContract):
    SCHEMA_VERSION: ClassVar[str] = RESEARCH_OS_PUBLICATION_SCHEMA_VERSION

    run_id: str
    status: PublicationStatus
    report_hash: str
    acceptance_report_hash: str
    run_evidence_hash: str
    manifest_hash: str
    artifact_hashes: tuple[str, ...]
    run_relative_root: str

    def __post_init__(self) -> None:
        require_safe_run_id(self.run_id)
        if not isinstance(self.status, PublicationStatus):
            object.__setattr__(self, "status", PublicationStatus(self.status))
        for name in ("report_hash", "acceptance_report_hash", "run_evidence_hash", "manifest_hash"):
            require_hash(getattr(self, name), name)
        _hash_tuple(self.artifact_hashes, "artifact_hashes", nonempty=True)
        require_safe_run_id(self.run_relative_root, "run_relative_root")
        if self.run_relative_root != self.run_id:
            fail(FailureCode.INVALID_VALUE, "run_relative_root must equal run_id")


@dataclass(frozen=True, kw_only=True)
class LatestPublicationPointer(CanonicalContract):
    SCHEMA_VERSION: ClassVar[str] = RESEARCH_OS_PUBLICATION_SCHEMA_VERSION

    run_id: str
    receipt_hash: str
    manifest_hash: str
    report_hash: str

    def __post_init__(self) -> None:
        require_safe_run_id(self.run_id)
        for name in ("receipt_hash", "manifest_hash", "report_hash"):
            require_hash(getattr(self, name), name)

