from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.veriquant.contracts import ContractValidationError, FailureCode
from src.veriquant.data import canonical_content_hash
from src.veriquant.evidence import (
    CostModel,
    CourtOutcome,
    EvaluationRow,
    EvidenceCourt,
    EvidenceCourtPolicy,
    EvidenceService,
    ExperimentIdentity,
    IncrementalityCritic,
    PITCritic,
    RegisteredFactor,
    StatisticalCritic,
    CostCapacityCritic,
    WalkForwardSpec,
    row_key,
)


BASE_TIME = datetime(2024, 1, 2, 16, 0, tzinfo=timezone.utc)
HASH_A = "sha256:" + "a" * 64
HASH_B = "sha256:" + "b" * 64
HASH_C = "sha256:" + "c" * 64
HASH_D = "sha256:" + "d" * 64


def deterministic_rows(periods: int = 32) -> tuple[EvaluationRow, ...]:
    symbols = ("AAA", "BBB", "CCC", "DDD")
    bases = {"AAA": -1.5, "BBB": -0.5, "CCC": 0.5, "DDD": 1.5}
    output: list[EvaluationRow] = []
    for period in range(periods):
        direction = 1.0 if period % 2 == 0 else -1.0
        regime = "CALM" if period < periods // 2 else "STRESSED"
        for symbol in symbols:
            score = direction * bases[symbol]
            output.append(
                EvaluationRow(
                    event_time=BASE_TIME + timedelta(days=period),
                    symbol=symbol,
                    score=score,
                    label=0.01 * score + 0.0001 * period,
                    market_regime=regime,
                    volume=1_000_000.0 + 1_000.0 * period,
                )
            )
    return tuple(output)


def identity_for(cost: CostModel, split: WalkForwardSpec, suffix: str = "001") -> ExperimentIdentity:
    return ExperimentIdentity(
        experiment_id=f"experiment-{suffix}",
        dataset_hash=HASH_A,
        universe_hash=HASH_B,
        factor_hash=HASH_C,
        split_hash=split.artifact_hash,
        label_hash=HASH_D,
        cost_hash=cost.artifact_hash,
        code_hash=canonical_content_hash({"service": "walk-forward-v1"}),
        availability_hash=canonical_content_hash({"clock": "bitemporal-v1"}),
        seed=19,
    )


def test_official_metrics_replay_identically_from_frozen_inputs() -> None:
    rows = deterministic_rows()
    split = WalkForwardSpec(
        train_periods=10,
        validation_periods=8,
        sealed_periods=8,
        step_periods=6,
    )
    costs = CostModel(spread_bps=1.0, linear_impact_bps=1.5)
    identity = identity_for(costs, split)
    service = EvidenceService()

    first = service.evaluate(
        identity=identity,
        rows=rows,
        partition="VALIDATION",
        cost_model=costs,
        annualization=split.annualization,
        quantile_count=split.quantile_count,
    )
    second = EvidenceService().evaluate(
        identity=identity,
        rows=tuple(reversed(rows)),
        partition="VALIDATION",
        cost_model=costs,
        annualization=split.annualization,
        quantile_count=split.quantile_count,
    )

    assert first.artifact_hash == second.artifact_hash
    assert first.metrics == second.metrics
    assert first.metrics.pearson_ic > 0.9
    assert first.metrics.rank_ic > 0.9
    assert first.metrics.ic_mean > 0.9
    assert first.metrics.ic_std == pytest.approx(0.0)
    assert first.metrics.quantile_spread > 0.0
    assert first.metrics.gross_return > first.metrics.net_return
    assert first.metrics.spread_cost > 0.0
    assert first.metrics.linear_impact_cost > 0.0
    assert first.metrics.max_drawdown >= 0.0
    assert first.metrics.subperiod_positive_fraction == 1.0
    assert first.metrics.regime_positive_fraction == 1.0


def test_walk_forward_visible_evidence_and_registered_factor_incrementality() -> None:
    rows = deterministic_rows()
    split = WalkForwardSpec(
        train_periods=10,
        validation_periods=8,
        sealed_periods=8,
        step_periods=6,
    )
    costs = CostModel(spread_bps=1.0, linear_impact_bps=1.0)
    identity = identity_for(costs, split)
    registered = RegisteredFactor(
        factor_id="registered-001",
        factor_hash=HASH_A,
        scores={row_key(row): row.score for row in rows},
    )
    service = EvidenceService()
    visible = service.evaluate_visible_walk_forward(
        identity=identity,
        rows=rows,
        spec=split,
        cost_model=costs,
        registered_factors=(registered,),
    )
    summary = service.research_summary(identity, visible)

    assert len(visible) == 2
    assert visible[0].train.partition == "TRAIN"
    assert visible[0].validation.partition == "VALIDATION"
    assert visible[0].validation.metrics.max_registered_abs_correlation == pytest.approx(1.0)
    assert abs(visible[0].validation.metrics.incremental_ic_mean) < 1e-12
    assert "rank_ic" in summary.validation_metrics
    assert "sealed" not in summary.canonical_json().lower()


