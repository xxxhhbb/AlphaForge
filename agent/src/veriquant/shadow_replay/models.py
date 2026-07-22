"""Typed entities for durable 20-session shadow replay."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from types import MappingProxyType
from typing import Mapping

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
from ..shadow_portfolio.models import (
    ChallengerState,
    ChallengerVersion,
    PerformanceAttribution,
    ShadowRunManifest,
)


class ReplayStatus(str, Enum):
    READY = "READY"
    HALT = "HALT"
    BLOCKED = "BLOCKED"


class ReplayCandidateState(str, Enum):
    CHALLENGER = "CHALLENGER"
    DRAFT = "DRAFT"
    REJECTED = "REJECTED"
    AUXILIARY_RESEARCH_EVIDENCE = "AUXILIARY_RESEARCH_EVIDENCE"
    RETIRED = "RETIRED"
    VALIDATED_RESEARCH = "VALIDATED_RESEARCH"


def _fail(code: FailureCode, message: str, **details: object) -> None:
    raise ContractValidationError(code, message, details=details)


def _freeze_str_float_mapping(value: Mapping[str, float], field_name: str) -> Mapping[str, float]:
    return freeze_mapping(value, field_name, numeric_values=True)


@dataclass(frozen=True, kw_only=True)
class ShadowReplayPolicy(CanonicalContract):
    replay_id: str
    account_id: str
    policy_version: str
    policy_hash: str
    trading_days: int = 20
    starting_cash: float = 100000.0
    max_gross_weight: float = 1.0
    transaction_cost_bps: float = 5.0
    slippage_bps: float = 10.0
    max_adv_participation: float = 0.10
    execution_delay_seconds: int = 300
    valuation_delay_seconds: int = 21600
    seed: int = 0

    def __post_init__(self) -> None:
        for field_name in ("replay_id", "account_id", "policy_version"):
            require_identifier(getattr(self, field_name), field_name)
        require_hash(self.policy_hash, "policy_hash")
        if isinstance(self.trading_days, bool) or not isinstance(self.trading_days, int) or self.trading_days < 20:
            _fail(FailureCode.INVALID_VALUE, "trading_days must be an integer >= 20")
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
            _fail(FailureCode.INVALID_VALUE, "transaction cost and slippage must be non-negative")
        if not 0.0 < self.max_adv_participation <= 1.0:
            _fail(FailureCode.INVALID_VALUE, "max_adv_participation must be in (0, 1]")
        for field_name in ("execution_delay_seconds", "valuation_delay_seconds"):
            value = getattr(self, field_name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                _fail(FailureCode.INVALID_VALUE, f"{field_name} must be a positive integer")
        if self.execution_delay_seconds >= self.valuation_delay_seconds:
            _fail(FailureCode.INVALID_TIME_ORDER, "execution must occur before valuation")
        if isinstance(self.seed, bool) or not isinstance(self.seed, int) or self.seed < 0:
            _fail(FailureCode.INVALID_VALUE, "seed must be a non-negative integer")


@dataclass(frozen=True, kw_only=True)
class ReplayChallengerCandidate(CanonicalContract):
    candidate_id: str
    version: str
    state: ReplayCandidateState
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
    available_time: datetime

    def __post_init__(self) -> None:
        for field_name in ("candidate_id", "version", "policy_version"):
            require_identifier(getattr(self, field_name), field_name)
        if not isinstance(self.state, ReplayCandidateState):
            object.__setattr__(self, "state", ReplayCandidateState(self.state))
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
        require_aware_datetime(self.available_time, "available_time")

    def to_shadow_challenger(self) -> ChallengerVersion:
        if self.state is not ReplayCandidateState.CHALLENGER:
            _fail(FailureCode.INVALID_ALPHA_SOURCE, "only CHALLENGER candidates can be adapted")
        return ChallengerVersion(
            challenger_id=self.candidate_id,
            version=self.version,
            state=ChallengerState.CHALLENGER,
            policy_version=self.policy_version,
            policy_hash=self.policy_hash,
            promotion_decision_hash=self.promotion_decision_hash,
            evidence_hash=self.evidence_hash,
            factor_hash=self.factor_hash,
            scores_by_symbol=self.scores_by_symbol,
            baseline_factor_mean=self.baseline_factor_mean,
            baseline_factor_std=self.baseline_factor_std,
            baseline_ic_proxy=self.baseline_ic_proxy,
            expected_contribution=self.expected_contribution,
        )


@dataclass(frozen=True, kw_only=True)
class ShadowRunOnceResult(CanonicalContract):
    manifest: ShadowRunManifest
    attribution: PerformanceAttribution
    ledger_event_count: int
    balanced: bool = True

    def __post_init__(self) -> None:
        if isinstance(self.ledger_event_count, bool) or not isinstance(self.ledger_event_count, int):
            _fail(FailureCode.INVALID_VALUE, "ledger_event_count must be an integer")
        if self.ledger_event_count < 0:
            _fail(FailureCode.INVALID_VALUE, "ledger_event_count cannot be negative")
        if type(self.balanced) is not bool:
            _fail(FailureCode.RECONCILIATION_FAILED, "balanced must be boolean")


@dataclass(frozen=True, kw_only=True)
class DailyReplayRecord(CanonicalContract):
    replay_id: str
    identity_hash: str
    session_index: int
    trading_day: str
    run_id: str
    vintage_id: str
    revision_id: str
    decision_time: datetime
    execution_time: datetime
    valuation_time: datetime
    data_available_time: datetime
    vintage_hash: str
    candidate_hashes_seen: tuple[str, ...]
    challenger_hashes_used: tuple[str, ...]
    daily_input_hash: str
    shadow_manifest_hash: str
    shadow_ledger_tip_hash: str
    shadow_status: str
    nav_start: float
    nav_end: float
    drawdown: float
    turnover: float
    transaction_cost: float
    slippage_cost: float
    attribution_hash: str
    attribution: Mapping[str, float]
    status: ReplayStatus
    failure_code: str | None = None
    failure_message: str | None = None

    def __post_init__(self) -> None:
        for field_name in ("replay_id", "trading_day", "run_id", "vintage_id", "revision_id", "shadow_status"):
            require_identifier(getattr(self, field_name), field_name)
        for field_name in (
            "identity_hash",
            "vintage_hash",
            "daily_input_hash",
            "shadow_manifest_hash",
            "shadow_ledger_tip_hash",
            "attribution_hash",
        ):
            require_hash(getattr(self, field_name), field_name)
        if isinstance(self.session_index, bool) or not isinstance(self.session_index, int) or self.session_index < 1:
            _fail(FailureCode.INVALID_VALUE, "session_index must be a positive integer")
        require_time_order(
            ("data_available_time", self.data_available_time),
            ("decision_time", self.decision_time),
            ("execution_time", self.execution_time),
            ("valuation_time", self.valuation_time),
        )
        for item in (*self.candidate_hashes_seen, *self.challenger_hashes_used):
            require_hash(item, "candidate_hash")
        for field_name in (
            "nav_start",
            "nav_end",
            "drawdown",
            "turnover",
            "transaction_cost",
            "slippage_cost",
        ):
            require_finite_number(getattr(self, field_name), field_name)
        object.__setattr__(
            self,
            "attribution",
            MappingProxyType(dict(freeze_mapping(self.attribution, "attribution", numeric_values=True))),
        )
        if not isinstance(self.status, ReplayStatus):
            object.__setattr__(self, "status", ReplayStatus(self.status))
        if self.failure_code is not None:
            require_identifier(self.failure_code, "failure_code")
        if self.failure_message is not None:
            require_text(self.failure_message, "failure_message")


@dataclass(frozen=True, kw_only=True)
class ShadowReplayManifest(CanonicalContract):
    replay_id: str
    status: ReplayStatus
    policy_hash: str
    trading_days_requested: int
    records_committed: int
    start_decision_time: datetime
    end_valuation_time: datetime
    final_nav: float
    max_drawdown: float
    total_turnover: float
    total_transaction_cost: float
    total_slippage_cost: float
    final_shadow_ledger_tip_hash: str
    journal_tip_hash: str
    daily_record_hashes: tuple[str, ...]
    halt_reason: str | None = None

    def __post_init__(self) -> None:
        require_identifier(self.replay_id, "replay_id")
        if not isinstance(self.status, ReplayStatus):
            object.__setattr__(self, "status", ReplayStatus(self.status))
        require_hash(self.policy_hash, "policy_hash")
        require_hash(self.final_shadow_ledger_tip_hash, "final_shadow_ledger_tip_hash")
        require_hash(self.journal_tip_hash, "journal_tip_hash")
        require_time_order(("start_decision_time", self.start_decision_time), ("end_valuation_time", self.end_valuation_time))
        for field_name in ("trading_days_requested", "records_committed"):
            value = getattr(self, field_name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                _fail(FailureCode.INVALID_VALUE, f"{field_name} must be a positive integer")
        for field_name in (
            "final_nav",
            "max_drawdown",
            "total_turnover",
            "total_transaction_cost",
            "total_slippage_cost",
        ):
            require_finite_number(getattr(self, field_name), field_name)
        require_nonempty_tuple(self.daily_record_hashes, "daily_record_hashes")
        for item in self.daily_record_hashes:
            require_hash(item, "daily_record_hash")
        if self.halt_reason is not None:
            require_text(self.halt_reason, "halt_reason")


@dataclass(frozen=True, kw_only=True)
class ShadowReplayReport(CanonicalContract):
    manifest: ShadowReplayManifest
    daily_records: tuple[DailyReplayRecord, ...]

    def __post_init__(self) -> None:
        require_nonempty_tuple(self.daily_records, "daily_records")
