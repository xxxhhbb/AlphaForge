"""Typed contracts for the pinned in-memory Qlib operator worker."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from types import MappingProxyType
from typing import Mapping

from ..qlib_expression.api import (
    CertifiedQlibExpression,
    PINNED_OPS_SOURCE_HASH,
    PINNED_PIT_SOURCE_HASH,
    PINNED_PYQLIB_VERSION,
    STATIC_QLIB_OPERATOR_MANIFEST,
    canonicalize_expression,
)
from ..qlib_expression.models import (
    BinaryNode,
    CallNode,
    FieldNode,
    NumberNode,
    StringNode,
    UnaryNode,
)


WORKER_SCHEMA_VERSION = "veriquant.qlib-operator-worker/v1"
WORKER_POLICY_VERSION = "veriquant.qlib-operator-worker-policy/pyqlib-0.9.7-v1"


class QlibOperatorWorkerFailureCode(str, Enum):
    INVALID_REQUEST = "INVALID_REQUEST"
    UNTRUSTED_PROGRAM = "UNTRUSTED_PROGRAM"
    PROGRAM_IDENTITY_MISMATCH = "PROGRAM_IDENTITY_MISMATCH"
    MANIFEST_MISMATCH = "MANIFEST_MISMATCH"
    EXCLUDED_OPERATOR = "EXCLUDED_OPERATOR"
    UNSUPPORTED_NODE = "UNSUPPORTED_NODE"
    DATA_SCHEMA_MISMATCH = "DATA_SCHEMA_MISMATCH"
    DUPLICATE_ROW = "DUPLICATE_ROW"
    CALENDAR_MISMATCH = "CALENDAR_MISMATCH"
    NON_FINITE_INPUT = "NON_FINITE_INPUT"
    QLIB_RUNTIME_UNAVAILABLE = "QLIB_RUNTIME_UNAVAILABLE"
    QLIB_RUNTIME_VERSION_MISMATCH = "QLIB_RUNTIME_VERSION_MISMATCH"
    QLIB_RUNTIME_SOURCE_MISMATCH = "QLIB_RUNTIME_SOURCE_MISMATCH"
    EXECUTION_FAILED = "EXECUTION_FAILED"
    INCOMPLETE_RESULT = "INCOMPLETE_RESULT"


class QlibOperatorWorkerError(ValueError):
    def __init__(
        self,
        code: QlibOperatorWorkerFailureCode,
        message: str,
        *,
        details: Mapping[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.details = MappingProxyType(dict(details or {}))


def _fail(code: QlibOperatorWorkerFailureCode, message: str, **details: object) -> None:
    raise QlibOperatorWorkerError(code, message, details=details)


def _stable_hash(value: object) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _require_hash(value: str, name: str) -> None:
    if not isinstance(value, str) or not value.startswith("sha256:") or len(value) != 71:
        _fail(QlibOperatorWorkerFailureCode.INVALID_REQUEST, f"{name} must be a SHA-256 identity")


_BINARY_OPERATOR_NAMES = {
    "+": "Add", "-": "Sub", "*": "Mul", "/": "Div", "**": "Power", ">": "Gt", ">=": "Ge",
    "<": "Lt", "<=": "Le", "==": "Eq", "!=": "Ne", "&": "And", "|": "Or",
}

WORKER_OPERATOR_NAMES = frozenset(
    {
        "ChangeInstrument", "Ref", "Max", "Min", "Sum", "Mean", "Std", "Var", "Skew", "Kurt", "Med",
        "Mad", "Slope", "Rsquare", "Resi", "Rank", "Count", "WMA", "Delta", "IdxMax", "IdxMin",
        "Quantile", "EMA", "Corr", "Cov", "Abs", "Sign", "Log", "Not", "Power", "Add", "Sub", "Mul",
        "Div", "Greater", "Less", "And", "Or", "Gt", "Ge", "Lt", "Le", "Eq", "Ne", "Mask", "If",
    }
)

_MANIFEST_EXECUTABLE_NAMES = frozenset(
    spec.name for spec in STATIC_QLIB_OPERATOR_MANIFEST.operators if spec.trust.value != "EXCLUDED"
)
if WORKER_OPERATOR_NAMES != _MANIFEST_EXECUTABLE_NAMES:  # pragma: no cover - import-time version fence
    raise RuntimeError("worker mapping and certified Qlib manifest disagree")


def inspect_certified_program(program: CertifiedQlibExpression) -> tuple[tuple[str, ...], tuple[str, ...], int]:
    """Recheck structured metadata without accepting or parsing a string."""

    if type(program) is not CertifiedQlibExpression:
        _fail(QlibOperatorWorkerFailureCode.UNTRUSTED_PROGRAM, "worker requires an exact CertifiedQlibExpression")
    if program.manifest_hash != STATIC_QLIB_OPERATOR_MANIFEST.manifest_hash:
        _fail(QlibOperatorWorkerFailureCode.MANIFEST_MISMATCH, "program operator manifest is not pinned")
    canonical = canonicalize_expression(program.root)
    if canonical != program.canonical_expression:
        _fail(QlibOperatorWorkerFailureCode.PROGRAM_IDENTITY_MISMATCH, "canonical tree and program text identity disagree")
    identity = {
        "schema_version": program.schema_version,
        "canonical_expression": canonical,
        "manifest_hash": program.manifest_hash,
        "policy_hash": program.policy_hash,
    }
    if _stable_hash(identity) != program.expression_hash:
        _fail(QlibOperatorWorkerFailureCode.PROGRAM_IDENTITY_MISMATCH, "certified expression hash mismatch")

    fields: set[str] = set()
    operators: set[str] = set()
    nodes = 0

    def visit(node: object) -> None:
        nonlocal nodes
        nodes += 1
        if isinstance(node, FieldNode):
            fields.add(node.name)
            return
        if isinstance(node, (NumberNode, StringNode)):
            return
        if isinstance(node, UnaryNode):
            visit(node.operand)
            return
        if isinstance(node, BinaryNode):
            name = _BINARY_OPERATOR_NAMES.get(node.operator)
            if name not in WORKER_OPERATOR_NAMES:
                _fail(QlibOperatorWorkerFailureCode.EXCLUDED_OPERATOR, "binary operator is not worker-allowlisted")
            operators.add(name)
            visit(node.left)
            visit(node.right)
            return
        if isinstance(node, CallNode):
            if node.name not in WORKER_OPERATOR_NAMES:
                _fail(QlibOperatorWorkerFailureCode.EXCLUDED_OPERATOR, "call operator is not worker-allowlisted", operator=node.name)
            operators.add(node.name)
            for argument in node.arguments:
                visit(argument)
            return
        _fail(QlibOperatorWorkerFailureCode.UNSUPPORTED_NODE, "program contains a non-certified node type")

    visit(program.root)
    if tuple(sorted(fields)) != program.required_fields:
        _fail(QlibOperatorWorkerFailureCode.PROGRAM_IDENTITY_MISMATCH, "required_fields do not match certified tree")
    if tuple(sorted(operators)) != program.operators:
        _fail(QlibOperatorWorkerFailureCode.PROGRAM_IDENTITY_MISMATCH, "operator list does not match certified tree")
    return tuple(sorted(fields)), tuple(sorted(operators)), nodes


@dataclass(frozen=True, slots=True)
class QlibInMemoryFeatureRow:
    event_time: datetime
    symbol: str
    values: Mapping[str, float | int | None]

    def __post_init__(self) -> None:
        if not isinstance(self.event_time, datetime) or self.event_time.tzinfo is None or self.event_time.utcoffset() is None:
            _fail(QlibOperatorWorkerFailureCode.INVALID_REQUEST, "event_time must be timezone-aware")
        if not isinstance(self.symbol, str) or not self.symbol.strip():
            _fail(QlibOperatorWorkerFailureCode.INVALID_REQUEST, "symbol must be non-empty")
        normalized: dict[str, float | None] = {}
        for field, value in self.values.items():
            if not isinstance(field, str) or not field:
                _fail(QlibOperatorWorkerFailureCode.DATA_SCHEMA_MISMATCH, "feature field must be non-empty")
            if value is None:
                normalized[field] = None
            elif isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
                _fail(QlibOperatorWorkerFailureCode.NON_FINITE_INPUT, "feature values must be finite or missing")
            else:
                normalized[field] = float(value)
        object.__setattr__(self, "symbol", self.symbol.strip())
        object.__setattr__(self, "values", MappingProxyType(dict(sorted(normalized.items()))))

    def to_canonical(self) -> dict[str, object]:
        return {"event_time": self.event_time.isoformat(), "symbol": self.symbol, "values": dict(self.values)}


@dataclass(frozen=True, slots=True)
class QlibOperatorExecutionRequest:
    program: CertifiedQlibExpression
    snapshot_hash: str
    universe_hash: str
    output_symbols: tuple[str, ...]
    rows: tuple[QlibInMemoryFeatureRow, ...]
    schema_version: str = WORKER_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != WORKER_SCHEMA_VERSION:
            _fail(QlibOperatorWorkerFailureCode.INVALID_REQUEST, "worker request schema mismatch")
        fields, _, _ = inspect_certified_program(self.program)
        _require_hash(self.snapshot_hash, "snapshot_hash")
        _require_hash(self.universe_hash, "universe_hash")
        if not self.output_symbols or len(set(self.output_symbols)) != len(self.output_symbols):
            _fail(QlibOperatorWorkerFailureCode.INVALID_REQUEST, "output_symbols must be unique and non-empty")
        if tuple(sorted(self.output_symbols)) != self.output_symbols:
            _fail(QlibOperatorWorkerFailureCode.INVALID_REQUEST, "output_symbols must be stable sorted")
        if not self.rows or any(type(row) is not QlibInMemoryFeatureRow for row in self.rows):
            _fail(QlibOperatorWorkerFailureCode.INVALID_REQUEST, "rows must be typed and non-empty")
        seen: set[tuple[datetime, str]] = set()
        calendars: dict[str, list[datetime]] = {}
        symbols: set[str] = set()
        for row in self.rows:
            key = (row.event_time, row.symbol)
            if key in seen:
                _fail(QlibOperatorWorkerFailureCode.DUPLICATE_ROW, "duplicate event_time/symbol row")
            seen.add(key)
            symbols.add(row.symbol)
            calendars.setdefault(row.symbol, []).append(row.event_time)
            if tuple(row.values) != fields:
                _fail(
                    QlibOperatorWorkerFailureCode.DATA_SCHEMA_MISMATCH,
                    "rows must contain exactly the certified feature fields; labels and extra fields are prohibited",
                )
        if not set(self.output_symbols).issubset(symbols):
            _fail(QlibOperatorWorkerFailureCode.DATA_SCHEMA_MISMATCH, "output symbol lacks in-memory rows")
        normalized_calendars = {symbol: tuple(sorted(values)) for symbol, values in calendars.items()}
        first = next(iter(normalized_calendars.values()))
        if any(calendar != first for calendar in normalized_calendars.values()):
            _fail(QlibOperatorWorkerFailureCode.CALENDAR_MISMATCH, "all in-memory instruments must share one calendar")

    @property
    def input_hash(self) -> str:
        ordered = sorted((row.to_canonical() for row in self.rows), key=lambda row: (row["event_time"], row["symbol"]))
        return _stable_hash(
            {
                "schema_version": self.schema_version,
                "program_hash": self.program.expression_hash,
                "snapshot_hash": self.snapshot_hash,
                "universe_hash": self.universe_hash,
                "output_symbols": list(self.output_symbols),
                "rows": ordered,
            }
        )


@dataclass(frozen=True, slots=True)
class QlibFactorScoreRow:
    event_time: datetime
    symbol: str
    score: float | None

    def __post_init__(self) -> None:
        if not isinstance(self.event_time, datetime) or self.event_time.tzinfo is None or self.event_time.utcoffset() is None:
            _fail(QlibOperatorWorkerFailureCode.INCOMPLETE_RESULT, "score event_time must be timezone-aware")
        if not isinstance(self.symbol, str) or not self.symbol:
            _fail(QlibOperatorWorkerFailureCode.INCOMPLETE_RESULT, "score symbol must be non-empty")
        if self.score is not None and (isinstance(self.score, bool) or not math.isfinite(self.score)):
            _fail(QlibOperatorWorkerFailureCode.INCOMPLETE_RESULT, "score must be finite or missing")

    def to_canonical(self) -> dict[str, object]:
        return {"event_time": self.event_time.isoformat(), "symbol": self.symbol, "score": self.score}


@dataclass(frozen=True, slots=True)
class QlibRuntimeIdentity:
    pyqlib_version: str
    ops_source_hash: str
    pit_source_hash: str
    worker_policy_version: str = WORKER_POLICY_VERSION

    @property
    def runtime_hash(self) -> str:
        return _stable_hash(
            {
                "pyqlib_version": self.pyqlib_version,
                "ops_source_hash": self.ops_source_hash,
                "pit_source_hash": self.pit_source_hash,
                "worker_policy_version": self.worker_policy_version,
            }
        )

    @classmethod
    def expected(cls) -> "QlibRuntimeIdentity":
        return cls(PINNED_PYQLIB_VERSION, PINNED_OPS_SOURCE_HASH, PINNED_PIT_SOURCE_HASH)


@dataclass(frozen=True, slots=True)
class QlibFactorScoreArtifact:
    program_hash: str
    snapshot_hash: str
    universe_hash: str
    input_hash: str
    runtime_hash: str
    output_hash: str
    rows: tuple[QlibFactorScoreRow, ...]
    missing_scores: int
    artifact_class: str = "FACTOR_SCORE_ARTIFACT"

    def __post_init__(self) -> None:
        for name in ("program_hash", "snapshot_hash", "universe_hash", "input_hash", "runtime_hash", "output_hash"):
            _require_hash(getattr(self, name), name)
        if self.artifact_class != "FACTOR_SCORE_ARTIFACT" or not self.rows:
            _fail(QlibOperatorWorkerFailureCode.INCOMPLETE_RESULT, "invalid factor score artifact")
        if tuple(sorted(self.rows, key=lambda row: (row.event_time, row.symbol))) != self.rows:
            _fail(QlibOperatorWorkerFailureCode.INCOMPLETE_RESULT, "score rows must be stable sorted")
        if self.missing_scores != sum(row.score is None for row in self.rows):
            _fail(QlibOperatorWorkerFailureCode.INCOMPLETE_RESULT, "missing score count mismatch")
        expected_output_hash = _stable_hash(
            {
                "program_hash": self.program_hash,
                "snapshot_hash": self.snapshot_hash,
                "universe_hash": self.universe_hash,
                "input_hash": self.input_hash,
                "runtime_hash": self.runtime_hash,
                "rows": [row.to_canonical() for row in self.rows],
            }
        )
        if self.output_hash != expected_output_hash:
            _fail(QlibOperatorWorkerFailureCode.INCOMPLETE_RESULT, "factor score artifact hash mismatch")


@dataclass(frozen=True, slots=True)
class QlibOperatorWorkerFailure:
    code: QlibOperatorWorkerFailureCode
    message: str
    program_hash: str
    input_hash: str
    retryable: bool = False

    def __post_init__(self) -> None:
        _require_hash(self.program_hash, "program_hash")
        _require_hash(self.input_hash, "input_hash")
        if not self.message or not isinstance(self.retryable, bool):
            _fail(QlibOperatorWorkerFailureCode.INVALID_REQUEST, "invalid worker failure")


def build_score_artifact(
    request: QlibOperatorExecutionRequest,
    runtime: QlibRuntimeIdentity,
    rows: tuple[QlibFactorScoreRow, ...],
) -> QlibFactorScoreArtifact:
    ordered = tuple(sorted(rows, key=lambda row: (row.event_time, row.symbol)))
    output_hash = _stable_hash(
        {
            "program_hash": request.program.expression_hash,
            "snapshot_hash": request.snapshot_hash,
            "universe_hash": request.universe_hash,
            "input_hash": request.input_hash,
            "runtime_hash": runtime.runtime_hash,
            "rows": [row.to_canonical() for row in ordered],
        }
    )
    return QlibFactorScoreArtifact(
        program_hash=request.program.expression_hash,
        snapshot_hash=request.snapshot_hash,
        universe_hash=request.universe_hash,
        input_hash=request.input_hash,
        runtime_hash=runtime.runtime_hash,
        output_hash=output_hash,
        rows=ordered,
        missing_scores=sum(row.score is None for row in ordered),
    )
