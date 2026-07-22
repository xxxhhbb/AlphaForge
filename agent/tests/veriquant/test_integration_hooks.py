from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from src.veriquant.adapters.proposal import ProposalContext
from src.veriquant.contracts import ResearchState
from src.veriquant.data import (
    DatasetRegistry,
    FieldRole,
    LABEL_FIELD,
    build_synthetic_market_fixture,
    canonical_content_hash,
)
from src.veriquant.evidence import CostModel, EvaluationRow, EvidenceService, ExperimentIdentity
from src.veriquant.evidence.models import MetricSet, OfficialEvidence
from src.veriquant.factors import execute_factor_dag
from src.veriquant.integration import (
    InMemoryOfficialEvidenceReader,
    PromotionEvaluationRequest,
    adapt_source_backed_draft,
    build_family_manifest,
    candidate_view_from_official_evidence,
    evaluate_statistical_promotion,
    run_bounded_llm_desks_offline,
    source_backed_hypothesis_to_proposal_payload,
)
from src.veriquant.llm_desks.api import FIVE_STAGE_SEQUENCE, LLMDeskStatus, ResearchQuestionView
from src.veriquant.research_memory.api import (
    CitationCritic,
    EvidenceGroundedResearchDAG,
    FrozenDocumentStore,
    FrozenWebFixtureGateway,
    HypothesisState,
    HypothesisSynthesizer,
    ResearchBrief,
    ResearchMemoryStore,
    Retriever,
    SourceKind,
    SourceRequest,
)
from src.veriquant.statistical_promotion.contracts import PromotionState, StatisticalPromotionFailureCode


DECISION_TIME = datetime(2026, 7, 18, 3, 0, tzinfo=timezone.utc)


def _hash(label: str) -> str:
    return canonical_content_hash({"fixture": label})


def _metrics(*, net_return: float = 0.03, gross_return: float = 0.08) -> MetricSet:
    return MetricSet(
        pearson_ic=0.05,
        rank_ic=0.04,
        ic_mean=0.02,
        ic_std=0.01,
        icir=1.25,
        quantile_spread=0.02,
        turnover=0.35,
        gross_return=gross_return,
        net_return=net_return,
        spread_cost=0.004,
        linear_impact_cost=0.004,
        max_drawdown=0.02,
        subperiod_positive_fraction=0.75,
        subperiod_min_ic=0.02,
        regime_positive_fraction=0.75,
        regime_min_ic=0.02,
        max_registered_abs_correlation=0.10,
        incremental_ic_mean=0.02,
        observation_count=90,
        period_count=30,
    )


def _official(partition: str, *, identity_hash: str, net_return: float = 0.03) -> OfficialEvidence:
    return OfficialEvidence(
        identity_hash=identity_hash,
        partition=partition,
        metrics=_metrics(net_return=net_return),
        input_hashes=(_hash(f"{partition}.input"),),
    )


def test_statistical_promotion_bridge_uses_official_visible_evidence_and_opaque_sealed_handle() -> None:
    manifest = build_family_manifest(
        family_id="family.integration",
        manifest_version="v1",
        frozen_at=DECISION_TIME,
        candidates=(("cand.pass", "v1"),),
    )
    identity_hash = _hash("identity.pass")
    view = candidate_view_from_official_evidence(
        candidate_id="cand.pass",
        candidate_version="v1",
        family_manifest=manifest,
        train=_official("TRAIN", identity_hash=identity_hash),
        validation=_official("VALIDATION", identity_hash=identity_hash),
        proposer_id="desk.fundamental",
        p_value=0.01,
    )
    reader = InMemoryOfficialEvidenceReader(manifest=manifest, views=(view,), sealed_passed=True)

    result = evaluate_statistical_promotion(
        reader=reader,
        request=PromotionEvaluationRequest(
            candidate_id="cand.pass",
            candidate_version="v1",
            family_id="family.integration",
            manifest_version="v1",
            idempotency_key="integration.pass",
            proposer_id="desk.fundamental",
            approver_id="integration.operator",
            decision_time=DECISION_TIME,
        ),
    )

    assert result.decision.to_state is PromotionState.CHALLENGER
    assert result.decision.verdict == "PROMOTE_CHALLENGER"
    rendered = json.dumps(result.decision.to_dict(), sort_keys=True).lower()
    assert "sealed_metric" not in rendered
    assert "raw_sealed" not in rendered


