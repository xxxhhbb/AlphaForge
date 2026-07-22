"""Pinned pyqlib 0.9.7 operator inventory and non-importing runtime probe."""

from __future__ import annotations

import importlib.metadata
import importlib.util
from dataclasses import dataclass
from typing import Callable

from .models import (
    QlibExpressionError,
    QlibExpressionFailureCode,
    QlibOperatorTrust,
    QlibRuntimeAvailability,
    QlibRuntimeStatus,
    stable_hash,
)


PINNED_PYQLIB_VERSION = "0.9.7"
PINNED_OPS_SOURCE_HASH = "sha256:6f78562448aaeca33375274e2406368a619744d4ca25733837e7463369858691"
PINNED_PIT_SOURCE_HASH = "sha256:faf8d6baa8f79673524f11a4cd8be95103dc743a0bd9d2e2dcc795ddaa6fc884"
OPERATOR_MANIFEST_SCHEMA_VERSION = "veriquant.qlib-operator-manifest/pyqlib-0.9.7-v1"


@dataclass(frozen=True, slots=True)
class QlibOperatorSpec:
    name: str
    argument_count: int
    trust: QlibOperatorTrust
    constraint: str = "NONE"
    category: str = "ELEMENT"

    def to_canonical(self) -> dict[str, object]:
        return {
            "name": self.name,
            "argument_count": self.argument_count,
            "trust": self.trust.value,
            "constraint": self.constraint,
            "category": self.category,
        }


@dataclass(frozen=True, slots=True)
class QlibOperatorManifest:
    operators: tuple[QlibOperatorSpec, ...]
    pyqlib_version: str = PINNED_PYQLIB_VERSION
    ops_source_hash: str = PINNED_OPS_SOURCE_HASH
    pit_source_hash: str = PINNED_PIT_SOURCE_HASH
    schema_version: str = OPERATOR_MANIFEST_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.pyqlib_version != PINNED_PYQLIB_VERSION or self.schema_version != OPERATOR_MANIFEST_SCHEMA_VERSION:
            raise QlibExpressionError(QlibExpressionFailureCode.MANIFEST_INVALID, "operator manifest version mismatch")
        names = tuple(item.name for item in self.operators)
        if len(names) != len(set(names)) or frozenset(names) != PINNED_OPERATOR_NAMES:
            raise QlibExpressionError(
                QlibExpressionFailureCode.MANIFEST_INVALID,
                "manifest must classify every and only pinned pyqlib operator",
            )

    @property
    def by_name(self) -> dict[str, QlibOperatorSpec]:
        return {item.name: item for item in self.operators}

    @property
    def manifest_hash(self) -> str:
        return stable_hash(
            {
                "schema_version": self.schema_version,
                "pyqlib_version": self.pyqlib_version,
                "ops_source_hash": self.ops_source_hash,
                "pit_source_hash": self.pit_source_hash,
                "operators": [item.to_canonical() for item in self.operators],
            }
        )


def _spec(
    name: str,
    arguments: int,
    trust: QlibOperatorTrust = QlibOperatorTrust.TRUSTED,
    constraint: str = "NONE",
    category: str = "ELEMENT",
) -> QlibOperatorSpec:
    return QlibOperatorSpec(name, arguments, trust, constraint, category)


