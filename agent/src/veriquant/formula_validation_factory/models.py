"""Auditable, non-executing whole-formula validation matrix contracts."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Mapping


SCHEMA_VERSION = "veriquant.formula-validation-matrix/v1"
FACTORY_VERSION = "veriquant.formula-validation-factory/v1.0.0"
_HASH = re.compile(r"^sha256:[0-9a-f]{64}$")
_ID = re.compile(r"^[A-Za-z][A-Za-z0-9_.:-]{0,191}$")


def canonical_hash(value: object) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


class ValidationScenario(str, Enum):
    NORMAL_VALUES = "NORMAL_VALUES"
    PARTIAL_WARMUP = "PARTIAL_WARMUP"
    NAN_VALUES = "NAN_VALUES"
    CONSECUTIVE_MISSING = "CONSECUTIVE_MISSING"
    ALL_MISSING = "ALL_MISSING"
    TIES = "TIES"
    EXTREME_VALUES = "EXTREME_VALUES"
    DIVISION_BY_ZERO = "DIVISION_BY_ZERO"
    UNIVERSE_ENTRY_EXIT = "UNIVERSE_ENTRY_EXIT"
    DATA_REVISION = "DATA_REVISION"
    FUTURE_PERTURBATION = "FUTURE_PERTURBATION"
    DIFFERENT_UNIVERSE = "DIFFERENT_UNIVERSE"
    DETERMINISTIC_REPLAY = "DETERMINISTIC_REPLAY"


class ValidationLayer(str, Enum):
    AST_IR_CANONICAL_IDENTITY = "AST_IR_CANONICAL_IDENTITY"
    OPERATOR_POLICY = "OPERATOR_POLICY"
    WHOLE_FORMULA_REFERENCE_SEMANTICS = "WHOLE_FORMULA_REFERENCE_SEMANTICS"
    PINNED_PYQLIB_097_COMPARISON = "PINNED_PYQLIB_097_COMPARISON"
    FROZEN_PIT_EXECUTION = "FROZEN_PIT_EXECUTION"
    FACTOR_SCORE_ARTIFACT = "FACTOR_SCORE_ARTIFACT"
    TRIAL_LEDGER = "TRIAL_LEDGER"
    OFFICIAL_EVIDENCE_REPLAY = "OFFICIAL_EVIDENCE_REPLAY"


class ValidationEligibility(str, Enum):
    ELIGIBLE = "ELIGIBLE"
    BLOCKED = "BLOCKED"


class ValidationStatus(str, Enum):
    NOT_RUN = "NOT_RUN"
    PASSED = "PASSED"
    FAILED = "FAILED"
    UNAVAILABLE = "UNAVAILABLE"


class ValidationFactoryFailureCode(str, Enum):
    INVALID_SUBJECT = "INVALID_SUBJECT"
    INCOMPLETE_IDENTITY = "INCOMPLETE_IDENTITY"
    UNSAFE_QUALIFICATION = "UNSAFE_QUALIFICATION"
    UNKNOWN_CELL = "UNKNOWN_CELL"
    DUPLICATE_OUTCOME = "DUPLICATE_OUTCOME"
    BLOCKED_CELL = "BLOCKED_CELL"
    UNEXECUTED_OUTCOME = "UNEXECUTED_OUTCOME"
    INCOMPLETE_EVIDENCE = "INCOMPLETE_EVIDENCE"
    IDENTITY_MISMATCH = "IDENTITY_MISMATCH"


class ValidationFactoryError(ValueError):
    def __init__(self, code: ValidationFactoryFailureCode, message: str, **details: object) -> None:
        super().__init__(message)
        self.code = code
        self.details = MappingProxyType(dict(sorted(details.items())))


def _require_hash(value: str | None, name: str, *, optional: bool = False) -> None:
    if optional and value is None:
        return
    if not isinstance(value, str) or _HASH.fullmatch(value) is None:
        raise ValidationFactoryError(ValidationFactoryFailureCode.INCOMPLETE_IDENTITY, f"invalid {name}")


@dataclass(frozen=True, slots=True)
class FormulaValidationSubject:
    factor_id: str
    source_kind: str
    source_hash: str
    program_hash: str | None
    compiler_hash: str | None
    operator_policy_hash: str | None
    adapter_policy_hash: str | None
    postprocess_plan_hash: str | None
    projection_hash: str | None
    projection_record_hash: str | None
    execution_qualification: str
    required_fields: tuple[str, ...]
    maximum_lookback: int | None
    operators: tuple[str, ...]
    semantic_adapters: tuple[str, ...]
    missing_fields: tuple[str, ...] = ()
    pit_missing_fields: tuple[str, ...] = ()
    canonical_expression_available: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.factor_id, str) or _ID.fullmatch(self.factor_id) is None:
            raise ValidationFactoryError(ValidationFactoryFailureCode.INVALID_SUBJECT, "invalid factor_id")
        _require_hash(self.source_hash, "source_hash")
        for name in ("program_hash", "compiler_hash", "operator_policy_hash", "adapter_policy_hash", "postprocess_plan_hash", "projection_hash", "projection_record_hash"):
            _require_hash(getattr(self, name), name, optional=True)
        if self.maximum_lookback is not None and (isinstance(self.maximum_lookback, bool) or self.maximum_lookback < 0):
            raise ValidationFactoryError(ValidationFactoryFailureCode.INVALID_SUBJECT, "invalid maximum_lookback")
        for name in ("required_fields", "operators", "semantic_adapters", "missing_fields", "pit_missing_fields"):
            object.__setattr__(self, name, tuple(sorted(set(getattr(self, name)))))

    @property
    def subject_hash(self) -> str:
        return canonical_hash(self.to_canonical())

    def to_canonical(self) -> dict[str, object]:
        return {
            "factor_id": self.factor_id, "source_kind": self.source_kind, "source_hash": self.source_hash,
            "program_hash": self.program_hash, "compiler_hash": self.compiler_hash,
            "operator_policy_hash": self.operator_policy_hash, "adapter_policy_hash": self.adapter_policy_hash,
            "postprocess_plan_hash": self.postprocess_plan_hash, "projection_hash": self.projection_hash,
            "projection_record_hash": self.projection_record_hash, "execution_qualification": self.execution_qualification,
            "required_fields": list(self.required_fields), "maximum_lookback": self.maximum_lookback,
            "operators": list(self.operators), "semantic_adapters": list(self.semantic_adapters),
            "missing_fields": list(self.missing_fields), "pit_missing_fields": list(self.pit_missing_fields),
            "canonical_expression_available": self.canonical_expression_available,
        }


@dataclass(frozen=True, slots=True)
class FormulaValidationCell:
    cell_id: str
    scenario: ValidationScenario
    layer: ValidationLayer
    eligibility: ValidationEligibility
    status: ValidationStatus
    assertions: tuple[str, ...]
    blocker_codes: tuple[str, ...]
    subject_hash: str
    evidence_hash: str | None = None
    command_hash: str | None = None
    runtime_hash: str | None = None
    test_count: int = 0

    def __post_init__(self) -> None:
        _require_hash(self.subject_hash, "subject_hash")
        for name in ("evidence_hash", "command_hash", "runtime_hash"):
            _require_hash(getattr(self, name), name, optional=True)
        object.__setattr__(self, "assertions", tuple(self.assertions))
        object.__setattr__(self, "blocker_codes", tuple(sorted(set(self.blocker_codes))))
        if self.eligibility is ValidationEligibility.BLOCKED and not self.blocker_codes:
            raise ValidationFactoryError(ValidationFactoryFailureCode.INVALID_SUBJECT, "blocked cell requires blocker code")
        if self.status is ValidationStatus.NOT_RUN:
            if any(value is not None for value in (self.evidence_hash, self.command_hash, self.runtime_hash)) or self.test_count != 0:
                raise ValidationFactoryError(ValidationFactoryFailureCode.UNEXECUTED_OUTCOME, "NOT_RUN cell cannot contain run evidence")
        elif self.eligibility is ValidationEligibility.BLOCKED:
            raise ValidationFactoryError(ValidationFactoryFailureCode.BLOCKED_CELL, "blocked cell cannot have an outcome")
        elif self.evidence_hash is None or self.command_hash is None or self.test_count < 1:
            raise ValidationFactoryError(ValidationFactoryFailureCode.INCOMPLETE_EVIDENCE, "executed outcome requires hashes and positive test_count")

    def to_canonical(self) -> dict[str, object]:
        return {
            "cell_id": self.cell_id, "scenario": self.scenario.value, "layer": self.layer.value,
            "eligibility": self.eligibility.value, "status": self.status.value,
            "assertions": list(self.assertions), "blocker_codes": list(self.blocker_codes),
            "subject_hash": self.subject_hash, "evidence_hash": self.evidence_hash,
            "command_hash": self.command_hash, "runtime_hash": self.runtime_hash, "test_count": self.test_count,
        }


@dataclass(frozen=True, slots=True)
class FormulaValidationMatrix:
    subject: FormulaValidationSubject
    cells: tuple[FormulaValidationCell, ...]
    factory_hash: str
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        _require_hash(self.factory_hash, "factory_hash")
        if self.schema_version != SCHEMA_VERSION:
            raise ValidationFactoryError(ValidationFactoryFailureCode.IDENTITY_MISMATCH, "matrix schema mismatch")
        expected = len(ValidationScenario) * len(ValidationLayer)
        if len(self.cells) != expected or len({cell.cell_id for cell in self.cells}) != expected:
            raise ValidationFactoryError(ValidationFactoryFailureCode.IDENTITY_MISMATCH, "matrix is not the complete scenario/layer product")
        combinations = {(cell.scenario, cell.layer) for cell in self.cells}
        if combinations != {(scenario, layer) for scenario in ValidationScenario for layer in ValidationLayer}:
            raise ValidationFactoryError(ValidationFactoryFailureCode.IDENTITY_MISMATCH, "matrix coverage is incomplete")
        if any(cell.subject_hash != self.subject.subject_hash for cell in self.cells):
            raise ValidationFactoryError(ValidationFactoryFailureCode.IDENTITY_MISMATCH, "cell subject identity mismatch")

    @property
    def matrix_hash(self) -> str:
        return canonical_hash(self.to_canonical())

    @property
    def status_counts(self) -> Mapping[str, int]:
        counts = {status.value: 0 for status in ValidationStatus}
        for cell in self.cells:
            counts[cell.status.value] += 1
        return MappingProxyType(counts)

    @property
    def eligibility_counts(self) -> Mapping[str, int]:
        counts = {status.value: 0 for status in ValidationEligibility}
        for cell in self.cells:
            counts[cell.eligibility.value] += 1
        return MappingProxyType(counts)

    def to_canonical(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version, "factory_hash": self.factory_hash,
            "subject": self.subject.to_canonical(), "cells": [cell.to_canonical() for cell in self.cells],
        }


@dataclass(frozen=True, slots=True)
class ValidationExecutionReceipt:
    cell_id: str
    subject_hash: str
    status: ValidationStatus
    evidence_hash: str
    command_hash: str
    test_count: int
    runtime_hash: str | None = None
    executed: bool = True

    def __post_init__(self) -> None:
        if self.status is ValidationStatus.NOT_RUN or not self.executed:
            raise ValidationFactoryError(ValidationFactoryFailureCode.UNEXECUTED_OUTCOME, "receipt must represent a real terminal execution")
        for name in ("subject_hash", "evidence_hash", "command_hash"):
            _require_hash(getattr(self, name), name)
        _require_hash(self.runtime_hash, "runtime_hash", optional=True)
        if isinstance(self.test_count, bool) or not isinstance(self.test_count, int) or self.test_count < 1:
            raise ValidationFactoryError(ValidationFactoryFailureCode.INCOMPLETE_EVIDENCE, "receipt requires positive test_count")

