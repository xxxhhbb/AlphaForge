"""Canonical contracts for certified Qlib expression score execution.

This module deliberately contains no expression parser and no official-evidence
types.  The runtime receives only the identity of a previously certified
program and produces a non-authoritative score artifact.
"""

from __future__ import annotations

import platform
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from types import MappingProxyType
from typing import ClassVar, Mapping

from ..contracts import CanonicalContract, FailureCode
from ..contracts.base import (
    fail,
    require_aware_datetime,
    require_finite_number,
    require_hash,
    require_identifier,
    require_nonempty_tuple,
    require_text,
)
from ..data import canonical_content_hash


RUNTIME_SCHEMA_VERSION = "veriquant.qlib-expression-runtime/v1"
_FORBIDDEN_FIELD_TOKENS = ("label", "sealed", "future_return")


class RuntimeBackend(str, Enum):
    """Closed set of expression runtime backends."""

    REFERENCE = "REFERENCE"
    OPTIONAL_PYQLIB = "OPTIONAL_PYQLIB"


class RuntimeFailureCode(str, Enum):
    """Machine-readable fail-closed runtime outcomes."""

    RUNTIME_DISABLED = "RUNTIME_DISABLED"
    RUNTIME_UNAVAILABLE = "RUNTIME_UNAVAILABLE"
    RUNTIME_MISMATCH = "RUNTIME_MISMATCH"
    INVALID_REQUEST = "INVALID_REQUEST"
    PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
    TIMEOUT = "TIMEOUT"
    PARTIAL_RESULT = "PARTIAL_RESULT"
    EXECUTION_FAILED = "EXECUTION_FAILED"
    OUTPUT_LIMIT_EXCEEDED = "OUTPUT_LIMIT_EXCEEDED"


class ReferenceOperation(str, Enum):
    """Small deterministic operator set used only by the offline reference runtime."""

    FIELD = "FIELD"
    ADD = "ADD"
    SUBTRACT = "SUBTRACT"
    MULTIPLY = "MULTIPLY"
    DIVIDE = "DIVIDE"
    SIGNED_POWER = "SIGNED_POWER"


def _positive_int(value: int, field_name: str, *, upper: int | None = None) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        fail(FailureCode.INVALID_VALUE, f"{field_name} must be a positive integer")
    if upper is not None and value > upper:
        fail(FailureCode.INVALID_VALUE, f"{field_name} exceeds its policy limit")


def _field_name(value: str, field_name: str) -> None:
    require_identifier(value, field_name)
    normalized = value.lower()
    if any(token in normalized for token in _FORBIDDEN_FIELD_TOKENS):
        fail(FailureCode.UNAUTHORIZED_CAPABILITY, f"{field_name} is not a feature field")


def _hash_mapping(value: Mapping[str, str], field_name: str) -> Mapping[str, str]:
    if not isinstance(value, Mapping):
        fail(FailureCode.INVALID_VALUE, f"{field_name} must be a mapping")
    output: dict[str, str] = {}
    for key, item in sorted(value.items()):
        require_identifier(key, f"{field_name}.key")
        require_text(item, f"{field_name}.{key}")
        output[key] = item
    return MappingProxyType(output)


@dataclass(frozen=True, kw_only=True)
class QlibRuntimeManifest(CanonicalContract):
    """Pinned identity and policy for one local expression runtime."""

    SCHEMA_VERSION: ClassVar[str] = RUNTIME_SCHEMA_VERSION

    runtime_id: str
    backend: RuntimeBackend
    implementation: str
    implementation_version: str
    python_version: str
    dependency_versions: Mapping[str, str]
    operator_policy_hash: str
    code_hash: str
    enabled: bool
    offline: bool = True
    native_string_parser_enabled: bool = False
    custom_operators_enabled: bool = False

    def __post_init__(self) -> None:
        require_identifier(self.runtime_id, "runtime_id")
        if not isinstance(self.backend, RuntimeBackend):
            object.__setattr__(self, "backend", RuntimeBackend(self.backend))
        require_identifier(self.implementation, "implementation")
        require_text(self.implementation_version, "implementation_version")
        require_text(self.python_version, "python_version")
        object.__setattr__(
            self,
            "dependency_versions",
            _hash_mapping(self.dependency_versions, "dependency_versions"),
        )
        require_hash(self.operator_policy_hash, "operator_policy_hash")
        require_hash(self.code_hash, "code_hash")
        for name in (
            "enabled",
            "offline",
            "native_string_parser_enabled",
            "custom_operators_enabled",
        ):
            if type(getattr(self, name)) is not bool:
                fail(FailureCode.INVALID_VALUE, f"{name} must be boolean")
        if not self.offline:
            fail(FailureCode.UNAUTHORIZED_CAPABILITY, "certified expression runtime must be offline")
        if self.native_string_parser_enabled or self.custom_operators_enabled:
            fail(
                FailureCode.UNAUTHORIZED_CAPABILITY,
                "native string parsing and custom operators are forbidden",
            )

    @classmethod
    def reference(cls, *, operator_policy_hash: str, code_hash: str) -> "QlibRuntimeManifest":
        """Build the dependency-free default runtime manifest."""

        return cls(
            runtime_id="certified-qlib-reference-v1",
            backend=RuntimeBackend.REFERENCE,
            implementation="veriquant-reference-expression-runtime",
            implementation_version="1.0.0",
            python_version=platform.python_version(),
            dependency_versions={},
            operator_policy_hash=operator_policy_hash,
            code_hash=code_hash,
            enabled=True,
        )