def test_statistical_promotion_bridge_rejects_gross_positive_net_negative() -> None:
    manifest = build_family_manifest(
        family_id="family.integration",
        manifest_version="v1",
        frozen_at=DECISION_TIME,
        candidates=(("cand.costfail", "v1"),),
    )
    identity_hash = _hash("identity.costfail")
    view = candidate_view_from_official_evidence(
        candidate_id="cand.costfail",
        candidate_version="v1",
        family_manifest=manifest,
        train=_official("TRAIN", identity_hash=identity_hash),
        validation=_official("VALIDATION", identity_hash=identity_hash, net_return=-0.01),
        proposer_id="desk.fundamental",
        p_value=0.01,
    )
    reader = InMemoryOfficialEvidenceReader(manifest=manifest, views=(view,), sealed_passed=True)

    result = evaluate_statistical_promotion(
        reader=reader,
        request=PromotionEvaluationRequest(
            candidate_id="cand.costfail",
            candidate_version="v1",
            family_id="family.integration",
            manifest_version="v1",
            idempotency_key="integration.costfail",
            proposer_id="desk.fundamental",
            approver_id="integration.operator",
            decision_time=DECISION_TIME,
        ),
    )

    assert result.decision.to_state is PromotionState.REJECTED
    assert result.decision.failure_code is StatisticalPromotionFailureCode.COST_FAILED


def test_bounded_llm_desks_run_through_factordag_and_redacted_evidence_only() -> None:
    field_roles = {
        name: FieldRole.FEATURE
        for name in ("book_value", "market_cap", "close", "volume", "activity_proxy", "macro_rate")
    }
    question = ResearchQuestionView(
        question_id="q.integration",
        text="Run integrated bounded desks without LLM keys.",
        universe=("AAA", "BBB"),
        horizon="5d",
        decision_time=DECISION_TIME,
        availability_assumptions=("available_at <= decision_time",),
    )

    result = run_bounded_llm_desks_offline(
        question=question,
        field_roles=field_roles,
        pit_snapshot_hash=_hash("pit.snapshot"),
    )

    assert len(result.results) == 6
    assert all(item.status is LLMDeskStatus.DRAFT for item in result.results)
    assert all(tuple(record.stage for record in item.stage_trace) == FIVE_STAGE_SEQUENCE for item in result.results)
    rendered = json.dumps(result.to_dict(), sort_keys=True).lower()
    assert "sealed_metric" not in rendered
    assert "target_weights" not in rendered
    assert "risk_approval" not in rendered


def _research_memory_dag(root: Path) -> EvidenceGroundedResearchDAG:
    document_store = FrozenDocumentStore(root / "frozen_docs")
    memory_store = ResearchMemoryStore(root / "memory")
    gateway = FrozenWebFixtureGateway(
        {
            "https://issuer.test/report": {
                "body": "Book value and market capitalization support positive valuation review. Reading evidence only.",
            }
        }
    )
    retriever = Retriever({SourceKind.FROZEN_WEB_FIXTURE: gateway}, document_store)
    return EvidenceGroundedResearchDAG(
        retriever=retriever,
        synthesizer=HypothesisSynthesizer(),
        citation_critic=CitationCritic(document_store),
        memory_store=memory_store,
    )


