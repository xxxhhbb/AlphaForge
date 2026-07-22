"""Immutable, PIT-visible contracts for registered research context.

This module contains no promotion, order, broker, sealed-test, or portfolio
construction authority.  It is a read-only research index over artifacts that
already exist elsewhere.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from types import MappingProxyType
from typing import Mapping

from ..contracts import CanonicalContract
from ..contracts.base import (
    fail,
    require_aware_datetime,
    require_finite_number,
    require_hash,
    require_identifier,
    require_text,
    require_time_order,
)
from ..contracts.enums import FailureCode
from ..evidence.models import VISIBLE_SUMMARY_METRICS


class PortfolioScope(str, Enum):
    REFERENCE_RESEARCH_PORTFOLIO = "REFERENCE_RESEARCH_PORTFOLIO"
    USER_PORTFOLIO = "USER_PORTFOLIO"


class VisiblePartition(str, Enum):
    TRAIN = "TRAIN"
    VALIDATION = "VALIDATION"


def _version(value: int, name: str = "version") -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        fail(FailureCode.INVALID_VALUE, f"{name} must be a positive integer")


def _bounded(value: float, name: str, lower: float, upper: float) -> None:
    require_finite_number(value, name)
    if not lower <= float(value) <= upper:
        fail(FailureCode.INVALID_VALUE, f"{name} must be between {lower} and {upper}")


@dataclass(frozen=True, kw_only=True)
class RegisteredFactorMetadata(CanonicalContract):
    factor_id: str
    version: int
    family: str
    factor_hash: str
    source_hash: str
    status: str
    registered_at: datetime
    available_at: datetime
    source_artifact_hash: str

    def __post_init__(self) -> None:
        require_identifier(self.factor_id, "factor_id")
        _version(self.version)
        require_identifier(self.family, "family")
        for name in ("factor_hash", "source_hash", "source_artifact_hash"):
            require_hash(getattr(self, name), name)
        if self.status not in {"REGISTERED", "CHALLENGER", "RETIRED", "REJECTED", "REFERENCE"}:
            fail(FailureCode.INVALID_STATE, "unsupported registered factor status")
        require_time_order(("registered_at", self.registered_at), ("available_at", self.available_at))


@dataclass(frozen=True, kw_only=True)
class FactorScorePoint(CanonicalContract):
    factor_id: str
    version: int
    symbol: str
    decision_time: datetime
    available_at: datetime
    score: float
    source_artifact_hash: str

    def __post_init__(self) -> None:
        require_identifier(self.factor_id, "factor_id")
        _version(self.version)
        require_identifier(self.symbol, "symbol")
        require_aware_datetime(self.decision_time, "decision_time")
        require_aware_datetime(self.available_at, "available_at")
        if self.available_at > self.decision_time:
            fail(FailureCode.INVALID_TIME_ORDER, "score was unavailable at its decision_time")
        require_finite_number(self.score, "score")
        require_hash(self.source_artifact_hash, "source_artifact_hash")


@dataclass(frozen=True, kw_only=True)
class VisibleEvidenceRecord(CanonicalContract):
    factor_id: str
    version: int
    identity_hash: str
    evidence_hash: str
    partition: VisiblePartition
    metrics: Mapping[str, float]
    computed_at: datetime
    available_at: datetime
    source_artifact_hash: str

    def __post_init__(self) -> None:
        require_identifier(self.factor_id, "factor_id")
        _version(self.version)
        for name in ("identity_hash", "evidence_hash", "source_artifact_hash"):
            require_hash(getattr(self, name), name)
        if not isinstance(self.partition, VisiblePartition):
            fail(FailureCode.UNAUTHORIZED_CAPABILITY, "only TRAIN/VALIDATION evidence is visible")
        if not isinstance(self.metrics, Mapping) or not self.metrics:
            fail(FailureCode.INVALID_VALUE, "visible evidence metrics must be non-empty")
        if set(self.metrics) - VISIBLE_SUMMARY_METRICS:
            fail(FailureCode.UNAUTHORIZED_CAPABILITY, "evidence contains non-visible metrics")
        frozen: dict[str, float] = {}
        for key, value in self.metrics.items():
            require_finite_number(value, f"metrics.{key}")
            frozen[key] = float(value)
        object.__setattr__(self, "metrics", MappingProxyType(frozen))
        require_time_order(("computed_at", self.computed_at), ("available_at", self.available_at))


@dataclass(frozen=True, kw_only=True)
class FactorCorrelationRecord(CanonicalContract):
    left_factor_id: str
    left_version: int
    right_factor_id: str
    right_version: int
    correlation: float
    as_of: datetime
    available_at: datetime
    source_artifact_hash: str

    def __post_init__(self) -> None:
        for name in ("left_factor_id", "right_factor_id"):
            require_identifier(getattr(self, name), name)
        _version(self.left_version, "left_version")
        _version(self.right_version, "right_version")
        if (self.left_factor_id, self.left_version) >= (self.right_factor_id, self.right_version):
            fail(FailureCode.INVALID_VALUE, "correlation pair must use canonical distinct ordering")
        _bounded(self.correlation, "correlation", -1.0, 1.0)
        require_time_order(("as_of", self.as_of), ("available_at", self.available_at))
        require_hash(self.source_artifact_hash, "source_artifact_hash")


@dataclass(frozen=True, kw_only=True)
class FactorExposureRecord(CanonicalContract):
    factor_id: str
    version: int
    exposure_name: str
    exposure_value: float
    as_of: datetime
    available_at: datetime
    source_artifact_hash: str

    def __post_init__(self) -> None:
        require_identifier(self.factor_id, "factor_id")
        _version(self.version)
        require_identifier(self.exposure_name, "exposure_name")
        require_finite_number(self.exposure_value, "exposure_value")
        require_time_order(("as_of", self.as_of), ("available_at", self.available_at))
        require_hash(self.source_artifact_hash, "source_artifact_hash")


@dataclass(frozen=True, kw_only=True)
class FactorCostTurnoverRecord(CanonicalContract):
    factor_id: str
    version: int
    turnover: float
    spread_cost: float
    linear_impact_cost: float
    as_of: datetime
    available_at: datetime
    source_artifact_hash: str

    def __post_init__(self) -> None:
        require_identifier(self.factor_id, "factor_id")
        _version(self.version)
        for name in ("turnover", "spread_cost", "linear_impact_cost"):
            value = getattr(self, name)
            require_finite_number(value, name)
            if value < 0:
                fail(FailureCode.INVALID_VALUE, f"{name} cannot be negative")
        require_time_order(("as_of", self.as_of), ("available_at", self.available_at))
        require_hash(self.source_artifact_hash, "source_artifact_hash")


@dataclass(frozen=True, kw_only=True)
class PortfolioMembershipRecord(CanonicalContract):
    portfolio_id: str
    portfolio_scope: PortfolioScope
    factor_id: str
    factor_version: int
    weight: float
    effective_at: datetime
    available_at: datetime
    source_artifact_hash: str

    def __post_init__(self) -> None:
        require_identifier(self.portfolio_id, "portfolio_id")
        if not isinstance(self.portfolio_scope, PortfolioScope):
            fail(FailureCode.INVALID_VALUE, "portfolio_scope must be explicit")
        if self.portfolio_scope is PortfolioScope.REFERENCE_RESEARCH_PORTFOLIO and not self.portfolio_id.startswith("reference-"):
            fail(FailureCode.INVALID_IDENTIFIER, "reference portfolio id must start with reference-")
        require_identifier(self.factor_id, "factor_id")
        _version(self.factor_version, "factor_version")
        _bounded(self.weight, "weight", -10.0, 10.0)
        require_time_order(("effective_at", self.effective_at), ("available_at", self.available_at))
        require_hash(self.source_artifact_hash, "source_artifact_hash")


@dataclass(frozen=True, kw_only=True)
class PortfolioReturnPoint(CanonicalContract):
    portfolio_id: str
    portfolio_scope: PortfolioScope
    event_time: datetime
    available_at: datetime
    return_value: float
    nav: float | None
    turnover: float | None
    source_artifact_hash: str

    def __post_init__(self) -> None:
        require_identifier(self.portfolio_id, "portfolio_id")
        if not isinstance(self.portfolio_scope, PortfolioScope):
            fail(FailureCode.INVALID_VALUE, "portfolio_scope must be explicit")
        if self.portfolio_scope is PortfolioScope.REFERENCE_RESEARCH_PORTFOLIO and not self.portfolio_id.startswith("reference-"):
            fail(FailureCode.INVALID_IDENTIFIER, "reference portfolio id must start with reference-")
        require_time_order(("event_time", self.event_time), ("available_at", self.available_at))
        require_finite_number(self.return_value, "return_value")
        if self.nav is not None:
            require_finite_number(self.nav, "nav")
            if self.nav < 0:
                fail(FailureCode.INVALID_VALUE, "nav cannot be negative")
        if self.turnover is not None:
            require_finite_number(self.turnover, "turnover")
            if self.turnover < 0:
                fail(FailureCode.INVALID_VALUE, "turnover cannot be negative")
        require_hash(self.source_artifact_hash, "source_artifact_hash")


@dataclass(frozen=True, kw_only=True)
class RegisteredResearchContext(CanonicalContract):
    factors: tuple[RegisteredFactorMetadata, ...] = ()
    score_history: tuple[FactorScorePoint, ...] = ()
    visible_evidence: tuple[VisibleEvidenceRecord, ...] = ()
    correlations: tuple[FactorCorrelationRecord, ...] = ()
    exposures: tuple[FactorExposureRecord, ...] = ()
    cost_turnover: tuple[FactorCostTurnoverRecord, ...] = ()
    portfolio_membership: tuple[PortfolioMembershipRecord, ...] = ()
    portfolio_returns: tuple[PortfolioReturnPoint, ...] = ()

    def __post_init__(self) -> None:
        factors = {(item.factor_id, item.version) for item in self.factors}
        if len(factors) != len(self.factors):
            fail(FailureCode.INVALID_VALUE, "duplicate registered factor version")
        for collection in (self.score_history, self.visible_evidence, self.correlations, self.exposures, self.cost_turnover, self.portfolio_membership):
            for item in collection:
                factor_keys = []
                if hasattr(item, "factor_id"):
                    factor_keys.append((item.factor_id, getattr(item, "version", getattr(item, "factor_version", None))))
                if hasattr(item, "left_factor_id"):
                    factor_keys.extend(((item.left_factor_id, item.left_version), (item.right_factor_id, item.right_version)))
                if any(key not in factors for key in factor_keys):
                    fail(FailureCode.INVALID_VALUE, "context record references an unregistered factor version")


@dataclass(frozen=True, kw_only=True)
class ImportAudit(CanonicalContract):
    source_artifact_hashes: tuple[str, ...]
    imported_counts: Mapping[str, int]
    missing_capabilities: tuple[str, ...]
    notes: tuple[str, ...]

    def __post_init__(self) -> None:
        for value in self.source_artifact_hashes:
            require_hash(value, "source_artifact_hashes")
        frozen: dict[str, int] = {}
        for key, value in self.imported_counts.items():
            require_identifier(key, "imported_counts key")
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                fail(FailureCode.INVALID_VALUE, "imported count must be non-negative")
            frozen[key] = value
        object.__setattr__(self, "imported_counts", MappingProxyType(frozen))
        for value in self.missing_capabilities + self.notes:
            require_text(value, "audit text")


__all__ = [
    "FactorCorrelationRecord", "FactorCostTurnoverRecord", "FactorExposureRecord",
    "FactorScorePoint", "ImportAudit", "PortfolioMembershipRecord", "PortfolioReturnPoint",
    "PortfolioScope", "RegisteredFactorMetadata", "RegisteredResearchContext",
    "VisibleEvidenceRecord", "VisiblePartition",
]
