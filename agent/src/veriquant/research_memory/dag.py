from __future__ import annotations

from dataclasses import dataclass

from .citation import CitationCritic
from .gateways import SourceGateway
from .models import (
    Citation,
    CitationCriticReport,
    EvidenceGroundedHypothesis,
    FactorDraftMapping,
    FrozenDocument,
    SourceKind,
    SourceRequest,
)
from .security import redact_sensitive
from .store import FrozenDocumentStore
from .memory import ResearchMemoryStore


@dataclass(frozen=True)
class ResearchBrief:
    question: str
    claim: str
    economic_mechanism: str
    expected_sign: str
    factor_name: str
    factor_expression: str
    feature_names: tuple[str, ...]
    required_terms: tuple[str, ...]
    falsification_tests: tuple[str, ...]
    decision_time: str
    requires_time_claim: bool = True


@dataclass(frozen=True)
class ResearchDAGResult:
    status: str
    documents: tuple[FrozenDocument, ...]
    hypothesis: EvidenceGroundedHypothesis
    citation_report: CitationCriticReport
    memory_record_id: str | None


class Retriever:
    def __init__(self, gateways: dict[SourceKind, SourceGateway], document_store: FrozenDocumentStore) -> None:
        self.gateways = gateways
        self.document_store = document_store

    def retrieve(self, requests: tuple[SourceRequest, ...]) -> tuple[FrozenDocument, ...]:
        documents: list[FrozenDocument] = []
        for request in requests:
            gateway = self.gateways[request.source_kind]
            documents.append(self.document_store.freeze(gateway.fetch(request)))
        return tuple(documents)


class HypothesisSynthesizer:
    def synthesize(self, brief: ResearchBrief, documents: tuple[FrozenDocument, ...]) -> EvidenceGroundedHypothesis:
        if not documents:
            raise ValueError("at least one frozen document is required")
        first_doc = documents[0]
        if not first_doc.chunks:
            raise ValueError("frozen document has no chunks")
        chunk = first_doc.chunks[0]
        quote = _first_quote(chunk.text)
        start = chunk.text.find(quote)
        mapping = FactorDraftMapping(
            factor_name=brief.factor_name,
            draft_expression=brief.factor_expression,
            feature_names=brief.feature_names,
            rationale=brief.economic_mechanism,
        )
        citation = Citation(
            document_id=first_doc.document_id,
            chunk_hash=chunk.chunk_hash,
            page=chunk.page,
            paragraph=chunk.paragraph,
            span_start=start,
            span_end=start + len(quote),
            quote=quote,
            claim=brief.claim,
            required_terms=brief.required_terms,
            requires_time_claim=brief.requires_time_claim,
        )
        return EvidenceGroundedHypothesis(
            hypothesis=redact_sensitive(brief.claim),
            economic_mechanism=redact_sensitive(brief.economic_mechanism),
            expected_sign=brief.expected_sign,
            falsification_tests=tuple(redact_sensitive(item) for item in brief.falsification_tests),
            factor_mapping=(mapping,),
            citations=(citation,),
            uncertainty="Document-grounded DRAFT only; quantitative evidence is still required.",
            limitations=(
                "Document/news content cannot replace official Evidence Service metrics.",
                "This DRAFT cannot directly affect downstream financial state or authority.",
            ),
        )


class EvidenceGroundedResearchDAG:
    def __init__(
        self,
        *,
        retriever: Retriever,
        synthesizer: HypothesisSynthesizer,
        citation_critic: CitationCritic,
        memory_store: ResearchMemoryStore | None = None,
    ) -> None:
        self.retriever = retriever
        self.synthesizer = synthesizer
        self.citation_critic = citation_critic
        self.memory_store = memory_store

    def run(self, brief: ResearchBrief, requests: tuple[SourceRequest, ...]) -> ResearchDAGResult:
        documents = self.retriever.retrieve(requests)
        hypothesis = self.synthesizer.synthesize(brief, documents)
        report = self.citation_critic.evaluate(hypothesis, decision_time=brief.decision_time)
        memory_record_id = None
        status = "DRAFT_READY" if report.status == "PASS" else "REJECTED"
        if self.memory_store is not None:
            record = self.memory_store.build_record(
                hypothesis,
                factor_draft_hash=hypothesis.factor_mapping[0].factor_draft_hash,
                train_validation_evidence_summary={},
                public_failure_reason=None if report.status == "PASS" else "citation critic rejected DRAFT",
                lineage=("retriever", "hypothesis_synthesizer", "citation_critic"),
                created_at=requests[0].retrieved_at,
                decision_time=brief.decision_time,
                available_at=max(doc.available_at for doc in documents),
            )
            self.memory_store.append(record)
            memory_record_id = record.record_id
        return ResearchDAGResult(
            status=status,
            documents=documents,
            hypothesis=hypothesis,
            citation_report=report,
            memory_record_id=memory_record_id,
        )


def _first_quote(text: str) -> str:
    stripped = text.strip()
    dot = stripped.find(".")
    first = stripped[: dot + 1] if dot >= 0 else stripped
    return first[:240]

