"""Controller for deterministic 20-session shadow replay."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from ..contracts import ContractValidationError, FailureCode
from ..shadow_portfolio.ledger import ShadowLedger
from ..shadow_portfolio.models import (
    ChallengerVersion,
    DataVintage,
    PerformanceAttribution,
    ShadowRiskPolicy,
    ShadowRunManifest,
    ShadowRunRequest,
    ShadowStatus,
)
from ..shadow_portfolio.protocols import (
    Clock,
    MarketPriceProvider,
    PortfolioConstructor,
    RiskPolicyReader,
)
from ..shadow_portfolio.service import ShadowPortfolioService
from .journal import ShadowReplayJournal, stable_hash
from .models import (
    DailyReplayRecord,
    ReplayCandidateState,
    ReplayStatus,
    ShadowReplayManifest,
    ShadowReplayPolicy,
    ShadowReplayReport,
    ShadowRunOnceResult,
)
from .protocols import ChallengerProvider, HistoricalVintageProvider, ShadowRunOncePort


def _fail(code: FailureCode, message: str, **details: Any) -> None:
    raise ContractValidationError(code, message, details=details)


class _SingleVintageProvider:
    def __init__(self, vintage: DataVintage) -> None:
        self._vintage = vintage

    def get_vintage(self, request: ShadowRunRequest) -> DataVintage:
        return self._vintage


class _SingleChallengerProvider:
    def __init__(self, challengers: tuple[ChallengerVersion, ...]) -> None:
        self._challengers = challengers

    def get_challengers(
        self, request: ShadowRunRequest, vintage: DataVintage
    ) -> tuple[ChallengerVersion, ...]:
        return self._challengers


@dataclass
class ShadowPortfolioRunOnceAdapter:
    """Adapter that delegates all one-day portfolio/accounting truth to ShadowPortfolioService."""

    price_provider: MarketPriceProvider
    portfolio_constructor: PortfolioConstructor
    risk_policy_reader: RiskPolicyReader
    ledger: ShadowLedger
    clock: Clock

    def run_once(
        self,
        request: ShadowRunRequest,
        vintage: DataVintage,
        challengers: tuple[ChallengerVersion, ...],
    ) -> ShadowRunOnceResult:
        service = ShadowPortfolioService(
            snapshot_provider=_SingleVintageProvider(vintage),
            challenger_provider=_SingleChallengerProvider(challengers),
            price_provider=self.price_provider,
            portfolio_constructor=self.portfolio_constructor,
            risk_policy_reader=self.risk_policy_reader,
            ledger=self.ledger,
            clock=self.clock,
        )
        before_events = self.ledger.event_count
        manifest = service.run_once(request)
        snapshot = self.ledger.snapshot()
        attribution = _latest_attribution(snapshot.events, snapshot.payloads, run_id=request.run_id)
        return ShadowRunOnceResult(
            manifest=manifest,
            attribution=attribution,
            ledger_event_count=self.ledger.event_count - before_events,
            balanced=manifest.status is ShadowStatus.READY,
        )


class ShadowReplayController:
    """Run exactly policy.trading_days frozen sessions unless a fail-closed HALT occurs."""

    def __init__(
        self,
        *,
        vintage_provider: HistoricalVintageProvider,
        challenger_provider: ChallengerProvider,
        run_once_port: ShadowRunOncePort,
        journal: ShadowReplayJournal,
    ) -> None:
        self.vintage_provider = vintage_provider
        self.challenger_provider = challenger_provider
        self.run_once_port = run_once_port
        self.journal = journal

    def run(self, policy: ShadowReplayPolicy) -> ShadowReplayReport:
        vintages = self._select_vintages(policy)
        snapshot = self.journal.snapshot()
        records: list[DailyReplayRecord] = []
        existing_by_identity = dict(snapshot.by_identity)
        nav = policy.starting_cash
        high_water = policy.starting_cash
        final_tip = stable_hash({"empty_shadow_ledger": True})

        for session_index, vintage in enumerate(vintages, start=1):
            previous_record_hash = records[-1].artifact_hash if records else None
            record = self._run_day(
                policy,
                session_index,
                vintage,
                nav,
                high_water,
                previous_record_hash,
                existing_by_identity,
            )
            records.append(record)
            existing_by_identity[record.identity_hash] = record
            final_tip = record.shadow_ledger_tip_hash
            nav = record.nav_end
            high_water = max(high_water, nav)
            if record.status is ReplayStatus.HALT:
                break

        if not records:
            _fail(FailureCode.INCOMPLETE_RESULT, "shadow replay produced no daily records")
        status = ReplayStatus.HALT if any(record.status is ReplayStatus.HALT for record in records) else ReplayStatus.READY
        if status is ReplayStatus.READY and len([record for record in records if record.status is ReplayStatus.READY]) != policy.trading_days:
            _fail(FailureCode.INCOMPLETE_RESULT, "shadow replay did not execute the requested number of sessions")
        journal_tip = self.journal.snapshot().tip_hash
        manifest = ShadowReplayManifest(
            replay_id=policy.replay_id,
            status=status,
            policy_hash=policy.policy_hash,
            trading_days_requested=policy.trading_days,
            records_committed=len(records),
            start_decision_time=records[0].decision_time,
            end_valuation_time=records[-1].valuation_time,
            final_nav=records[-1].nav_end,
            max_drawdown=max(record.drawdown for record in records),
            total_turnover=sum(record.turnover for record in records),
            total_transaction_cost=sum(record.transaction_cost for record in records),
            total_slippage_cost=sum(record.slippage_cost for record in records),
            final_shadow_ledger_tip_hash=final_tip,
            journal_tip_hash=journal_tip,
            daily_record_hashes=tuple(record.artifact_hash for record in records),
            halt_reason=records[-1].failure_message if status is ReplayStatus.HALT else None,
        )
        return ShadowReplayReport(manifest=manifest, daily_records=tuple(records))

    def _select_vintages(self, policy: ShadowReplayPolicy) -> tuple[DataVintage, ...]:
        vintages = self.vintage_provider.list_vintages(policy)
        if len(vintages) < policy.trading_days:
            _fail(FailureCode.INCOMPLETE_RESULT, "not enough frozen trading-day vintages")
        selected = tuple(vintages[: policy.trading_days])
        last_decision = None
        seen_vintage_ids: set[str] = set()
        for vintage in selected:
            if vintage.vintage_id in seen_vintage_ids:
                _fail(FailureCode.INVALID_VALUE, "duplicate daily vintage id", vintage_id=vintage.vintage_id)
            seen_vintage_ids.add(vintage.vintage_id)
            if vintage.data_available_time > vintage.decision_time:
                _fail(FailureCode.INVALID_TIME_ORDER, "daily vintage uses future data")
            if last_decision is not None and vintage.decision_time <= last_decision:
                _fail(FailureCode.INVALID_TIME_ORDER, "daily vintages must be strictly time ordered")
            last_decision = vintage.decision_time
        return selected

    def _run_day(
        self,
        policy: ShadowReplayPolicy,
        session_index: int,
        vintage: DataVintage,
        nav_start: float,
        high_water: float,
        previous_record_hash: str | None,
        existing_by_identity: dict[str, DailyReplayRecord],
    ) -> DailyReplayRecord:
        candidates = self.challenger_provider.get_candidates(policy, vintage)
        eligible = tuple(
            candidate
            for candidate in candidates
            if candidate.state is ReplayCandidateState.CHALLENGER
            and candidate.policy_version == policy.policy_version
            and candidate.policy_hash == policy.policy_hash
            and candidate.available_time <= vintage.decision_time
        )
        challengers = tuple(candidate.to_shadow_challenger() for candidate in eligible)
        identity = self._daily_identity(policy, session_index, vintage, candidates, challengers, previous_record_hash)
        existing = existing_by_identity.get(identity)
        if existing is not None:
            return existing
        request = _shadow_request(policy, session_index, vintage, nav_start, identity)
        daily_input_hash = stable_hash(
            {
                "policy": policy,
                "request": request,
                "vintage": vintage,
                "candidates_seen": tuple(candidate.artifact_hash for candidate in candidates),
                "challengers_used": tuple(challenger.artifact_hash for challenger in challengers),
                "previous_record_hash": previous_record_hash,
            }
        )
        if not challengers:
            record = self._halt_record(
                policy,
                session_index,
                vintage,
                request,
                candidates,
                challengers,
                daily_input_hash,
                nav_start,
                high_water,
                previous_record_hash,
                FailureCode.INVALID_ALPHA_SOURCE,
                "no eligible CHALLENGER available for the daily vintage",
            )
            return self.journal.append_or_get(record)
        try:
            outcome = self.run_once_port.run_once(request, vintage, challengers)
            if not outcome.balanced:
                _fail(FailureCode.RECONCILIATION_FAILED, "shadow run_once reported unbalanced accounting")
            if outcome.manifest.status is not ShadowStatus.READY:
                _fail(FailureCode.EXECUTION_HALTED, "shadow run_once did not return READY")
        except ContractValidationError as exc:
            record = self._halt_record(
                policy,
                session_index,
                vintage,
                request,
                candidates,
                challengers,
                daily_input_hash,
                nav_start,
                high_water,
                previous_record_hash,
                exc.code,
                str(exc),
            )
            return self.journal.append_or_get(record)
        record = _ready_record(
            policy=policy,
            session_index=session_index,
            vintage=vintage,
            request=request,
            candidates=candidates,
            challengers=challengers,
            daily_input_hash=daily_input_hash,
            outcome=outcome,
            high_water=high_water,
            previous_record_hash=previous_record_hash,
        )
        return self.journal.append_or_get(record)

    @staticmethod
    def _daily_identity(
        policy: ShadowReplayPolicy,
        session_index: int,
        vintage: DataVintage,
        candidates: tuple[Any, ...],
        challengers: tuple[ChallengerVersion, ...],
        previous_record_hash: str | None,
    ) -> str:
        return stable_hash(
            {
                "replay_id": policy.replay_id,
                "session_index": session_index,
                "vintage_id": vintage.vintage_id,
                "revision_id": vintage.revision_id,
                "decision_time": vintage.decision_time,
                "candidate_hashes_seen": tuple(candidate.artifact_hash for candidate in candidates),
                "challenger_hashes_used": tuple(challenger.artifact_hash for challenger in challengers),
                "previous_record_hash": previous_record_hash,
                "policy_hash": policy.policy_hash,
            }
        )

    def _halt_record(
        self,
        policy: ShadowReplayPolicy,
        session_index: int,
        vintage: DataVintage,
        request: ShadowRunRequest,
        candidates: tuple[Any, ...],
        challengers: tuple[ChallengerVersion, ...],
        daily_input_hash: str,
        nav_start: float,
        high_water: float,
        previous_record_hash: str | None,
        failure_code: FailureCode,
        failure_message: str,
    ) -> DailyReplayRecord:
        failure_hash = stable_hash(
            {
                "status": ReplayStatus.HALT.value,
                "failure_code": failure_code.value,
                "failure_message": failure_message,
                "daily_input_hash": daily_input_hash,
            }
        )
        drawdown = _drawdown(nav_start, high_water)
        return DailyReplayRecord(
            replay_id=policy.replay_id,
            identity_hash=self._daily_identity(policy, session_index, vintage, candidates, challengers, previous_record_hash),
            session_index=session_index,
            trading_day=vintage.decision_time.date().isoformat(),
            run_id=request.run_id,
            vintage_id=vintage.vintage_id,
            revision_id=vintage.revision_id,
            decision_time=request.decision_time,
            execution_time=request.execution_time,
            valuation_time=request.valuation_time,
            data_available_time=vintage.data_available_time,
            vintage_hash=vintage.artifact_hash,
            candidate_hashes_seen=tuple(candidate.artifact_hash for candidate in candidates),
            challenger_hashes_used=tuple(challenger.artifact_hash for challenger in challengers),
            daily_input_hash=daily_input_hash,
            shadow_manifest_hash=failure_hash,
            shadow_ledger_tip_hash=stable_hash({"halt_before_shadow_tip": True, "session_index": session_index}),
            shadow_status=ReplayStatus.HALT.value,
            nav_start=nav_start,
            nav_end=nav_start,
            drawdown=drawdown,
            turnover=0.0,
            transaction_cost=0.0,
            slippage_cost=0.0,
            attribution_hash=stable_hash({"halt_before_attribution": True, "session_index": session_index}),
            attribution={"net_pnl": 0.0, "return_pct": 0.0},
            status=ReplayStatus.HALT,
            failure_code=failure_code.value,
            failure_message=failure_message,
        )


def _shadow_request(
    policy: ShadowReplayPolicy,
    session_index: int,
    vintage: DataVintage,
    nav_start: float,
    identity_hash: str,
) -> ShadowRunRequest:
    decision_time = vintage.decision_time
    execution_time = decision_time + timedelta(seconds=policy.execution_delay_seconds)
    valuation_time = decision_time + timedelta(seconds=policy.valuation_delay_seconds)
    suffix = identity_hash.split(":", 1)[1][:24]
    return ShadowRunRequest(
        run_id=f"{policy.replay_id}-d{session_index:02d}-{suffix}",
        account_id=policy.account_id,
        vintage_id=vintage.vintage_id,
        policy_version=policy.policy_version,
        policy_hash=policy.policy_hash,
        decision_time=decision_time,
        execution_time=execution_time,
        valuation_time=valuation_time,
        starting_cash=nav_start,
        previous_positions={},
        max_gross_weight=policy.max_gross_weight,
        transaction_cost_bps=policy.transaction_cost_bps,
        slippage_bps=policy.slippage_bps,
        max_adv_participation=policy.max_adv_participation,
        seed=policy.seed + session_index,
    )


def _ready_record(
    *,
    policy: ShadowReplayPolicy,
    session_index: int,
    vintage: DataVintage,
    request: ShadowRunRequest,
    candidates: tuple[Any, ...],
    challengers: tuple[ChallengerVersion, ...],
    daily_input_hash: str,
    outcome: ShadowRunOnceResult,
    high_water: float,
    previous_record_hash: str | None,
) -> DailyReplayRecord:
    attribution = outcome.attribution
    drawdown = _drawdown(attribution.nav_end, max(high_water, attribution.nav_start))
    return DailyReplayRecord(
        replay_id=policy.replay_id,
        identity_hash=ShadowReplayController._daily_identity(
            policy,
            session_index,
            vintage,
            candidates,
            challengers,
            previous_record_hash,
        ),
        session_index=session_index,
        trading_day=vintage.decision_time.date().isoformat(),
        run_id=request.run_id,
        vintage_id=vintage.vintage_id,
        revision_id=vintage.revision_id,
        decision_time=request.decision_time,
        execution_time=request.execution_time,
        valuation_time=request.valuation_time,
        data_available_time=vintage.data_available_time,
        vintage_hash=vintage.artifact_hash,
        candidate_hashes_seen=tuple(candidate.artifact_hash for candidate in candidates),
        challenger_hashes_used=tuple(challenger.artifact_hash for challenger in challengers),
        daily_input_hash=daily_input_hash,
        shadow_manifest_hash=outcome.manifest.artifact_hash,
        shadow_ledger_tip_hash=outcome.manifest.ledger_tip_hash,
        shadow_status=outcome.manifest.status.value,
        nav_start=attribution.nav_start,
        nav_end=attribution.nav_end,
        drawdown=drawdown,
        turnover=attribution.turnover,
        transaction_cost=attribution.transaction_cost,
        slippage_cost=attribution.slippage_cost,
        attribution_hash=attribution.artifact_hash,
        attribution={
            "gross_pnl": attribution.gross_pnl,
            "net_pnl": attribution.net_pnl,
            "return_pct": attribution.return_pct,
            "gross_exposure": attribution.gross_exposure,
            "beta_exposure": attribution.beta_exposure,
            "data_coverage": attribution.data_coverage,
        },
        status=ReplayStatus.READY,
    )


def _drawdown(nav: float, high_water: float) -> float:
    if high_water <= 0:
        _fail(FailureCode.INVALID_VALUE, "high_water must be positive")
    return max(0.0, (high_water - nav) / high_water)


def _latest_attribution(events: tuple[Any, ...], payloads: tuple[Any, ...], *, run_id: str) -> PerformanceAttribution:
    for event, payload in reversed(tuple(zip(events, payloads))):
        if event.run_id == run_id and event.event_type == "ATTRIBUTION" and isinstance(payload, PerformanceAttribution):
            return payload
    _fail(FailureCode.INCOMPLETE_RESULT, "shadow ledger did not expose attribution payload", run_id=run_id)
