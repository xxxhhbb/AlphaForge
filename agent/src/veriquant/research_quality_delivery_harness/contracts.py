"""Versioned state contracts for Research Quality & Delivery Harness v1."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import re
from typing import Any, Mapping

from .storage import canonical_hash, normalize_json


SCHEMA_VERSION = "alphaforge.research-quality-delivery/v1"
_HASH = re.compile(r"^sha256:[0-9a-f]{64}$")


class StrEnum(str, Enum):
    pass


class ExecutionStatus(StrEnum):
    NOT_RUN = "NOT_RUN"
    PREFLIGHT_BLOCKED = "PREFLIGHT_BLOCKED"
    RUNNING = "RUNNING"
    PASSED = "PASSED"
    FAILED = "FAILED"


class EvidenceAuthority(StrEnum):
    NONE = "NONE"
    AUXILIARY_ONLY = "AUXILIARY_ONLY"
    OFFICIAL_EVIDENCE = "OFFICIAL_EVIDENCE"


class ResearchVerdict(StrEnum):
    SUPPORTED = "SUPPORTED"
    CONDITIONALLY_SUPPORTED = "CONDITIONALLY_SUPPORTED"
    REJECTED = "REJECTED"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
    DATA_UNAVAILABLE = "DATA_UNAVAILABLE"


class ReportStatus(StrEnum):
    NOT_BUILT = "NOT_BUILT"
    BUILT = "BUILT"
    QUALIFIED = "QUALIFIED"
    REJECTED = "REJECTED"


class GroupStatus(StrEnum):
    NOT_QUALIFIED = "NOT_QUALIFIED"
    PARTIAL = "PARTIAL"
    QUALIFIED = "QUALIFIED"
    BLOCKED = "BLOCKED"


class HarnessVerdict(StrEnum):
    QUALIFIED = "QUALIFIED"
    PARTIAL = "PARTIAL"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"


@dataclass(frozen=True, slots=True)
class CaseState:
    case_id: str
    trial_id: str
    idempotency_key: str
    group_id: str
    role: str
    output_kind: str
    execution_status: ExecutionStatus = ExecutionStatus.NOT_RUN
    evidence_authority: EvidenceAuthority = EvidenceAuthority.NONE
    research_verdict: ResearchVerdict = ResearchVerdict.INSUFFICIENT_EVIDENCE
    report_status: ReportStatus = ReportStatus.NOT_BUILT
    failure_stage: str | None = None
    failure_code: str | None = None
    provider_attempts: int = 0
    draft_revisions: int = 0
    platform_retries: int = 0
    recovery_cursor: str = "PREFLIGHT"
    artifact_hashes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name in ("case_id", "trial_id", "idempotency_key", "group_id", "role", "output_kind"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} is required")
        for name in ("provider_attempts", "draft_revisions", "platform_retries"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 3:
                raise ValueError(f"{name} must be 0..3")
        if any(_HASH.fullmatch(item) is None for item in self.artifact_hashes):
            raise ValueError("case artifacts must be content hashes")
        if self.evidence_authority is EvidenceAuthority.AUXILIARY_ONLY and self.report_status is ReportStatus.QUALIFIED:
            raise ValueError("auxiliary evidence cannot qualify a formal report")

    def to_dict(self) -> dict[str, Any]:
        value = {
            "schema_version": SCHEMA_VERSION,
            "case_id": self.case_id,
            "trial_id": self.trial_id,
            "idempotency_key": self.idempotency_key,
            "group_id": self.group_id,
            "role": self.role,
            "output_kind": self.output_kind,
            "execution_status": self.execution_status.value,
            "evidence_authority": self.evidence_authority.value,
            "research_verdict": self.research_verdict.value,
            "report_status": self.report_status.value,
            "failure_stage": self.failure_stage,
            "failure_code": self.failure_code,
            "attempt_counters": {
                "provider_attempts": self.provider_attempts,
                "draft_revisions": self.draft_revisions,
                "platform_retries": self.platform_retries,
            },
            "recovery_cursor": self.recovery_cursor,
            "artifact_hashes": list(self.artifact_hashes),
        }
        value["case_hash"] = canonical_hash(value)
        return value


@dataclass(frozen=True, slots=True)
class EvidenceSnapshotV2:
    evidence_id: str
    output_kind: str
    authority: EvidenceAuthority
    verdict: ResearchVerdict
    data_scope: Mapping[str, Any]
    missingness: Mapping[str, Any]
    split: Mapping[str, Any]
    cost_model: Mapping[str, Any]
    metrics: Mapping[str, Any]
    metric_hashes: Mapping[str, str]
    subperiod_results: tuple[Mapping[str, Any], ...]
    regime_results: tuple[Mapping[str, Any], ...]
    layer_results: tuple[Mapping[str, Any], ...]
    robustness: tuple[Mapping[str, Any], ...]
    falsification: tuple[Mapping[str, Any], ...]
    diagnostics: tuple[Mapping[str, Any], ...]
    source_artifact_hashes: tuple[str, ...]
    evaluator_version: str

    def __post_init__(self) -> None:
        if not self.evidence_id or not self.output_kind or not self.evaluator_version:
            raise ValueError("evidence identity is required")
        if set(self.metrics) != set(self.metric_hashes):
            raise ValueError("every metric must have exactly one Evidence hash")
        for name, expected in self.metric_hashes.items():
            if _HASH.fullmatch(expected) is None:
                raise ValueError(f"metric {name} has no content hash")
        if any(_HASH.fullmatch(item) is None for item in self.source_artifact_hashes):
            raise ValueError("Evidence sources must be hashes")

    def to_dict(self, *, include_hash: bool = True) -> dict[str, Any]:
        value = normalize_json({
            "schema_version": "alphaforge.evidence-snapshot/v2",
            "evidence_id": self.evidence_id,
            "output_kind": self.output_kind,
            "authority": self.authority.value,
            "verdict": self.verdict.value,
            "data_scope": dict(self.data_scope),
            "missingness": dict(self.missingness),
            "split": dict(self.split),
            "cost_model": dict(self.cost_model),
            "metrics": dict(self.metrics),
            "metric_hashes": dict(self.metric_hashes),
            "subperiod_results": list(self.subperiod_results),
            "regime_results": list(self.regime_results),
            "layer_results": list(self.layer_results),
            "robustness": list(self.robustness),
            "falsification": list(self.falsification),
            "diagnostics": list(self.diagnostics),
            "source_artifact_hashes": list(self.source_artifact_hashes),
            "evaluator_version": self.evaluator_version,
        })
        if include_hash:
            value["artifact_hash"] = canonical_hash(value)
        return value

    @property
    def artifact_hash(self) -> str:
        return str(self.to_dict()["artifact_hash"])


__all__ = [
    "CaseState", "EvidenceAuthority", "EvidenceSnapshotV2", "ExecutionStatus",
    "GroupStatus", "HarnessVerdict", "ReportStatus", "ResearchVerdict",
    "SCHEMA_VERSION",
]