@dataclass(frozen=True, kw_only=True)
class FormulaExecutionRequest(CanonicalContract):
    """Execution request containing identities, never an untrusted expression string."""

    SCHEMA_VERSION: ClassVar[str] = RUNTIME_SCHEMA_VERSION

    request_id: str
    program_hash: str
    operator_manifest_hash: str
    snapshot_hash: str
    universe_hash: str
    runtime_hash: str
    required_fields: tuple[str, ...]
    start_time: datetime
    end_time: datetime
    decision_time: datetime
    expected_row_count: int
    max_output_rows: int
    timeout_ms: int
    seed: int

    def __post_init__(self) -> None:
        require_identifier(self.request_id, "request_id")
        for name in (
            "program_hash",
            "operator_manifest_hash",
            "snapshot_hash",
            "universe_hash",
            "runtime_hash",
        ):
            require_hash(getattr(self, name), name)
        require_nonempty_tuple(self.required_fields, "required_fields")
        if len(set(self.required_fields)) != len(self.required_fields):
            fail(FailureCode.INVALID_VALUE, "required_fields must be unique")
        for field in self.required_fields:
            _field_name(field, "required_fields")
        object.__setattr__(self, "required_fields", tuple(sorted(self.required_fields)))
        for name in ("start_time", "end_time", "decision_time"):
            require_aware_datetime(getattr(self, name), name)
        if self.start_time > self.end_time or self.end_time > self.decision_time:
            fail(FailureCode.INVALID_TIME_ORDER, "request clocks must be start <= end <= decision")
        _positive_int(self.expected_row_count, "expected_row_count")
        _positive_int(self.max_output_rows, "max_output_rows", upper=10_000_000)
        if self.expected_row_count > self.max_output_rows:
            fail(FailureCode.INVALID_VALUE, "expected rows exceed output policy")
        _positive_int(self.timeout_ms, "timeout_ms", upper=300_000)
        if isinstance(self.seed, bool) or not isinstance(self.seed, int) or self.seed < 0:
            fail(FailureCode.INVALID_VALUE, "seed must be a non-negative integer")


@dataclass(frozen=True, kw_only=True)
class RuntimeFeatureRow(CanonicalContract):
    """One already-PIT-filtered feature row supplied by a frozen provider."""

    SCHEMA_VERSION: ClassVar[str] = RUNTIME_SCHEMA_VERSION

    event_time: datetime
    available_at: datetime
    symbol: str
    values: Mapping[str, float]

    def __post_init__(self) -> None:
        require_aware_datetime(self.event_time, "event_time")
        require_aware_datetime(self.available_at, "available_at")
        if self.event_time > self.available_at:
            fail(FailureCode.INVALID_TIME_ORDER, "event_time must not follow available_at")
        require_identifier(self.symbol, "symbol")
        if not isinstance(self.values, Mapping) or not self.values:
            fail(FailureCode.INVALID_VALUE, "feature values are required")
        output: dict[str, float] = {}
        for key, value in sorted(self.values.items()):
            _field_name(key, "values.key")
            require_finite_number(value, f"values.{key}")
            output[key] = float(value)
        object.__setattr__(self, "values", MappingProxyType(output))


@dataclass(frozen=True, kw_only=True)
class RuntimeFeatureBatch(CanonicalContract):
    """Frozen provider response with explicit completeness."""

    SCHEMA_VERSION: ClassVar[str] = RUNTIME_SCHEMA_VERSION

    snapshot_hash: str
    universe_hash: str
    rows: tuple[RuntimeFeatureRow, ...]
    complete: bool

    def __post_init__(self) -> None:
        require_hash(self.snapshot_hash, "snapshot_hash")
        require_hash(self.universe_hash, "universe_hash")
        if not isinstance(self.rows, tuple) or any(
            not isinstance(row, RuntimeFeatureRow) for row in self.rows
        ):
            fail(FailureCode.INVALID_VALUE, "rows must contain RuntimeFeatureRow values")
        if type(self.complete) is not bool:
            fail(FailureCode.INVALID_VALUE, "complete must be boolean")
        ordered = tuple(sorted(self.rows, key=lambda row: (row.event_time, row.symbol)))
        keys = tuple((row.event_time, row.symbol) for row in ordered)
        if len(set(keys)) != len(keys):
            fail(FailureCode.INVALID_VALUE, "feature batch contains duplicate row keys")
        object.__setattr__(self, "rows", ordered)


