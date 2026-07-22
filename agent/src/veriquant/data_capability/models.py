"""Stable, secret-free contracts for research data capability proof."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
import math
from types import MappingProxyType
from typing import Mapping

from ..dynamic_data_planning.models import StableContract, _require_text, _unique_text


SCHEMA_VERSION = "veriquant.data-capability/v1"
APPROVED_SOURCE_LABEL = "APPROVED_PROXY_DATA_VENDOR"


class CapabilityStatus(str, Enum):
    READY = "READY"
    PARTIAL = "PARTIAL"
    UNAVAILABLE = "UNAVAILABLE"


class DataGapCode(str, Enum):
    SOURCE_NOT_APPROVED = "SOURCE_NOT_APPROVED"
    SNAPSHOT_NOT_FROZEN = "SNAPSHOT_NOT_FROZEN"
    FREEZE_MANIFEST_MISSING = "FREEZE_MANIFEST_MISSING"
    HISTORY_TOO_SHORT = "HISTORY_TOO_SHORT"
    CROSS_SECTION_TOO_NARROW = "CROSS_SECTION_TOO_NARROW"
    FIELD_HAS_NO_OBSERVATIONS = "FIELD_HAS_NO_OBSERVATIONS"
    FIELD_COVERAGE_INSUFFICIENT = "FIELD_COVERAGE_INSUFFICIENT"
    AVAILABLE_AT_UNVERIFIED = "AVAILABLE_AT_UNVERIFIED"
    REVISION_SEMANTICS_UNVERIFIED = "REVISION_SEMANTICS_UNVERIFIED"
    SOURCE_PROVENANCE_UNVERIFIED = "SOURCE_PROVENANCE_UNVERIFIED"
    HISTORICAL_UNIVERSE_UNVERIFIED = "HISTORICAL_UNIVERSE_UNVERIFIED"
    LABELS_MISSING = "LABELS_MISSING"
    LABEL_COVERAGE_INSUFFICIENT = "LABEL_COVERAGE_INSUFFICIENT"


@dataclass(frozen=True, kw_only=True)
class DataRequirementPlan(StableContract):
    schema_version: str
    dynamic_plan_hash: str
    question: str
    required_datasets: tuple[str, ...]
    required_fields: tuple[str, ...]
    warmup_sessions: int
    train_sessions: int
    validation_sessions: int
    sealed_sessions: int
    label_horizon_sessions: int
    purge_sessions: int
    embargo_sessions: int
    buffer_sessions: int
    minimum_sessions: int
    minimum_cross_section: int
    minimum_rows: int
    minimum_field_coverage: Mapping[str, float]
    split_policy: str
    source_label: str = APPROVED_SOURCE_LABEL

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError("invalid data-capability schema_version")
        if not self.dynamic_plan_hash.startswith("sha256:"):
            raise ValueError("dynamic_plan_hash must be sha256")
        _require_text(self.question, "question")
        object.__setattr__(self, "required_datasets", _unique_text(self.required_datasets, "required_datasets"))
        object.__setattr__(self, "required_fields", _unique_text(self.required_fields, "required_fields"))
        for name in (
            "warmup_sessions", "train_sessions", "validation_sessions", "sealed_sessions",
            "label_horizon_sessions", "purge_sessions", "embargo_sessions", "buffer_sessions",
            "minimum_sessions", "minimum_cross_section", "minimum_rows",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if min(self.label_horizon_sessions, self.minimum_sessions, self.minimum_cross_section, self.minimum_rows) < 1:
            raise ValueError("label/session/cross-section/row requirements must be positive")
        coverage: dict[str, float] = {}
        for field, value in self.minimum_field_coverage.items():
            _require_text(field, "minimum_field_coverage.key")
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0.0 < float(value) <= 1.0:
                raise ValueError("minimum field coverage must lie in (0,1]")
            coverage[field] = float(value)
        if tuple(sorted(coverage)) != self.required_fields:
            raise ValueError("coverage policy must cover every required field exactly")
        object.__setattr__(self, "minimum_field_coverage", MappingProxyType(dict(sorted(coverage.items()))))
        _require_text(self.split_policy, "split_policy")
        if self.source_label != APPROVED_SOURCE_LABEL:
            raise ValueError("only the approved proxy source label is permitted")


@dataclass(frozen=True, kw_only=True)
class FieldCoverageProof(StableContract):
    field_name: str
    observed_rows: int
    total_rows: int
    observed_sessions: int
    coverage: float
    required_coverage: float
    usable: bool

    def __post_init__(self) -> None:
        _require_text(self.field_name, "field_name")
        for name in ("observed_rows", "total_rows", "observed_sessions"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be non-negative")
        for name in ("coverage", "required_coverage"):
            value = float(getattr(self, name))
            if not math.isfinite(value) or not 0.0 <= value <= 1.0:
                raise ValueError(f"{name} must lie in [0,1]")
        if self.usable != (self.observed_rows > 0 and self.coverage >= self.required_coverage):
            raise ValueError("usable must be derived from observed values, not field-name presence")


@dataclass(frozen=True, kw_only=True)
class DatasetCapabilityReport(StableContract):
    schema_version: str
    requirement_plan_hash: str
    source_label: str
    snapshot_hash: str
    observed_start: datetime | None
    observed_end: datetime | None
    observed_sessions: int
    observed_symbols: int
    minimum_cross_section_observed: int
    median_cross_section_observed: float
    maximum_cross_section_observed: int
    field_proofs: tuple[FieldCoverageProof, ...]
    available_at_verified: bool
    revision_semantics_verified: bool
    source_provenance_verified: bool
    historical_universe_verified: bool
    label_rows: int
    label_coverage: float
    status: CapabilityStatus
    gap_codes: tuple[DataGapCode, ...]

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError("invalid data-capability schema_version")
        for value in (self.requirement_plan_hash, self.snapshot_hash):
            if not value.startswith("sha256:"):
                raise ValueError("report identities must be sha256")
        if self.source_label != APPROVED_SOURCE_LABEL:
            raise ValueError("capability report source must be approved proxy")
        for name in (
            "observed_sessions", "observed_symbols", "minimum_cross_section_observed",
            "maximum_cross_section_observed", "label_rows",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be non-negative")
        if not 0.0 <= float(self.label_coverage) <= 1.0:
            raise ValueError("label_coverage must lie in [0,1]")
        names = tuple(item.field_name for item in self.field_proofs)
        if names != tuple(sorted(names)) or len(names) != len(set(names)):
            raise ValueError("field proofs must be unique and stable-sorted")
        gaps = tuple(sorted({DataGapCode(item) for item in self.gap_codes}, key=lambda item: item.value))
        object.__setattr__(self, "gap_codes", gaps)
        object.__setattr__(self, "status", CapabilityStatus(self.status))
        if self.status is CapabilityStatus.READY and gaps:
            raise ValueError("READY capability cannot contain gaps")
        if self.status is not CapabilityStatus.READY and not gaps:
            raise ValueError("non-ready capability requires typed gaps")


@dataclass(frozen=True, kw_only=True)
class FrozenResearchReceipt(StableContract):
    schema_version: str
    requirement_plan_hash: str
    capability_report_hash: str
    snapshot_hash: str
    labels_hash: str
    freeze_manifest_hash: str | None
    source_label: str
    status: CapabilityStatus
    gap_codes: tuple[DataGapCode, ...]
    official_evidence_authority: bool = False
    network_used: bool = False

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError("invalid data-capability schema_version")
        for value in (self.requirement_plan_hash, self.capability_report_hash, self.snapshot_hash, self.labels_hash):
            if not value.startswith("sha256:"):
                raise ValueError("receipt identities must be sha256")
        if self.freeze_manifest_hash is not None and not self.freeze_manifest_hash.startswith("sha256:"):
            raise ValueError("freeze_manifest_hash must be sha256")
        if self.source_label != APPROVED_SOURCE_LABEL:
            raise ValueError("receipt source must be approved proxy")
        object.__setattr__(self, "status", CapabilityStatus(self.status))
        object.__setattr__(self, "gap_codes", tuple(sorted({DataGapCode(item) for item in self.gap_codes}, key=lambda item: item.value)))
        if self.official_evidence_authority:
            raise ValueError("data receipts have no official Evidence authority")
        if self.network_used:
            raise ValueError("default product-acceptance receipt must be offline")
        if self.status is CapabilityStatus.READY and self.freeze_manifest_hash is None:
            raise ValueError("READY receipt requires a content-addressed freeze manifest")


@dataclass(frozen=True, kw_only=True)
class FreezeExpansionRequest(StableContract):
    requirement_plan_hash: str
    source_label: str
    missing_sessions: int
    missing_fields: tuple[str, ...]
    requested_api_names: tuple[str, ...]
    status: str
    network_allowed: bool = False

    def __post_init__(self) -> None:
        if not self.requirement_plan_hash.startswith("sha256:"):
            raise ValueError("requirement_plan_hash must be sha256")
        if self.source_label != APPROVED_SOURCE_LABEL:
            raise ValueError("expansion source must be approved proxy")
        if isinstance(self.missing_sessions, bool) or not isinstance(self.missing_sessions, int) or self.missing_sessions < 0:
            raise ValueError("missing_sessions must be non-negative")
        object.__setattr__(self, "missing_fields", _unique_text(self.missing_fields, "missing_fields"))
        object.__setattr__(self, "requested_api_names", _unique_text(self.requested_api_names, "requested_api_names"))
        if self.status not in {"NOT_REQUIRED", "OFFLINE_ACQUISITION_REQUIRED", "BLOCKED_NO_APPROVED_LOCAL_SOURCE"}:
            raise ValueError("invalid expansion status")
        if self.network_allowed:
            raise ValueError("data expansion is offline by default")
