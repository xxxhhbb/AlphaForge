"""Adapters from frozen document-like payloads into DRAFT-only citations."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping

from veriquant.research_tool_contracts import (
    CitationBundle,
    ResearchToolCapability,
    ResearchToolContractError,
    ResearchToolFailureCode,
    ResearchToolRequest,
    ResearchToolResult,
    ResearchToolVisibility,
    content_hash,
)

from .models import (
    CitationStatus,
    CitationValidationFinding,
    CitationValidationReport,
    FrozenCitation,
    FrozenSource,
    FrozenSourceProvider,
    ProxyDraft,
    SourceClass,
    _assert_no_authority_text,
    _assert_no_forbidden_keys,
    _assert_no_secret_or_location_text,
    _canonical_tree,
    _fail,
    _is_retrieval_safe,
    _parse_dt,
    _require_safe_segment,
    _require_text,
    build_frozen_source,
)


_DROP_URL_KEYS = {"url", "uri", "document_url", "source_url"}
_REJECT_LOCATION_KEYS = {"path", "filepath", "file_path", "local_locator", "provider_url"}
_REJECT_SECRET_KEYS = {
    "authorization",
    "auth",
    "api_key",
    "apikey",
    "access_token",
    "refresh_token",
    "token",
    "secret",
    "cookie",
    "headers",
}


def _strip_nonvisible_location_keys(value: Any, *, path: str = "payload") -> Any:
    if isinstance(value, Mapping):
        output: dict[str, Any] = {}
        for key, item in value.items():
            lowered = str(key).strip().lower()
            if lowered in _REJECT_SECRET_KEYS:
                _fail(ResearchToolFailureCode.SECRET_LEAKAGE, "secret-like upstream key is forbidden", path=f"{path}.{key}")
            if lowered in _REJECT_LOCATION_KEYS:
                _fail(ResearchToolFailureCode.PATH_OR_URL_LEAKAGE, "file path or provider URL key is forbidden", path=f"{path}.{key}")
            if lowered in _DROP_URL_KEYS:
                continue
            output[str(key)] = _strip_nonvisible_location_keys(item, path=f"{path}.{key}")
        return output
    if isinstance(value, (list, tuple)):
        return tuple(_strip_nonvisible_location_keys(item, path=f"{path}[{idx}]") for idx, item in enumerate(value))
    return value


def _payload_text(payload: Mapping[str, Any], *keys: str) -> str:
    parts: list[str] = []
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            parts.append(value.strip())
    if not parts:
        _fail(ResearchToolFailureCode.MISSING_REQUIRED_FIELD, "payload does not contain text")
    return "\n\n".join(parts)


def _payload_time(payload: Mapping[str, Any], key: str, fallback: datetime | str) -> datetime:
    value = payload.get(key, fallback)
    return _parse_dt(value, key)


def _validate_raw_payload(payload: Mapping[str, Any], *, text_fields: tuple[str, ...]) -> Mapping[str, Any]:
    if not isinstance(payload, Mapping):
        _fail(ResearchToolFailureCode.INVALID_PAYLOAD, "source payload must be a mapping")
    sanitized = _strip_nonvisible_location_keys(payload)
    _assert_no_forbidden_keys(sanitized)
    for key in text_fields:
        value = sanitized.get(key)
        if isinstance(value, str):
            _assert_no_secret_or_location_text(value, field_name=key)
            _assert_no_authority_text(value, field_name=key)
    return sanitized


@dataclass(frozen=True, kw_only=True)
class DocumentResearchAdapter:
    """Freeze caller-supplied document text; does not read file paths."""

    provider: str = "frozen_document_fixture"

    def freeze_document(
        self,
        *,
        source_id: str,
        title: str,
        body: str,
        event_time: datetime | str,
        published_at: datetime | str,
        available_at: datetime | str,
        retrieved_at: datetime | str,
        author: str | None = None,
        institution: str | None = None,
    ) -> FrozenSource:
        _require_safe_segment(source_id, "source_id")
        _require_text(title, "title")
        _require_text(body, "body")
        _assert_no_secret_or_location_text(title, field_name="title")
        _assert_no_secret_or_location_text(body, field_name="document body")
        _assert_no_authority_text(body, field_name="document body")
        return build_frozen_source(
            source_id=source_id,
            source_class=SourceClass.DOCUMENT,
            title=title,
            body=body,
            event_time=event_time,
            published_at=published_at,
            available_at=available_at,
            retrieved_at=retrieved_at,
            provider=self.provider,
            author=author,
            institution=institution,
        )


@dataclass(frozen=True, kw_only=True)
class NewsResearchAdapter:
    """Freeze already-collected news payloads; never changes holdings."""

    provider: str = "frozen_news_fixture"

    def freeze_news(
        self,
        payload: Mapping[str, Any],
        *,
        source_id: str,
        retrieved_at: datetime | str,
    ) -> FrozenSource:
        safe_payload = _validate_raw_payload(payload, text_fields=("title", "summary", "snippet", "body"))
        title = str(safe_payload.get("title") or "Frozen news item")
        body = _payload_text(safe_payload, "title", "summary", "snippet", "body")
        published_at = _payload_time(safe_payload, "published_at", safe_payload.get("published") or retrieved_at)
        return build_frozen_source(
            source_id=source_id,
            source_class=SourceClass.NEWS,
            title=title,
            body=body,
            event_time=_payload_time(safe_payload, "event_time", published_at),
            published_at=published_at,
            available_at=_payload_time(safe_payload, "available_at", published_at),
            retrieved_at=retrieved_at,
            provider=self.provider,
            institution=str(safe_payload.get("source") or self.provider),
        )


@dataclass(frozen=True, kw_only=True)
class ResearchReportAdapter:
    """Freeze broker/research-report summaries as DRAFT-only support."""

    provider: str = "frozen_research_report_fixture"

    def freeze_report(
        self,
        payload: Mapping[str, Any],
        *,
        source_id: str,
        retrieved_at: datetime | str,
    ) -> FrozenSource:
        safe_payload = _validate_raw_payload(payload, text_fields=("title", "summary", "rating", "body"))
        title = str(safe_payload.get("title") or "Frozen research report")
        body = _payload_text(safe_payload, "title", "summary", "rating", "body")
        published_at = _payload_time(safe_payload, "published_at", safe_payload.get("publish_date") or retrieved_at)
        return build_frozen_source(
            source_id=source_id,
            source_class=SourceClass.RESEARCH_REPORT,
            title=title,
            body=body,
            event_time=_payload_time(safe_payload, "event_time", published_at),
            published_at=published_at,
            available_at=_payload_time(safe_payload, "available_at", published_at),
            retrieved_at=retrieved_at,
            provider=self.provider,
            author=str(safe_payload.get("analyst") or "") or None,
            institution=str(safe_payload.get("brokerage") or safe_payload.get("institution") or self.provider),
        )


@dataclass(frozen=True, kw_only=True)
class FilingResearchAdapter:
    """Freeze filing index/body summaries without exposing filing URLs."""

    provider: str = "frozen_filing_fixture"

    def freeze_filing(
        self,
        payload: Mapping[str, Any],
        *,
        source_id: str,
        retrieved_at: datetime | str,
    ) -> FrozenSource:
        safe_payload = _validate_raw_payload(payload, text_fields=("form", "title", "summary", "body"))
        form = str(safe_payload.get("form") or "filing")
        title = str(safe_payload.get("title") or f"Frozen {form} filing")
        body = _payload_text(safe_payload, "form", "title", "summary", "body")
        filing_date = _payload_time(safe_payload, "filing_date", retrieved_at)
        return build_frozen_source(
            source_id=source_id,
            source_class=SourceClass.FILING,
            title=title,
            body=body,
            event_time=_payload_time(safe_payload, "report_date", filing_date),
            published_at=filing_date,
            available_at=_payload_time(safe_payload, "available_at", filing_date),
            retrieved_at=retrieved_at,
            provider=self.provider,
            institution=str(safe_payload.get("issuer") or self.provider),
        )


@dataclass(frozen=True, kw_only=True)
class CitationQueryTool:
    """Desk-visible retrieval over frozen sources with fixed permissions."""

    provider: FrozenSourceProvider
    max_citations: int = 3
    allowed_tools: tuple[str, ...] = ("citation_query", "citation_validate", "claim_to_proxy_draft")

    def query(
        self,
        request: ResearchToolRequest,
        *,
        terms: tuple[str, ...],
        source_ids: tuple[str, ...] = (),
    ) -> ResearchToolResult:
        if request.tool_name != "citation_query":
            _fail(ResearchToolFailureCode.UNAUTHORIZED_TOOL, "request is not for citation_query")
        if not request.offline:
            _fail(ResearchToolFailureCode.OFFLINE_NETWORK_DISABLED, "citation query requires offline frozen fixtures")
        if not terms:
            _fail(ResearchToolFailureCode.MISSING_REQUIRED_FIELD, "terms are required")
        normalized_terms = tuple(term.casefold() for term in terms if isinstance(term, str) and term.strip())
        if not normalized_terms:
            _fail(ResearchToolFailureCode.MISSING_REQUIRED_FIELD, "terms are required")
        sources = tuple(self.provider.get_source(source_id) for source_id in source_ids) if source_ids else self.provider.list_sources()

        citations: list[FrozenCitation] = []
        skipped_prompt_injection = 0
        skipped_forbidden = 0
        for source in sources:
            if source.available_at > request.decision_time:
                _fail(
                    ResearchToolFailureCode.DATA_NOT_AVAILABLE_AS_OF,
                    "frozen source is not available at decision_time",
                    source_id=source.source_id,
                )
            for chunk in source.chunks:
                lowered = chunk.text.casefold()
                if not any(term in lowered for term in normalized_terms):
                    continue
                if not _is_retrieval_safe(chunk.text):
                    if "ignore" in lowered or "instruction" in lowered:
                        skipped_prompt_injection += 1
                    else:
                        skipped_forbidden += 1
                    continue
                quote = chunk.text[: min(len(chunk.text), 180)]
                claim = f"Frozen {source.source_class.value} supports terms: {', '.join(terms)}"
                bundle = CitationBundle(
                    source_hash=source.source_hash,
                    chunk_hash=chunk.chunk_hash,
                    span_start=0,
                    span_end=len(quote),
                    available_at=source.available_at,
                    claim=claim,
                )
                citations.append(
                    FrozenCitation(
                        source_id=source.source_id,
                        source_class=source.source_class,
                        source_hash=source.source_hash,
                        chunk_hash=chunk.chunk_hash,
                        span_start=0,
                        span_end=len(quote),
                        quote=quote,
                        claim=claim,
                        event_time=source.event_time,
                        published_at=source.published_at,
                        available_at=source.available_at,
                        bundle_hash=bundle.artifact_hash,
                    )
                )
                if len(citations) >= self.max_citations:
                    break
            if len(citations) >= self.max_citations:
                break

        payload = {
            "citations": [citation.to_visible_payload() for citation in citations],
            "citation_count": len(citations),
            "skipped_prompt_injection": skipped_prompt_injection,
            "skipped_forbidden": skipped_forbidden,
            "tools_granted": (),
            "permission_delta": "NONE",
        }
        return ResearchToolResult(
            request=request,
            result_id="citation_query_" + content_hash(payload).removeprefix("sha256:")[:16],
            visibility=ResearchToolVisibility.DRAFT_SUPPORT,
            capability=ResearchToolCapability.READ_FROZEN_DOCUMENT,
            payload=payload,
            artifacts=tuple(source.to_artifact() for source in sources),
        )


@dataclass(frozen=True, kw_only=True)
class CitationValidationTool:
    """Validate frozen citations against source hash, chunk hash, span, and time."""

    provider: FrozenSourceProvider

    def validate(self, *, citation: FrozenCitation, decision_time: datetime | str) -> CitationValidationReport:
        decision_dt = _parse_dt(decision_time, "decision_time")
        findings: list[CitationValidationFinding] = []
        source = self.provider.get_source(citation.source_id)
        if source.source_hash != citation.source_hash:
            findings.append(
                CitationValidationFinding(
                    status=CitationStatus.REJECT,
                    code=ResearchToolFailureCode.INVALID_HASH,
                    message="source hash mismatch",
                )
            )
        if source.available_at > decision_dt or citation.available_at > decision_dt:
            findings.append(
                CitationValidationFinding(
                    status=CitationStatus.REJECT,
                    code=ResearchToolFailureCode.DATA_NOT_AVAILABLE_AS_OF,
                    message="source was not available at decision_time",
                )
            )
        chunk = next((item for item in source.chunks if item.chunk_hash == citation.chunk_hash), None)
        if chunk is None:
            findings.append(
                CitationValidationFinding(
                    status=CitationStatus.REJECT,
                    code=ResearchToolFailureCode.INVALID_HASH,
                    message="chunk hash mismatch",
                )
            )
        else:
            if citation.span_start < 0 or citation.span_end > len(chunk.text) or citation.span_start >= citation.span_end:
                findings.append(
                    CitationValidationFinding(
                        status=CitationStatus.REJECT,
                        code=ResearchToolFailureCode.INVALID_PAYLOAD,
                        message="citation span is outside frozen chunk",
                    )
                )
            elif chunk.text[citation.span_start : citation.span_end] != citation.quote:
                findings.append(
                    CitationValidationFinding(
                        status=CitationStatus.REJECT,
                        code=ResearchToolFailureCode.INVALID_PAYLOAD,
                        message="citation quote does not match frozen span",
                    )
                )
            if not _is_retrieval_safe(chunk.text) or not _is_retrieval_safe(citation.quote):
                findings.append(
                    CitationValidationFinding(
                        status=CitationStatus.REJECT,
                        code=ResearchToolFailureCode.FORBIDDEN_AUTHORITY_FIELD,
                        message="citation contains prompt-injection, sealed, secret, path, or privileged text",
                    )
                )
        expected_bundle_hash = citation.bundle.artifact_hash
        if citation.bundle_hash != expected_bundle_hash:
            findings.append(
                CitationValidationFinding(
                    status=CitationStatus.REJECT,
                    code=ResearchToolFailureCode.INVALID_HASH,
                    message="citation bundle hash mismatch",
                )
            )
        if not findings:
            findings.append(
                CitationValidationFinding(
                    status=CitationStatus.PASS,
                    code=None,
                    message="citation verified",
                )
            )
        status = CitationStatus.PASS if all(item.code is None for item in findings) else CitationStatus.REJECT
        report = CitationValidationReport(status=status, citation=citation, findings=tuple(findings))
        if status is CitationStatus.REJECT:
            first = next(item for item in findings if item.code is not None)
            raise ResearchToolContractError(first.code, first.message, details={"report_hash": report.artifact_hash})
        return report

    def validate_result(
        self,
        request: ResearchToolRequest,
        *,
        citation: FrozenCitation,
    ) -> ResearchToolResult:
        if request.tool_name != "citation_validate":
            _fail(ResearchToolFailureCode.UNAUTHORIZED_TOOL, "request is not for citation_validate")
        report = self.validate(citation=citation, decision_time=request.decision_time)
        payload = {
            "status": report.status.value,
            "report_hash": report.artifact_hash,
            "bundle_hash": citation.bundle_hash,
            "findings": tuple(
                {
                    "status": finding.status.value,
                    "code": finding.code.value if finding.code else None,
                    "message": finding.message,
                }
                for finding in report.findings
            ),
        }
        return ResearchToolResult(
            request=request,
            result_id="citation_validation_" + report.artifact_hash.removeprefix("sha256:")[:16],
            visibility=ResearchToolVisibility.DRAFT_SUPPORT,
            capability=ResearchToolCapability.READ_FROZEN_DOCUMENT,
            payload=payload,
        )


@dataclass(frozen=True, kw_only=True)
class ClaimToProxyDraftMapper:
    """Map citation-supported claims to inert DRAFT proxy fields."""

    validator: CitationValidationTool

    def map_claim(
        self,
        *,
        claim: str,
        citations: tuple[FrozenCitation, ...],
        decision_time: datetime | str,
        proxy_id: str,
        required_fields: tuple[str, ...] = ("citation_support_flag",),
    ) -> ProxyDraft:
        _require_text(claim, "claim")
        _assert_no_secret_or_location_text(claim, field_name="claim")
        _assert_no_authority_text(claim, field_name="claim")
        if not citations:
            _fail(ResearchToolFailureCode.MISSING_REQUIRED_FIELD, "citations are required")
        for citation in citations:
            self.validator.validate(citation=citation, decision_time=decision_time)
        bundle_hashes = tuple(sorted(citation.bundle_hash for citation in citations))
        proxy_expression = "draft_proxy_from_frozen_citation_count(citation_support_flag)"
        return ProxyDraft(
            proxy_id=proxy_id,
            state="DRAFT",
            claim=claim,
            proxy_expression=proxy_expression,
            required_fields=required_fields,
            citation_bundle_hashes=bundle_hashes,
            rationale="Frozen citations can support a DRAFT proxy hypothesis only; deterministic services must validate any factor later.",
            limitations=(
                "Document and event sources are not official Evidence.",
                "No portfolio, risk, order, promotion, or ledger authority is granted.",
            ),
        )

    def map_result(
        self,
        request: ResearchToolRequest,
        *,
        claim: str,
        citations: tuple[FrozenCitation, ...],
        proxy_id: str,
    ) -> ResearchToolResult:
        if request.tool_name != "claim_to_proxy_draft":
            _fail(ResearchToolFailureCode.UNAUTHORIZED_TOOL, "request is not for claim_to_proxy_draft")
        draft = self.map_claim(
            claim=claim,
            citations=citations,
            decision_time=request.decision_time,
            proxy_id=proxy_id,
        )
        payload = {
            "proxy_id": draft.proxy_id,
            "state": draft.state,
            "proxy_expression": draft.proxy_expression,
            "required_fields": draft.required_fields,
            "citation_bundle_hashes": draft.citation_bundle_hashes,
            "draft_hash": draft.artifact_hash,
        }
        return ResearchToolResult(
            request=request,
            result_id="proxy_draft_" + draft.artifact_hash.removeprefix("sha256:")[:16],
            visibility=ResearchToolVisibility.DRAFT_SUPPORT,
            capability=ResearchToolCapability.DESIGN_EXPERIMENT,
            payload=payload,
        )


def citation_from_payload(payload: Mapping[str, Any]) -> FrozenCitation:
    """Rebuild a citation from a desk-visible query payload."""

    return FrozenCitation(
        source_id=str(payload["source_id"]),
        source_class=SourceClass(str(payload["source_class"])),
        source_hash=str(payload["source_hash"]),
        chunk_hash=str(payload["chunk_hash"]),
        span_start=int(payload["span_start"]),
        span_end=int(payload["span_end"]),
        quote=str(payload["quote"]),
        claim=str(payload["claim"]),
        event_time=_parse_dt(payload["event_time"], "event_time"),
        published_at=_parse_dt(payload["published_at"], "published_at"),
        available_at=_parse_dt(payload["available_at"], "available_at"),
        bundle_hash=str(payload["bundle_hash"]),
    )
