from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timedelta, timezone

import pytest

from src.veriquant.contracts import ContractValidationError, FailureCode
from src.veriquant.data import canonical_content_hash
from src.veriquant.evidence import (
    CostModel,
    EvaluationRow,
    EvidenceService,
    ExperimentIdentity,
    PromotionController,
    ResearchChannel,
    ResearchDestination,
    SealedActor,
    SealedTestStore,
    WalkForwardSpec,
    build_walk_forward_folds,
)


START = datetime(2024, 1, 2, 16, 0, tzinfo=timezone.utc)
HASH = "sha256:" + "e" * 64


def rows() -> tuple[EvaluationRow, ...]:
    output: list[EvaluationRow] = []
    values = {"AAA": -1.5, "BBB": -0.5, "CCC": 0.5, "DDD": 1.5}
    for period in range(30):
        direction = 1.0 if period % 2 == 0 else -1.0
        for symbol, base in values.items():
            score = direction * base
            output.append(
                EvaluationRow(
                    event_time=START + timedelta(days=period),
                    symbol=symbol,
                    score=score,
                    label=score * 0.01,
                    market_regime="CALM" if period < 15 else "STRESSED",
                    volume=1_000_000.0,
                )
            )
    return tuple(output)


def setup_visible_and_sealed():
    all_rows = rows()
    spec = WalkForwardSpec(
        train_periods=10,
        validation_periods=8,
        sealed_periods=8,
        step_periods=8,
    )
    costs = CostModel(spread_bps=0.1, linear_impact_bps=0.1)
    identity = ExperimentIdentity(
        experiment_id="sealed-experiment-001",
        dataset_hash=HASH,
        universe_hash=canonical_content_hash({"universe": "historical"}),
        factor_hash=canonical_content_hash({"factor": "momentum"}),
        split_hash=spec.artifact_hash,
        label_hash=canonical_content_hash({"label": "future-5d"}),
        cost_hash=costs.artifact_hash,
        code_hash=canonical_content_hash({"code": "evidence-v1"}),
        availability_hash=canonical_content_hash({"availability": "pit-v1"}),
        seed=3,
    )
    service = EvidenceService()
    folds = build_walk_forward_folds(all_rows, spec)
    visible = service.evaluate_visible_walk_forward(
        identity=identity,
        rows=all_rows,
        spec=spec,
        cost_model=costs,
    )
    summary = service.research_summary(identity, visible)
    store = SealedTestStore({fold.fold_id: fold.sealed_rows for fold in folds})
    return identity, costs, service, folds, summary, store


@pytest.mark.parametrize(
    "actor",
    [
        SealedActor.RESEARCHER,
        SealedActor.EVOLUTION,
        SealedActor.FEEDBACK,
        SealedActor.PARENT_SELECTION,
    ],
)
def test_researcher_and_evolution_authorities_cannot_read_sealed(actor: SealedActor) -> None:
    _, _, _, folds, _, store = setup_visible_and_sealed()

    with pytest.raises(ContractValidationError) as caught:
        store.read(folds[0].fold_id, actor=actor)

    assert caught.value.code is FailureCode.UNAUTHORIZED_CAPABILITY


def test_promotion_controller_is_exclusive_reader_and_returns_only_terminal_boolean() -> None:
    identity, costs, service, folds, _, store = setup_visible_and_sealed()
    controller = PromotionController(service, store)

    verdict = controller.evaluate_sealed(
        fold_id=folds[0].fold_id,
        identity=identity,
        cost_model=costs,
        min_net_return=0.0,
        min_rank_ic=0.0,
    )

    assert verdict.passed is True
    assert asdict(verdict) == {"passed": True}
    assert "metric" not in repr(verdict).lower()
    assert "hash" not in repr(verdict).lower()


def test_research_channels_publish_only_train_validation_redacted_summary() -> None:
    _, _, _, _, summary, _ = setup_visible_and_sealed()
    channel = ResearchChannel()

    for destination in ResearchDestination:
        payload = channel.publish(summary, destination=destination)
        rendered = str(payload).lower()
        assert payload["destination"] == destination.value
        assert "sealed" not in rendered
        assert "test_metric" not in rendered
        assert "artifact_path" not in rendered


def test_research_channel_rejects_non_summary_payload() -> None:
    channel = ResearchChannel()

    with pytest.raises(ContractValidationError) as caught:
        channel.publish(  # type: ignore[arg-type]
            {"sealed_metrics": {"rank_ic": 0.9}},
            destination=ResearchDestination.LLM_CONTEXT,
        )

    assert caught.value.code is FailureCode.UNAUTHORIZED_CAPABILITY

