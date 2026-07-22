"""Offline-first Qlib research adapter boundary with auxiliary-only outputs."""

from __future__ import annotations

import importlib.util
import math
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import MappingProxyType
from typing import Mapping, Protocol, Sequence, runtime_checkable

from ..contracts import (
    AuxiliaryEvidence,
    CanonicalContract,
    Component,
    ContractValidationError,
    EvidenceClass,
    FailureCode,
)
from ..contracts.base import (
    freeze_mapping,
    require_aware_datetime,
    require_finite_number,
    require_hash,
    require_identifier,
    require_nonempty_tuple,
    require_text,
)
from ..data import canonical_content_hash
from ..factors import CompiledFactorDAG


_REMOTE_PREFIXES = ("http://", "https://", "ftp://", "s3://", "gs://", "hf://")
_VISIBLE_FORBIDDEN_TOKENS = ("sealed", "test", "official", "promotion")


def _fail(code: FailureCode, message: str, **details) -> None:
    raise ContractValidationError(code, message, details=details)


@dataclass(frozen=True, kw_only=True)
class QlibDateSegment(CanonicalContract):
    start: datetime
    end: datetime

    def __post_init__(self) -> None:
        require_aware_datetime(self.start, "start")
        require_aware_datetime(self.end, "end")
        if self.start > self.end:
            _fail(FailureCode.INVALID_TIME_ORDER, "Qlib segment start follows end")

    def contains(self, event_time: datetime) -> bool:
        return self.start <= event_time <= self.end


@dataclass(frozen=True, kw_only=True)
class QlibCostConfig(CanonicalContract):
    open_cost_bps: float
    close_cost_bps: float
    linear_impact_bps: float
    minimum_cost: float

    def __post_init__(self) -> None:
        for name in ("open_cost_bps", "close_cost_bps", "linear_impact_bps", "minimum_cost"):
            value = getattr(self, name)
            require_finite_number(value, name)
            if value < 0:
                _fail(FailureCode.INVALID_VALUE, f"{name} must be non-negative")


@dataclass(frozen=True, kw_only=True)
class QlibManifest(CanonicalContract):
    """Frozen identity for local Qlib research configuration."""

    provider_uri: str
    region: str
    universe: tuple[str, ...]
    train: QlibDateSegment
    validation: QlibDateSegment
    test: QlibDateSegment
    label: str
    model: str
    strategy: str
    cost: QlibCostConfig
    seed: int

    def __post_init__(self) -> None:
        require_text(self.provider_uri, "provider_uri")
        lowered = self.provider_uri.lower()
        if lowered.startswith(_REMOTE_PREFIXES) or not Path(self.provider_uri).is_absolute():
            _fail(
                FailureCode.INVALID_VALUE,
                "provider_uri must be an explicit absolute local path",
            )
        require_text(self.region, "region")
        require_nonempty_tuple(self.universe, "universe")
        if any(not isinstance(item, str) or not item for item in self.universe):
            _fail(FailureCode.INVALID_VALUE, "universe values must be non-empty strings")
        if not all(
            isinstance(item, QlibDateSegment) for item in (self.train, self.validation, self.test)
        ):
            _fail(FailureCode.INVALID_VALUE, "train/validation/test must be QlibDateSegment")
        if self.train.end >= self.validation.start or self.validation.end >= self.test.start:
            _fail(FailureCode.INVALID_TIME_ORDER, "Qlib train/validation/test segments must be disjoint")
        require_text(self.label, "label")
        require_text(self.model, "model")
        require_text(self.strategy, "strategy")
        if not isinstance(self.cost, QlibCostConfig):
            _fail(FailureCode.INVALID_VALUE, "cost must be QlibCostConfig")
        if isinstance(self.seed, bool) or not isinstance(self.seed, int) or self.seed < 0:
            _fail(FailureCode.INVALID_VALUE, "seed must be a non-negative integer")

    def visible(self) -> "VisibleQlibManifest":
        return VisibleQlibManifest(
            manifest_hash=self.artifact_hash,
            provider_uri=self.provider_uri,
            region=self.region,
            universe=self.universe,
            train=self.train,
            validation=self.validation,
            label=self.label,
            model=self.model,
            strategy=self.strategy,
            cost=self.cost,
            seed=self.seed,
        )


