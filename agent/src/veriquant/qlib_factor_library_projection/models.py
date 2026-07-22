"""Read-only contracts for the merged Qlib factor-library qualification view."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Mapping


SCHEMA_VERSION = "veriquant.qlib-factor-library-projection/v2"
_HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_ID_RE = re.compile(r"^[a-z][a-z0-9_]{1,127}$")


def stable_hash(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


class ProjectionFailureCode(str, Enum):
    INPUT_IDENTITY_MISMATCH = "INPUT_IDENTITY_MISMATCH"
    SOURCE_HASH_MISMATCH = "SOURCE_HASH_MISMATCH"
    PARITY_EVIDENCE_INVALID = "PARITY_EVIDENCE_INVALID"
    EXPRESSION_IDENTITY_MISMATCH = "EXPRESSION_IDENTITY_MISMATCH"
    DUPLICATE_FACTOR_ID = "DUPLICATE_FACTOR_ID"
    INVALID_QUERY = "INVALID_QUERY"
    STALE_CURSOR = "STALE_CURSOR"
    FACTOR_NOT_FOUND = "FACTOR_NOT_FOUND"


class ProjectionError(ValueError):
    def __init__(self, code: ProjectionFailureCode, message: str, **details: object) -> None:
        super().__init__(message)
        self.code = code
        self.details = MappingProxyType(dict(details))


class QlibExecutionQualification(str, Enum):
    EXECUTION_READY = "EXECUTION_READY"
    SEMANTICS_REVIEW_REQUIRED = "SEMANTICS_REVIEW_REQUIRED"
    DATA_FIELD_MISSING = "DATA_FIELD_MISSING"
    PIT_METADATA_MISSING = "PIT_METADATA_MISSING"
    AUXILIARY_ONLY = "AUXILIARY_ONLY"


class ProgramOrigin(str, Enum):
    FACTOR_IR_V2 = "FACTOR_IR_V2"
    WHOLE_FORMULA = "WHOLE_FORMULA"
    REVIEWED_TEMPLATE = "REVIEWED_TEMPLATE"
    NONE = "NONE"


@dataclass(frozen=True, slots=True)
class OperatorParityProjection:
    operator: str
    status: str
    policy_hash: str
    reason_codes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.operator or not self.status or _HASH_RE.fullmatch(self.policy_hash) is None:
            raise ProjectionError(
                ProjectionFailureCode.PARITY_EVIDENCE_INVALID,
                "operator parity identity is malformed",
            )
        object.__setattr__(self, "reason_codes", tuple(sorted(set(self.reason_codes))))

    def to_dict(self) -> dict[str, object]:
        return {
            "operator": self.operator,
            "status": self.status,
            "policy_hash": self.policy_hash,
            "reason_codes": list(self.reason_codes),
        }


@dataclass(frozen=True, slots=True)
class QlibFactorProjectionRecord:
    factor_id: str
    zoo: str
    family: str
    themes: tuple[str, ...]
    markets: tuple[str, ...]
    source_relpath: str
    catalog_hash: str
    source_hash: str
    metadata_hash: str
    coverage_report_hash: str
    formula_report_hash: str
    coverage_status: str
    formula_status: str
    execution_qualification: QlibExecutionQualification
    program_origin: ProgramOrigin
    static_union_member: bool
    operator_compatible: bool
    required_fields: tuple[str, ...]
    missing_fields: tuple[str, ...]
    pit_missing_fields: tuple[str, ...]
    maximum_lookback: int | None
    program_hash: str | None
    expression_hash: str | None
    expression_policy_hash: str | None
    operator_manifest_hash: str | None
    canonical_expression: str | None
    operators: tuple[str, ...]
    postprocess_steps: tuple[str, ...]
    factor_ir_hash: str | None
    factor_ir_policy_hash: str | None
    runtime_hash: str | None
    adapter_hash: str | None
    pit_requirement_hash: str | None
    semantic_adapters: tuple[str, ...]
    parity_policy_hash: str
    parity_report_hash: str
    parity: tuple[OperatorParityProjection, ...]
    reason_codes: tuple[str, ...]
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if _ID_RE.fullmatch(self.factor_id) is None or not self.zoo or not self.family:
            raise ProjectionError(ProjectionFailureCode.INPUT_IDENTITY_MISMATCH, "invalid factor identity")
        if "\\" in self.source_relpath or ".." in self.source_relpath.split("/"):
            raise ProjectionError(ProjectionFailureCode.INPUT_IDENTITY_MISMATCH, "unsafe source path")
        for name in (
            "catalog_hash",
            "source_hash",
            "metadata_hash",
            "coverage_report_hash",
            "formula_report_hash",
            "parity_policy_hash",
            "parity_report_hash",
        ):
            if _HASH_RE.fullmatch(getattr(self, name)) is None:
                raise ProjectionError(ProjectionFailureCode.INPUT_IDENTITY_MISMATCH, f"invalid {name}")
        optional_identity = (
            self.program_hash,
            self.expression_hash,
            self.expression_policy_hash,
            self.operator_manifest_hash,
            self.canonical_expression,
        )
        if any(value is not None for value in optional_identity) and not all(
            value is not None for value in optional_identity
        ):
            raise ProjectionError(
                ProjectionFailureCode.EXPRESSION_IDENTITY_MISMATCH,
                "formula program identity is incomplete",
            )
        for value in optional_identity[:4]:
            if value is not None and _HASH_RE.fullmatch(value) is None:
                raise ProjectionError(
                    ProjectionFailureCode.EXPRESSION_IDENTITY_MISMATCH,
                    "formula program hash is malformed",
                )
        factor_ir_identity = (
            self.factor_ir_hash,
            self.factor_ir_policy_hash,
            self.runtime_hash,
            self.adapter_hash,
            self.pit_requirement_hash,
        )
        if any(value is not None for value in factor_ir_identity) and not all(value is not None for value in factor_ir_identity):
            raise ProjectionError(ProjectionFailureCode.EXPRESSION_IDENTITY_MISMATCH, "FactorIR execution identity is incomplete")
        for value in factor_ir_identity:
            if value is not None and _HASH_RE.fullmatch(value) is None:
                raise ProjectionError(ProjectionFailureCode.EXPRESSION_IDENTITY_MISMATCH, "FactorIR execution hash is malformed")
        if self.maximum_lookback is not None and (
            isinstance(self.maximum_lookback, bool) or self.maximum_lookback < 0
        ):
            raise ProjectionError(ProjectionFailureCode.INPUT_IDENTITY_MISMATCH, "invalid lookback")
        for name in (
            "themes",
            "markets",
            "required_fields",
            "missing_fields",
            "pit_missing_fields",
            "operators",
            "postprocess_steps",
            "semantic_adapters",
            "reason_codes",
        ):
            object.__setattr__(self, name, tuple(sorted(set(getattr(self, name)))))
        object.__setattr__(self, "parity", tuple(sorted(self.parity, key=lambda item: item.operator)))
        if self.execution_qualification is QlibExecutionQualification.EXECUTION_READY:
            if self.program_origin is ProgramOrigin.NONE or self.program_hash is None:
                raise ProjectionError(
                    ProjectionFailureCode.EXPRESSION_IDENTITY_MISMATCH,
                    "execution-ready factor requires a complete program",
                )
            if self.missing_fields or self.pit_missing_fields:
                raise ProjectionError(
                    ProjectionFailureCode.INPUT_IDENTITY_MISMATCH,
                    "execution-ready factor cannot have data or PIT gaps",
                )
            if any(
                item.status != "CERTIFIED_EQUIVALENT"
                and {"IdxMax": "SafeIdxMax", "IdxMin": "SafeIdxMin", "If": "SafeIf", "WMA": "SafeWMA"}.get(item.operator)
                not in self.semantic_adapters
                for item in self.parity
            ):
                raise ProjectionError(
                    ProjectionFailureCode.PARITY_EVIDENCE_INVALID,
                    "execution-ready factor contains a non-equivalent parity operator",
                )
            if self.program_origin is not ProgramOrigin.FACTOR_IR_V2 or any(value is None for value in factor_ir_identity):
                raise ProjectionError(ProjectionFailureCode.EXPRESSION_IDENTITY_MISMATCH, "execution-ready factor requires FactorIR v2 identity")

    @property
    def record_hash(self) -> str:
        return stable_hash(self.to_dict())

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "factor_id": self.factor_id,
            "zoo": self.zoo,
            "family": self.family,
            "themes": list(self.themes),
            "markets": list(self.markets),
            "source_relpath": self.source_relpath,
            "catalog_hash": self.catalog_hash,
            "source_hash": self.source_hash,
            "metadata_hash": self.metadata_hash,
            "coverage_report_hash": self.coverage_report_hash,
            "formula_report_hash": self.formula_report_hash,
            "coverage_status": self.coverage_status,
            "formula_status": self.formula_status,
            "execution_qualification": self.execution_qualification.value,
            "program_origin": self.program_origin.value,
            "static_union_member": self.static_union_member,
            "operator_compatible": self.operator_compatible,
            "required_fields": list(self.required_fields),
            "missing_fields": list(self.missing_fields),
            "pit_missing_fields": list(self.pit_missing_fields),
            "maximum_lookback": self.maximum_lookback,
            "program_hash": self.program_hash,
            "expression_hash": self.expression_hash,
            "expression_policy_hash": self.expression_policy_hash,
            "operator_manifest_hash": self.operator_manifest_hash,
            "canonical_expression": self.canonical_expression,
            "operators": list(self.operators),
            "postprocess_steps": list(self.postprocess_steps),
            "factor_ir_hash": self.factor_ir_hash,
            "factor_ir_policy_hash": self.factor_ir_policy_hash,
            "runtime_hash": self.runtime_hash,
            "adapter_hash": self.adapter_hash,
            "pit_requirement_hash": self.pit_requirement_hash,
            "semantic_adapters": list(self.semantic_adapters),
            "parity_policy_hash": self.parity_policy_hash,
            "parity_report_hash": self.parity_report_hash,
            "parity": [item.to_dict() for item in self.parity],
            "reason_codes": list(self.reason_codes),
        }


@dataclass(frozen=True, slots=True)
class QlibFactorLibraryProjection:
    records: tuple[QlibFactorProjectionRecord, ...]
    catalog_hash: str
    coverage_report_hash: str
    formula_report_hash: str
    parity_policy_hash: str
    parity_report_hash: str
    source_counts: Mapping[str, int]
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        ordered = tuple(sorted(self.records, key=lambda item: item.factor_id))
        if len({item.factor_id for item in ordered}) != len(ordered):
            raise ProjectionError(ProjectionFailureCode.DUPLICATE_FACTOR_ID, "duplicate factor id")
        object.__setattr__(self, "records", ordered)
        object.__setattr__(self, "source_counts", MappingProxyType(dict(sorted(self.source_counts.items()))))
        for record in ordered:
            if (
                record.catalog_hash != self.catalog_hash
                or record.coverage_report_hash != self.coverage_report_hash
                or record.formula_report_hash != self.formula_report_hash
                or record.parity_policy_hash != self.parity_policy_hash
                or record.parity_report_hash != self.parity_report_hash
            ):
                raise ProjectionError(
                    ProjectionFailureCode.INPUT_IDENTITY_MISMATCH,
                    "record provenance differs from projection provenance",
                )

    @property
    def counts_by_qualification(self) -> Mapping[str, int]:
        counts = {status.value: 0 for status in QlibExecutionQualification}
        for record in self.records:
            counts[record.execution_qualification.value] += 1
        return MappingProxyType(dict(sorted(counts.items())))

    @property
    def projection_hash(self) -> str:
        return stable_hash(self.stable_payload())

    def stable_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "catalog_hash": self.catalog_hash,
            "coverage_report_hash": self.coverage_report_hash,
            "formula_report_hash": self.formula_report_hash,
            "parity_policy_hash": self.parity_policy_hash,
            "parity_report_hash": self.parity_report_hash,
            "source_counts": dict(self.source_counts),
            "counts_by_qualification": dict(self.counts_by_qualification),
            "records": [record.to_dict() for record in self.records],
        }

    def summary(self) -> dict[str, object]:
        counts = dict(self.counts_by_qualification)
        return {
            "schema_version": self.schema_version,
            "projection_hash": self.projection_hash,
            "catalog_hash": self.catalog_hash,
            "coverage_report_hash": self.coverage_report_hash,
            "formula_report_hash": self.formula_report_hash,
            "parity_policy_hash": self.parity_policy_hash,
            "parity_report_hash": self.parity_report_hash,
            "total_records": len(self.records),
            "source_counts": dict(self.source_counts),
            "counts_by_qualification": counts,
            "review_or_gap_count": sum(
                counts[name]
                for name in (
                    QlibExecutionQualification.SEMANTICS_REVIEW_REQUIRED.value,
                    QlibExecutionQualification.DATA_FIELD_MISSING.value,
                    QlibExecutionQualification.PIT_METADATA_MISSING.value,
                )
            ),
        }


@dataclass(frozen=True, slots=True)
class QlibFactorProjectionPage:
    records: tuple[QlibFactorProjectionRecord, ...]
    total_matching: int
    next_cursor: str | None
    projection_hash: str

    def to_dict(self) -> dict[str, object]:
        return {
            "projection_hash": self.projection_hash,
            "total_matching": self.total_matching,
            "next_cursor": self.next_cursor,
            "records": [record.to_dict() for record in self.records],
        }


__all__ = [
    "OperatorParityProjection",
    "ProgramOrigin",
    "ProjectionError",
    "ProjectionFailureCode",
    "QlibExecutionQualification",
    "QlibFactorLibraryProjection",
    "QlibFactorProjectionPage",
    "QlibFactorProjectionRecord",
    "SCHEMA_VERSION",
    "stable_hash",
]
