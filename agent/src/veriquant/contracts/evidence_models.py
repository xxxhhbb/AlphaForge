"""Evidence, governance, and downstream data-envelope contracts.

Downstream names are defined here only to make authority boundaries explicit.
No portfolio, risk, execution, or accounting behavior is implemented in Wave 01.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import ClassVar, Mapping

from .authority import require_component_capability
from .base import (
    CanonicalContract,
    fail,
    require_aware_datetime,
    require_finite_number,
    require_hash,
    require_identifier,
    require_nonempty_tuple,
    require_text,
    require_time_order,
)
from .enums import (
    Capability,
    Component,
    EvidenceClass,
    FailureCode,
    ResearchState,
    RiskVerdict,
)
from .models import (
    _AUXILIARY_PRODUCERS,
    _require_enum,
    _require_exact_component,
    _set_mapping,
    _validate_hash_tuple,
    _validate_state,
    _validate_string_tuple,
)
from .transitions import validate_transition


@dataclass(frozen=True, kw_only=True)
class TrialEvent(CanonicalContract):
    ALLOWED_PRODUCERS: ClassVar[frozenset[Component]] = frozenset({Component.TRIAL_LEDGER})

    trial_id: str
    sequence: int
    family_id: str
    candidate_hash: str
    proposer_id: str
    component: Component
    from_state: ResearchState
    to_state: ResearchState
    event_time: datetime
    budget_units: int
    failure_code: FailureCode | None
    artifact_hashes: tuple[str, ...]
    recorded_by: Component = Component.TRIAL_LEDGER

    def __post_init__(self) -> None:
        require_identifier(self.trial_id, "trial_id")
        require_identifier(self.family_id, "family_id")
        require_identifier(self.proposer_id, "proposer_id")
        require_hash(self.candidate_hash, "candidate_hash")
        if isinstance(self.sequence, bool) or not isinstance(self.sequence, int) or self.sequence < 0:
            fail(FailureCode.INVALID_VALUE, "sequence must be a non-negative integer")
        if isinstance(self.budget_units, bool) or not isinstance(self.budget_units, int) or self.budget_units < 1:
            fail(
                FailureCode.INVALID_VALUE,
                "every trial outcome, including failure, must consume positive family budget",
            )
        _require_enum(self.component, Component, "component")
        _require_enum(self.from_state, ResearchState, "from_state")
        _require_enum(self.to_state, ResearchState, "to_state")
        validate_transition(self.from_state, self.to_state)
        require_aware_datetime(self.event_time, "event_time")
        if self.failure_code is not None:
            _require_enum(self.failure_code, FailureCode, "failure_code")
        _validate_hash_tuple(self.artifact_hashes, "artifact_hashes", nonempty=False)
        _require_exact_component(self.recorded_by, Component.TRIAL_LEDGER, "recorded_by")


@dataclass(frozen=True, kw_only=True)
class AuxiliaryEvidence(CanonicalContract):
    ALLOWED_PRODUCERS: ClassVar[frozenset[Component]] = _AUXILIARY_PRODUCERS

    evidence_id: str
    source_component: Component
    experiment_identity_hash: str
    metrics: Mapping[str, float]
    artifact_hashes: tuple[str, ...]
    event_time: datetime
    published_at: datetime
    available_at: datetime
    state: ResearchState = ResearchState.EVALUATED
    evidence_class: EvidenceClass = EvidenceClass.AUXILIARY_RESEARCH_EVIDENCE

    def __post_init__(self) -> None:
        require_identifier(self.evidence_id, "evidence_id")
        _require_enum(self.source_component, Component, "source_component")
        if self.source_component not in _AUXILIARY_PRODUCERS:
            fail(FailureCode.INVALID_ARTIFACT_PRODUCER, "source cannot produce auxiliary evidence")
        require_hash(self.experiment_identity_hash, "experiment_identity_hash")
        _set_mapping(self, "metrics", numeric_values=True)
        _validate_hash_tuple(self.artifact_hashes, "artifact_hashes")
        require_time_order(
            ("event_time", self.event_time),
            ("published_at", self.published_at),
            ("available_at", self.available_at),
        )
        _validate_state(self.state, frozenset({ResearchState.EVALUATED}))
        _require_enum(self.evidence_class, EvidenceClass, "evidence_class")
        if self.evidence_class is not EvidenceClass.AUXILIARY_RESEARCH_EVIDENCE:
            fail(
                FailureCode.AUXILIARY_MASQUERADE,
                "auxiliary evidence cannot declare itself OFFICIAL_EVIDENCE",
            )


@dataclass(frozen=True, kw_only=True)
class EvidencePack(CanonicalContract):
    ALLOWED_PRODUCERS: ClassVar[frozenset[Component]] = frozenset({Component.EVIDENCE_SERVICE})

    evidence_id: str
    experiment_identity_hash: str
    official_metrics: Mapping[str, float]
    artifact_hashes: tuple[str, ...]
    input_hashes: tuple[str, ...]
    decision_time: datetime
    label_start: datetime
    label_end: datetime
    event_time: datetime
    published_at: datetime
    available_at: datetime
    produced_by: Component = Component.EVIDENCE_SERVICE
    state: ResearchState = ResearchState.EVALUATED
    evidence_class: EvidenceClass = EvidenceClass.OFFICIAL_EVIDENCE

    def __post_init__(self) -> None:
        require_identifier(self.evidence_id, "evidence_id")
        require_hash(self.experiment_identity_hash, "experiment_identity_hash")
        _set_mapping(self, "official_metrics", numeric_values=True)
        if not self.official_metrics:
            fail(FailureCode.INVALID_VALUE, "official_metrics cannot be empty")
        _validate_hash_tuple(self.artifact_hashes, "artifact_hashes")
        _validate_hash_tuple(self.input_hashes, "input_hashes")
        require_time_order(
            ("decision_time", self.decision_time),
            ("label_start", self.label_start),
            ("label_end", self.label_end),
            ("event_time", self.event_time),
            ("published_at", self.published_at),
            ("available_at", self.available_at),
        )
        _require_exact_component(self.produced_by, Component.EVIDENCE_SERVICE, "produced_by")
        require_component_capability(self.produced_by, Capability.CALCULATE_OFFICIAL_METRICS)
        _validate_state(self.state, frozenset({ResearchState.EVALUATED}))
        _require_enum(self.evidence_class, EvidenceClass, "evidence_class")
        if self.evidence_class is not EvidenceClass.OFFICIAL_EVIDENCE:
            fail(FailureCode.INVALID_VALUE, "EvidencePack must be OFFICIAL_EVIDENCE")


@dataclass(frozen=True, kw_only=True)
class CriticFinding(CanonicalContract):
    ALLOWED_PRODUCERS: ClassVar[frozenset[Component]] = frozenset({Component.CRITIC_SERVICE})

    finding_id: str
    evidence_hash: str
    severity: str
    code: FailureCode
    message: str
    recommendation: str
    created_at: datetime
    state: ResearchState
    produced_by: Component = Component.CRITIC_SERVICE

    def __post_init__(self) -> None:
        require_identifier(self.finding_id, "finding_id")
        require_hash(self.evidence_hash, "evidence_hash")
        if self.severity not in {"INFO", "WARNING", "ERROR", "CRITICAL"}:
            fail(FailureCode.INVALID_VALUE, "severity has an unsupported value")
        _require_enum(self.code, FailureCode, "code")
        require_text(self.message, "message")
        require_text(self.recommendation, "recommendation")
        require_aware_datetime(self.created_at, "created_at")
        _validate_state(
            self.state,
            frozenset({ResearchState.REVISE, ResearchState.REJECTED, ResearchState.BLOCKED}),
        )
        _require_exact_component(self.produced_by, Component.CRITIC_SERVICE, "produced_by")


@dataclass(frozen=True, kw_only=True)
class PromotionDecision(CanonicalContract):
    ALLOWED_PRODUCERS: ClassVar[frozenset[Component]] = frozenset({Component.PROMOTION_SERVICE})

    decision_id: str
    candidate_hash: str
    evidence_hash: str
    proposer_id: str
    approver_id: str
    from_state: ResearchState
    to_state: ResearchState
    decision_time: datetime
    reason_codes: tuple[str, ...]
    approved: bool
    approver_component: Component = Component.PROMOTION_SERVICE

    def __post_init__(self) -> None:
        require_identifier(self.decision_id, "decision_id")
        require_hash(self.candidate_hash, "candidate_hash")
        require_hash(self.evidence_hash, "evidence_hash")
        require_identifier(self.proposer_id, "proposer_id")
        require_identifier(self.approver_id, "approver_id")
        if self.proposer_id == self.approver_id:
            fail(FailureCode.SELF_APPROVAL, "a proposer cannot approve its own candidate")
        _require_enum(self.from_state, ResearchState, "from_state")
        _require_enum(self.to_state, ResearchState, "to_state")
        validate_transition(self.from_state, self.to_state)
        require_aware_datetime(self.decision_time, "decision_time")
        _validate_string_tuple(self.reason_codes, "reason_codes")
        if type(self.approved) is not bool:
            fail(FailureCode.INVALID_VALUE, "approved must be boolean")
        if self.approved and self.to_state not in {
            ResearchState.VALIDATED_RESEARCH,
            ResearchState.CHALLENGER,
        }:
            fail(FailureCode.INVALID_STATE, "approved decisions must promote to a promotion state")
        if not self.approved and self.to_state not in {
            ResearchState.REVISE,
            ResearchState.REJECTED,
            ResearchState.BLOCKED,
        }:
            fail(FailureCode.INVALID_STATE, "non-approved decisions must revise, reject, or block")
        _require_exact_component(
            self.approver_component, Component.PROMOTION_SERVICE, "approver_component"
        )
        require_component_capability(self.approver_component, Capability.PROMOTE_CANDIDATE)


@dataclass(frozen=True, kw_only=True)
class AlphaForecast(CanonicalContract):
    ALLOWED_PRODUCERS: ClassVar[frozenset[Component]] = frozenset({Component.ALPHA_SERVICE})

    forecast_id: str
    candidate_hash: str
    evidence_hash: str
    universe: tuple[str, ...]
    horizon: str
    decision_time: datetime
    expected_returns: Mapping[str, float]
    confidence: float
    decay_half_life_days: float = 20.0
    factor_correlation: float = 0.0
    evidence_identity_hash: str | None = None
    produced_by: Component = Component.ALPHA_SERVICE
    state: ResearchState = ResearchState.CHALLENGER

    def __post_init__(self) -> None:
        require_identifier(self.forecast_id, "forecast_id")
        require_hash(self.candidate_hash, "candidate_hash")
        require_hash(self.evidence_hash, "evidence_hash")
        _validate_string_tuple(self.universe, "universe")
        require_text(self.horizon, "horizon")
        require_aware_datetime(self.decision_time, "decision_time")
        _set_mapping(self, "expected_returns", numeric_values=True)
        if set(self.expected_returns) != set(self.universe):
            fail(FailureCode.INVALID_VALUE, "expected_returns keys must exactly match universe")
        require_finite_number(self.confidence, "confidence")
        if not 0.0 <= self.confidence <= 1.0:
            fail(FailureCode.INVALID_VALUE, "confidence must be between zero and one")
        require_finite_number(self.decay_half_life_days, "decay_half_life_days")
        if self.decay_half_life_days <= 0.0:
            fail(FailureCode.INVALID_VALUE, "decay_half_life_days must be positive")
        require_finite_number(self.factor_correlation, "factor_correlation")
        if not -1.0 <= self.factor_correlation <= 1.0:
            fail(FailureCode.INVALID_VALUE, "factor_correlation must be between -1 and 1")
        if self.evidence_identity_hash is not None:
            require_hash(self.evidence_identity_hash, "evidence_identity_hash")
        _require_exact_component(self.produced_by, Component.ALPHA_SERVICE, "produced_by")
        _validate_state(self.state, frozenset({ResearchState.CHALLENGER}))


@dataclass(frozen=True, kw_only=True)
class PortfolioPlan(CanonicalContract):
    ALLOWED_PRODUCERS: ClassVar[frozenset[Component]] = frozenset({Component.PORTFOLIO_SERVICE})

    plan_id: str
    forecast_hash: str
    universe: tuple[str, ...]
    decision_time: datetime
    target_weights: Mapping[str, float]
    constraints_hash: str
    objective_terms: Mapping[str, float] = field(default_factory=dict)
    produced_by: Component = Component.PORTFOLIO_SERVICE
    state: ResearchState = ResearchState.CHALLENGER

    def __post_init__(self) -> None:
        require_identifier(self.plan_id, "plan_id")
        require_hash(self.forecast_hash, "forecast_hash")
        _validate_string_tuple(self.universe, "universe")
        require_aware_datetime(self.decision_time, "decision_time")
        _set_mapping(self, "target_weights", numeric_values=True)
        if set(self.target_weights) != set(self.universe):
            fail(FailureCode.INVALID_VALUE, "target_weights keys must exactly match universe")
        require_hash(self.constraints_hash, "constraints_hash")
        _set_mapping(self, "objective_terms", numeric_values=True)
        _require_exact_component(self.produced_by, Component.PORTFOLIO_SERVICE, "produced_by")
        require_component_capability(self.produced_by, Capability.SET_TARGET_WEIGHTS)
        _validate_state(self.state, frozenset({ResearchState.CHALLENGER}))


@dataclass(frozen=True, kw_only=True)
class RiskDecision(CanonicalContract):
    ALLOWED_PRODUCERS: ClassVar[frozenset[Component]] = frozenset({Component.RISK_GATE})

    risk_decision_id: str
    portfolio_plan_hash: str
    decision_time: datetime
    approved: bool
    policy_hash: str
    breaches: tuple[str, ...]
    produced_by: Component = Component.RISK_GATE
    state: ResearchState = ResearchState.BLOCKED
    verdict: RiskVerdict | None = None
    scale_factor: float = 1.0
    checked_metrics: Mapping[str, float] = field(default_factory=dict)

    def __post_init__(self) -> None:
        require_identifier(self.risk_decision_id, "risk_decision_id")
        require_hash(self.portfolio_plan_hash, "portfolio_plan_hash")
        require_aware_datetime(self.decision_time, "decision_time")
        if type(self.approved) is not bool:
            fail(FailureCode.INVALID_VALUE, "approved must be boolean")
        require_hash(self.policy_hash, "policy_hash")
        _validate_string_tuple(self.breaches, "breaches", nonempty=False)
        verdict = self.verdict
        if verdict is None:
            verdict = RiskVerdict.APPROVE if self.approved else RiskVerdict.REJECT
            object.__setattr__(self, "verdict", verdict)
            object.__setattr__(self, "scale_factor", 1.0 if self.approved else 0.0)
        _require_enum(verdict, RiskVerdict, "verdict")
        require_finite_number(self.scale_factor, "scale_factor")
        _set_mapping(self, "checked_metrics", numeric_values=True)
        if verdict is RiskVerdict.APPROVE:
            if not self.approved or self.breaches or self.scale_factor != 1.0:
                fail(FailureCode.INVALID_VALUE, "APPROVE requires approved=true, no breaches, scale=1")
        elif verdict is RiskVerdict.SCALE_DOWN:
            if self.approved or not self.breaches or not 0.0 < self.scale_factor < 1.0:
                fail(
                    FailureCode.INVALID_VALUE,
                    "SCALE_DOWN requires approved=false, breaches, and a scale between zero and one",
                )
        elif verdict is RiskVerdict.REJECT:
            if self.approved or not self.breaches or self.scale_factor != 0.0:
                fail(FailureCode.INVALID_VALUE, "REJECT requires approved=false, breaches, scale=0")
        expected_state = (
            ResearchState.CHALLENGER
            if verdict is RiskVerdict.APPROVE
            else ResearchState.BLOCKED
        )
        if self.state is not expected_state:
            fail(FailureCode.INVALID_STATE, f"risk state must be {expected_state.value}")
        _require_exact_component(self.produced_by, Component.RISK_GATE, "produced_by")
        require_component_capability(self.produced_by, Capability.APPROVE_RISK)


@dataclass(frozen=True, kw_only=True)
class PlannedOrder:
    client_order_id: str
    symbol: str
    side: str
    quantity: Decimal
    order_type: str
    limit_price: Decimal | None = None

    def __post_init__(self) -> None:
        require_identifier(self.client_order_id, "client_order_id")
        require_text(self.symbol, "symbol")
        if self.side not in {"BUY", "SELL"}:
            fail(FailureCode.INVALID_VALUE, "side must be BUY or SELL")
        require_finite_number(self.quantity, "quantity")
        if self.quantity <= 0:
            fail(FailureCode.INVALID_VALUE, "quantity must be positive")
        if self.order_type not in {"MARKET", "LIMIT"}:
            fail(FailureCode.INVALID_VALUE, "order_type must be MARKET or LIMIT")
        if self.order_type == "LIMIT" and self.limit_price is None:
            fail(FailureCode.INVALID_VALUE, "LIMIT orders require limit_price")
        if self.limit_price is not None:
            require_finite_number(self.limit_price, "limit_price")
            if self.limit_price <= 0:
                fail(FailureCode.INVALID_VALUE, "limit_price must be positive")


@dataclass(frozen=True, kw_only=True)
class OrderPlan(CanonicalContract):
    ALLOWED_PRODUCERS: ClassVar[frozenset[Component]] = frozenset({Component.ORDER_SERVICE})

    order_plan_id: str
    portfolio_plan_hash: str
    risk_decision_hash: str
    decision_time: datetime
    orders: tuple[PlannedOrder, ...]
    environment: str = "paper"
    produced_by: Component = Component.ORDER_SERVICE
    state: ResearchState = ResearchState.CHALLENGER

    def __post_init__(self) -> None:
        require_identifier(self.order_plan_id, "order_plan_id")
        require_hash(self.portfolio_plan_hash, "portfolio_plan_hash")
        require_hash(self.risk_decision_hash, "risk_decision_hash")
        require_aware_datetime(self.decision_time, "decision_time")
        require_nonempty_tuple(self.orders, "orders")
        if not all(isinstance(order, PlannedOrder) for order in self.orders):
            fail(FailureCode.INVALID_VALUE, "orders must contain PlannedOrder values")
        if self.environment != "paper":
            fail(FailureCode.INVALID_PAPER_ENVIRONMENT, "only paper order plans are permitted")
        _require_exact_component(self.produced_by, Component.ORDER_SERVICE, "produced_by")
        require_component_capability(self.produced_by, Capability.CREATE_ORDER_PLAN)
        _validate_state(self.state, frozenset({ResearchState.CHALLENGER}))


@dataclass(frozen=True, kw_only=True)
class FillEvent(CanonicalContract):
    ALLOWED_PRODUCERS: ClassVar[frozenset[Component]] = frozenset({Component.EXECUTION_SERVICE})

    fill_id: str
    order_plan_hash: str
    client_order_id: str
    symbol: str
    quantity: Decimal
    price: Decimal
    event_time: datetime
    published_at: datetime
    available_at: datetime
    venue: str
    environment: str = "paper"
    produced_by: Component = Component.EXECUTION_SERVICE

    def __post_init__(self) -> None:
        require_identifier(self.fill_id, "fill_id")
        require_hash(self.order_plan_hash, "order_plan_hash")
        require_identifier(self.client_order_id, "client_order_id")
        require_text(self.symbol, "symbol")
        require_finite_number(self.quantity, "quantity")
        require_finite_number(self.price, "price")
        if self.quantity <= 0 or self.price <= 0:
            fail(FailureCode.INVALID_VALUE, "fill quantity and price must be positive")
        require_time_order(("event_time", self.event_time), ("published_at", self.published_at), ("available_at", self.available_at))
        require_text(self.venue, "venue")
        if self.environment != "paper":
            fail(FailureCode.INVALID_PAPER_ENVIRONMENT, "only paper fills are permitted")
        _require_exact_component(self.produced_by, Component.EXECUTION_SERVICE, "produced_by")


@dataclass(frozen=True, kw_only=True)
class LedgerEntry(CanonicalContract):
    ALLOWED_PRODUCERS: ClassVar[frozenset[Component]] = frozenset({Component.LEDGER_SERVICE})

    entry_id: str
    fill_hash: str
    account: str
    asset: str
    currency: str
    debit: Decimal
    credit: Decimal
    event_time: datetime
    available_at: datetime
    produced_by: Component = Component.LEDGER_SERVICE

    def __post_init__(self) -> None:
        require_identifier(self.entry_id, "entry_id")
        require_hash(self.fill_hash, "fill_hash")
        require_text(self.account, "account")
        require_text(self.asset, "asset")
        require_text(self.currency, "currency")
        require_finite_number(self.debit, "debit")
        require_finite_number(self.credit, "credit")
        if self.debit < 0 or self.credit < 0 or (self.debit == 0 and self.credit == 0):
            fail(FailureCode.INVALID_VALUE, "ledger debit/credit must be non-negative and non-zero")
        if self.debit > 0 and self.credit > 0:
            fail(FailureCode.INVALID_VALUE, "a ledger entry side cannot debit and credit simultaneously")
        require_time_order(("event_time", self.event_time), ("available_at", self.available_at))
        _require_exact_component(self.produced_by, Component.LEDGER_SERVICE, "produced_by")


@dataclass(frozen=True, kw_only=True)
class AttributionReport(CanonicalContract):
    ALLOWED_PRODUCERS: ClassVar[frozenset[Component]] = frozenset({Component.ATTRIBUTION_SERVICE})

    report_id: str
    ledger_snapshot_hash: str
    portfolio_plan_hash: str
    period_start: datetime
    period_end: datetime
    decision_time: datetime
    pnl_components: Mapping[str, float]
    factor_contributions: Mapping[str, float]
    risk_components: Mapping[str, float] = field(default_factory=dict)
    cost_components: Mapping[str, float] = field(default_factory=dict)
    produced_by: Component = Component.ATTRIBUTION_SERVICE
    state: ResearchState = ResearchState.RETIRED

    def __post_init__(self) -> None:
        require_identifier(self.report_id, "report_id")
        require_hash(self.ledger_snapshot_hash, "ledger_snapshot_hash")
        require_hash(self.portfolio_plan_hash, "portfolio_plan_hash")
        require_time_order(("period_start", self.period_start), ("period_end", self.period_end), ("decision_time", self.decision_time))
        _set_mapping(self, "pnl_components", numeric_values=True)
        _set_mapping(self, "factor_contributions", numeric_values=True)
        _set_mapping(self, "risk_components", numeric_values=True)
        _set_mapping(self, "cost_components", numeric_values=True)
        _require_exact_component(self.produced_by, Component.ATTRIBUTION_SERVICE, "produced_by")
        _validate_state(self.state, frozenset({ResearchState.CHALLENGER, ResearchState.RETIRED}))