@dataclass(frozen=True, kw_only=True)
class ReferenceExpressionProgram(CanonicalContract):
    """Repository-owned structured formula used by the offline reference backend."""

    SCHEMA_VERSION: ClassVar[str] = RUNTIME_SCHEMA_VERSION

    program_id: str
    operator_manifest_hash: str
    operation: ReferenceOperation
    input_fields: tuple[str, ...]
    exponent: float | None = None

    def __post_init__(self) -> None:
        require_identifier(self.program_id, "program_id")
        require_hash(self.operator_manifest_hash, "operator_manifest_hash")
        if not isinstance(self.operation, ReferenceOperation):
            object.__setattr__(self, "operation", ReferenceOperation(self.operation))
        require_nonempty_tuple(self.input_fields, "input_fields")
        if len(set(self.input_fields)) != len(self.input_fields):
            fail(FailureCode.INVALID_VALUE, "input_fields must be unique")
        for field in self.input_fields:
            _field_name(field, "input_fields")
        expected = 1 if self.operation in {ReferenceOperation.FIELD, ReferenceOperation.SIGNED_POWER} else 2
        if len(self.input_fields) != expected:
            fail(FailureCode.INVALID_VALUE, f"{self.operation.value} requires {expected} input fields")
        if self.operation is ReferenceOperation.SIGNED_POWER:
            if self.exponent is None:
                fail(FailureCode.MISSING_REQUIRED_FIELD, "SIGNED_POWER requires exponent")
            require_finite_number(self.exponent, "exponent")
        elif self.exponent is not None:
            fail(FailureCode.INVALID_VALUE, "exponent is only valid for SIGNED_POWER")
        # Preserve operand order: subtraction and division are not commutative.
        object.__setattr__(self, "input_fields", tuple(self.input_fields))

    @property
    def required_fields(self) -> tuple[str, ...]:
        """Return the normalized provider field set for protocol interoperability."""

        return tuple(sorted(self.input_fields))


@dataclass(frozen=True, kw_only=True)
class FactorScoreRow(CanonicalContract):
    """One deterministic factor score."""

    SCHEMA_VERSION: ClassVar[str] = RUNTIME_SCHEMA_VERSION

    event_time: datetime
    symbol: str
    score: float

    def __post_init__(self) -> None:
        require_aware_datetime(self.event_time, "event_time")
        require_identifier(self.symbol, "symbol")
        require_finite_number(self.score, "score")
        object.__setattr__(self, "score", float(self.score))


@dataclass(frozen=True, kw_only=True)
class FactorScoreArtifact(CanonicalContract):
    """Non-authoritative score artifact consumed by a later Evidence Service."""

    SCHEMA_VERSION: ClassVar[str] = RUNTIME_SCHEMA_VERSION

    request_hash: str
    program_hash: str
    operator_manifest_hash: str
    snapshot_hash: str
    universe_hash: str
    runtime_hash: str
    rows: tuple[FactorScoreRow, ...]
    scores_hash: str

    def __post_init__(self) -> None:
        for name in (
            "request_hash",
            "program_hash",
            "operator_manifest_hash",
            "snapshot_hash",
            "universe_hash",
            "runtime_hash",
            "scores_hash",
        ):
            require_hash(getattr(self, name), name)
        if not isinstance(self.rows, tuple) or not self.rows:
            fail(FailureCode.INCOMPLETE_RESULT, "score artifact requires rows")
        if any(not isinstance(row, FactorScoreRow) for row in self.rows):
            fail(FailureCode.INVALID_VALUE, "rows must contain FactorScoreRow values")
        ordered = tuple(sorted(self.rows, key=lambda row: (row.event_time, row.symbol)))
        keys = tuple((row.event_time, row.symbol) for row in ordered)
        if len(set(keys)) != len(keys):
            fail(FailureCode.INVALID_VALUE, "score artifact contains duplicate row keys")
        object.__setattr__(self, "rows", ordered)
        if self.scores_hash != canonical_content_hash(ordered):
            fail(FailureCode.INVALID_HASH, "scores_hash does not match canonical score rows")


@dataclass(frozen=True, kw_only=True)
class FormulaExecutionFailure(CanonicalContract):
    """Typed terminal failure.  A failure never carries partial score rows."""

    SCHEMA_VERSION: ClassVar[str] = RUNTIME_SCHEMA_VERSION

    request_hash: str
    runtime_hash: str
    code: RuntimeFailureCode
    message: str
    retryable: bool = False

    def __post_init__(self) -> None:
        require_hash(self.request_hash, "request_hash")
        require_hash(self.runtime_hash, "runtime_hash")
        if not isinstance(self.code, RuntimeFailureCode):
            object.__setattr__(self, "code", RuntimeFailureCode(self.code))
        require_text(self.message, "message")
        if type(self.retryable) is not bool:
            fail(FailureCode.INVALID_VALUE, "retryable must be boolean")


def scores_hash(rows: tuple[FactorScoreRow, ...]) -> str:
    """Return the stable content hash of rows in canonical key order."""

    ordered = tuple(sorted(rows, key=lambda row: (row.event_time, row.symbol)))
    return canonical_content_hash(ordered)


RuntimeResult = FactorScoreArtifact | FormulaExecutionFailure