@dataclass(frozen=True, kw_only=True)
class VisibleQlibManifest(CanonicalContract):
    """Train/validation-only view passed to a visible research runner."""

    manifest_hash: str
    provider_uri: str
    region: str
    universe: tuple[str, ...]
    train: QlibDateSegment
    validation: QlibDateSegment
    label: str
    model: str
    strategy: str
    cost: QlibCostConfig
    seed: int

    def __post_init__(self) -> None:
        require_hash(self.manifest_hash, "manifest_hash")
        if any(name in {"test", "sealed"} for name in self.to_dict() if name not in {"contract_type", "schema_version"}):
            _fail(FailureCode.UNAUTHORIZED_CAPABILITY, "visible manifest contains sealed fields")


@dataclass(frozen=True, kw_only=True)
class QlibVisibleRow(CanonicalContract):
    partition: str
    event_time: datetime
    symbol: str
    score: float
    label: float

    def __post_init__(self) -> None:
        if self.partition not in {"train", "validation"}:
            _fail(FailureCode.UNAUTHORIZED_CAPABILITY, "visible Qlib rows are train/validation only")
        require_aware_datetime(self.event_time, "event_time")
        require_identifier(self.symbol, "symbol")
        require_finite_number(self.score, "score")
        require_finite_number(self.label, "label")


@dataclass(frozen=True, kw_only=True)
class AdapterFailure(CanonicalContract):
    adapter: str
    code: FailureCode
    message: str
    manifest_hash: str
    retryable: bool = False

    def __post_init__(self) -> None:
        require_identifier(self.adapter, "adapter")
        if self.code not in {
            FailureCode.ADAPTER_UNAVAILABLE,
            FailureCode.ADAPTER_EXECUTION_FAILED,
            FailureCode.QLIB_DATA_UNAVAILABLE,
            FailureCode.UNAUTHORIZED_CAPABILITY,
        }:
            _fail(FailureCode.INVALID_VALUE, "adapter failure code is not adapter-scoped")
        require_text(self.message, "message")
        require_hash(self.manifest_hash, "manifest_hash")
        if type(self.retryable) is not bool:
            _fail(FailureCode.INVALID_VALUE, "retryable must be boolean")


@dataclass(frozen=True, kw_only=True)
class QlibVisibleResult(CanonicalContract):
    manifest_hash: str
    candidate_hash: str
    auxiliary_evidence: AuxiliaryEvidence
    train_metrics: Mapping[str, float]
    validation_metrics: Mapping[str, float]

    def __post_init__(self) -> None:
        require_hash(self.manifest_hash, "manifest_hash")
        require_hash(self.candidate_hash, "candidate_hash")
        if not isinstance(self.auxiliary_evidence, AuxiliaryEvidence):
            _fail(FailureCode.INVALID_VALUE, "Qlib result requires AuxiliaryEvidence")
        if self.auxiliary_evidence.source_component is not Component.QLIB:
            _fail(FailureCode.INVALID_ARTIFACT_PRODUCER, "Qlib result evidence must come from QLIB")
        if self.auxiliary_evidence.evidence_class is not EvidenceClass.AUXILIARY_RESEARCH_EVIDENCE:
            _fail(FailureCode.AUXILIARY_MASQUERADE, "Qlib cannot emit official evidence")
        object.__setattr__(self, "train_metrics", freeze_mapping(self.train_metrics, "train_metrics", numeric_values=True))
        object.__setattr__(
            self,
            "validation_metrics",
            freeze_mapping(self.validation_metrics, "validation_metrics", numeric_values=True),
        )
        for key in (
            *self.train_metrics,
            *self.validation_metrics,
            *self.auxiliary_evidence.metrics,
        ):
            normalized = key.lower()
            if any(token in normalized for token in _VISIBLE_FORBIDDEN_TOKENS):
                _fail(FailureCode.UNAUTHORIZED_CAPABILITY, "visible Qlib metrics contain a forbidden key")


@runtime_checkable
class QlibResearchAdapter(Protocol):
    """Visible-research protocol; deliberately has no sealed-test method."""

    def run_visible(
        self,
        *,
        manifest: QlibManifest,
        candidate: CompiledFactorDAG,
        rows: Sequence[QlibVisibleRow],
        as_of: datetime,
    ) -> QlibVisibleResult | AdapterFailure: ...


class LocalQlibVisibleRunner(Protocol):
    """Trusted local integration point; receives no test/sealed segment."""

    def run_visible(
        self,
        *,
        manifest: VisibleQlibManifest,
        candidate: CompiledFactorDAG,
        rows: tuple[QlibVisibleRow, ...],
    ) -> Mapping[str, Mapping[str, float]] | AdapterFailure: ...


def _ranks(values: Sequence[float]) -> tuple[float, ...]:
    order = sorted(range(len(values)), key=lambda index: (values[index], index))
    result = [0.0] * len(values)
    cursor = 0
    while cursor < len(order):
        end = cursor + 1
        while end < len(order) and values[order[end]] == values[order[cursor]]:
            end += 1
        rank = (cursor + 1 + end) / 2.0
        for index in order[cursor:end]:
            result[index] = rank
        cursor = end
    return tuple(result)


