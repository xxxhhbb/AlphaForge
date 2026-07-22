"""Stable contracts for the read-only AlphaForge research toolbox.

These contracts deliberately carry no network credential, filesystem path,
Evidence authority, or execution permission.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import Enum
import hashlib
import json
import re
from typing import Any, Mapping


SCHEMA_VERSION = "alphaforge.research-toolbox/v1"
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")


def canonical_hash(value: Any) -> str:
    if hasattr(value, "to_dict"):
        value = value.to_dict()
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
        default=lambda item: item.value if isinstance(item, Enum) else item,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


class CoverageStatus(str, Enum):
    OFFICIAL_READY = "OFFICIAL_READY"
    RESEARCH_READY = "RESEARCH_READY"
    UNRESOLVED = "UNRESOLVED"
    NEEDS_EXTERNAL_APPROVAL = "NEEDS_EXTERNAL_APPROVAL"


class CoverageFailureCode(str, Enum):
    CAPABILITY_PROOF_MISSING = "CAPABILITY_PROOF_MISSING"
    GATEWAY_MANIFEST_MISSING = "GATEWAY_MANIFEST_MISSING"
    REQUIRED_SOURCE_MISSING = "REQUIRED_SOURCE_MISSING"
    REQUIRED_SOURCE_EMPTY = "REQUIRED_SOURCE_EMPTY"
    REQUIRED_SOURCE_FAILED = "REQUIRED_SOURCE_FAILED"
    AUXILIARY_ONLY = "AUXILIARY_ONLY"
    REGISTERED_CONTEXT_MISSING = "REGISTERED_CONTEXT_MISSING"
    EXTERNAL_APPROVAL_REQUIRED = "EXTERNAL_APPROVAL_REQUIRED"


class ResearchAuthority(str, Enum):
    OFFICIAL_INPUT = "OFFICIAL_INPUT"
    AUXILIARY_RESEARCH_EVIDENCE = "AUXILIARY_RESEARCH_EVIDENCE"
    REGISTERED_VISIBLE_CONTEXT = "REGISTERED_VISIBLE_CONTEXT"


@dataclass(frozen=True, slots=True)
class RegisteredContextReceipt:
    data_id: str
    content_hash: str
    status: str = "READY"
    authority: ResearchAuthority = ResearchAuthority.REGISTERED_VISIBLE_CONTEXT
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        _safe_identifier(self.data_id, "data_id")
        if not _SHA256.fullmatch(self.content_hash):
            raise ValueError("content_hash must be canonical sha256")
        if self.status not in {"READY", "UNAVAILABLE", "NEEDS_EXTERNAL_APPROVAL"}:
            raise ValueError("invalid registered context status")
        if self.authority is not ResearchAuthority.REGISTERED_VISIBLE_CONTEXT:
            raise ValueError("registered context cannot claim another authority")

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["authority"] = self.authority.value
        return value


@dataclass(frozen=True, slots=True)
class ExternalApprovalNeed:
    data_id: str
    reason_code: str
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        _safe_identifier(self.data_id, "data_id")
        _safe_identifier(self.reason_code, "reason_code")


@dataclass(frozen=True, slots=True)
class DataCoverageDecision:
    required_data: tuple[str, ...]
    status: CoverageStatus
    satisfied_data: tuple[str, ...]
    unresolved_data: tuple[str, ...]
    research_only_data: tuple[str, ...]
    evidence_hashes: tuple[str, ...]
    failure_codes: tuple[CoverageFailureCode, ...]
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        for name in ("required_data", "satisfied_data", "unresolved_data", "research_only_data"):
            values = tuple(getattr(self, name))
            if values != tuple(sorted(set(values))):
                raise ValueError(f"{name} must be unique and stable-sorted")
            for value in values:
                _safe_identifier(value, name)
        if (
            set(self.satisfied_data) & set(self.unresolved_data)
            or set(self.satisfied_data) & set(self.research_only_data)
            or set(self.unresolved_data) & set(self.research_only_data)
        ):
            raise ValueError("coverage partitions must be disjoint")
        if set(self.satisfied_data) | set(self.unresolved_data) | set(self.research_only_data) != set(self.required_data):
            raise ValueError("coverage partitions must exactly cover required_data")
        hashes = tuple(self.evidence_hashes)
        if hashes != tuple(sorted(set(hashes))) or any(not _SHA256.fullmatch(item) for item in hashes):
            raise ValueError("evidence_hashes must be unique stable sha256 values")
        failures = tuple(sorted(set(self.failure_codes), key=lambda item: item.value))
        object.__setattr__(self, "failure_codes", failures)
        object.__setattr__(self, "status", CoverageStatus(self.status))
        if self.status is CoverageStatus.OFFICIAL_READY and (self.unresolved_data or self.research_only_data or failures):
            raise ValueError("OFFICIAL_READY cannot contain gaps or research-only data")

    @property
    def artifact_hash(self) -> str:
        return canonical_hash(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "required_data": list(self.required_data),
            "status": self.status.value,
            "satisfied_data": list(self.satisfied_data),
            "unresolved_data": list(self.unresolved_data),
            "research_only_data": list(self.research_only_data),
            "evidence_hashes": list(self.evidence_hashes),
            "failure_codes": [item.value for item in self.failure_codes],
        }


@dataclass(frozen=True, slots=True)
class ReadOnlyArtifactHandle:
    handle_id: str
    media_type: str
    size_bytes: int
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if not _SHA256.fullmatch(self.handle_id):
            raise ValueError("handle_id must be a content sha256, never a path")
        if not self.media_type or any(char in self.media_type for char in "\r\n"):
            raise ValueError("invalid media_type")
        if isinstance(self.size_bytes, bool) or self.size_bytes < 0:
            raise ValueError("size_bytes must be non-negative")


def _safe_identifier(value: str, name: str) -> None:
    if not isinstance(value, str) or not value or not re.fullmatch(r"[A-Za-z0-9_.:-]+", value):
        raise ValueError(f"{name} must be a non-path identifier")


__all__ = [
    "CoverageFailureCode",
    "CoverageStatus",
    "DataCoverageDecision",
    "ExternalApprovalNeed",
    "ReadOnlyArtifactHandle",
    "RegisteredContextReceipt",
    "ResearchAuthority",
    "SCHEMA_VERSION",
    "canonical_hash",
]
