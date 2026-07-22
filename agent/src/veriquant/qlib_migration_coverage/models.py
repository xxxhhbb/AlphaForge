"""Stable, machine-readable contracts for factor-library Qlib migration coverage."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping

from ..qlib_expression.models import stable_hash


SCHEMA_VERSION = "veriquant.qlib-migration-coverage/v1"
RULE_VERSION = "veriquant.qlib-migration-coverage-rules/v1"
_HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,254}$")


class MigrationCoverageError(ValueError):
    """Fail-closed coverage scan error with a stable code."""

    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.code = code
        self.details = MappingProxyType(dict(details))


class QlibMigrationStatus(str, Enum):
    QLIB_DIRECT = "QLIB_DIRECT"
    QLIB_WITH_POSTPROCESS = "QLIB_WITH_POSTPROCESS"
    QLIB_SAFE_COMPOSITE = "QLIB_SAFE_COMPOSITE"
    DATA_FIELD_MISSING = "DATA_FIELD_MISSING"
    PIT_METADATA_MISSING = "PIT_METADATA_MISSING"
    SEMANTICS_REVIEW_REQUIRED = "SEMANTICS_REVIEW_REQUIRED"
    AUXILIARY_ONLY = "AUXILIARY_ONLY"


class BacklogDisposition(str, Enum):
    AVAILABLE = "AVAILABLE"
    SAFE_COMPOSITE = "SAFE_COMPOSITE"
    SEMANTICS_PARITY_REQUIRED = "SEMANTICS_PARITY_REQUIRED"
    POSTPROCESS_REQUIRED = "POSTPROCESS_REQUIRED"
    UNSUPPORTED = "UNSUPPORTED"


def _require_hash(value: str, name: str) -> None:
    if not isinstance(value, str) or _HASH_RE.fullmatch(value) is None:
        raise MigrationCoverageError(
            "INVALID_HASH", f"{name} must be sha256:<64 lowercase hex>"
        )


def _require_id(value: str, name: str) -> None:
    if not isinstance(value, str) or _ID_RE.fullmatch(value) is None:
        raise MigrationCoverageError(
            "INVALID_IDENTIFIER", f"{name} is not a stable identifier"
        )


def _unique_sorted(values: tuple[str, ...], name: str) -> tuple[str, ...]:
    if not isinstance(values, tuple) or any(
        not isinstance(item, str) or not item for item in values
    ):
        raise MigrationCoverageError("INVALID_TUPLE", f"{name} must be a string tuple")
    if len(set(values)) != len(values):
        raise MigrationCoverageError("DUPLICATE_VALUE", f"{name} contains duplicates")
    return tuple(sorted(values))


@dataclass(frozen=True, kw_only=True)
class MigrationDataPolicy:
    """Frozen field and PIT-readiness assumptions for one coverage scan."""

    available_fields: tuple[str, ...]
    pit_ready_fields: tuple[str, ...]
    rule_version: str = "approved-frozen-market-fields/v1"

    def __post_init__(self) -> None:
        available = _unique_sorted(self.available_fields, "available_fields")
        pit = _unique_sorted(self.pit_ready_fields, "pit_ready_fields")
        if set(pit) - set(available):
            raise MigrationCoverageError(
                "INVALID_DATA_POLICY", "PIT-ready fields must be available"
            )
        _require_id(self.rule_version, "rule_version")
        object.__setattr__(self, "available_fields", available)
        object.__setattr__(self, "pit_ready_fields", pit)

    @classmethod
    def default(cls) -> "MigrationDataPolicy":
        fields = ("amount", "close", "high", "low", "open", "volume", "vwap")
        return cls(available_fields=fields, pit_ready_fields=fields)

    def to_dict(self) -> dict[str, object]:
        return {
            "rule_version": self.rule_version,
            "available_fields": list(self.available_fields),
            "pit_ready_fields": list(self.pit_ready_fields),
        }

    @property
    def policy_hash(self) -> str:
        return stable_hash(self.to_dict())


@dataclass(frozen=True, kw_only=True)
class QlibMigrationRecord:
    factor_id: str
    zoo: str
    source_relpath: str
    source_hash: str
    metadata_hash: str
    source_formula_hash: str
    baseline_compatibility: str
    legacy_gap_member: bool
    status: QlibMigrationStatus
    required_fields: tuple[str, ...]
    missing_fields: tuple[str, ...]
    pit_missing_fields: tuple[str, ...]
    detected_legacy_operators: tuple[str, ...]
    qlib_operators: tuple[str, ...]
    composite_operators: tuple[str, ...]
    postprocess_steps: tuple[str, ...]
    canonical_expression: str | None
    expression_hash: str | None
    expression_policy_hash: str | None
    operator_compatible: bool
    expression_certified: bool
    data_fields_available: bool
    pit_metadata_available: bool
    execution_eligible: bool
    reason_codes: tuple[str, ...]

    def __post_init__(self) -> None:
        _require_id(self.factor_id, "factor_id")
        _require_id(self.zoo, "zoo")
        if (
            not self.source_relpath
            or "\\" in self.source_relpath
            or ".." in self.source_relpath.split("/")
        ):
            raise MigrationCoverageError(
                "INVALID_PATH", "source_relpath must be a safe relative path"
            )
        for name in ("source_hash", "metadata_hash", "source_formula_hash"):
            _require_hash(getattr(self, name), name)
        if type(self.legacy_gap_member) is not bool:
            raise MigrationCoverageError(
                "INVALID_BOOLEAN", "legacy_gap_member must be boolean"
            )
        if not isinstance(self.status, QlibMigrationStatus):
            object.__setattr__(self, "status", QlibMigrationStatus(self.status))
        for name in (
            "required_fields",
            "missing_fields",
            "pit_missing_fields",
            "detected_legacy_operators",
            "qlib_operators",
            "composite_operators",
            "postprocess_steps",
            "reason_codes",
        ):
            object.__setattr__(self, name, _unique_sorted(getattr(self, name), name))
        if self.expression_hash is not None:
            _require_hash(self.expression_hash, "expression_hash")
        if self.expression_policy_hash is not None:
            _require_hash(self.expression_policy_hash, "expression_policy_hash")
        for name in (
            "operator_compatible",
            "expression_certified",
            "data_fields_available",
            "pit_metadata_available",
            "execution_eligible",
        ):
            if type(getattr(self, name)) is not bool:
                raise MigrationCoverageError(
                    "INVALID_BOOLEAN", f"{name} must be boolean"
                )
        if self.expression_certified != bool(
            self.canonical_expression
            and self.expression_hash
            and self.expression_policy_hash
        ):
            raise MigrationCoverageError(
                "INVALID_EXPRESSION_STATE",
                "certified expression identity is incomplete",
            )
        executable_statuses = {
            QlibMigrationStatus.QLIB_DIRECT,
            QlibMigrationStatus.QLIB_WITH_POSTPROCESS,
            QlibMigrationStatus.QLIB_SAFE_COMPOSITE,
        }
        expected_eligible = (
            self.status in executable_statuses
            and self.expression_certified
            and self.data_fields_available
            and self.pit_metadata_available
        )
        if self.execution_eligible != expected_eligible:
            raise MigrationCoverageError(
                "INVALID_EXECUTION_ELIGIBILITY", "execution eligibility is inconsistent"
            )

    def to_dict(self) -> dict[str, object]:
        return {
            "factor_id": self.factor_id,
            "zoo": self.zoo,
            "source_relpath": self.source_relpath,
            "source_hash": self.source_hash,
            "metadata_hash": self.metadata_hash,
            "source_formula_hash": self.source_formula_hash,
            "baseline_compatibility": self.baseline_compatibility,
            "legacy_gap_member": self.legacy_gap_member,
            "status": self.status.value,
            "required_fields": list(self.required_fields),
            "missing_fields": list(self.missing_fields),
            "pit_missing_fields": list(self.pit_missing_fields),
            "detected_legacy_operators": list(self.detected_legacy_operators),
            "qlib_operators": list(self.qlib_operators),
            "composite_operators": list(self.composite_operators),
            "postprocess_steps": list(self.postprocess_steps),
            "canonical_expression": self.canonical_expression,
            "expression_hash": self.expression_hash,
            "expression_policy_hash": self.expression_policy_hash,
            "operator_compatible": self.operator_compatible,
            "expression_certified": self.expression_certified,
            "data_fields_available": self.data_fields_available,
            "pit_metadata_available": self.pit_metadata_available,
            "execution_eligible": self.execution_eligible,
            "reason_codes": list(self.reason_codes),
        }


@dataclass(frozen=True, kw_only=True)
class OperatorBacklogItem:
    legacy_operator: str
    qlib_mapping: tuple[str, ...]
    disposition: BacklogDisposition
    impacted_factor_ids: tuple[str, ...]
    note: str

    def __post_init__(self) -> None:
        _require_id(self.legacy_operator, "legacy_operator")
        object.__setattr__(
            self, "qlib_mapping", _unique_sorted(self.qlib_mapping, "qlib_mapping")
        )
        object.__setattr__(
            self,
            "impacted_factor_ids",
            _unique_sorted(self.impacted_factor_ids, "impacted_factor_ids"),
        )
        if not isinstance(self.disposition, BacklogDisposition):
            object.__setattr__(
                self, "disposition", BacklogDisposition(self.disposition)
            )
        if not isinstance(self.note, str) or not self.note:
            raise MigrationCoverageError("INVALID_NOTE", "backlog note is required")

    @property
    def impacted_factor_count(self) -> int:
        return len(self.impacted_factor_ids)

    def to_dict(self) -> dict[str, object]:
        return {
            "legacy_operator": self.legacy_operator,
            "qlib_mapping": list(self.qlib_mapping),
            "disposition": self.disposition.value,
            "impacted_factor_count": self.impacted_factor_count,
            "impacted_factor_ids": list(self.impacted_factor_ids),
            "note": self.note,
        }


@dataclass(frozen=True, kw_only=True)
class QlibMigrationCoverageReport:
    catalog_hash: str
    baseline_qualification_hash: str
    operator_manifest_hash: str
    data_policy: MigrationDataPolicy
    records: tuple[QlibMigrationRecord, ...]
    operator_backlog: tuple[OperatorBacklogItem, ...]
    expected_total: int
    expected_legacy_gap_count: int
    schema_version: str = SCHEMA_VERSION
    rule_version: str = RULE_VERSION

    def __post_init__(self) -> None:
        for name in (
            "catalog_hash",
            "baseline_qualification_hash",
            "operator_manifest_hash",
        ):
            _require_hash(getattr(self, name), name)
        if not isinstance(self.data_policy, MigrationDataPolicy):
            raise MigrationCoverageError(
                "INVALID_DATA_POLICY", "data_policy type is invalid"
            )
        ordered = tuple(sorted(self.records, key=lambda item: item.factor_id))
        ids = tuple(item.factor_id for item in ordered)
        if len(set(ids)) != len(ids):
            raise MigrationCoverageError(
                "DUPLICATE_FACTOR_ID", "coverage contains duplicate factor ids"
            )
        if len(ordered) != self.expected_total:
            raise MigrationCoverageError(
                "TOTAL_COUNT_MISMATCH",
                "coverage record count differs from expected scan total",
                expected=self.expected_total,
                actual=len(ordered),
            )
        gap_count = sum(item.legacy_gap_member for item in ordered)
        if gap_count != self.expected_legacy_gap_count:
            raise MigrationCoverageError(
                "LEGACY_GAP_COUNT_MISMATCH",
                "legacy deterministic-operator cohort count changed",
                expected=self.expected_legacy_gap_count,
                actual=gap_count,
            )
        backlog = tuple(
            sorted(
                self.operator_backlog,
                key=lambda item: (-item.impacted_factor_count, item.legacy_operator),
            )
        )
        if len({item.legacy_operator for item in backlog}) != len(backlog):
            raise MigrationCoverageError(
                "DUPLICATE_BACKLOG_OPERATOR", "operator backlog contains duplicates"
            )
        known_ids = set(ids)
        if any(set(item.impacted_factor_ids) - known_ids for item in backlog):
            raise MigrationCoverageError(
                "UNKNOWN_BACKLOG_FACTOR", "backlog references unknown factor"
            )
        object.__setattr__(self, "records", ordered)
        object.__setattr__(self, "operator_backlog", backlog)

    @property
    def counts_by_status(self) -> Mapping[str, int]:
        counts = {status.value: 0 for status in QlibMigrationStatus}
        for record in self.records:
            counts[record.status.value] += 1
        return MappingProxyType(dict(sorted(counts.items())))

    @property
    def legacy_gap_unclassified_count(self) -> int:
        # Status is a closed enum on every record, so a present gap member is classified.
        return 0

    @property
    def operator_compatible_count(self) -> int:
        return sum(record.operator_compatible for record in self.records)

    @property
    def field_available_count(self) -> int:
        return sum(record.data_fields_available for record in self.records)

    @property
    def pit_ready_count(self) -> int:
        return sum(record.pit_metadata_available for record in self.records)

    @property
    def execution_eligible_count(self) -> int:
        return sum(record.execution_eligible for record in self.records)

    def stable_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "rule_version": self.rule_version,
            "catalog_hash": self.catalog_hash,
            "baseline_qualification_hash": self.baseline_qualification_hash,
            "operator_manifest_hash": self.operator_manifest_hash,
            "data_policy": self.data_policy.to_dict(),
            "expected_total": self.expected_total,
            "expected_legacy_gap_count": self.expected_legacy_gap_count,
            "records": [record.to_dict() for record in self.records],
            "operator_backlog": [item.to_dict() for item in self.operator_backlog],
        }

    @property
    def report_hash(self) -> str:
        return stable_hash(self.stable_payload())

    def summary(self) -> dict[str, object]:
        compact_backlog = [
            {
                "legacy_operator": item.legacy_operator,
                "qlib_mapping": list(item.qlib_mapping),
                "disposition": item.disposition.value,
                "impacted_factor_count": item.impacted_factor_count,
                "note": item.note,
            }
            for item in self.operator_backlog
        ]
        return {
            "schema_version": self.schema_version,
            "report_hash": self.report_hash,
            "catalog_hash": self.catalog_hash,
            "baseline_qualification_hash": self.baseline_qualification_hash,
            "operator_manifest_hash": self.operator_manifest_hash,
            "data_policy_hash": self.data_policy.policy_hash,
            "total_records": len(self.records),
            "classified_records": len(self.records),
            "unclassified_records": 0,
            "legacy_gap_records": sum(item.legacy_gap_member for item in self.records),
            "legacy_gap_unclassified_records": self.legacy_gap_unclassified_count,
            "counts_by_status": dict(self.counts_by_status),
            "operator_compatible_count": self.operator_compatible_count,
            "field_available_count": self.field_available_count,
            "pit_ready_count": self.pit_ready_count,
            "execution_eligible_count": self.execution_eligible_count,
            "operator_backlog": compact_backlog,
        }

    def to_dict(self) -> dict[str, object]:
        return {
            **self.stable_payload(),
            "report_hash": self.report_hash,
            "summary": self.summary(),
        }