def _pearson(left: Sequence[float], right: Sequence[float]) -> float:
    if len(left) != len(right) or len(left) < 2:
        return 0.0
    left_mean = sum(left) / len(left)
    right_mean = sum(right) / len(right)
    numerator = sum((x - left_mean) * (y - right_mean) for x, y in zip(left, right))
    left_ss = sum((x - left_mean) ** 2 for x in left)
    right_ss = sum((y - right_mean) ** 2 for y in right)
    denominator = math.sqrt(left_ss * right_ss)
    return 0.0 if denominator == 0.0 else numerator / denominator


def _measured_metrics(rows: Sequence[QlibVisibleRow]) -> Mapping[str, float]:
    scores = tuple(row.score for row in rows)
    labels = tuple(row.label for row in rows)
    return MappingProxyType(
        {
            "rank_ic": _pearson(_ranks(scores), _ranks(labels)),
            "mean_score": sum(scores) / len(scores),
            "observations": float(len(rows)),
        }
    )


def _validate_rows(
    manifest: VisibleQlibManifest, rows: Sequence[QlibVisibleRow]
) -> tuple[QlibVisibleRow, ...]:
    if not isinstance(rows, (list, tuple)) or not rows:
        _fail(FailureCode.INVALID_VALUE, "visible Qlib rows must be a non-empty sequence")
    ordered = tuple(sorted(rows, key=lambda row: (row.partition, row.event_time, row.symbol)))
    if any(not isinstance(row, QlibVisibleRow) for row in ordered):
        _fail(FailureCode.INVALID_VALUE, "visible Qlib rows contain an invalid value")
    train = tuple(row for row in ordered if row.partition == "train")
    validation = tuple(row for row in ordered if row.partition == "validation")
    if not train or not validation:
        _fail(FailureCode.INVALID_VALUE, "both train and validation rows are required")
    if any(not manifest.train.contains(row.event_time) for row in train):
        _fail(FailureCode.INVALID_TIME_ORDER, "train row lies outside frozen train segment")
    if any(not manifest.validation.contains(row.event_time) for row in validation):
        _fail(FailureCode.INVALID_TIME_ORDER, "validation row lies outside frozen validation segment")
    return ordered


def _numeric_metrics(value: Mapping[str, float], field: str) -> Mapping[str, float]:
    if not isinstance(value, Mapping) or not value:
        _fail(FailureCode.ADAPTER_EXECUTION_FAILED, f"{field} must be a non-empty metric mapping")
    result: dict[str, float] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not key:
            _fail(FailureCode.ADAPTER_EXECUTION_FAILED, f"{field} metric keys must be strings")
        if any(token in key.lower() for token in _VISIBLE_FORBIDDEN_TOKENS):
            _fail(FailureCode.UNAUTHORIZED_CAPABILITY, "local Qlib runner returned forbidden metrics")
        require_finite_number(item, f"{field}.{key}")
        result[key] = float(item)
    return MappingProxyType(dict(sorted(result.items())))


def _build_result(
    *,
    manifest: QlibManifest,
    visible: VisibleQlibManifest,
    candidate: CompiledFactorDAG,
    rows: tuple[QlibVisibleRow, ...],
    as_of: datetime,
    train_metrics: Mapping[str, float],
    validation_metrics: Mapping[str, float],
) -> QlibVisibleResult:
    require_aware_datetime(as_of, "as_of")
    train_metrics = _numeric_metrics(train_metrics, "train_metrics")
    validation_metrics = _numeric_metrics(validation_metrics, "validation_metrics")
    rows_hash = canonical_content_hash(rows)
    experiment_hash = canonical_content_hash(
        {
            "manifest_hash": manifest.artifact_hash,
            "visible_manifest_hash": visible.artifact_hash,
            "candidate_hash": candidate.artifact_hash,
            "rows_hash": rows_hash,
        }
    )
    combined = {
        **{f"train_{key}": value for key, value in train_metrics.items()},
        **{f"validation_{key}": value for key, value in validation_metrics.items()},
    }
    evidence = AuxiliaryEvidence(
        evidence_id=f"qlib-aux-{experiment_hash[7:27]}",
        source_component=Component.QLIB,
        experiment_identity_hash=experiment_hash,
        metrics=combined,
        artifact_hashes=(visible.artifact_hash, rows_hash, candidate.artifact_hash),
        event_time=as_of,
        published_at=as_of,
        available_at=as_of,
    )
    return QlibVisibleResult(
        manifest_hash=manifest.artifact_hash,
        candidate_hash=candidate.artifact_hash,
        auxiliary_evidence=evidence,
        train_metrics=train_metrics,
        validation_metrics=validation_metrics,
    )


