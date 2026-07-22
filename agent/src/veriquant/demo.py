"""Deterministic, offline, end-to-end VeriQuant demonstration.

The demo is deliberately an integration harness rather than another authority.  It
connects the typed services built in Waves 01--08 and checks their boundary
invariants before emitting machine- and human-readable reports.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping

from .adapters import ProposalContext
from .contracts import ResearchQuestion, ResearchState, RiskVerdict
from .contracts.base import canonicalize
from .data import FEATURE_FIELDS, canonical_content_hash
from .downstream import (
    AlphaCalibrationRequest,
    AlphaRegistry,
    AssetRiskInput,
    DeterministicAlphaService,
    DeterministicPortfolioConstructor,
    IndependentRiskGate,
    PortfolioConstructionRequest,
    PortfolioConstraints,
    PortfolioObjectivePolicy,
    RiskGateRequest,
    RiskPolicy,
)
from .evidence import CourtOutcome, TrialLedger
from .evolution import (
    EvolutionController,
    EvolutionLimits,
    EvolutionOperator,
    FrozenIdentityInputs,
    GenerationIdentityAuthority,
    LineageStore,
    ProposalMemory,
)
from .paper import (
    DailyReconciler,
    DeterministicClock,
    PaperAttributionService,
    PaperExecutionEngine,
    PaperExecutionPolicy,
    PaperJournal,
    PaperMarketQuote,
    PaperOrderPlanner,
    ReconciliationStatus,
)
from .research import (
    FIVE_STAGE_SEQUENCE,
    DeterministicOfficialEvidenceAuthority,
    ResearchDAGController,
    TaskStatus,
    build_default_research_dataset,
)


class DemoInvariantError(RuntimeError):
    """Raised when an integration invariant fails; the CLI converts it to exit 1."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise DemoInvariantError(message)


def _write_atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _latest_complete_cross_section(branch, universe: tuple[str, ...]):
    by_time: dict[Any, dict[str, float]] = {}
    for item in branch.calculation.factor_result.values:
        if item.symbol in universe and item.value is not None:
            by_time.setdefault(item.event_time, {})[item.symbol] = float(item.value)
    for event_time in sorted(by_time, reverse=True):
        values = by_time[event_time]
        if set(values) == set(universe):
            return event_time, {symbol: values[symbol] for symbol in universe}
    raise DemoInvariantError("selected factor has no complete PIT cross-section for current universe")


def _asset_inputs(universe: tuple[str, ...]) -> tuple[AssetRiskInput, ...]:
    catalog = {
        "AAA": ("TECH", 1.10, 0.20, 20_000_000.0, 2.0, 0.12),
        "BBB": ("FINANCE", 0.90, 0.18, 18_000_000.0, 2.5, 0.10),
        "CCC": ("TECH", 1.20, 0.22, 16_000_000.0, 3.0, 0.14),
        "DDD": ("INDUSTRIAL", 0.80, 0.16, 15_000_000.0, 2.0, 0.09),
    }
    output = []
    for symbol in universe:
        industry, beta, volatility, adv, spread, stress = catalog[symbol]
        output.append(
            AssetRiskInput(
                symbol=symbol,
                industry=industry,
                beta=beta,
                volatility=volatility,
                adv_dollars=adv,
                spread_bps=spread,
                stress_loss_rate=stress,
            )
        )
    return tuple(output)


