"""Typed artifacts for pure-static legacy formula migration."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping

from ..qlib_expression.models import stable_hash


SCHEMA_VERSION = "veriquant.qlib-formula-migration/v1"
RULE_VERSION = "veriquant.qlib-formula-static-translator/v1"
_HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


class FormulaMigrationFailureCode(str, Enum):
    ROOT_UNAVAILABLE = "ROOT_UNAVAILABLE"
    PATH_TRAVERSAL = "PATH_TRAVERSAL"
    INVALID_PATH = "INVALID_PATH"
    SOURCE_HASH_MISMATCH = "SOURCE_HASH_MISMATCH"
    MALFORMED_SOURCE = "MALFORMED_SOURCE"
    SOURCE_TOO_LARGE = "SOURCE_TOO_LARGE"
    DUPLICATE_FACTOR_ID = "DUPLICATE_FACTOR_ID"
    CATALOG_MISMATCH = "CATALOG_MISMATCH"
    COMPUTE_MISSING = "COMPUTE_MISSING"
    MULTIPLE_COMPUTE = "MULTIPLE_COMPUTE"
    UNSUPPORTED_STATEMENT = "UNSUPPORTED_STATEMENT"
    UNSUPPORTED_NODE = "UNSUPPORTED_NODE"
    UNKNOWN_NAME = "UNKNOWN_NAME"
    UNKNOWN_CALL = "UNKNOWN_CALL"
    DYNAMIC_SUBSCRIPT = "DYNAMIC_SUBSCRIPT"
    INVALID_ARGUMENT = "INVALID_ARGUMENT"
    FUTURE_REFERENCE = "FUTURE_REFERENCE"
    CROSS_SECTIONAL_INTERMEDIATE = "CROSS_SECTIONAL_INTERMEDIATE"
    FIELD_NAME_UNSUPPORTED = "FIELD_NAME_UNSUPPORTED"
    CERTIFICATION_FAILED = "CERTIFICATION_FAILED"
    SEMANTICS_PARITY_REQUIRED = "SEMANTICS_PARITY_REQUIRED"
    AUXILIARY_BASELINE = "AUXILIARY_BASELINE"


class FormulaMigrationStatus(str, Enum):
    CERTIFIED_QLIB_EXPRESSION = "CERTIFIED_QLIB_EXPRESSION"
    CERTIFIED_WITH_POSTPROCESS = "CERTIFIED_WITH_POSTPROCESS"
    SEMANTICS_REVIEW_REQUIRED = "SEMANTICS_REVIEW_REQUIRED"
    AUXILIARY_ONLY = "AUXILIARY_ONLY"


class FormulaMigrationError(ValueError):
    def __init__(
        self,
        code: FormulaMigrationFailureCode | str,
        message: str,
        **details: Any,
    ) -> None:
        super().__init__(message)
        self.code = (
            code
            if isinstance(code, FormulaMigrationFailureCode)
            else FormulaMigrationFailureCode(code)
        )
        self.details = MappingProxyType(dict(details))


def _require_hash(value: str, name: str) -> None:
    if not isinstance(value, str) or _HASH_RE.fullmatch(value) is None:
        raise FormulaMigrationError(
            FormulaMigrationFailureCode.CERTIFICATION_FAILED,
            f"{name} must be a canonical SHA-256 hash",
        )


def _sorted_unique(values: tuple[str, ...]) -> tuple[str, ...]:
    if not isinstance(values, tuple) or any(
        not isinstance(value, str) or not value for value in values
    ):
        raise FormulaMigrationError(
            FormulaMigrationFailureCode.CERTIFICATION_FAILED,
            "string tuple is malformed",
        )
    if len(set(values)) != len(values):
        raise FormulaMigrationError(
            FormulaMigrationFailureCode.CERTIFICATION_FAILED,
            "string tuple contains duplicates",
        )
    return tuple(sorted(values))


@dataclass(frozen=True, kw_only=True)
class FormulaMigrationRecord:
    factor_id: str
    zoo: str
    source_relpath: str
    source_hash: str
    metadata_hash: str
    formula_hash: str
    program_hash: str
    status: FormulaMigrationStatus
    required_fields: tuple[str, ...]
    maximum_lookback: int
    canonical_expression: str | None
    expression_hash: str | None
    expression_policy_hash: str | None
    operator_manifest_hash: str | None
    operators: tuple[str, ...]
    postprocess_steps: tuple[str, ...]
    reason_codes: tuple[str, ...]
    source_formula: str | None = None
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if not self.factor_id or not self.zoo:
            raise FormulaMigrationError(
                FormulaMigrationFailureCode.CERTIFICATION_FAILED,
                "factor identity is required",
            )
        if (
            not self.source_relpath
            or "\\" in self.source_relpath
            or ".." in self.source_relpath.split("/")
        ):
            raise FormulaMigrationError(
                FormulaMigrationFailureCode.PATH_TRAVERSAL,
                "source path must be a safe relative path",
            )
        for name in ("source_hash", "metadata_hash", "formula_hash", "program_hash"):
            _require_hash(getattr(self, name), name)
        if not isinstance(self.status, FormulaMigrationStatus):
            object.__setattr__(self, "status", FormulaMigrationStatus(self.status))
        if isinstance(self.maximum_lookback, bool) or self.maximum_lookback < 0:
            raise FormulaMigrationError(
                FormulaMigrationFailureCode.CERTIFICATION_FAILED,
                "maximum lookback must be non-negative",
            )
        for name in ("required_fields", "operators", "postprocess_steps", "reason_codes"):
            object.__setattr__(self, name, _sorted_unique(getattr(self, name)))
        expression_values = (
            self.canonical_expression,
            self.expression_hash,
            self.expression_policy_hash,
            self.operator_manifest_hash,
        )
        if any(value is not None for value in expression_values) and not all(
            value is not None for value in expression_values
        ):
            raise FormulaMigrationError(
                FormulaMigrationFailureCode.CERTIFICATION_FAILED,
                "certified expression identity is incomplete",
            )
        for name in ("expression_hash", "expression_policy_hash", "operator_manifest_hash"):
            value = getattr(self, name)
            if value is not None:
                _require_hash(value, name)
        if self.status in {
            FormulaMigrationStatus.CERTIFIED_QLIB_EXPRESSION,
            FormulaMigrationStatus.CERTIFIED_WITH_POSTPROCESS,
        } and self.canonical_expression is None:
            raise FormulaMigrationError(
                FormulaMigrationFailureCode.CERTIFICATION_FAILED,
                "certified status requires a certified expression",
            )
        expected_formula_hash = stable_hash(
            {
                "source_hash": self.source_hash,
                "source_formula": self.source_formula
                if self.source_formula is not None
                else "UNAVAILABLE",
            }
        )
        if self.formula_hash != expected_formula_hash:
            raise FormulaMigrationError(
                FormulaMigrationFailureCode.SOURCE_HASH_MISMATCH,
                "formula hash does not match source/formula identity",
            )
        expected_program_hash = stable_hash(
            {
                "schema_version": "veriquant.qlib-formula-program/v1",
                "factor_id": self.factor_id,
                "source_hash": self.source_hash,
                "formula_hash": self.formula_hash,
                "status": self.status.value,
                "required_fields": list(self.required_fields),
                "maximum_lookback": self.maximum_lookback,
                "canonical_expression": self.canonical_expression,
                "expression_hash": self.expression_hash,
                "policy_hash": self.expression_policy_hash,
                "operator_manifest_hash": self.operator_manifest_hash,
                "operators": list(self.operators),
                "postprocess_steps": list(self.postprocess_steps),
                "reason_codes": list(self.reason_codes),
            }
        )
        if self.program_hash != expected_program_hash:
            raise FormulaMigrationError(
                FormulaMigrationFailureCode.SOURCE_HASH_MISMATCH,
                "program hash does not match the static migration artifact",
            )

    @property
    def certified(self) -> bool:
        return self.status in {
            FormulaMigrationStatus.CERTIFIED_QLIB_EXPRESSION,
            FormulaMigrationStatus.CERTIFIED_WITH_POSTPROCESS,
        }

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "factor_id": self.factor_id,
            "zoo": self.zoo,
            "source_relpath": self.source_relpath,
            "source_hash": self.source_hash,
            "metadata_hash": self.metadata_hash,
            "source_formula": self.source_formula,
            "formula_hash": self.formula_hash,
            "program_hash": self.program_hash,
            "status": self.status.value,
            "required_fields": list(self.required_fields),
            "maximum_lookback": self.maximum_lookback,
            "canonical_expression": self.canonical_expression,
            "expression_hash": self.expression_hash,
            "expression_policy_hash": self.expression_policy_hash,
            "operator_manifest_hash": self.operator_manifest_hash,
            "operators": list(self.operators),
            "postprocess_steps": list(self.postprocess_steps),
            "reason_codes": list(self.reason_codes),
        }


@dataclass(frozen=True, kw_only=True)
class FormulaMigrationReport:
    catalog_hash: str
    baseline_coverage_hash: str
    records: tuple[FormulaMigrationRecord, ...]
    baseline_certified_ids: tuple[str, ...]
    expected_total: int = 461
    schema_version: str = SCHEMA_VERSION
    rule_version: str = RULE_VERSION

    def __post_init__(self) -> None:
        _require_hash(self.catalog_hash, "catalog_hash")
        _require_hash(self.baseline_coverage_hash, "baseline_coverage_hash")
        ordered = tuple(sorted(self.records, key=lambda value: value.factor_id))
        if len(ordered) != self.expected_total:
            raise FormulaMigrationError(
                FormulaMigrationFailureCode.CATALOG_MISMATCH,
                "migration record count differs from the frozen catalog",
                expected=self.expected_total,
                actual=len(ordered),
            )
        if len({record.factor_id for record in ordered}) != len(ordered):
            raise FormulaMigrationError(
                FormulaMigrationFailureCode.DUPLICATE_FACTOR_ID,
                "migration report contains duplicate factor ids",
            )
        object.__setattr__(self, "records", ordered)
        object.__setattr__(self, "baseline_certified_ids", _sorted_unique(self.baseline_certified_ids))

    @property
    def certified_ids(self) -> tuple[str, ...]:
        return tuple(record.factor_id for record in self.records if record.certified)

    @property
    def newly_certified_ids(self) -> tuple[str, ...]:
        return tuple(sorted(set(self.certified_ids) - set(self.baseline_certified_ids)))

    @property
    def combined_certified_ids(self) -> tuple[str, ...]:
        """Union of reviewed baseline templates and new whole-formula ports."""

        return tuple(sorted(set(self.certified_ids) | set(self.baseline_certified_ids)))

    @property
    def counts_by_status(self) -> Mapping[str, int]:
        counts = {status.value: 0 for status in FormulaMigrationStatus}
        for record in self.records:
            counts[record.status.value] += 1
        return MappingProxyType(dict(sorted(counts.items())))

    def stable_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "rule_version": self.rule_version,
            "catalog_hash": self.catalog_hash,
            "baseline_coverage_hash": self.baseline_coverage_hash,
            "expected_total": self.expected_total,
            "baseline_certified_ids": list(self.baseline_certified_ids),
            "records": [record.to_dict() for record in self.records],
        }

    @property
    def report_hash(self) -> str:
        return stable_hash(self.stable_payload())

    def summary(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "rule_version": self.rule_version,
            "report_hash": self.report_hash,
            "catalog_hash": self.catalog_hash,
            "baseline_coverage_hash": self.baseline_coverage_hash,
            "total_records": len(self.records),
            "classified_records": len(self.records),
            "unclassified_records": 0,
            "counts_by_status": dict(self.counts_by_status),
            "certified_count": len(self.certified_ids),
            "baseline_certified_count": len(self.baseline_certified_ids),
            "newly_certified_count": len(self.newly_certified_ids),
            "newly_certified_ids": list(self.newly_certified_ids),
            "combined_certified_count": len(self.combined_certified_ids),
        }

    def to_dict(self) -> dict[str, object]:
        return {**self.stable_payload(), "report_hash": self.report_hash, "summary": self.summary()}