def test_source_backed_draft_routes_through_proposal_factordag_pit_and_evidence_without_downstream_authority(tmp_path: Path) -> None:
    brief = ResearchBrief(
        question="Can documents suggest a valuation factor?",
        claim="Book value and market capitalization can suggest a positive valuation DRAFT.",
        economic_mechanism="Documents suggest book value and market capitalization channels; deterministic Evidence Service must test them.",
        expected_sign="positive",
        factor_name="draft_book_to_market",
        factor_expression="rank_cross_section(divide_safe(field('book_value'), field('market_cap')))",
        feature_names=("book_value", "market_cap"),
        required_terms=("book", "market", "positive"),
        falsification_tests=("Reject if deterministic validation net ICIR is not positive.",),
        decision_time="2024-02-05T10:00:00Z",
    )
    request = SourceRequest(
        source_kind=SourceKind.FROZEN_WEB_FIXTURE,
        locator="https://issuer.test/report",
        retrieved_at="2024-02-02T09:00:00Z",
        available_at="2024-02-02T09:30:00Z",
        metadata={
            "title": "Issuer report",
            "institution": "Fixture Research",
            "published_at": "2024-02-01T08:00:00Z",
        },
    )
    dag_result = _research_memory_dag(tmp_path).run(brief, (request,))
    assert dag_result.status == "DRAFT_READY"
    assert dag_result.hypothesis.state is HypothesisState.DRAFT

    payload = source_backed_hypothesis_to_proposal_payload(dag_result.hypothesis)
    assert set(payload) == {"hypothesis", "factor_task"}
    rendered_payload = json.dumps(payload, sort_keys=True).lower()
    for forbidden in ("official_metrics", "promotion", "target_weights", "orders", "risk_approval"):
        assert forbidden not in rendered_payload

    context = ProposalContext(
        direction_hash=_hash("source.direction"),
        universe=("AAA", "BBB"),
        horizon="5d",
        decision_time=datetime(2024, 2, 5, 10, 0, tzinfo=timezone.utc),
        availability_assumptions=("document available_at <= decision_time",),
        field_roles={"book_value": FieldRole.FEATURE, "market_cap": FieldRole.FEATURE},
    )
    bridge = adapt_source_backed_draft(dag_result.hypothesis, context=context)

    registry = DatasetRegistry()
    fixture = build_synthetic_market_fixture(seed=7, session_count=45)
    card = fixture.register(registry)
    decision_time = fixture.calendar[40]
    snapshot = registry.as_of_snapshot(
        fixture.dataset_id,
        decision_time=decision_time,
        fields=bridge.proposal.factor_dag.required_fields,
        start_time=fixture.calendar[20],
    )
    factor_result = execute_factor_dag(bridge.proposal.factor_dag, snapshot, seed=7)
    labels = registry.labels(fixture.dataset_id, label_field=LABEL_FIELD, evaluation_time=fixture.calendar[-1])
    label_by_key = {(row.event_time, row.symbol): row.value for row in labels}
    evidence_rows = tuple(
        EvaluationRow(
            event_time=value.event_time,
            symbol=value.symbol,
            score=value.value,
            label=label_by_key[(value.event_time, value.symbol)],
            market_regime="fixture",
            volume=1_000_000.0,
        )
        for value in factor_result.values
        if value.value is not None and label_by_key.get((value.event_time, value.symbol)) is not None
    )
    cost_model = CostModel(spread_bps=1.0, linear_impact_bps=1.0)
    identity = ExperimentIdentity(
        experiment_id="doc.integration.evidence",
        dataset_hash=card.snapshot_hash,
        universe_hash=_hash("doc.universe"),
        factor_hash=bridge.proposal.factor_dag.artifact_hash,
        split_hash=_hash("doc.validation.split"),
        label_hash=_hash(LABEL_FIELD),
        cost_hash=cost_model.artifact_hash,
        code_hash=_hash("integration.test.code"),
        availability_hash=snapshot.artifact_hash,
        seed=7,
    )
    evidence = EvidenceService().evaluate(
        identity=identity,
        rows=evidence_rows,
        partition="VALIDATION",
        cost_model=cost_model,
    )

    assert bridge.status == "DRAFT_ONLY"
    assert bridge.direct_downstream_authority is False
    assert bridge.citation_hashes
    assert bridge.proposal.state is ResearchState.DRAFT
    assert bridge.proposal.factor_dag.required_fields == ("book_value", "market_cap")
    assert len(evidence_rows) > 0
    assert evidence.partition == "VALIDATION"