def _markdown(report: Mapping[str, Any]) -> str:
    outcomes = report["research"]["court_outcomes"]
    outcome_rows = "\n".join(
        f"| {branch} | {outcome} |" for branch, outcome in sorted(outcomes.items())
    )
    checks = "\n".join(
        f"- [{'x' if passed else ' '}] `{name}`"
        for name, passed in sorted(report["invariants"].items())
    )
    return f"""# VeriQuant offline demo

- Status: **{report['status']}**
- Seed: `{report['seed']}`
- Dataset: `{report['pit']['dataset_id']}`
- Selected validated branch: `{report['research']['selected_branch_id']}`
- Risk verdict: `{report['risk']['verdict']}`
- Reconciliation: `{report['paper']['reconciliation_status']}`
- Report hash: `{report['report_hash']}`

## Evidence Court outcomes

| Research Desk | Outcome |
|---|---|
{outcome_rows}

## Trusted path

The six fixed five-stage Desk state machines used the synthetic bitemporal
fixture, restricted FactorDAG execution, one deterministic Evidence authority,
and the append-only Trial Ledger. Bounded evolution ran with a one-child budget
and produced only a DRAFT; it did not approve itself or feed Alpha Service.
The selected `VALIDATED_RESEARCH` candidate then passed deterministic alpha
calibration, market-neutral portfolio construction, the independent Risk Gate,
paper execution, append-only accounting, reconciliation, and attribution.

## Invariants

{checks}

## Principal artifacts

- Research run: `research_runs/{report['artifacts']['research_run']}`
- Research Trial Ledger: `research_runs/{report['artifacts']['research_run']}/trial_ledger.sqlite3`
- Evolution Trial Ledger: `{report['artifacts']['evolution_ledger']}`
- Evolution lineage memory: `{report['artifacts']['evolution_memory']}`
- Paper journal: `{report['artifacts']['paper_journal']}`
"""


