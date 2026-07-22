"""Typed, content-addressed contracts for FactorIR data requirements.

This package is a planning/read-model boundary.  It cannot fetch data, grant a
provider approval, calculate Evidence, or promote a factor.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass
from datetime import datetime
from enum import Enum
from typing import Any, Mapping


SCHEMA_VERSION = "veriquant.factor-data-requirements/v1"
PROVIDER_CLASS = "APPROVED_PROXY_DATA_VENDOR"
SOURCE_CLASS = "TUSHARE_COMPATIBLE_PROXY"
APPROVED_PROXY_POLICY_ID = "operator-approved-tushare-compatible-proxy-pit/v1"


class RequirementDisposition(str, Enum):
    DERIVABLE_FROM_APPROVED_MARKET_DATA = "DERIVABLE_FROM_APPROVED_MARKET_DATA"
    AVAILABLE_FROM_APPROVED_PROXY_VENDOR = "AVAILABLE_FROM_APPROVED_PROXY_VENDOR"
    PIT_TIME_UNPROVEN = "PIT_TIME_UNPROVEN"
    SOURCE_UNAVAILABLE = "SOURCE_UNAVAILABLE"
    AUXILIARY_ONLY = "AUXILIARY_ONLY"


class AcquisitionPlanStatus(str, Enum):
    READY_FOR_APPROVED_CAPTURE = "READY_FOR_APPROVED_CAPTURE"
    BLOCKED_PIT_PROOF = "BLOCKED_PIT_PROOF"
    BLOCKED_SOURCE_APPROVAL = "BLOCKED_SOURCE_APPROVAL"
    AUXILIARY_ONLY = "AUXILIARY_ONLY"


class AcquisitionGate(str, Enum):
    APPROVED_PROVIDER_POLICY = "APPROVED_PROVIDER_POLICY"
    APPROVED_API_AND_FIELD_ALLOWLIST = "APPROVED_API_AND_FIELD_ALLOWLIST"
    FROZEN_SNAPSHOT = "FROZEN_SNAPSHOT"
    PUBLISHED_AT = "PUBLISHED_AT"
    AVAILABLE_AT = "AVAILABLE_AT"
    DECISION_TIME = "DECISION_TIME"
    CONTENT_HASH = "CONTENT_HASH"
    ROW_PROVENANCE = "ROW_PROVENANCE"
    REVISION_AS_OF = "REVISION_AS_OF"
    EXACT_FIELD_SEMANTICS = "EXACT_FIELD_SEMANTICS"


class DataRequirementError(ValueError):
    """Fail-closed validation error for the planning boundary."""


def stable_hash(value: Any) -> str:
    payload = json.dumps(_canonical(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _canonical(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return value.isoformat()
    if hasattr(value, "__dataclass_fields__"):
        return _canonical(asdict(value))
    if isinstance(value, Mapping):
        return {str(key): _canonical(value[key]) for key in sorted(value, key=str)}
    if isinstance(value, (tuple, list)):
        return [_canonical(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    raise DataRequirementError(f"unsupported canonical value: {type(value).__name__}")


APPROVED_PROXY_POLICY_HASH = stable_hash(
    {
        "policy_id": APPROVED_PROXY_POLICY_ID,
        "provider_class": PROVIDER_CLASS,
        "source_class": SOURCE_CLASS,
        "frozen_snapshot_required": True,
        "clock_order": "published_at<=available_at<=decision_time",
        "official_tushare_claim": False,
    }
)
APPROVED_PROXY_POLICY_REGISTRY = {APPROVED_PROXY_POLICY_ID: APPROVED_PROXY_POLICY_HASH}


def require_hash(value: str, name: str) -> None:
    if not isinstance(value, str) or len(value) != 71 or not value.startswith("sha256:"):
        raise DataRequirementError(f"{name} must be a sha256 content hash")
    try:
        int(value[7:], 16)
    except ValueError as exc:
        raise DataRequirementError(f"{name} must be a sha256 content hash") from exc


def require_aware(value: datetime, name: str) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise DataRequirementError(f"{name} must be timezone-aware")


@dataclass(frozen=True, slots=True)
class ApprovedProxySnapshotBinding:
    """Proof supplied by integration before a vendor field may enter PIT input."""

    provider_class: str
    source_class: str
    approved_policy_id: str
    approved_policy_hash: str
    snapshot_hash: str
    content_hash: str
    provenance_hash: str
    published_at: datetime
    available_at: datetime
    decision_time: datetime
    frozen: bool

    def __post_init__(self) -> None:
        if self.provider_class != PROVIDER_CLASS or self.source_class != SOURCE_CLASS:
            raise DataRequirementError("source must remain approved proxy / compatible proxy")
        if "OFFICIAL_TUSHARE" in (self.provider_class, self.source_class):
            raise DataRequirementError("approved proxy must not be represented as OFFICIAL_TUSHARE")
        expected_policy_hash = APPROVED_PROXY_POLICY_REGISTRY.get(self.approved_policy_id)
        if expected_policy_hash is None or self.approved_policy_hash != expected_policy_hash:
            raise DataRequirementError("proxy policy is not present in the closed operator-approved registry")
        for name in ("approved_policy_hash", "snapshot_hash", "content_hash", "provenance_hash"):
            require_hash(getattr(self, name), name)
        for name in ("published_at", "available_at", "decision_time"):
            require_aware(getattr(self, name), name)
        if self.published_at > self.available_at or self.available_at > self.decision_time:
            raise DataRequirementError("PIT clocks must satisfy published_at <= available_at <= decision_time")
        if self.frozen is not True:
            raise DataRequirementError("an immutable frozen snapshot is required")

    @property
    def binding_hash(self) -> str:
        return stable_hash(self)


@dataclass(frozen=True, slots=True)
class FactorFieldRequirement:
    factor_id: str
    source_hash: str
    projection_hash: str
    required_fields: tuple[str, ...]
    missing_fields: tuple[str, ...]
    disposition: RequirementDisposition
    approved_apis: tuple[str, ...]
    approved_source_fields: tuple[str, ...]
    derivation: str | None
    pit_requirement: str
    reason_codes: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.factor_id.strip():
            raise DataRequirementError("factor_id is required")
        require_hash(self.source_hash, "source_hash")
        require_hash(self.projection_hash, "projection_hash")
        if not self.required_fields or not self.missing_fields:
            raise DataRequirementError("required_fields and missing_fields are required")
        for name in ("required_fields", "missing_fields", "approved_apis", "approved_source_fields", "reason_codes"):
            values = getattr(self, name)
            if tuple(sorted(set(values))) != values:
                raise DataRequirementError(f"{name} must be sorted and unique")
        if not set(self.missing_fields).issubset(self.required_fields):
            raise DataRequirementError("missing_fields must be a subset of required_fields")
        if not isinstance(self.disposition, RequirementDisposition):
            raise DataRequirementError("disposition must be typed")
        if not self.pit_requirement.strip() or not self.reason_codes:
            raise DataRequirementError("PIT requirement and reason codes are required")
        if self.disposition is RequirementDisposition.AVAILABLE_FROM_APPROVED_PROXY_VENDOR:
            if not self.approved_apis or not self.approved_source_fields:
                raise DataRequirementError("approved proxy disposition requires closed API and source-field mappings")
        elif self.disposition is RequirementDisposition.DERIVABLE_FROM_APPROVED_MARKET_DATA:
            if not self.derivation or self.approved_apis:
                raise DataRequirementError("market-data derivation requires a formula and no vendor API claim")
        elif self.disposition is RequirementDisposition.PIT_TIME_UNPROVEN:
            if self.approved_apis != ("stock_basic",) or self.approved_source_fields != (
                "industry", "industry_available_date", "industry_effective_date"
            ):
                raise DataRequirementError("PIT-unproven classification may reference only the closed industry-history capability")
        elif self.approved_apis or self.approved_source_fields or self.derivation is not None:
            raise DataRequirementError("unavailable or auxiliary dispositions cannot claim data capability")

    @property
    def requirement_hash(self) -> str:
        return stable_hash(self)


@dataclass(frozen=True, slots=True)
class DataAdequacyRequest:
    maximum_lookback: int
    label_horizon: int
    walk_forward_folds: int
    train_sessions_per_fold: int
    validation_sessions_per_fold: int
    warmup: int
    minimum_cross_section: int
    minimum_coverage: float
    minimum_effective_observations: int
    minimum_independent_sessions: int

    def __post_init__(self) -> None:
        integer_fields = (
            "maximum_lookback", "label_horizon", "walk_forward_folds",
            "train_sessions_per_fold", "validation_sessions_per_fold", "warmup",
            "minimum_cross_section", "minimum_effective_observations",
            "minimum_independent_sessions",
        )
        for name in integer_fields:
            value = getattr(self, name)
            lower = 0 if name in {"maximum_lookback", "warmup"} else 1
            if isinstance(value, bool) or not isinstance(value, int) or value < lower:
                raise DataRequirementError(f"{name} must be an integer >= {lower}")
        if not isinstance(self.minimum_coverage, (int, float)) or isinstance(self.minimum_coverage, bool):
            raise DataRequirementError("minimum_coverage must be numeric")
        if not 0.0 < float(self.minimum_coverage) <= 1.0 or not math.isfinite(float(self.minimum_coverage)):
            raise DataRequirementError("minimum_coverage must lie in (0,1]")


@dataclass(frozen=True, slots=True)
class DataAdequacyPlan:
    request_hash: str
    effective_warmup_sessions: int
    analysis_sessions: int
    required_sessions: int
    required_symbols: int
    required_rows: int
    label_safe_sessions: int
    formula: str

    def __post_init__(self) -> None:
        require_hash(self.request_hash, "request_hash")
        for name in (
            "effective_warmup_sessions", "analysis_sessions", "required_sessions",
            "required_symbols", "required_rows", "label_safe_sessions",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise DataRequirementError(f"{name} must be positive")
        if self.required_rows != self.required_sessions * self.required_symbols:
            raise DataRequirementError("required_rows identity is inconsistent")

    @property
    def plan_hash(self) -> str:
        return stable_hash(self)


@dataclass(frozen=True, slots=True)
class FactorDataRequirementReport:
    schema_version: str
    projection_hash: str
    records: tuple[FactorFieldRequirement, ...]

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise DataRequirementError("unsupported schema version")
        require_hash(self.projection_hash, "projection_hash")
        if tuple(sorted(self.records, key=lambda item: item.factor_id)) != self.records:
            raise DataRequirementError("records must be sorted by factor_id")
        if len({item.factor_id for item in self.records}) != len(self.records):
            raise DataRequirementError("duplicate factor_id")
        if any(item.projection_hash != self.projection_hash for item in self.records):
            raise DataRequirementError("record projection identity mismatch")

    @property
    def disposition_counts(self) -> Mapping[str, int]:
        return {
            item.value: sum(record.disposition is item for record in self.records)
            for item in RequirementDisposition
        }

    @property
    def report_hash(self) -> str:
        return stable_hash({
            "schema_version": self.schema_version,
            "projection_hash": self.projection_hash,
            "records": self.records,
        })


@dataclass(frozen=True, slots=True)
class ApprovedAcquisitionStep:
    sequence: int
    api_name: str
    requested_fields: tuple[str, ...]
    parameter_names: tuple[str, ...]
    purpose: str

    def __post_init__(self) -> None:
        if isinstance(self.sequence, bool) or not isinstance(self.sequence, int) or self.sequence < 1:
            raise DataRequirementError("step sequence must be positive")
        if not self.api_name.strip() or not self.purpose.strip():
            raise DataRequirementError("api_name and purpose are required")
        for name in ("requested_fields", "parameter_names"):
            values = getattr(self, name)
            if not values or tuple(sorted(set(values))) != values:
                raise DataRequirementError(f"{name} must be non-empty, sorted, and unique")
        forbidden = {"token", "api_key", "authorization", "url", "endpoint"}
        if forbidden.intersection(name.casefold() for name in self.parameter_names):
            raise DataRequirementError("acquisition parameters contain a forbidden authority surface")


@dataclass(frozen=True, slots=True)
class FactorAcquisitionPlan:
    factor_id: str
    requirement_hash: str
    projection_hash: str
    status: AcquisitionPlanStatus
    steps: tuple[ApprovedAcquisitionStep, ...]
    compliance_gates: tuple[AcquisitionGate, ...]
    terminal_precondition: str
    execution_ready_increment: bool
    authority_note: str

    def __post_init__(self) -> None:
        if not self.factor_id.strip():
            raise DataRequirementError("factor_id is required")
        require_hash(self.requirement_hash, "requirement_hash")
        require_hash(self.projection_hash, "projection_hash")
        if not isinstance(self.status, AcquisitionPlanStatus):
            raise DataRequirementError("acquisition status must be typed")
        if tuple(step.sequence for step in self.steps) != tuple(range(1, len(self.steps) + 1)):
            raise DataRequirementError("acquisition steps must have contiguous sequence identity")
        if tuple(sorted(set(self.compliance_gates), key=lambda item: item.value)) != self.compliance_gates:
            raise DataRequirementError("compliance gates must be sorted and unique")
        if not self.terminal_precondition.strip() or not self.authority_note.strip():
            raise DataRequirementError("terminal precondition and authority note are required")
        if self.execution_ready_increment is not False:
            raise DataRequirementError("a data acquisition plan cannot itself increase execution-ready qualification")
        if self.status is AcquisitionPlanStatus.BLOCKED_SOURCE_APPROVAL and self.steps:
            raise DataRequirementError("source-approval blocker cannot expose executable API steps")

    @property
    def plan_hash(self) -> str:
        return stable_hash(self)


@dataclass(frozen=True, slots=True)
class AcquisitionPlanReport:
    schema_version: str
    requirements_report_hash: str
    projection_hash: str
    plans: tuple[FactorAcquisitionPlan, ...]
    execution_ready_increment_count: int

    def __post_init__(self) -> None:
        if self.schema_version != "veriquant.factor-acquisition-plan/v1":
            raise DataRequirementError("unsupported acquisition plan schema")
        require_hash(self.requirements_report_hash, "requirements_report_hash")
        require_hash(self.projection_hash, "projection_hash")
        if tuple(sorted(self.plans, key=lambda item: item.factor_id)) != self.plans:
            raise DataRequirementError("plans must be sorted by factor_id")
        if len({item.factor_id for item in self.plans}) != len(self.plans):
            raise DataRequirementError("duplicate factor acquisition plan")
        if any(item.projection_hash != self.projection_hash for item in self.plans):
            raise DataRequirementError("acquisition plan projection mismatch")
        measured = sum(item.execution_ready_increment for item in self.plans)
        if self.execution_ready_increment_count != measured:
            raise DataRequirementError("execution-ready increment count is inconsistent")

    @property
    def status_counts(self) -> Mapping[str, int]:
        return {
            status.value: sum(plan.status is status for plan in self.plans)
            for status in AcquisitionPlanStatus
        }

    @property
    def report_hash(self) -> str:
        return stable_hash(self)
