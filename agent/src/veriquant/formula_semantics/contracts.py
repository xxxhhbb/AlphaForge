"""Canonical contracts for deterministic formula-semantic review."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

from ..agent_research_sandbox.process_runner import canonical_hash


class FormulaFindingSeverity(str, Enum):
    BLOCKING = "BLOCKING"
    REVISION_REQUIRED = "REVISION_REQUIRED"
    ADVISORY = "ADVISORY"


@dataclass(frozen=True, slots=True)
class FeatureSemanticProfile:
    field_name: str
    observation_count: int
    usable_observation_count: int
    decision_group_count: int
    cross_section_group_count: int
    cross_section_constant_fraction: float
    coverage: float

    def __post_init__(self) -> None:
        if not self.field_name or self.observation_count < 1:
            raise ValueError("feature profile requires a field and observations")
        if not 0 <= self.usable_observation_count <= self.observation_count:
            raise ValueError("invalid usable observation count")
        if not 0 <= self.cross_section_group_count <= self.decision_group_count:
            raise ValueError("invalid cross-section group count")
        for value in (self.cross_section_constant_fraction, self.coverage):
            if not 0.0 <= value <= 1.0:
                raise ValueError("profile fractions must be in [0, 1]")

    def to_dict(self) -> dict[str, Any]:
        return {
            "field_name": self.field_name,
            "observation_count": self.observation_count,
            "usable_observation_count": self.usable_observation_count,
            "decision_group_count": self.decision_group_count,
            "cross_section_group_count": self.cross_section_group_count,
            "cross_section_constant_fraction": self.cross_section_constant_fraction,
            "coverage": self.coverage,
        }


@dataclass(frozen=True, slots=True)
class FormulaSemanticFinding:
    code: str
    severity: FormulaFindingSeverity
    field_names: tuple[str, ...]
    message: str

    def __post_init__(self) -> None:
        if not self.code or not self.message.strip():
            raise ValueError("formula finding requires code and message")
        if tuple(sorted(set(self.field_names))) != self.field_names:
            raise ValueError("finding fields must be unique and stable-sorted")

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": self.severity.value,
            "field_names": list(self.field_names),
            "message": self.message,
        }


@dataclass(frozen=True, slots=True)
class FormulaReviewResult:
    source_hash: str
    data_snapshot_hash: str
    profiles: tuple[FeatureSemanticProfile, ...]
    findings: tuple[FormulaSemanticFinding, ...]
    schema_version: str = "alphaforge.formula-semantic-review/v1"

    @property
    def requires_revision(self) -> bool:
        return any(item.severity is FormulaFindingSeverity.REVISION_REQUIRED for item in self.findings)

    @property
    def blocked(self) -> bool:
        return any(item.severity is FormulaFindingSeverity.BLOCKING for item in self.findings)

    def to_dict(self, *, include_hash: bool = True) -> dict[str, Any]:
        value = {
            "schema_version": self.schema_version,
            "source_hash": self.source_hash,
            "data_snapshot_hash": self.data_snapshot_hash,
            "profiles": [item.to_dict() for item in self.profiles],
            "findings": [item.to_dict() for item in self.findings],
            "requires_revision": self.requires_revision,
            "blocked": self.blocked,
        }
        if include_hash:
            value["artifact_hash"] = canonical_hash(value)
        return value

    @property
    def artifact_hash(self) -> str:
        return str(self.to_dict()["artifact_hash"])


__all__ = [
    "FeatureSemanticProfile",
    "FormulaFindingSeverity",
    "FormulaReviewResult",
    "FormulaSemanticFinding",
]
