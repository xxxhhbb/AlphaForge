from __future__ import annotations

from pathlib import Path

import pytest

from src.veriquant.research_memory.api import (
    CitationCritic,
    EvidenceGroundedResearchDAG,
    FrozenDocumentStore,
    FrozenWebFixtureGateway,
    HypothesisState,
    HypothesisSynthesizer,
    ResearchBrief,
    ResearchMemoryError,
    ResearchMemoryStore,
    Retriever,
    SourceKind,
    SourceRequest,
)
from src.veriquant.research_memory.models import MemoryRecord


PUBLISHED = "2024-02-01T08:00:00Z"
RETRIEVED = "2024-02-02T09:00:00Z"
AVAILABLE = "2024-02-02T09:30:00Z"
DECISION = "2024-02-05T10:00:00Z"
BODY = "Margin expansion and inventory drawdown support positive demand revisions. This is reading material only."


def _brief() -> ResearchBrief:
    return ResearchBrief(
        question="Can reports suggest a demand-revision factor?",
        claim="Margin expansion and inventory drawdown can suggest a positive demand-revision DRAFT.",
        economic_mechanism="Documents suggest margin and inventory channels, but official Evidence Service must test them.",
        expected_sign="positive",
        factor_name="draft_margin_inventory",
        factor_expression="rank_cross_section(add(field('margin'), field('inventory_drawdown')))",
        feature_names=("margin", "inventory_drawdown"),
        required_terms=("margin", "inventory", "positive"),
        falsification_tests=("Reject if deterministic validation net ICIR is not positive.",),
        decision_time=DECISION,
    )


def _request() -> SourceRequest:
    return SourceRequest(
        source_kind=SourceKind.FROZEN_WEB_FIXTURE,
        locator="https://issuer.test/report",
        retrieved_at=RETRIEVED,
        available_at=AVAILABLE,
        metadata={
            "title": "Issuer report",
            "institution": "Fixture Research",
            "published_at": PUBLISHED,
            "language": "en",
            "license_note": "offline fixture",
        },
    )


def _dag(root: Path) -> EvidenceGroundedResearchDAG:
    document_store = FrozenDocumentStore(root / "frozen_docs")
    memory_store = ResearchMemoryStore(root / "memory")
    gateway = FrozenWebFixtureGateway({"https://issuer.test/report": {"body": BODY}})
    retriever = Retriever({SourceKind.FROZEN_WEB_FIXTURE: gateway}, document_store)
    return EvidenceGroundedResearchDAG(
        retriever=retriever,
        synthesizer=HypothesisSynthesizer(),
        citation_critic=CitationCritic(document_store),
        memory_store=memory_store,
    )


def test_dag_runs_without_llm_and_outputs_draft_only(tmp_path: Path) -> None:
    result = _dag(tmp_path).run(_brief(), (_request(),))

    assert result.status == "DRAFT_READY"
    assert result.hypothesis.state is HypothesisState.DRAFT
    assert result.memory_record_id is not None
    payload = result.hypothesis.canonical_json().lower()
    for forbidden in ("official_metrics", "promotion", "target_weights", "orders", "risk_approval"):
        assert f'"{forbidden}":' not in payload
    assert result.citation_report.status == "PASS"


def test_same_input_reproduces_hashes(tmp_path: Path) -> None:
    left = _dag(tmp_path / "left").run(_brief(), (_request(),))
    right = _dag(tmp_path / "right").run(_brief(), (_request(),))

    assert left.documents[0].content_hash == right.documents[0].content_hash
    assert left.documents[0].chunks[0].chunk_hash == right.documents[0].chunks[0].chunk_hash
    assert left.hypothesis.hypothesis_hash == right.hypothesis.hypothesis_hash
    assert left.memory_record_id == right.memory_record_id


def test_memory_retrieval_is_filtered_by_decision_time(tmp_path: Path) -> None:
    result = _dag(tmp_path).run(_brief(), (_request(),))
    store = ResearchMemoryStore(tmp_path / "memory")

    assert result.memory_record_id is not None
    assert store.query_as_of("2024-02-01T00:00:00Z") == ()
    visible = store.query_as_of(DECISION)
    assert len(visible) == 1
    assert visible[0].record_id == result.memory_record_id


def test_memory_rejects_sealed_metrics_secret_and_unfrozen_content(tmp_path: Path) -> None:
    result = _dag(tmp_path).run(_brief(), (_request(),))
    store = ResearchMemoryStore(tmp_path / "memory")
    good_record = store.all_records()[0]

    bad_summary = MemoryRecord(
        record_id="bad_sealed",
        redacted_hypothesis=good_record.redacted_hypothesis,
        citations=good_record.citations,
        factor_draft_hash=good_record.factor_draft_hash,
        train_validation_evidence_summary={"sealed_metrics": {"rank": 1}},
        public_failure_reason=None,
        lineage=good_record.lineage,
        created_at=good_record.created_at,
        decision_time=good_record.decision_time,
        available_at=good_record.available_at,
    )
    with pytest.raises(ResearchMemoryError) as sealed:
        store.append(bad_summary)
    assert sealed.value.code == "FORBIDDEN_MEMORY_PAYLOAD"

    bad_secret = MemoryRecord(
        record_id="bad_secret",
        redacted_hypothesis="token=abc123 should not be stored",
        citations=good_record.citations,
        factor_draft_hash=good_record.factor_draft_hash,
        train_validation_evidence_summary={},
        public_failure_reason=None,
        lineage=good_record.lineage,
        created_at=good_record.created_at,
        decision_time=good_record.decision_time,
        available_at=good_record.available_at,
    )
    with pytest.raises(ResearchMemoryError) as secret:
        store.append(bad_secret)
    assert secret.value.code == "FORBIDDEN_MEMORY_PAYLOAD"

    bad_raw = MemoryRecord(
        record_id="bad_raw",
        redacted_hypothesis=good_record.redacted_hypothesis,
        citations=good_record.citations,
        factor_draft_hash=good_record.factor_draft_hash,
        train_validation_evidence_summary={"raw_content": BODY},
        public_failure_reason=None,
        lineage=good_record.lineage,
        created_at=good_record.created_at,
        decision_time=good_record.decision_time,
        available_at=good_record.available_at,
    )
    with pytest.raises(ResearchMemoryError) as raw:
        store.append(bad_raw)
    assert raw.value.code == "FORBIDDEN_MEMORY_PAYLOAD"
