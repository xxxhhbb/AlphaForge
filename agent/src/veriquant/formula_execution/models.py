"""Backend-neutral contracts for deterministic formula score execution."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from ..contracts import CanonicalContract, FailureCode
from ..contracts.base import fail, require_aware_datetime, require_hash, require_identifier


FORMULA_EXECUTION_SCHEMA_VERSION = "veriquant.formula-execution/v1"


class FormulaProgramKind(str, Enum):
    LEGACY_FACTOR_DAG = "LEGACY_FACTOR_DAG"
    CERTIFIED_QLIB_EXPRESSION = "CERTIFIED_QLIB_EXPRESSION"


@dataclass(frozen=True, kw_only=True)
class FormulaProgramIdentity(CanonicalContract):
    """Identity of an inert formula plus its frozen execution policy."""

    program_id: str
    kind: FormulaProgramKind
    program_hash: str
    required_fields: tuple[str, ...]
    runtime_hash: str
    policy_hash: str

    SCHEMA_VERSION = FORMULA_EXECUTION_SCHEMA_VERSION

    def __post_init__(self) -> None:
        require_identifier(self.program_id, "program_id")
        if not isinstance(self.kind, FormulaProgramKind):
            fail(FailureCode.INVALID_VALUE, "kind must be FormulaProgramKind")
        for name, value in (
            ("program_hash", self.program_hash),
            ("runtime_hash", self.runtime_hash),
            ("policy_hash", self.policy_hash),
        ):
            require_hash(value, name)
        if not self.required_fields or any(
            not isinstance(field, str) or not field.strip() for field in self.required_fields
        ):
            fail(FailureCode.INVALID_VALUE, "required_fields must be non-empty strings")
        canonical = tuple(sorted(set(self.required_fields)))
        if canonical != self.required_fields:
            fail(FailureCode.INVALID_VALUE, "required_fields must be sorted and unique")


@dataclass(frozen=True, kw_only=True)
class FormulaExecutionRequest(CanonicalContract):
    program: FormulaProgramIdentity
    snapshot_hash: str
    universe_hash: str
    seed: int

    SCHEMA_VERSION = FORMULA_EXECUTION_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if not isinstance(self.program, FormulaProgramIdentity):
            fail(FailureCode.INVALID_VALUE, "program must be FormulaProgramIdentity")
        require_hash(self.snapshot_hash, "snapshot_hash")
        require_hash(self.universe_hash, "universe_hash")
        if isinstance(self.seed, bool) or not isinstance(self.seed, int) or self.seed < 0:
            fail(FailureCode.INVALID_VALUE, "seed must be a non-negative integer")


@dataclass(frozen=True, kw_only=True)
class FactorScorePoint:
    event_time: datetime
    symbol: str
    score: float | None

    def __post_init__(self) -> None:
        require_aware_datetime(self.event_time, "event_time")
        require_identifier(self.symbol, "symbol")
        if self.score is not None:
            if isinstance(self.score, bool) or not isinstance(self.score, (int, float)):
                fail(FailureCode.INVALID_VALUE, "score must be numeric or missing")
            if not math.isfinite(float(self.score)):
                fail(FailureCode.NON_FINITE_NUMBER, "score must be finite or missing")


@dataclass(frozen=True, kw_only=True)
class FactorScoreArtifact(CanonicalContract):
    """Only output a formula backend may submit to the evidence boundary."""

    program_hash: str
    snapshot_hash: str
    universe_hash: str
    runtime_hash: str
    policy_hash: str
    seed: int
    values: tuple[FactorScorePoint, ...]

    SCHEMA_VERSION = FORMULA_EXECUTION_SCHEMA_VERSION

    def __post_init__(self) -> None:
        for name, value in (
            ("program_hash", self.program_hash),
            ("snapshot_hash", self.snapshot_hash),
            ("universe_hash", self.universe_hash),
            ("runtime_hash", self.runtime_hash),
            ("policy_hash", self.policy_hash),
        ):
            require_hash(value, name)
        if isinstance(self.seed, bool) or not isinstance(self.seed, int) or self.seed < 0:
            fail(FailureCode.INVALID_VALUE, "seed must be a non-negative integer")
        if not isinstance(self.values, tuple) or not self.values:
            fail(FailureCode.INVALID_VALUE, "values must be a non-empty tuple")
        keys: list[tuple[datetime, str]] = []
        for value in self.values:
            if not isinstance(value, FactorScorePoint):
                fail(FailureCode.INVALID_VALUE, "values must contain FactorScorePoint")
            keys.append((value.event_time, value.symbol))
        if len(keys) != len(set(keys)):
            fail(FailureCode.INVALID_VALUE, "factor score keys must be unique")
        if keys != sorted(keys):
            fail(FailureCode.INVALID_VALUE, "factor scores must use stable event_time/symbol ordering")