def test_all_four_critics_are_mechanical_and_gross_good_net_negative_rejects() -> None:
    rows = deterministic_rows()
    split = WalkForwardSpec(
        train_periods=10,
        validation_periods=8,
        sealed_periods=8,
        step_periods=6,
    )
    expensive = CostModel(spread_bps=300.0, linear_impact_bps=300.0)
    identity = identity_for(expensive, split, "costly")
    evidence = EvidenceService().evaluate(
        identity=identity,
        rows=rows,
        partition="VALIDATION",
        cost_model=expensive,
    )
    court = EvidenceCourt(
        EvidenceCourtPolicy(
            min_observations=20,
            min_rank_ic=0.0,
            min_icir=-1.0,
            max_turnover=1.1,
            max_drawdown=1.0,
            min_net_return=0.0,
            max_registered_abs_correlation=1.0,
            min_incremental_ic=-1.0,
        )
    )

    assert evidence.metrics.gross_return > 0.0
    assert evidence.metrics.net_return < 0.0
    findings = court.findings(evidence, pit_valid=True)
    assert {item.critic for item in findings} == {
        PITCritic.name,
        StatisticalCritic.name,
        CostCapacityCritic.name,
        IncrementalityCritic.name,
    }
    decision = court.decide(
        evidence=evidence,
        identity_hash=identity.identity_hash,
        proposer_id="researcher-001",
        approver_id="promotion-001",
        decided_at=BASE_TIME + timedelta(days=40),
        pit_valid=True,
        sealed_passed=True,
    )
    assert decision.outcome is CourtOutcome.REJECT
    assert "COSTS_ERASE_ALPHA" in decision.reason_codes


def test_mechanical_court_validates_then_promotes_only_after_sealed_pass() -> None:
    rows = deterministic_rows()
    split = WalkForwardSpec(
        train_periods=10,
        validation_periods=8,
        sealed_periods=8,
        step_periods=6,
    )
    costs = CostModel(spread_bps=0.1, linear_impact_bps=0.1)
    identity = identity_for(costs, split, "court")
    evidence = EvidenceService().evaluate(
        identity=identity,
        rows=rows,
        partition="VALIDATION",
        cost_model=costs,
    )
    court = EvidenceCourt(
        EvidenceCourtPolicy(
            min_observations=20,
            min_rank_ic=0.0,
            min_icir=-1.0,
            max_turnover=1.1,
            max_drawdown=1.0,
            min_net_return=0.0,
            max_registered_abs_correlation=1.0,
            min_incremental_ic=-1.0,
        )
    )
    validated = court.decide(
        evidence=evidence,
        identity_hash=identity.identity_hash,
        proposer_id="researcher-001",
        approver_id="promotion-001",
        decided_at=BASE_TIME + timedelta(days=40),
        pit_valid=True,
        sealed_passed=None,
    )
    promoted = court.decide(
        evidence=evidence,
        identity_hash=identity.identity_hash,
        proposer_id="researcher-001",
        approver_id="promotion-001",
        decided_at=BASE_TIME + timedelta(days=41),
        pit_valid=True,
        sealed_passed=True,
    )

    assert validated.outcome is CourtOutcome.VALIDATED_RESEARCH
    assert promoted.outcome is CourtOutcome.PROMOTE_CHALLENGER


def test_same_proposer_cannot_approve_own_evidence() -> None:
    rows = deterministic_rows()
    split = WalkForwardSpec(
        train_periods=10,
        validation_periods=8,
        sealed_periods=8,
        step_periods=6,
    )
    costs = CostModel(spread_bps=1.0, linear_impact_bps=1.0)
    identity = identity_for(costs, split, "self")
    evidence = EvidenceService().evaluate(
        identity=identity,
        rows=rows,
        partition="VALIDATION",
        cost_model=costs,
    )

    with pytest.raises(ContractValidationError) as caught:
        EvidenceCourt().decide(
            evidence=evidence,
            identity_hash=identity.identity_hash,
            proposer_id="same-agent",
            approver_id="same-agent",
            decided_at=BASE_TIME + timedelta(days=40),
            pit_valid=True,
            sealed_passed=True,
        )

    assert caught.value.code is FailureCode.SELF_APPROVAL


def test_experiment_identity_binds_all_required_frozen_hashes_and_seed() -> None:
    split = WalkForwardSpec(
        train_periods=10,
        validation_periods=8,
        sealed_periods=8,
        step_periods=6,
    )
    costs = CostModel(spread_bps=1.0, linear_impact_bps=1.0)
    identity = identity_for(costs, split)

    assert set(identity.to_dict()) >= {
        "dataset_hash",
        "universe_hash",
        "factor_hash",
        "split_hash",
        "label_hash",
        "cost_hash",
        "code_hash",
        "availability_hash",
        "seed",
    }

