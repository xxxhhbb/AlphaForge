"""Typed, content-addressed evidence for pinned Qlib semantic parity."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Mapping


PARITY_SCHEMA_VERSION = "veriquant.qlib-semantic-parity/v1"
PARITY_POLICY_VERSION = "veriquant.qlib-semantic-policy/pyqlib-0.9.7-v1"


class SemanticParityStatus(str, Enum):
    CERTIFIED_EQUIVALENT = "CERTIFIED_EQUIVALENT"
    KNOWN_DIFFERENCE = "KNOWN_DIFFERENCE"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"


def stable_hash(value: object) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class OperatorSemanticPolicy:
    operator: str
    semantics: Mapping[str, object]
    schema_version: str = PARITY_SCHEMA_VERSION
    policy_version: str = PARITY_POLICY_VERSION

    def __post_init__(self) -> None:
        if not self.operator or self.schema_version != PARITY_SCHEMA_VERSION:
            raise ValueError("invalid operator semantic policy")
        object.__setattr__(self, "semantics", MappingProxyType(dict(sorted(self.semantics.items()))))

    def to_canonical(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "policy_version": self.policy_version,
            "operator": self.operator,
            "semantics": dict(self.semantics),
        }

    @property
    def policy_hash(self) -> str:
        return stable_hash(self.to_canonical())


@dataclass(frozen=True, slots=True)
class OperatorParityEvidence:
    operator: str
    status: SemanticParityStatus
    policy_hash: str
    cases_total: int
    matched_cases: int
    mismatched_cases: int
    case_hashes: tuple[str, ...]
    difference_codes: tuple[str, ...] = ()
    pyqlib_version: str = "0.9.7"

    def __post_init__(self) -> None:
        if not self.operator or not self.policy_hash.startswith("sha256:"):
            raise ValueError("invalid parity evidence identity")
        if self.cases_total < 0 or self.matched_cases + self.mismatched_cases != self.cases_total:
            raise ValueError("parity case counts are inconsistent")
        if len(self.case_hashes) != self.cases_total or len(set(self.case_hashes)) != len(self.case_hashes):
            raise ValueError("parity evidence requires one unique hash per case")
        if self.status is not SemanticParityStatus.INSUFFICIENT_EVIDENCE and self.cases_total == 0:
            raise ValueError("a parity disposition requires executed cases")
        if self.status is SemanticParityStatus.CERTIFIED_EQUIVALENT and self.mismatched_cases:
            raise ValueError("equivalent status cannot contain mismatches")
        if self.status is SemanticParityStatus.KNOWN_DIFFERENCE and not self.mismatched_cases:
            raise ValueError("known difference requires an observed mismatch")
        if self.status is SemanticParityStatus.INSUFFICIENT_EVIDENCE and self.cases_total:
            raise ValueError("insufficient evidence cannot claim executed cases")

    def to_canonical(self) -> dict[str, object]:
        return {
            "operator": self.operator,
            "status": self.status.value,
            "policy_hash": self.policy_hash,
            "cases_total": self.cases_total,
            "matched_cases": self.matched_cases,
            "mismatched_cases": self.mismatched_cases,
            "case_hashes": list(self.case_hashes),
            "difference_codes": list(self.difference_codes),
            "pyqlib_version": self.pyqlib_version,
        }


PARITY_OPERATORS = (
    "Corr",
    "Cov",
    "IdxMax",
    "IdxMin",
    "If",
    "Max",
    "Min",
    "Quantile",
    "Rank",
    "Rsquare",
    "WMA",
)


@dataclass(frozen=True, slots=True)
class QlibSemanticParityReport:
    evidence: tuple[OperatorParityEvidence, ...]
    runtime_hash: str
    schema_version: str = PARITY_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != PARITY_SCHEMA_VERSION or not self.runtime_hash.startswith("sha256:"):
            raise ValueError("invalid semantic parity report identity")
        names = tuple(item.operator for item in self.evidence)
        if names != tuple(sorted(PARITY_OPERATORS)):
            raise ValueError("semantic parity report must classify every target operator exactly once")

    def to_canonical(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "runtime_hash": self.runtime_hash,
            "evidence": [item.to_canonical() for item in self.evidence],
        }

    @property
    def report_hash(self) -> str:
        return stable_hash(self.to_canonical())