_PINNED_SPECS = (
    _spec("ChangeInstrument", 2, QlibOperatorTrust.TRUSTED_WITH_CONSTRAINTS, "APPROVED_INSTRUMENT", "INSTRUMENT"),
    _spec("Rolling", 3, QlibOperatorTrust.EXCLUDED, "DYNAMIC_METHOD", "INTERNAL"),
    _spec("Ref", 2, QlibOperatorTrust.TRUSTED_WITH_CONSTRAINTS, "PAST_ONLY_REF", "ROLLING"),
    *(_spec(name, 2, QlibOperatorTrust.TRUSTED_WITH_CONSTRAINTS, "BOUNDED_INTEGER_WINDOW", "ROLLING") for name in (
        "Max", "Min", "Sum", "Mean", "Std", "Var", "Skew", "Kurt", "Med", "Mad", "Slope", "Rsquare",
        "Resi", "Rank", "Count", "WMA", "Delta", "IdxMax", "IdxMin",
    )),
    _spec("Quantile", 3, QlibOperatorTrust.TRUSTED_WITH_CONSTRAINTS, "WINDOW_AND_QUANTILE", "ROLLING"),
    _spec("EMA", 2, QlibOperatorTrust.TRUSTED_WITH_CONSTRAINTS, "WINDOW_OR_ALPHA", "ROLLING"),
    _spec("Corr", 3, QlibOperatorTrust.TRUSTED_WITH_CONSTRAINTS, "BOUNDED_INTEGER_WINDOW", "PAIR_ROLLING"),
    _spec("Cov", 3, QlibOperatorTrust.TRUSTED_WITH_CONSTRAINTS, "BOUNDED_INTEGER_WINDOW", "PAIR_ROLLING"),
    *(_spec(name, 1) for name in ("Abs", "Sign", "Log", "Not")),
    *(_spec(name, 2, category="PAIR") for name in (
        "Power", "Add", "Sub", "Mul", "Div", "Greater", "Less", "And", "Or", "Gt", "Ge", "Lt", "Le", "Eq", "Ne",
    )),
    _spec("Mask", 2, QlibOperatorTrust.TRUSTED_WITH_CONSTRAINTS, "APPROVED_INSTRUMENT", "INSTRUMENT"),
    _spec("If", 3, category="CONDITIONAL"),
    _spec("Feature", 1, QlibOperatorTrust.EXCLUDED, "FIELDS_USE_DOLLAR_SYNTAX", "INTERNAL"),
    _spec("PFeature", 1, QlibOperatorTrust.EXCLUDED, "PROVIDER_DEPENDENT", "INTERNAL"),
    _spec("TResample", 3, QlibOperatorTrust.EXCLUDED, "DYNAMIC_METHOD_AND_FREQUENCY", "RESAMPLE"),
    _spec("P", 1, QlibOperatorTrust.EXCLUDED, "PIT_PROVIDER_SEMANTICS_UNCERTIFIED", "PIT"),
    _spec("PRef", 2, QlibOperatorTrust.EXCLUDED, "PIT_PROVIDER_SEMANTICS_UNCERTIFIED", "PIT"),
)

PINNED_OPERATOR_NAMES = frozenset(item.name for item in _PINNED_SPECS)
STATIC_QLIB_OPERATOR_MANIFEST = QlibOperatorManifest(tuple(sorted(_PINNED_SPECS, key=lambda item: item.name)))


def probe_qlib_runtime(
    *,
    module_finder: Callable[[str], object | None] | None = None,
    version_reader: Callable[[str], str] | None = None,
) -> QlibRuntimeAvailability:
    """Check availability without importing Qlib or installing/downloading anything."""

    finder = module_finder or importlib.util.find_spec
    reader = version_reader or importlib.metadata.version
    try:
        module = finder("qlib")
    except (ImportError, AttributeError, ValueError):
        module = None
    if module is None:
        return QlibRuntimeAvailability(
            status=QlibRuntimeStatus.UNAVAILABLE,
            expected_version=PINNED_PYQLIB_VERSION,
            observed_version=None,
            failure_code=QlibExpressionFailureCode.QLIB_RUNTIME_UNAVAILABLE,
            message="pyqlib is unavailable; no installation or download was attempted",
        )
    try:
        observed = reader("pyqlib")
    except importlib.metadata.PackageNotFoundError:
        return QlibRuntimeAvailability(
            status=QlibRuntimeStatus.UNAVAILABLE,
            expected_version=PINNED_PYQLIB_VERSION,
            observed_version=None,
            failure_code=QlibExpressionFailureCode.QLIB_RUNTIME_UNAVAILABLE,
            message="pyqlib distribution metadata is unavailable",
        )
    if observed != PINNED_PYQLIB_VERSION:
        return QlibRuntimeAvailability(
            status=QlibRuntimeStatus.VERSION_MISMATCH,
            expected_version=PINNED_PYQLIB_VERSION,
            observed_version=observed,
            failure_code=QlibExpressionFailureCode.QLIB_RUNTIME_VERSION_MISMATCH,
            message="pyqlib runtime version does not match the certified manifest",
        )
    return QlibRuntimeAvailability(
        status=QlibRuntimeStatus.AVAILABLE,
        expected_version=PINNED_PYQLIB_VERSION,
        observed_version=observed,
        failure_code=None,
        message="pinned pyqlib runtime metadata is available",
    )
