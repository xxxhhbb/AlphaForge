"""Typed, non-authoritative scheduler for FactorIR execution qualification."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import Enum
from hashlib import sha256
import json


SCHEMA_VERSION = "veriquant.execution-qualification-agent/v1"
PRODUCT_THRESHOLD = 231


def canonical_hash(value: object) -> str:
    return "sha256:" + sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


class TaskStatus(str, Enum):
    QUEUED = "QUEUED"
    BLOCKED = "BLOCKED"
    STATIC_READY = "STATIC_READY"


@dataclass(frozen=True)
class QualificationInput:
    factor_id: str
    family: str
    qualification: str
    reason_codes: tuple[str, ...]
    missing_fields: tuple[str, ...]
    pit_missing_fields: tuple[str, ...]
    static_union_member: bool


@dataclass(frozen=True)
class QualificationTask:
    factor_id: str
    family: str
    status: TaskStatus
    required_evidence: tuple[str, ...]
    blockers: tuple[str, ...]

    def payload(self) -> dict[str, object]:
        return asdict(self) | {"status": self.status.value}


@dataclass(frozen=True)
class QualificationRun:
    projection_hash: str
    counts: dict[str, int]
    tasks: tuple[QualificationTask, ...]

    @property
    def product_gate(self) -> str:
        return "GO" if self.counts.get("EXECUTION_READY", 0) >= PRODUCT_THRESHOLD else "NO_GO"

    def payload(self) -> dict[str, object]:
        body = {
            "schema_version": SCHEMA_VERSION,
            "projection_hash": self.projection_hash,
            "counts": dict(sorted(self.counts.items())),
            "product_gate": self.product_gate,
            "product_threshold": PRODUCT_THRESHOLD,
            "tasks": [task.payload() for task in self.tasks],
            "authority": {
                "may_execute_legacy_python": False,
                "may_access_network": False,
                "may_create_trial": False,
                "may_create_official_evidence": False,
                "may_start_product_loop": False,
            },
        }
        return body | {"run_hash": canonical_hash(body)}
