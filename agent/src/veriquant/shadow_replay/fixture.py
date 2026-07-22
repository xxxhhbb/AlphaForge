"""Deterministic offline fixtures for shadow replay tests."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Mapping

from ..shadow_portfolio.fixture import POLICY_HASH
from ..shadow_portfolio.ledger import ShadowLedger
from ..shadow_portfolio.models import (
    DataVintage,
    MarketPrice,
    PerformanceAttribution,
    ShadowRunManifest,
    ShadowStatus,
)
from .controller import ShadowReplayController
from .journal import ShadowReplayJournal, stable_hash
from .models import (
    ReplayCandidateState,
    ReplayChallengerCandidate,
    ShadowReplayPolicy,
    ShadowRunOnceResult,
)


@dataclass(frozen=True)
class InMemoryHistoricalVintageProvider:
    vintages: tuple[DataVintage, ...]

    def list_vintages(self, policy: ShadowReplayPolicy) -> tuple[DataVintage, ...]:
        return self.vintages


@dataclass(frozen=True)
class InMemoryChallengerProvider:
    candidates_by_vintage: Mapping[str, tuple[ReplayChallengerCandidate, ...]]
    default_candidates: tuple[ReplayChallengerCandidate, ...]

    def get_candidates(
        self, policy: ShadowReplayPolicy, vintage: DataVintage
    ) -> tuple[ReplayChallengerCandidate, ...]:
        return self.candidates_by_vintage.get(vintage.vintage_id, self.default_candidates)


class DeterministicRunOncePort:
    """Fake port for replay tests; it does not implement portfolio truth."""

    def __init__(self, *, halt_day: int | None = None, unbalanced_day: int | None = None) -> None:
        self.calls: list[tuple[object, tuple[object, ...]]] = []
        self.ledger_tip = stable_hash({"fake_shadow_ledger": "empty"})
        self.halt_day = halt_day
        self.unbalanced_day = unbalanced_day
        self.ledger_events = 0

    def run_once(self, request, vintage: DataVintage, challengers) -> ShadowRunOnceResult:
        self.calls.append((request, tuple(challengers)))
        day = int("".join(ch for ch in vintage.revision_id if ch.isdigit()))
        if self.halt_day == day:
            from ..contracts import ContractValidationError, FailureCode

            raise ContractValidationError(FailureCode.RISK_NOT_APPROVED, "fake risk rejection")
        daily_return = 0.001 * ((day % 5) - 2)
        nav_end = request.starting_cash * (1.0 + daily_return) - 10.0
        attribution = PerformanceAttribution(
            nav_start=request.starting_cash,
            nav_end=nav_end,
            gross_pnl=nav_end - request.starting_cash + 10.0,
            net_pnl=nav_end - request.starting_cash,
            transaction_cost=6.0,
            slippage_cost=4.0,
            return_pct=(nav_end - request.starting_cash) / request.starting_cash,
            turnover=0.05 + day * 0.001,
            gross_exposure=0.6,
            beta_exposure=0.02,
            industry_exposure={"FIN": 0.3, "TECH": -0.3},
            challenger_contribution={f"{challengers[0].challenger_id}@{challengers[0].version}": 0.01},
            data_coverage=sum(vintage.coverage_by_symbol.values()) / len(vintage.universe),
        )
        self.ledger_events += 5
        self.ledger_tip = stable_hash(
            {"previous": self.ledger_tip, "run_id": request.run_id, "attribution": attribution.artifact_hash}
        )
        manifest = ShadowRunManifest(
            run_id=request.run_id,
            status=ShadowStatus.READY,
            request_hash=stable_hash({"request": request, "challengers": tuple(c.artifact_hash for c in challengers)}),
            vintage_hash=vintage.artifact_hash,
            challenger_hashes=tuple(challenger.artifact_hash for challenger in challengers),
            target_hash=stable_hash({"target": request.run_id}),
            fill_hash=stable_hash({"fills": request.run_id}),
            attribution_hash=attribution.artifact_hash,
            ledger_tip_hash=self.ledger_tip,
            report_time=request.valuation_time,
            alerts=(),
        )
        return ShadowRunOnceResult(
            manifest=manifest,
            attribution=attribution,
            ledger_event_count=5,
            balanced=self.unbalanced_day != day,
        )


@dataclass(frozen=True)
class ShadowReplayFixture:
    policy: ShadowReplayPolicy
    controller: ShadowReplayController
    journal: ShadowReplayJournal
    vintage_provider: InMemoryHistoricalVintageProvider
    challenger_provider: InMemoryChallengerProvider
    run_once_port: DeterministicRunOncePort


def make_replay_policy(*, replay_id: str = "replay-20d") -> ShadowReplayPolicy:
    return ShadowReplayPolicy(
        replay_id=replay_id,
        account_id="shadow-account",
        policy_version="policy.v1",
        policy_hash=POLICY_HASH,
    )


def make_vintages(*, revision_day: int | None = None, revision_suffix: str = "b") -> tuple[DataVintage, ...]:
    start = datetime(2026, 1, 5, 9, 30, tzinfo=timezone.utc)
    vintages: list[DataVintage] = []
    for index in range(20):
        day = index + 1
        decision_time = start + timedelta(days=index)
        revision_id = f"rev{day}"
        vintage_id = f"vintage-{day:02d}"
        if revision_day == day:
            revision_id = f"rev{day}{revision_suffix}"
            vintage_id = f"vintage-{day:02d}-{revision_suffix}"
        dataset_hash = stable_hash({"fixture": "vintage", "day": day, "revision": revision_id})
        vintages.append(
            DataVintage(
                vintage_id=vintage_id,
                revision_id=revision_id,
                dataset_hash=dataset_hash,
                universe=("AAA", "BBB"),
                data_available_time=decision_time - timedelta(hours=1),
                decision_time=decision_time,
                corporate_action_available_time=decision_time - timedelta(hours=2),
                coverage_by_symbol={"AAA": 0.99, "BBB": 0.98},
                beta_by_symbol={"AAA": 0.5, "BBB": -0.4},
                industry_by_symbol={"AAA": "FIN", "BBB": "TECH"},
                adv_by_symbol={"AAA": 1000000.0, "BBB": 1000000.0},
            )
        )
    return tuple(vintages)


def make_candidate(
    *,
    state: ReplayCandidateState = ReplayCandidateState.CHALLENGER,
    version: str = "v1",
    available_time: datetime | None = None,
) -> ReplayChallengerCandidate:
    available = available_time or datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    return ReplayChallengerCandidate(
        candidate_id="challenger-main",
        version=version,
        state=state,
        policy_version="policy.v1",
        policy_hash=POLICY_HASH,
        promotion_decision_hash=stable_hash({"promotion": version}),
        evidence_hash=stable_hash({"evidence": version}),
        factor_hash=stable_hash({"factor": version}),
        scores_by_symbol={"AAA": 1.0, "BBB": -1.0},
        baseline_factor_mean=0.0,
        baseline_factor_std=1.0,
        baseline_ic_proxy=0.02,
        expected_contribution=0.01,
        available_time=available,
    )


def build_shadow_replay_fixture(
    tmp_path: Path,
    *,
    policy: ShadowReplayPolicy | None = None,
    vintages: tuple[DataVintage, ...] | None = None,
    candidates: tuple[ReplayChallengerCandidate, ...] | None = None,
    run_once_port: DeterministicRunOncePort | None = None,
) -> ShadowReplayFixture:
    policy = policy or make_replay_policy()
    vintages = vintages or make_vintages()
    default_candidates = candidates or (make_candidate(),)
    journal = ShadowReplayJournal(tmp_path / "shadow_replay.jsonl")
    port = run_once_port or DeterministicRunOncePort()
    vintage_provider = InMemoryHistoricalVintageProvider(vintages)
    challenger_provider = InMemoryChallengerProvider({}, default_candidates)
    controller = ShadowReplayController(
        vintage_provider=vintage_provider,
        challenger_provider=challenger_provider,
        run_once_port=port,
        journal=journal,
    )
    return ShadowReplayFixture(
        policy=policy,
        controller=controller,
        journal=journal,
        vintage_provider=vintage_provider,
        challenger_provider=challenger_provider,
        run_once_port=port,
    )


def make_market_price(symbol: str, price: float, decision_time: datetime) -> MarketPrice:
    return MarketPrice(
        symbol=symbol,
        price=price,
        price_time=decision_time + timedelta(minutes=5),
        available_time=decision_time + timedelta(minutes=5),
        price_kind="mid",
    )


def new_shadow_ledger() -> ShadowLedger:
    return ShadowLedger()
