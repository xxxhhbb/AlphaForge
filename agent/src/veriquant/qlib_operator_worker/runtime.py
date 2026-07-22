"""Pinned-runtime preflight that performs no installation or download."""

from __future__ import annotations

import hashlib
import importlib.metadata
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Callable, Mapping, Sequence

from .models import (
    QlibOperatorWorkerFailureCode,
    QlibRuntimeIdentity,
    WORKER_OPERATOR_NAMES,
)


@dataclass(frozen=True, slots=True)
class PinnedQlibBindings:
    identity: QlibRuntimeIdentity
    operator_classes: Mapping[str, type]
    leaf_factory: Callable[[str, Mapping[str, Sequence[float]], str], object]
    clear_cache: Callable[[], None]


@dataclass(frozen=True, slots=True)
class QlibRuntimeLoadFailure:
    code: QlibOperatorWorkerFailureCode
    message: str


def _sha256_file(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def load_pinned_qlib_runtime() -> PinnedQlibBindings | QlibRuntimeLoadFailure:
    """Load only the exact local pyqlib distribution certified by source hashes."""

    expected = QlibRuntimeIdentity.expected()
    try:
        distribution = importlib.metadata.distribution("pyqlib")
    except importlib.metadata.PackageNotFoundError:
        return QlibRuntimeLoadFailure(
            QlibOperatorWorkerFailureCode.QLIB_RUNTIME_UNAVAILABLE,
            "pyqlib is unavailable; no installation or download was attempted",
        )
    observed_version = distribution.version
    if observed_version != expected.pyqlib_version:
        return QlibRuntimeLoadFailure(
            QlibOperatorWorkerFailureCode.QLIB_RUNTIME_VERSION_MISMATCH,
            "pyqlib version does not match pinned worker runtime",
        )
    ops_path = Path(distribution.locate_file("qlib/data/ops.py"))
    pit_path = Path(distribution.locate_file("qlib/data/pit.py"))
    if not ops_path.is_file() or not pit_path.is_file():
        return QlibRuntimeLoadFailure(
            QlibOperatorWorkerFailureCode.QLIB_RUNTIME_SOURCE_MISMATCH,
            "pinned Qlib source files are unavailable",
        )
    observed_ops = _sha256_file(ops_path)
    observed_pit = _sha256_file(pit_path)
    if observed_ops != expected.ops_source_hash or observed_pit != expected.pit_source_hash:
        return QlibRuntimeLoadFailure(
            QlibOperatorWorkerFailureCode.QLIB_RUNTIME_SOURCE_MISMATCH,
            "Qlib operator source hash does not match certified runtime",
        )
    try:
        from .pinned_qlib_097 import BUILTIN_OPERATOR_CLASSES, InMemorySeriesLeaf, clear_expression_cache
    except (ImportError, OSError, ValueError) as exc:
        return QlibRuntimeLoadFailure(
            QlibOperatorWorkerFailureCode.QLIB_RUNTIME_UNAVAILABLE,
            f"pinned Qlib runtime import failed: {type(exc).__name__}",
        )
    if frozenset(BUILTIN_OPERATOR_CLASSES) != WORKER_OPERATOR_NAMES:
        return QlibRuntimeLoadFailure(
            QlibOperatorWorkerFailureCode.QLIB_RUNTIME_SOURCE_MISMATCH,
            "fixed class mapping does not match worker allowlist",
        )
    return PinnedQlibBindings(
        identity=expected,
        operator_classes=MappingProxyType(dict(BUILTIN_OPERATOR_CLASSES)),
        leaf_factory=InMemorySeriesLeaf,
        clear_cache=clear_expression_cache,
    )
