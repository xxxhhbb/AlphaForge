from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.veriquant.contracts import ContractValidationError, FailureCode
from src.veriquant.evidence import ResearchSummary
from src.veriquant.evolution import (
    EvolutionOperator,
    EvolutionPromptContext,
    ParentSelector,
    VisibleParentEvidence,
    parent_evidence_from_summary,
)


HASH_A = "sha256:" + "a" * 64
HASH_B = "sha256:" + "b" * 64
HASH_C = "sha256:" + "c" * 64


def visible(
    candidate_id: str,
    *,
    candidate_hash: str,
    rank_ic: float,
    net_icir: float,
    stability: float,
    cost: float,
    novelty: float,
    incrementality: float,
) -> VisibleParentEvidence:
    return VisibleParentEvidence(
        candidate_id=candidate_id,
        candidate_hash=candidate_hash,
        evidence_hash=HASH_C,
        net_icir=net_icir,
        stability=stability,
        cost=cost,
        novelty=novelty,
        incrementality=incrementality,
        rank_ic_diagnostic=rank_ic,
    )


def test_rankic_only_cannot_win_parent_selection_or_create_a_verdict() -> None:
    rankic_only = visible(
        "parent-rankic",
        candidate_hash=HASH_A,
        rank_ic=0.99,
        net_icir=-4.0,
        stability=0.0,
        cost=0.50,
        novelty=0.0,
        incrementality=-0.20,
    )
    balanced = visible(
        "parent-balanced",
        candidate_hash=HASH_B,
        rank_ic=-0.20,
        net_icir=1.4,
        stability=0.85,
        cost=0.001,
        novelty=0.8,
        incrementality=0.08,
    )

    selected = ParentSelector().select((rankic_only, balanced), count=1)

    assert selected[0].candidate_id == "parent-balanced"
    assert "rank_ic" not in selected[0].canonical_json().lower()
    assert not hasattr(selected[0], "promotion")
    assert not hasattr(selected[0], "verdict")


def test_parent_evidence_is_derived_from_train_validation_composite_metrics() -> None:
    metrics = {
        "icir": 1.5,
        "rank_ic": 0.08,
        "gross_return": 0.12,
        "net_return": 0.09,
        "spread_cost": 0.002,
        "linear_impact_cost": 0.003,
        "subperiod_positive_fraction": 0.75,
        "regime_positive_fraction": 0.80,
        "max_registered_abs_correlation": 0.20,
        "incremental_ic_mean": 0.04,
    }
    summary = ResearchSummary(
        identity_hash=HASH_A,
        train_metrics=metrics,
        validation_metrics={**metrics, "net_return": 0.06},
    )

    evidence = parent_evidence_from_summary(
        candidate_id="parent-visible",
        candidate_hash=HASH_B,
        summary=summary,
        novelty=0.7,
    )

    assert evidence.source_partitions == ("TRAIN", "VALIDATION")
    assert evidence.net_icir > 0
    assert evidence.stability == pytest.approx(0.75)
    assert evidence.cost == pytest.approx(0.005)
    assert evidence.incrementality > 0


def test_rankic_only_summary_is_rejected_as_incomplete_parent_evidence() -> None:
    summary = ResearchSummary(
        identity_hash=HASH_A,
        train_metrics={"rank_ic": 0.9},
        validation_metrics={"rank_ic": 0.8},
    )

    with pytest.raises(ContractValidationError) as caught:
        parent_evidence_from_summary(
            candidate_id="parent-incomplete",
            candidate_hash=HASH_B,
            summary=summary,
            novelty=0.5,
        )

    assert caught.value.code is FailureCode.MISSING_REQUIRED_FIELD


@pytest.mark.parametrize("candidate_id", ["sealed-rank", "fold/test/leader", "holdout-best"])
def test_sensitive_parent_identifiers_fail_closed(candidate_id: str) -> None:
    with pytest.raises(ContractValidationError) as caught:
        visible(
            candidate_id,
            candidate_hash=HASH_A,
            rank_ic=0.1,
            net_icir=1.0,
            stability=0.8,
            cost=0.001,
            novelty=0.7,
            incrementality=0.05,
        )
    assert caught.value.code is FailureCode.EVOLUTION_CONTEXT_REJECTED


def test_mutation_context_contains_only_allowed_composite_parent_fields() -> None:
    parent = visible(
        "parent-visible",
        candidate_hash=HASH_A,
        rank_ic=0.99,
        net_icir=1.0,
        stability=0.8,
        cost=0.001,
        novelty=0.7,
        incrementality=0.05,
    )
    score = ParentSelector().select((parent,), count=1)[0]
    context = EvolutionPromptContext(
        operator=EvolutionOperator.MUTATION,
        generation=1,
        revision=0,
        child_index=0,
        parent_scores=(score,),
        deterministic_seed=7,
    )
    rendered = context.prompt_text().lower()

    assert "sealed" not in rendered
    assert "holdout" not in rendered
    assert "test_metric" not in rendered
    assert "rank_ic" not in rendered
    assert "promotion" not in rendered
