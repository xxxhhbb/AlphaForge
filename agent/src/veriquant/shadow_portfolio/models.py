"""Typed entities for offline shadow portfolio monitoring."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping

from ..contracts import CanonicalContract, ContractValidationError, FailureCode
from ..contracts.base import (
    freeze_mapping,
    require_aware_datetime,
    require_finite_number,
    require_hash,
    require_identifier,
    require_nonempty_tuple,
    require_text,
    require_time_order,
)


class ShadowStatus(str, Enum):
    READY = "READY"
    BLOCKED = "BLOCKED"


class ChallengerState(str, Enum):
    CHALLENGER = "CHALLENGER"
    VALIDATED_RESEARCH = "VALIDATED_RESEARCH"
    DRAFT = "DRAFT"
    REJECTED = "REJECTED"
    RETIRED = "RETIRED"


class FillSide(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class FillStatus(str, Enum):
    FILLED = "FILLED"
    PARTIAL = "PARTIAL"


class AlertSeverity(str, Enum):
    INFO = "INFO"
    WARN = "WARN"
    HALT_RECOMMENDED = "HALT_RECOMMENDED"


def _fail(code: FailureCode, message: str, **details: Any) -> None:
    raise ContractValidationError(code, message, details=details)


def _freeze_str_float_mapping(value: Mapping[str, float], field_name: str) -> Mapping[str, float]:
    return freeze_mapping(value, field_name, numeric_values=True)


def _freeze_nested_str_float_mapping(
    value: Mapping[str, Mapping[str, float]], field_name: str
) -> Mapping[str, Mapping[str, float]]:
    if not isinstance(value, Mapping):
        _fail(FailureCode.INVALID_VALUE, f"{field_name} must be a mapping")
    frozen: dict[str, Mapping[str, float]] = {}
    for key, item in value.items():
        require_identifier(key, f"{field_name}.key")
        frozen[key] = _freeze_str_float_mapping(item, f"{field_name}.{key}")
    return MappingProxyType(frozen)


@dataclass(frozen=True, kw_only=True)
class ShadowRunRequest(CanonicalContract):
    run_id: str
    account_id: str
    vintage_id: str
    policy_version: str
    policy_hash: str
    decision_time: datetime
    execution_time: datetime
    valuation_time: datetime
    starting_cash: float
    previous_positions: Mapping[str, float]
    max_gross_weight: float = 1.0
    transaction_cost_bps: float = 5.0
    slippage_bps: float = 10.0
    max_adv_participation: float = 0.10
    seed: int = 0

    def __post_init__(self) -> None:
        for field_name in ("run_id", "account_id", "vintage_id", "policy_version"):
            require_identifier(getattr(self, field_name), field_name)
        require_hash(self.policy_hash, "policy_hash")
        require_time_order(
            ("decision_time", self.decision_time),
            ("execution_time", self.execution_time),
            ("valuation_time", self.valuation_time),
        )
        if self.decision_time == self.execution_time:
            _fail(FailureCode.INVALID_TIME_ORDER, "execution_time must be after decision_time")
        for field_name in (
            "starting_cash",
            "max_gross_weight",
            "transaction_cost_bps",
            "slippage_bps",
            "max_adv_participation",
        ):
            require_finite_number(getattr(self, field_name), field_name)
        if self.starting_cash <= 0:
            _fail(FailureCode.INVALID_VALUE, "starting_cash must be positive")
        if not 0.0 < self.max_gross_weight <= 5.0:
            _fail(FailureCode.INVALID_VALUE, "max_gross_weight must be in (0, 5]")
        if self.transaction_cost_bps < 0 or self.slippage_bps < 0:
            _fail(FailureCode.INVALID_VALUE, "cost and slippage must be non-negative")
        if not 0.0 < self.max_adv_participation <= 1.0:
            _fail(FailureCode.INVALID_VALUE, "max_adv_participation must be in (0, 1]")
        if isinstance(self.seed, bool) or not isinstance(self.seed, int) or self.seed < 0:
            _fail(FailureCode.INVALID_VALUE, "seed must be a non-negative integer")
        object.__setattr__(
            self,
            "previous_positions",
            _freeze_str_float_mapping(self.previous_positions, "previous_positions"),
        )


@dataclass(frozen=True, kw_only=True)
class DataVintage(CanonicalContract):
    vintage_id: str
    revision_id: str
    dataset_hash: str
    universe: tuple[str, ...]
    data_available_time: datetime
    decision_time: datetime
    corporate_action_available_time: datetime
    coverage_by_symbol: Mapping[str, float]
    beta_by_symbol: Mapping[str, float]
    industry_by_symbol: Mapping[str, str]
    adv_by_symbol: Mapping[str, float]

    def __post_init__(self) -> None:
        for field_name in ("vintage_id", "revision_id"):
            require_identifier(getattr(self, field_name), field_name)
        require_hash(self.dataset_hash, "dataset_hash")
        require_nonempty_tuple(self.universe, "universe")
        if any(not isinstance(item, str) or not item for item in self.universe):
            _fail(FailureCode.INVALID_VALUE, "universe entries must be non-empty strings")
        require_aware_datetime(self.data_available_time, "data_available_time")
        require_aware_datetime(self.decision_time, "decision_time")
        require_aware_datetime(self.corporate_action_available_time, "corporate_action_available_time")
        if self.data_available_time > self.decision_time:
            _fail(FailureCode.INVALID_TIME_ORDER, "data vintage is not available at decision time")
        if self.corporate_action_available_time > self.decision_time:
            _fail(FailureCode.INVALID_TIME_ORDER, "corporate actions are not available at decision time")
        for field_name in ("coverage_by_symbol", "beta_by_symbol", "adv_by_symbol"):
            object.__setattr__(
                self,
                field_name,
                _freeze_str_float_mapping(getattr(self, field_name), field_name),
            )
        if not isinstance(self.industry_by_symbol, Mapping):
            _fail(FailureCode.INVALID_VALUE, "industry_by_symbol must be a mapping")
        missing = set(self.universe) - set(self.coverage_by_symbol)
        if missing:
            _fail(FailureCode.MISSING_REQUIRED_FIELD, "coverage missing universe members", symbols=tuple(sorted(missing)))
        object.__setattr__(self, "industry_by_symbol", MappingProxyType(dict(self.industry_by_symbol)))


@dataclass(frozen=True, kw_only=True)
class ChallengerVersion(CanonicalContract):
    challenger_id: str
    version: str
    state: ChallengerState
    policy_version: str
    policy_hash: str
    promotion_decision_hash: str
    evidence_hash: str
    factor_hash: str
    scores_by_symbol: Mapping[str, float]
    baseline_factor_mean: float
    baseline_factor_std: float
    baseline_ic_proxy: float
    expected_contribution: float

    def __post_init__(self) -> None:
        for field_name in ("challenger_id", "version", "policy_version"):
            require_identifier(getattr(self, field_name), field_name)
        if not isinstance(self.state, ChallengerState):
            object.__setattr__(self, "state", ChallengerState(self.state))
        for field_name in ("policy_hash", "promotion_decision_hash", "evidence_hash", "factor_hash"):
            require_hash(getattr(self, field_name), field_name)
        object.__setattr__(
            self,
            "scores_by_symbol",
            _freeze_str_float_mapping(self.scores_by_symbol, "scores_by_symbol"),
        )
        for field_name in (
            "baseline_factor_mean",
            "baseline_factor_std",
            "baseline_ic_proxy",
            "expected_contribution",
        ):
            require_finite_number(getattr(self, field_name), field_name)
        if self.baseline_factor_std < 0:
            _fail(FailureCode.INVALID_VALUE, "baseline_factor_std cannot be negative")


@dataclass(frozen=True, kw_only=True)
class TargetPosition(CanonicalContract):
    symbol: str
    target_weight: float
    target_quantity: float
    target_value: float
    source_challenger_id: str
    source_challenger_version: str

    def __post_init__(self) -> None:
        for field_name in ("symbol", "source_challenger_id", "source_challenger_version"):
            require_identifier(getattr(self, field_name), field_name)
        for field_name in ("target_weight", "target_quantity", "target_value"):
            require_finite_number(getattr(self, field_name), field_name)


@dataclass(frozen=True, kw_only=True)
class MarketPrice(CanonicalContract):
    symbol: str
    price: float
    price_time: datetime
    available_time: datetime
    price_kind: str
    suspended: bool = False
    limit_up: bool = False
    limit_down: bool = False

    def __post_init__(self) -> None:
        require_identifier(self.symbol, "symbol")
        require_finite_number(self.price, "price")
        if self.price <= 0:
            _fail(FailureCode.RISK_INPUT_INVALID, "price must be positive")
        require_aware_datetime(self.price_time, "price_time")
        require_aware_datetime(self.available_time, "available_time")
        if self.price_kind not in {"open", "mid", "vwap", "mark"}:
            _fail(FailureCode.RISK_INPUT_INVALID, "unsupported price kind")
        for field_name in ("suspended", "limit_up", "limit_down"):
            if type(getattr(self, field_name)) is not bool:
                _fail(FailureCode.RISK_INPUT_INVALID, f"{field_name} must be boolean")


@dataclass(frozen=True, kw_only=True)
class PaperFill(CanonicalContract):
    fill_id: str
    run_id: str
    symbol: str
    side: FillSide
    requested_quantity: float
    filled_quantity: float
    execution_price: float
    execution_time: datetime
    transaction_cost: float
    slippage_cost: float
    status: FillStatus

    def __post_init__(self) -> None:
        for field_name in ("fill_id", "run_id", "symbol"):
            require_identifier(getattr(self, field_name), field_name)
        if not isinstance(self.side, FillSide):
            object.__setattr__(self, "side", FillSide(self.side))
        if not isinstance(self.status, FillStatus):
            object.__setattr__(self, "status", FillStatus(self.status))
        for field_name in (
            "requested_quantity",
            "filled_quantity",
            "execution_price",
            "transaction_cost",
            "slippage_cost",
        ):
            require_finite_number(getattr(self, field_name), field_name)
        if self.requested_quantity <= 0 or self.filled_quantity <= 0:
            _fail(FailureCode.INVALID_VALUE, "fill quantities must be positive")
        if self.filled_quantity > self.requested_quantity:
            _fail(FailureCode.INVALID_VALUE, "filled quantity cannot exceed requested quantity")
        require_aware_datetime(self.execution_time, "execution_time")


@dataclass(frozen=True, kw_only=True)
class ShadowLedgerEvent(CanonicalContract):
    sequence: int
    run_id: str
    event_type: str
    event_time: datetime
    previous_hash: str | None
    payload_hash: str
    event_hash: str | None = None

    def __post_init__(self) -> None:
        if isinstance(self.sequence, bool) or not isinstance(self.sequence, int) or self.sequence < 1:
            _fail(FailureCode.INVALID_VALUE, "sequence must be a positive integer")
        require_identifier(self.run_id, "run_id")
        require_identifier(self.event_type, "event_type")
        require_aware_datetime(self.event_time, "event_time")
        if self.previous_hash is not None:
            require_hash(self.previous_hash, "previous_hash")
        require_hash(self.payload_hash, "payload_hash")
        if self.event_hash is not None:
            require_hash(self.event_hash, "event_hash")


@dataclass(frozen=True, kw_only=True)
class PerformanceAttribution(CanonicalContract):
    nav_start: float
    nav_end: float
    gross_pnl: float
    net_pnl: float
    transaction_cost: float
    slippage_cost: float
    return_pct: float
    turnover: float
    gross_exposure: float
    beta_exposure: float
    industry_exposure: Mapping[str, float]
    challenger_contribution: Mapping[str, float]
    data_coverage: float

    def __post_init__(self) -> None:
        for field_name in (
            "nav_start",
            "nav_end",
            "gross_pnl",
            "net_pnl",
            "transaction_cost",
            "slippage_cost",
            "return_pct",
            "turnover",
            "gross_exposure",
            "beta_exposure",
            "data_coverage",
        ):
            require_finite_number(getattr(self, field_name), field_name)
        object.__setattr__(
            self,
            "industry_exposure",
            _freeze_str_float_mapping(self.industry_exposure, "industry_exposure"),
        )
        object.__setattr__(
            self,
            "challenger_contribution",
            _freeze_str_float_mapping(self.challenger_contribution, "challenger_contribution"),
        )


@dataclass(frozen=True, kw_only=True)
class DriftAlert(CanonicalContract):
    alert_type: str
    severity: AlertSeverity
    metric_name: str
    observed_value: float
    threshold: float
    recommendation: str

    def __post_init__(self) -> None:
        require_identifier(self.alert_type, "alert_type")
        require_identifier(self.metric_name, "metric_name")
        if not isinstance(self.severity, AlertSeverity):
            object.__setattr__(self, "severity", AlertSeverity(self.severity))
        require_finite_number(self.observed_value, "observed_value")
        require_finite_number(self.threshold, "threshold")
        require_text(self.recommendation, "recommendation")


@dataclass(frozen=True, kw_only=True)
class ShadowRiskPolicy(CanonicalContract):
    policy_version: str
    policy_hash: str
    max_turnover_alert: float = 0.60
    max_abs_beta_alert: float = 0.30
    max_abs_industry_alert: float = 0.50
    min_data_coverage: float = 0.85
    max_factor_mean_shift: float = 1.0
    max_ic_proxy_decay: float = 0.05
    min_challenger_contribution: float = -0.01

    def __post_init__(self) -> None:
        require_identifier(self.policy_version, "policy_version")
        require_hash(self.policy_hash, "policy_hash")
        for item in (
            "max_turnover_alert",
            "max_abs_beta_alert",
            "max_abs_industry_alert",
            "min_data_coverage",
            "max_factor_mean_shift",
            "max_ic_proxy_decay",
            "min_challenger_contribution",
        ):
            require_finite_number(getattr(self, item), item)


@dataclass(frozen=True, kw_only=True)
class ShadowRunManifest(CanonicalContract):
    run_id: str
    status: ShadowStatus
    request_hash: str
    vintage_hash: str
    challenger_hashes: tuple[str, ...]
    target_hash: str
    fill_hash: str
    attribution_hash: str
    ledger_tip_hash: str
    report_time: datetime
    alerts: tuple[DriftAlert, ...]

    def __post_init__(self) -> None:
        require_identifier(self.run_id, "run_id")
        if not isinstance(self.status, ShadowStatus):
            object.__setattr__(self, "status", ShadowStatus(self.status))
        for field_name in (
            "request_hash",
            "vintage_hash",
            "target_hash",
            "fill_hash",
            "attribution_hash",
            "ledger_tip_hash",
        ):
            require_hash(getattr(self, field_name), field_name)
        for item in self.challenger_hashes:
            require_hash(item, "challenger_hash")
        require_aware_datetime(self.report_time, "report_time")
