from __future__ import annotations

from .canonical import parse_datetime
from .models import (
    CitationFinding,
    CitationCriticReport,
    EvidenceGroundedHypothesis,
    FailureCode,
    PublishedAtStatus,
    ResearchMemoryError,
)
from .security import detect_prompt_injection
from .store import FrozenDocumentStore


class CitationCritic:
    def __init__(self, document_store: FrozenDocumentStore) -> None:
        self.document_store = document_store

    def evaluate(self, hypothesis: EvidenceGroundedHypothesis, *, decision_time: str) -> CitationCriticReport:
        findings: list[CitationFinding] = []
        if not hypothesis.citations:
            findings.append(
                CitationFinding(
                    citation_index=-1,
                    status="REJECT",
                    code=FailureCode.MISSING_CITATION.value,
                    message="hypothesis has no citation",
                )
            )
            return CitationCriticReport(status="REJECT", findings=tuple(findings))

        for idx, citation in enumerate(hypothesis.citations):
            try:
                findings.extend(self._evaluate_one(idx, citation, decision_time=decision_time))
            except ResearchMemoryError as exc:
                findings.append(CitationFinding(idx, "REJECT", exc.code, exc.message))

        status = "PASS" if all(finding.code is None for finding in findings) else "REJECT"
        return CitationCriticReport(status=status, findings=tuple(findings))

    def _evaluate_one(self, idx: int, citation, *, decision_time: str) -> list[CitationFinding]:
        findings: list[CitationFinding] = []
        doc = self.document_store.get(citation.document_id)
        chunk = self.document_store.locate_chunk(
            document_id=citation.document_id,
            chunk_hash=citation.chunk_hash,
            page=citation.page,
            paragraph=citation.paragraph,
        )
        decision_dt = parse_datetime(decision_time, field_name="decision_time")
        available_dt = parse_datetime(doc.available_at, field_name="available_at")
        if available_dt > decision_dt:
            findings.append(
                CitationFinding(
                    idx,
                    "REJECT",
                    FailureCode.FUTURE_DOCUMENT.value,
                    "document was not available at decision_time",
                )
            )
        if citation.requires_time_claim and doc.published_at_status is PublishedAtStatus.UNKNOWN:
            findings.append(
                CitationFinding(
                    idx,
                    "REJECT",
                    FailureCode.UNKNOWN_PUBLISHED_AT.value,
                    "unknown published_at cannot support a time-sensitive PIT claim",
                )
            )
        if citation.span_start < 0 or citation.span_end > len(chunk.text) or citation.span_start >= citation.span_end:
            findings.append(
                CitationFinding(idx, "REJECT", FailureCode.INVALID_CITATION.value, "citation span is outside chunk")
            )
        else:
            quoted = chunk.text[citation.span_start : citation.span_end]
            if quoted != citation.quote:
                findings.append(
                    CitationFinding(
                        idx,
                        "REJECT",
                        FailureCode.INVALID_CITATION.value,
                        "citation quote does not match frozen span",
                    )
                )
        injected = detect_prompt_injection(chunk.text)
        if injected:
            findings.append(
                CitationFinding(
                    idx,
                    "REJECT",
                    FailureCode.PROMPT_INJECTION.value,
                    "citation chunk contains untrusted prompt-injection instructions: " + ",".join(injected),
                )
            )
        support_text = citation.quote.casefold()
        missing_terms = [term for term in citation.required_terms if term.casefold() not in support_text]
        if missing_terms:
            findings.append(
                CitationFinding(
                    idx,
                    "REJECT",
                    FailureCode.UNSUPPORTED_CLAIM.value,
                    "citation does not contain required support terms: " + ",".join(missing_terms),
                )
            )
        if not findings:
            findings.append(CitationFinding(idx, "PASS", None, "citation verified"))
        return findings