def run_offline_demo(*, output_dir: str | Path, seed: int = 7) -> dict[str, Any]:
    """Run the complete offline thin slice and return its canonical report."""

    if isinstance(seed, bool) or not isinstance(seed, int) or seed < 0:
        raise DemoInvariantError("seed must be a non-negative integer")
    root = Path(output_dir).resolve()
    try:
        root.mkdir(parents=True, exist_ok=False)
    except FileExistsError as exc:
        raise DemoInvariantError(f"output directory already exists: {root}") from exc

    authority = DeterministicOfficialEvidenceAuthority(
        dataset=build_default_research_dataset(seed=seed)
    )
    question = ResearchQuestion(
        question_id="prompt09-offline-question",
        text="Find reproducible visible factors using only the frozen offline PIT fixture.",
        universe=("AAA", "BBB", "CCC", "DDD"),
        horizon="5d",
        decision_time=authority.dataset.decision_time,
        availability_assumptions=(
            "Every feature must be available at or before the decision time.",
            "Historical membership, not the final universe, defines each cross-section.",
        ),
        state=ResearchState.DRAFT,
    )
    dataset = authority.dataset.registry.get(authority.dataset.dataset_id)
    pit_snapshot = authority.dataset.registry.as_of_snapshot(
        authority.dataset.dataset_id,
        decision_time=authority.dataset.decision_time,
        fields=FEATURE_FIELDS,
        start_time=dataset.calendar[0],
    )
    current_universe = authority.dataset.registry.universe_snapshot(
        authority.dataset.dataset_id,
        authority.dataset.decision_time,
    )

    research_run_id = f"prompt09-seed-{seed}"
    research = ResearchDAGController(
        authority=authority,
        artifact_root=root / "research_runs",
        max_workers=6,
    ).run(run_id=research_run_id, question=question)
    records = research.dag_result.records
    _require(records["selection"].status is TaskStatus.COMPLETED, "selection did not complete")
    _require(records["validated_aggregation"].status is TaskStatus.COMPLETED, "aggregation failed")

    court_results = {
        desk: records[f"court.{desk}"].output
        for desk in sorted(item.removeprefix("court.") for item in records if item.startswith("court."))
    }
    _require(all(item is not None for item in court_results.values()), "a court task has no result")
    court_outcomes = {
        desk: item.decision.outcome.value for desk, item in court_results.items()
    }
    validated = [
        desk for desk, result in court_results.items()
        if result.decision.outcome is CourtOutcome.VALIDATED_RESEARCH
    ]
    rejected_or_revise = [
        desk for desk, result in court_results.items()
        if result.decision.outcome in {CourtOutcome.REJECT, CourtOutcome.REVISE}
    ]
    _require(bool(validated), "Evidence Court produced no VALIDATED_RESEARCH")
    _require(bool(rejected_or_revise), "Evidence Court produced no REJECT/REVISE")
    selection = records["selection"].output
    selected = court_results[selection.selected_branch_id]
    branch_outputs = [records[f"branch.{desk}"].output for desk in court_results]
    _require(
        all(output.stage_trace == FIVE_STAGE_SEQUENCE for output in branch_outputs),
        "a Research Desk bypassed the fixed five-stage protocol",
    )

    official_calls = [call for call in authority.calls if call.operation == "OFFICIAL_EVALUATE"]
    _require(len(official_calls) == 6, "six Desks did not share six official evaluations")
    _require(
        len({call.authority_object_id for call in official_calls}) == 1,
        "Research Desks did not share one Evidence authority",
    )
    research_ledger = TrialLedger(research.run_dir / "trial_ledger.sqlite3").replay()
    _require(
        len(research_ledger.families) == 6
        and all(family.consumed == 1 for family in research_ledger.families.values()),
        "research family budgets were not consumed exactly once",
    )

    manifest = selected.branch.evaluation.manifest
    evolution_dir = root / "evolution"
    evolution_dir.mkdir()
    evolution_ledger = TrialLedger(
        evolution_dir / "trial-ledger.sqlite3",
        clock=lambda: authority.dataset.evaluation_time,
    )
    evolution_memory = ProposalMemory(LineageStore(evolution_dir / "lineage-memory.sqlite3"))
    evolution = EvolutionController(
        family_id="prompt09-bounded-family",
        proposer_id="deterministic-evolution-provider",
        ledger=evolution_ledger,
        memory=evolution_memory,
        limits=EvolutionLimits(
            max_generations=1,
            max_children=1,
            family_budget=1,
            max_revisions=0,
        ),
        proposal_context=ProposalContext(
            direction_hash=question.artifact_hash,
            universe=question.universe,
            horizon=question.horizon,
            decision_time=question.decision_time,
            availability_assumptions=question.availability_assumptions,
            field_roles=dataset.field_roles,
        ),
        identity_authority=GenerationIdentityAuthority(
            FrozenIdentityInputs(
                dataset_hash=manifest.dataset_hash,
                universe_hash=manifest.universe_hash,
                split_hash=manifest.split_hash,
                label_hash=manifest.label_hash,
                cost_hash=manifest.cost_hash,
                code_hash=manifest.code_hash,
                availability_hash=manifest.availability_hash,
            )
        ),
        seed=seed,
        clock=lambda: authority.dataset.evaluation_time,
    )
    evolution_child = evolution.generate(
        operator=EvolutionOperator.ORIGINAL,
        generation=0,
        count=1,
        max_workers=1,
    ).children[0]
    evolution_replay = evolution_ledger.replay()
    _require(evolution_child.committed, "bounded evolution child did not commit")
    _require(
        evolution_child.proposal is not None
        and evolution_child.proposal.factor_draft.state is ResearchState.DRAFT,
        "bounded evolution exceeded DRAFT authority",
    )
    _require(
        evolution_replay.families["prompt09-bounded-family"].consumed == 1,
        "bounded evolution did not consume its reserved family budget",
    )

    score_time, factor_scores = _latest_complete_cross_section(
        selected.branch,
        current_universe,
    )
    alpha_request = AlphaCalibrationRequest(
        candidate_id=f"candidate-{selected.branch_id}",
        candidate_hash=selected.branch.proposal.factor_draft.artifact_hash,
        state=ResearchState.VALIDATED_RESEARCH,
        universe=current_universe,
        factor_scores=factor_scores,
        horizon=question.horizon,
        decision_time=question.decision_time,
        evidence=selected.branch.evaluation.evidence,
        decision=selected.decision,
        decay_half_life_days=20.0,
    )
    forecast = DeterministicAlphaService().calibrate(alpha_request)
    registry = AlphaRegistry()
    alpha_record = registry.register(
        alpha_id=f"alpha-{selected.branch_id}",
        forecast=forecast,
        identity_hash=selected.branch.evaluation.evidence.identity_hash,
        registered_at=authority.dataset.evaluation_time,
    )
    assets = _asset_inputs(current_universe)
    current_weights = {symbol: 0.0 for symbol in current_universe}
    capital = 1_000_000.0
    portfolio = DeterministicPortfolioConstructor().construct(
        PortfolioConstructionRequest(
            request_id="prompt09-portfolio-request",
            forecast=forecast,
            assets=assets,
            current_weights=current_weights,
            constraints=PortfolioConstraints(
                capital=capital,
                market_neutral=True,
                max_gross_leverage=0.60,
                max_single_name=0.20,
                max_industry_exposure=0.20,
                max_beta_exposure=0.05,
                max_turnover=0.60,
                max_adv_participation=0.05,
            ),
            objective_policy=PortfolioObjectivePolicy(
                risk_aversion=2.0,
                turnover_penalty=0.0001,
                cost_penalty=1.0,
                impact_coefficient=0.10,
                step_size=0.20,
                iterations=120,
            ),
        )
    )
    risk = IndependentRiskGate().assess(
        RiskGateRequest(
            request_id="prompt09-risk-request",
            plan=portfolio.plan,
            assets=assets,
            current_weights=current_weights,
            capital=capital,
            policy=RiskPolicy(
                max_beta_exposure=0.10,
                max_concentration=0.25,
                max_industry_exposure=0.25,
                max_adv_participation=0.06,
                max_gross_leverage=0.70,
                max_stress_loss_rate=0.10,
            ),
            decision_time=question.decision_time,
        )
    )
    _require(
        risk.verdict in {RiskVerdict.APPROVE, RiskVerdict.SCALE_DOWN},
        "independent Risk Gate did not authorize paper execution",
    )

    price_snapshot = authority.dataset.registry.as_of_snapshot(
        authority.dataset.dataset_id,
        decision_time=question.decision_time,
        fields=("close",),
        start_time=question.decision_time,
    )
    prices = {
        row.symbol: Decimal(str(row.values["close"]))
        for row in price_snapshot.rows
        if row.symbol in current_universe and row.values["close"] is not None
    }
    _require(set(prices) == set(current_universe), "current PIT prices do not match universe")
    order_batch = PaperOrderPlanner().create(
        portfolio_plan=portfolio.plan,
        risk_decision=risk,
        capital=Decimal(str(capital)),
        prices=prices,
        current_positions={symbol: Decimal("0") for symbol in current_universe},
    )
    asset_by_symbol = {asset.symbol: asset for asset in assets}
    quotes = {
        symbol: PaperMarketQuote(
            symbol=symbol,
            mid_price=prices[symbol],
            spread_bps=Decimal(str(asset_by_symbol[symbol].spread_bps)),
            adv_shares=Decimal(str(asset_by_symbol[symbol].adv_dollars)) / prices[symbol],
            event_time=question.decision_time,
        )
        for symbol in current_universe
    }
    execution_engine = PaperExecutionEngine(
        clock=DeterministicClock(
            start=question.decision_time,
            step=timedelta(microseconds=1),
        ),
        seed=seed,
    )
    execution = execution_engine.execute_once(
        batch=order_batch,
        quotes=quotes,
        policy=PaperExecutionPolicy(
            max_adv_participation=Decimal("0.50"),
            base_fill_fraction=Decimal("0.60"),
            linear_impact_bps=Decimal("25"),
            commission_bps=Decimal("1"),
        ),
    )
    journal_path = root / "paper" / "journal.sqlite3"
    journal = PaperJournal(
        journal_path,
        initial_cash=Decimal(str(capital)),
        opened_at=question.decision_time - timedelta(seconds=1),
    )
    for fill in execution.fills:
        journal.post_fill(paper_fill=fill, batch=order_batch)
    marks = {
        symbol: prices[symbol]
        * (Decimal("1.01") if portfolio.plan.target_weights[symbol] >= 0 else Decimal("0.995"))
        for symbol in current_universe
    }
    cost_time = question.decision_time + timedelta(days=1)
    journal.mark_to_market(marks=marks, event_time=cost_time)
    journal.post_daily_costs(
        marks=marks,
        borrow_bps=Decimal("2"),
        financing_bps=Decimal("1"),
        event_time=cost_time + timedelta(microseconds=1),
    )
    before_reconciliation = journal.replay()
    reconciliation_time = question.decision_time + timedelta(days=2)
    reconciliation = DailyReconciler().reconcile(
        journal=journal,
        marks=marks,
        reconciled_at=reconciliation_time,
        external_cash=before_reconciliation.cash,
        external_positions=before_reconciliation.positions,
        execution_engine=execution_engine,
    )
    _require(
        reconciliation.status is ReconciliationStatus.RECONCILED,
        "paper journal did not reconcile",
    )
    journal_state = journal.replay()
    attribution = PaperAttributionService().attribute(
        state=journal_state,
        portfolio_plan=portfolio.plan,
        assets=assets,
        factor_loadings={"validated_factor": factor_scores},
        period_start=question.decision_time,
        period_end=reconciliation_time,
    )

    invariants = {
        "offline_deterministic_provider": True,
        "synthetic_pit_snapshot": all(
            row.event_time <= pit_snapshot.decision_time for row in pit_snapshot.rows
        ),
        "six_isolated_research_desks": len(court_results) == 6,
        "fixed_five_stage_protocol": all(
            output.stage_trace == FIVE_STAGE_SEQUENCE for output in branch_outputs
        ),
        "single_official_evidence_authority": len({call.authority_object_id for call in official_calls}) == 1,
        "reject_or_revise_observed": bool(rejected_or_revise),
        "validated_research_observed": bool(validated),
        "trial_ledger_budget_consumed": all(
            family.consumed == 1 for family in research_ledger.families.values()
        ),
        "evolution_is_draft_only": evolution_child.proposal.factor_draft.state is ResearchState.DRAFT,
        "evolution_budget_consumed": evolution_replay.families["prompt09-bounded-family"].consumed == 1,
        "alpha_registry_is_challenger": alpha_record.status is ResearchState.CHALLENGER,
        "portfolio_is_market_neutral": abs(portfolio.diagnostics.net_exposure) <= 2e-7,
        "risk_gate_authorized_paper_only": risk.verdict in {RiskVerdict.APPROVE, RiskVerdict.SCALE_DOWN},
        "paper_fills_are_partial_or_full": bool(execution.fills),
        "journal_reconciled": reconciliation.status is ReconciliationStatus.RECONCILED,
        "accounting_identity_holds": abs(journal_state.equity - journal_state.expected_equity) <= Decimal("0.0001"),
        "attribution_bound_to_ledger": attribution.ledger_snapshot_hash == journal_state.artifact_hash,
    }
    failed = sorted(name for name, passed in invariants.items() if not passed)
    _require(not failed, "failed invariants: " + ", ".join(failed))

    report: dict[str, Any] = {
        "schema_version": "veriquant.demo.v1",
        "status": "PASSED",
        "mode": "OFFLINE",
        "seed": seed,
        "pit": {
            "dataset_id": dataset.card.dataset_id,
            "dataset_hash": dataset.card.snapshot_hash,
            "dataset_card_hash": dataset.card.artifact_hash,
            "snapshot_hash": pit_snapshot.artifact_hash,
            "decision_time": question.decision_time.isoformat(),
            "historical_question_universe": list(question.universe),
            "current_universe": list(current_universe),
            "coverage_by_field": dict(pit_snapshot.coverage_by_field),
        },
        "research": {
            "run_id": research_run_id,
            "court_outcomes": court_outcomes,
            "validated_branches": validated,
            "reject_or_revise_branches": rejected_or_revise,
            "selected_branch_id": selected.branch_id,
            "selected_evidence_hash": selected.branch.evaluation.evidence.artifact_hash,
            "selected_identity_hash": selected.branch.evaluation.evidence.identity_hash,
            "selected_metrics": dict(selected.branch.evaluation.evidence.metrics.flat()),
            "official_evaluation_count": len(official_calls),
            "score_time": score_time.isoformat(),
        },
        "evolution": {
            "operator": EvolutionOperator.ORIGINAL.value,
            "child_id": evolution_child.child_id,
            "state": evolution_child.proposal.factor_draft.state.value,
            "lineage_hash": evolution_child.lineage.artifact_hash,
            "budget_consumed": evolution_replay.families["prompt09-bounded-family"].consumed,
            "family_budget": 1,
            "trusted_for_alpha": False,
        },
        "alpha": {
            "forecast_hash": forecast.artifact_hash,
            "registry_version": alpha_record.version,
            "registry_status": alpha_record.status.value,
            "confidence": forecast.confidence,
            "horizon": forecast.horizon,
        },
        "portfolio": {
            "plan_hash": portfolio.plan.artifact_hash,
            "target_weights": dict(portfolio.plan.target_weights),
            "gross_leverage": portfolio.diagnostics.gross_leverage,
            "net_exposure": portfolio.diagnostics.net_exposure,
            "estimated_cost": portfolio.diagnostics.estimated_cost,
            "objective_value": portfolio.diagnostics.objective_value,
        },
        "risk": {
            "decision_hash": risk.artifact_hash,
            "verdict": risk.verdict.value,
            "scale_factor": risk.scale_factor,
            "breaches": list(risk.breaches),
            "checked_metrics": dict(risk.checked_metrics),
        },
        "paper": {
            "order_plan_hash": order_batch.order_plan.artifact_hash,
            "execution_hash": execution.artifact_hash,
            "fill_count": len(execution.fills),
            "journal_state_hash": journal_state.artifact_hash,
            "journal_event_count": journal_state.event_count,
            "reconciliation_hash": reconciliation.artifact_hash,
            "reconciliation_status": reconciliation.status.value,
            "attribution_hash": attribution.artifact_hash,
            "pnl_total": attribution.pnl_components["total"],
        },
        "invariants": invariants,
        "artifacts": {
            "research_run": research_run_id,
            "evolution_ledger": "evolution/trial-ledger.sqlite3",
            "evolution_memory": "evolution/lineage-memory.sqlite3",
            "paper_journal": "paper/journal.sqlite3",
            "json_report": "demo_report.json",
            "markdown_report": "demo_report.md",
        },
    }
    report["report_hash"] = canonical_content_hash(report)
    canonical_report = canonicalize(report)
    json_text = json.dumps(
        canonical_report,
        sort_keys=True,
        indent=2,
        ensure_ascii=False,
        allow_nan=False,
    ) + "\n"
    _write_atomic(root / "demo_report.json", json_text)
    _write_atomic(root / "demo_report.md", _markdown(canonical_report))
    return canonical_report


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the VeriQuant offline trusted thin slice")
    parser.add_argument("--offline", action="store_true", help="required fail-closed offline mode")
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path(".autobuild/veriquant/demo/prompt09-seed7"),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        _require(args.offline, "--offline is required; networked demo mode does not exist")
        report = run_offline_demo(output_dir=args.output_dir, seed=args.seed)
    except Exception as exc:
        failure = {
            "schema_version": "veriquant.demo.failure.v1",
            "status": "FAILED",
            "error_type": type(exc).__name__,
            "message": str(exc),
        }
        sys.stderr.write(json.dumps(failure, sort_keys=True, ensure_ascii=False) + "\n")
        return 1
    sys.stdout.write(json.dumps(report, sort_keys=True, ensure_ascii=False) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