class FakeQlibAdapter:
    """Default deterministic adapter over caller-supplied offline visible rows."""

    def run_visible(
        self,
        *,
        manifest: QlibManifest,
        candidate: CompiledFactorDAG,
        rows: Sequence[QlibVisibleRow],
        as_of: datetime,
    ) -> QlibVisibleResult:
        visible = manifest.visible()
        ordered = _validate_rows(visible, rows)
        train_rows = tuple(row for row in ordered if row.partition == "train")
        validation_rows = tuple(row for row in ordered if row.partition == "validation")
        return _build_result(
            manifest=manifest,
            visible=visible,
            candidate=candidate,
            rows=ordered,
            as_of=as_of,
            train_metrics=_measured_metrics(train_rows),
            validation_metrics=_measured_metrics(validation_rows),
        )


class OptionalPyQlibAdapter:
    """Opt-in local-only pyqlib bridge; unavailable conditions are typed results."""

    def __init__(self, *, runner: LocalQlibVisibleRunner | None = None) -> None:
        self._runner = runner

    @staticmethod
    def _failure(manifest: QlibManifest, code: FailureCode, message: str) -> AdapterFailure:
        return AdapterFailure(
            adapter="optional-pyqlib",
            code=code,
            message=message,
            manifest_hash=manifest.artifact_hash,
        )

    def run_visible(
        self,
        *,
        manifest: QlibManifest,
        candidate: CompiledFactorDAG,
        rows: Sequence[QlibVisibleRow],
        as_of: datetime,
    ) -> QlibVisibleResult | AdapterFailure:
        if os.environ.get("ENABLE_QLIB_ADAPTER") != "1":
            return self._failure(
                manifest,
                FailureCode.ADAPTER_UNAVAILABLE,
                "optional pyqlib adapter is disabled; set ENABLE_QLIB_ADAPTER=1 explicitly",
            )
        provider = Path(manifest.provider_uri).expanduser()
        if not provider.is_dir():
            return self._failure(
                manifest,
                FailureCode.QLIB_DATA_UNAVAILABLE,
                "frozen local Qlib provider directory is unavailable",
            )
        probe = getattr(self._runner, "pyqlib_available", None)
        if callable(probe):
            try:
                available = bool(probe(manifest.artifact_hash))
            except Exception:
                available = False
            if not available:
                return self._failure(
                    manifest,
                    FailureCode.ADAPTER_UNAVAILABLE,
                    "pyqlib is unavailable in the isolated QLIB_PYTHON runtime",
                )
        elif importlib.util.find_spec("qlib") is None:
            return self._failure(
                manifest,
                FailureCode.ADAPTER_UNAVAILABLE,
                "pyqlib is not installed in the active environment",
            )
        if self._runner is None:
            return self._failure(
                manifest,
                FailureCode.ADAPTER_UNAVAILABLE,
                "no trusted local visible-only Qlib runner is configured",
            )
        visible = manifest.visible()
        try:
            ordered = _validate_rows(visible, rows)
            payload = self._runner.run_visible(
                manifest=visible,
                candidate=candidate,
                rows=ordered,
            )
            if isinstance(payload, AdapterFailure):
                return payload
            if not isinstance(payload, Mapping) or set(payload) != {"train", "validation"}:
                _fail(
                    FailureCode.ADAPTER_EXECUTION_FAILED,
                    "local Qlib runner must return exactly train and validation metrics",
                )
            return _build_result(
                manifest=manifest,
                visible=visible,
                candidate=candidate,
                rows=ordered,
                as_of=as_of,
                train_metrics=payload["train"],
                validation_metrics=payload["validation"],
            )
        except ContractValidationError as exc:
            if exc.code is FailureCode.UNAUTHORIZED_CAPABILITY:
                code = FailureCode.UNAUTHORIZED_CAPABILITY
            else:
                code = FailureCode.ADAPTER_EXECUTION_FAILED
            return self._failure(manifest, code, str(exc))
        except Exception as exc:  # boundary converts optional dependency failures to typed results
            return self._failure(
                manifest,
                FailureCode.ADAPTER_EXECUTION_FAILED,
                f"local Qlib runner failed: {type(exc).__name__}",
            )


def default_qlib_adapter() -> QlibResearchAdapter:
    """Return the dependency-free, network-free default adapter."""

    return FakeQlibAdapter()
