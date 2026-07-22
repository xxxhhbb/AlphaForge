"""Strict evidence-grounded research memory API for parallel package D.

Documents and news are untrusted reading evidence.  This module can freeze
sources, verify citations, and emit DRAFT hypotheses only.  It never produces
official metrics, promotion, calibrated alpha, portfolio weights, risk approval,
orders, fills, or ledger entries.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import math
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Mapping, Protocol
from urllib.parse import urlsplit


SCHEMA_VERSION = "veriquant.research-memory.strict/v1"
HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,254}$")


class ResearchMemoryFailureCode(str, Enum):
    INVALID_SOURCE = "INVALID_SOURCE"
    NETWORK_DISABLED = "NETWORK_DISABLED"
    NETWORK_BUDGET_EXCEEDED = "NETWORK_BUDGET_EXCEEDED"
    SOURCE_NOT_ALLOWLISTED = "SOURCE_NOT_ALLOWLISTED"
    DOCUMENT_NOT_FOUND = "DOCUMENT_NOT_FOUND"
    DOCUMENT_COLLISION = "DOCUMENT_COLLISION"
    INVALID_CITATION = "INVALID_CITATION"
    MISSING_CITATION = "MISSING_CITATION"
    FUTURE_DOCUMENT = "FUTURE_DOCUMENT"
    UNKNOWN_PUBLISHED_AT = "UNKNOWN_PUBLISHED_AT"
    UNSUPPORTED_CLAIM = "UNSUPPORTED_CLAIM"
    OVER_EXTRAPOLATED = "OVER_EXTRAPOLATED"
    PROMPT_INJECTION = "PROMPT_INJECTION"
    FORBIDDEN_MEMORY_PAYLOAD = "FORBIDDEN_MEMORY_PAYLOAD"
    SECRET_DETECTED = "SECRET_DETECTED"
    UNFROZEN_CONTENT = "UNFROZEN_CONTENT"
    DAG_VALIDATION_FAILED = "DAG_VALIDATION_FAILED"
    INVALID_VALUE = "INVALID_VALUE"


class ResearchMemoryError(ValueError):
    def __init__(self, code: ResearchMemoryFailureCode, message: str, **details: Any) -> None:
        super().__init__(message)
        self.code = code
        self.details = MappingProxyType(details)


class SourceKind(str, Enum):
    LOCAL_DOCUMENT = "LOCAL_DOCUMENT"
    FROZEN_WEB_FIXTURE = "FROZEN_WEB_FIXTURE"
    OPTIONAL_VIBE_READONLY = "OPTIONAL_VIBE_READONLY"


class PublishedAtStatus(str, Enum):
    KNOWN = "KNOWN"
    UNKNOWN = "UNKNOWN"


class ResearchState(str, Enum):
    DRAFT = "DRAFT"


class CitationStatus(str, Enum):
    PASS = "PASS"
    REJECT = "REJECT"


def _fail(code: ResearchMemoryFailureCode, message: str, **details: Any) -> None:
    raise ResearchMemoryError(code, message, **details)


def _dt(value: datetime, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        _fail(ResearchMemoryFailureCode.INVALID_VALUE, f"{field_name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _text(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        _fail(ResearchMemoryFailureCode.INVALID_VALUE, f"{field_name} must be non-empty text")


def _identifier(value: str, field_name: str) -> None:
    if not isinstance(value, str) or ID_RE.fullmatch(value) is None:
        _fail(ResearchMemoryFailureCode.INVALID_VALUE, f"{field_name} must be a stable identifier")


def _hash(value: str, field_name: str) -> None:
    if not isinstance(value, str) or HASH_RE.fullmatch(value) is None:
        _fail(ResearchMemoryFailureCode.INVALID_VALUE, f"{field_name} must be sha256:<64 hex>")


def normalize(value: Any) -> Any:
    if dataclasses.is_dataclass(value):
        return normalize(dataclasses.asdict(value))
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return _dt(value, "datetime").isoformat(timespec="microseconds").replace("+00:00", "Z")
    if isinstance(value, float):
        if not math.isfinite(value):
            _fail(ResearchMemoryFailureCode.INVALID_VALUE, "non-finite float")
        return 0.0 if value == 0.0 else value
    if isinstance(value, Mapping):
        return {str(key): normalize(value[key]) for key in sorted(value)}
    if isinstance(value, (tuple, list)):
        return [normalize(item) for item in value]
    return value


def canonical_json(value: Any) -> str:
    return json.dumps(normalize(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def artifact_hash(value: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def content_hash(text: str) -> str:
    _text(text, "content")
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class SourceRequest:
    source_kind: SourceKind
    locator: str
    retrieved_at: datetime
    available_at: datetime
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.source_kind, SourceKind):
            _fail(ResearchMemoryFailureCode.INVALID_SOURCE, "source_kind is invalid")
        _text(self.locator, "locator")
        _dt(self.retrieved_at, "retrieved_at")
        _dt(self.available_at, "available_at")
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))


@dataclass(frozen=True)
class GatewayResult:
    source_kind: SourceKind
    locator: str
    body: str
    title: str
    retrieved_at: datetime
    available_at: datetime
    source_url: str | None = None
    local_locator: str | None = None
    author_institution: str = "UNKNOWN"
    published_at: datetime | None = None
    published_at_status: PublishedAtStatus = PublishedAtStatus.KNOWN
    language: str = "unknown"
    license_note: str = "unspecified"
    parser_version: str = "strict-text/v1"

    def __post_init__(self) -> None:
        if not isinstance(self.source_kind, SourceKind):
            _fail(ResearchMemoryFailureCode.INVALID_SOURCE, "source_kind is invalid")
        for name in ("locator", "body", "title", "author_institution", "language", "license_note", "parser_version"):
            _text(getattr(self, name), name)
        _dt(self.retrieved_at, "retrieved_at")
        _dt(self.available_at, "available_at")
        if not isinstance(self.published_at_status, PublishedAtStatus):
            _fail(ResearchMemoryFailureCode.INVALID_SOURCE, "published_at_status is invalid")
        if self.published_at_status is PublishedAtStatus.KNOWN:
            if self.published_at is None:
                _fail(ResearchMemoryFailureCode.INVALID_SOURCE, "published_at required when status is KNOWN")
            if _dt(self.published_at, "published_at") > _dt(self.available_at, "available_at"):
                _fail(ResearchMemoryFailureCode.INVALID_SOURCE, "published_at must not follow available_at")
        elif self.published_at is not None:
            _fail(ResearchMemoryFailureCode.INVALID_SOURCE, "UNKNOWN published_at requires published_at=None")


class SourceGateway(Protocol):
    def fetch(self, request: SourceRequest) -> GatewayResult:
        ...


class LocalDocumentGateway:
    def __init__(self, allowed_roots: tuple[str | Path, ...]) -> None:
        if not allowed_roots:
            _fail(ResearchMemoryFailureCode.SOURCE_NOT_ALLOWLISTED, "allowed_roots is required")
        self.allowed_roots = tuple(Path(root).expanduser().resolve() for root in allowed_roots)

    def fetch(self, request: SourceRequest) -> GatewayResult:
        if request.source_kind is not SourceKind.LOCAL_DOCUMENT:
            _fail(ResearchMemoryFailureCode.INVALID_SOURCE, "LocalDocumentGateway requires LOCAL_DOCUMENT")
        path = Path(request.locator).expanduser().resolve()
        if not any(_is_relative_to(path, root) for root in self.allowed_roots):
            _fail(ResearchMemoryFailureCode.SOURCE_NOT_ALLOWLISTED, "local document escapes allowed roots")
        if not path.is_file():
            _fail(ResearchMemoryFailureCode.DOCUMENT_NOT_FOUND, "local document not found")
        if path.suffix.lower() not in {"", ".txt", ".md", ".markdown", ".csv", ".json", ".html", ".htm"}:
            _fail(ResearchMemoryFailureCode.INVALID_SOURCE, "unsupported local document format")
        metadata = dict(request.metadata)
        body = path.read_text(encoding=str(metadata.get("encoding") or "utf-8"))
        return GatewayResult(
            source_kind=request.source_kind,
            locator=request.locator,
            body=body,
            title=str(metadata.get("title") or path.name),
            retrieved_at=request.retrieved_at,
            available_at=request.available_at,
            local_locator=str(path),
            author_institution=str(metadata.get("author_institution") or "UNKNOWN"),
            published_at=metadata.get("published_at"),
            published_at_status=_published_status(metadata),
            language=str(metadata.get("language") or "unknown"),
            license_note=str(metadata.get("license_note") or "local file; caller responsible for license"),
            parser_version=str(metadata.get("parser_version") or "strict-local-text/v1"),
        )


class FrozenWebFixtureGateway:
    def __init__(self, fixtures: Mapping[str, Mapping[str, Any] | GatewayResult]) -> None:
        self.fixtures = dict(fixtures)

    def fetch(self, request: SourceRequest) -> GatewayResult:
        if request.source_kind is not SourceKind.FROZEN_WEB_FIXTURE:
            _fail(ResearchMemoryFailureCode.INVALID_SOURCE, "FrozenWebFixtureGateway requires FROZEN_WEB_FIXTURE")
        fixture = self.fixtures.get(request.locator)
        if fixture is None:
            _fail(ResearchMemoryFailureCode.DOCUMENT_NOT_FOUND, "frozen web fixture not found")
        if isinstance(fixture, GatewayResult):
            return fixture
        metadata = {**dict(request.metadata), **dict(fixture)}
        body = str(metadata.pop("body", "") or metadata.pop("content", ""))
        return GatewayResult(
            source_kind=request.source_kind,
            locator=request.locator,
            body=body,
            title=str(metadata.get("title") or request.locator),
            retrieved_at=request.retrieved_at,
            available_at=request.available_at,
            source_url=request.locator,
            author_institution=str(metadata.get("author_institution") or "UNKNOWN"),
            published_at=metadata.get("published_at"),
            published_at_status=_published_status(metadata),
            language=str(metadata.get("language") or "unknown"),
            license_note=str(metadata.get("license_note") or "offline fixture; license not assumed"),
            parser_version=str(metadata.get("parser_version") or "strict-web-fixture/v1"),
        )


class OptionalVibeReadOnlyGateway:
    def __init__(
        self,
        *,
        enable_research_network: str,
        source_allowlist: tuple[str, ...],
        network_budget: int,
        timeout_seconds: float,
        vibe_executor: Callable[[str, dict[str, Any]], str] | None = None,
    ) -> None:
        self.enable_research_network = enable_research_network
        self.source_allowlist = tuple(item.lower().strip() for item in source_allowlist if item.strip())
        self.network_budget = network_budget
        self.timeout_seconds = timeout_seconds
        self.vibe_executor = vibe_executor
        self.calls_used = 0

    def fetch(self, request: SourceRequest) -> GatewayResult:
        if request.source_kind is not SourceKind.OPTIONAL_VIBE_READONLY:
            _fail(ResearchMemoryFailureCode.INVALID_SOURCE, "OptionalVibeReadOnlyGateway requires OPTIONAL_VIBE_READONLY")
        if self.enable_research_network != "1":
            _fail(ResearchMemoryFailureCode.NETWORK_DISABLED, "ENABLE_RESEARCH_NETWORK=1 was not explicitly passed")
        if self.timeout_seconds <= 0:
            _fail(ResearchMemoryFailureCode.NETWORK_DISABLED, "positive timeout is required")
        if self.calls_used >= self.network_budget:
            _fail(ResearchMemoryFailureCode.NETWORK_BUDGET_EXCEEDED, "network budget exhausted")
        if self.vibe_executor is None:
            _fail(ResearchMemoryFailureCode.NETWORK_DISABLED, "no Vibe read-only executor supplied")
        if not _url_allowed(request.locator, self.source_allowlist):
            _fail(ResearchMemoryFailureCode.SOURCE_NOT_ALLOWLISTED, "source is not allowlisted")
        self.calls_used += 1
        raw = self.vibe_executor(
            "read_url",
            {"url": request.locator, "no_cache": True, "timeout_seconds": self.timeout_seconds},
        )
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ResearchMemoryError(ResearchMemoryFailureCode.INVALID_SOURCE, "Vibe output is not JSON") from exc
        if payload.get("status") != "ok":
            _fail(ResearchMemoryFailureCode.INVALID_SOURCE, "Vibe read-only executor failed")
        body = str(payload.get("content") or "")
        metadata = dict(request.metadata)
        return GatewayResult(
            source_kind=request.source_kind,
            locator=request.locator,
            body=body,
            title=str(payload.get("title") or metadata.get("title") or request.locator),
            retrieved_at=request.retrieved_at,
            available_at=request.available_at,
            source_url=request.locator,
            author_institution=str(metadata.get("author_institution") or "VIBE_READONLY"),
            published_at=metadata.get("published_at"),
            published_at_status=_published_status(metadata),
            language=str(metadata.get("language") or "unknown"),
            license_note=str(metadata.get("license_note") or "caller must verify source license"),
            parser_version="strict-vibe-readonly/v1",
        )


@dataclass(frozen=True)
class FrozenDocumentChunk:
    document_id: str
    chunk_index: int
    page: int
    paragraph: int
    span_start: int
    span_end: int
    text: str
    chunk_hash: str


@dataclass(frozen=True)
class FrozenDocument:
    schema_version: str
    document_id: str
    content_hash: str
    source_kind: SourceKind
    locator: str
    title: str
    body: str
    chunks: tuple[FrozenDocumentChunk, ...]
    retrieved_at: datetime
    available_at: datetime
    author_institution: str
    published_at: datetime | None
    published_at_status: PublishedAtStatus
    language: str
    license_note: str
    parser_version: str
    prompt_injection_findings: tuple[str, ...]
    source_url: str | None = None
    local_locator: str | None = None

    @property
    def document_hash(self) -> str:
        return artifact_hash(self.manifest())

    def manifest(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "document_id": self.document_id,
            "content_hash": self.content_hash,
            "source_kind": self.source_kind.value,
            "locator": self.locator,
            "title": self.title,
            "author_institution": self.author_institution,
            "published_at": self.published_at,
            "published_at_status": self.published_at_status.value,
            "retrieved_at": self.retrieved_at,
            "available_at": self.available_at,
            "language": self.language,
            "license_note": self.license_note,
            "parser_version": self.parser_version,
            "source_url": self.source_url,
            "local_locator": self.local_locator,
            "prompt_injection_findings": self.prompt_injection_findings,
            "chunks": [
                {
                    "chunk_index": chunk.chunk_index,
                    "page": chunk.page,
                    "paragraph": chunk.paragraph,
                    "span_start": chunk.span_start,
                    "span_end": chunk.span_end,
                    "chunk_hash": chunk.chunk_hash,
                }
                for chunk in self.chunks
            ],
        }


class FrozenDocumentStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.documents_dir = self.root / "documents"
        self.documents_dir.mkdir(parents=True, exist_ok=True)

    def freeze(self, result: GatewayResult, *, document_id: str | None = None) -> FrozenDocument:
        body_hash = content_hash(result.body)
        resolved_id = document_id or "doc_" + artifact_hash(
            {
                "locator": result.locator,
                "content_hash": body_hash,
                "available_at": result.available_at,
                "published_at": result.published_at,
            }
        ).removeprefix("sha256:")[:24]
        _identifier(resolved_id, "document_id")
        chunks = _chunk_body(document_id=resolved_id, body=result.body, body_hash=body_hash)
        doc = FrozenDocument(
            schema_version=SCHEMA_VERSION,
            document_id=resolved_id,
            content_hash=body_hash,
            source_kind=result.source_kind,
            locator=result.locator,
            title=result.title,
            body=result.body,
            chunks=chunks,
            retrieved_at=result.retrieved_at,
            available_at=result.available_at,
            author_institution=result.author_institution,
            published_at=result.published_at,
            published_at_status=result.published_at_status,
            language=result.language,
            license_note=result.license_note,
            parser_version=result.parser_version,
            prompt_injection_findings=detect_prompt_injection(result.body),
            source_url=result.source_url,
            local_locator=result.local_locator,
        )
        doc_dir = self.documents_dir / resolved_id
        manifest_path = doc_dir / "manifest.json"
        body_path = doc_dir / "body.txt"
        if manifest_path.exists() or body_path.exists():
            existing = self.get(resolved_id)
            if existing.document_hash != doc.document_hash:
                _fail(ResearchMemoryFailureCode.DOCUMENT_COLLISION, "document_id already exists with different content")
            return existing
        doc_dir.mkdir(parents=True, exist_ok=False)
        _write_text_atomic(body_path, result.body)
        _write_text_atomic(manifest_path, canonical_json(doc.manifest()))
        return doc

    def get(self, document_id: str) -> FrozenDocument:
        _identifier(document_id, "document_id")
        manifest_path = self.documents_dir / document_id / "manifest.json"
        body_path = self.documents_dir / document_id / "body.txt"
        if not manifest_path.is_file() or not body_path.is_file():
            _fail(ResearchMemoryFailureCode.DOCUMENT_NOT_FOUND, "document not found")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        body = body_path.read_text(encoding="utf-8")
        if manifest.get("content_hash") != content_hash(body):
            _fail(ResearchMemoryFailureCode.DOCUMENT_COLLISION, "stored body hash mismatch")
        chunks = _chunk_body(document_id=document_id, body=body, body_hash=str(manifest["content_hash"]))
        recorded = {item["chunk_hash"] for item in manifest.get("chunks", [])}
        computed = {chunk.chunk_hash for chunk in chunks}
        if recorded != computed:
            _fail(ResearchMemoryFailureCode.DOCUMENT_COLLISION, "stored chunk hash mismatch")
        return FrozenDocument(
            schema_version=str(manifest["schema_version"]),
            document_id=document_id,
            content_hash=str(manifest["content_hash"]),
            source_kind=SourceKind(str(manifest["source_kind"])),
            locator=str(manifest["locator"]),
            title=str(manifest["title"]),
            body=body,
            chunks=chunks,
            retrieved_at=_parse_dt(manifest["retrieved_at"]),
            available_at=_parse_dt(manifest["available_at"]),
            author_institution=str(manifest["author_institution"]),
            published_at=_parse_dt_or_none(manifest.get("published_at")),
            published_at_status=PublishedAtStatus(str(manifest["published_at_status"])),
            language=str(manifest["language"]),
            license_note=str(manifest["license_note"]),
            parser_version=str(manifest["parser_version"]),
            prompt_injection_findings=tuple(str(item) for item in manifest.get("prompt_injection_findings", ())),
            source_url=manifest.get("source_url"),
            local_locator=manifest.get("local_locator"),
        )

    def locate_chunk(self, citation: "Citation") -> FrozenDocumentChunk:
        doc = self.get(citation.document_id)
        for chunk in doc.chunks:
            if chunk.chunk_hash == citation.chunk_hash and chunk.page == citation.page and chunk.paragraph == citation.paragraph:
                return chunk
        _fail(ResearchMemoryFailureCode.INVALID_CITATION, "citation chunk/page/paragraph not found")


@dataclass(frozen=True)
class Citation:
    document_id: str
    source_content_hash: str
    chunk_hash: str
    page: int
    paragraph: int
    span_start: int
    span_end: int
    quote: str
    claim: str
    required_terms: tuple[str, ...]
    published_at: datetime | None
    available_at: datetime
    requires_time_claim: bool = True
    support_scope: str = "DIRECT"


@dataclass(frozen=True)
class FactorDraftMapping:
    factor_name: str
    draft_expression: str
    feature_names: tuple[str, ...]
    rationale: str

    @property
    def factor_draft_hash(self) -> str:
        return artifact_hash(self)


@dataclass(frozen=True)
class EvidenceGroundedHypothesis:
    hypothesis: str
    economic_mechanism: str
    expected_sign: str
    falsification_tests: tuple[str, ...]
    factor_mapping: tuple[FactorDraftMapping, ...]
    citations: tuple[Citation, ...]
    uncertainty: str
    limitations: tuple[str, ...]
    state: ResearchState = ResearchState.DRAFT
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.state is not ResearchState.DRAFT:
            _fail(ResearchMemoryFailureCode.INVALID_VALUE, "hypotheses must stay DRAFT")
        if self.expected_sign not in {"positive", "negative", "mixed", "unknown"}:
            _fail(ResearchMemoryFailureCode.INVALID_VALUE, "expected_sign is invalid")

    @property
    def hypothesis_hash(self) -> str:
        return artifact_hash(self)

    def canonical_json(self) -> str:
        return canonical_json(self)


@dataclass(frozen=True)
class CitationFinding:
    citation_index: int
    status: CitationStatus
    code: ResearchMemoryFailureCode | None
    message: str


@dataclass(frozen=True)
class CitationCriticReport:
    status: CitationStatus
    findings: tuple[CitationFinding, ...]


class CitationCritic:
    def __init__(self, document_store: FrozenDocumentStore) -> None:
        self.document_store = document_store

    def evaluate(self, hypothesis: EvidenceGroundedHypothesis, *, decision_time: datetime) -> CitationCriticReport:
        _dt(decision_time, "decision_time")
        if not hypothesis.citations:
            return CitationCriticReport(
                status=CitationStatus.REJECT,
                findings=(CitationFinding(-1, CitationStatus.REJECT, ResearchMemoryFailureCode.MISSING_CITATION, "hypothesis has no citation"),),
            )
        findings: list[CitationFinding] = []
        for idx, citation in enumerate(hypothesis.citations):
            findings.extend(self._one(idx, citation, decision_time=decision_time))
        status = CitationStatus.PASS if all(item.code is None for item in findings) else CitationStatus.REJECT
        return CitationCriticReport(status=status, findings=tuple(findings))

    def _one(self, idx: int, citation: Citation, *, decision_time: datetime) -> list[CitationFinding]:
        doc = self.document_store.get(citation.document_id)
        out: list[CitationFinding] = []
        if citation.source_content_hash != doc.content_hash:
            out.append(CitationFinding(idx, CitationStatus.REJECT, ResearchMemoryFailureCode.INVALID_CITATION, "source content hash mismatch"))
        if doc.available_at > decision_time or citation.available_at > decision_time:
            out.append(CitationFinding(idx, CitationStatus.REJECT, ResearchMemoryFailureCode.FUTURE_DOCUMENT, "document was not available at decision_time"))
        if citation.requires_time_claim and doc.published_at_status is PublishedAtStatus.UNKNOWN:
            out.append(CitationFinding(idx, CitationStatus.REJECT, ResearchMemoryFailureCode.UNKNOWN_PUBLISHED_AT, "unknown published_at cannot support time claim"))
        chunk = self.document_store.locate_chunk(citation)
        if citation.span_start < 0 or citation.span_end > len(chunk.text) or citation.span_start >= citation.span_end:
            out.append(CitationFinding(idx, CitationStatus.REJECT, ResearchMemoryFailureCode.INVALID_CITATION, "span out of range"))
        else:
            quote = chunk.text[citation.span_start : citation.span_end]
            if quote != citation.quote:
                out.append(CitationFinding(idx, CitationStatus.REJECT, ResearchMemoryFailureCode.INVALID_CITATION, "quote does not match frozen span"))
            if detect_prompt_injection(quote):
                out.append(CitationFinding(idx, CitationStatus.REJECT, ResearchMemoryFailureCode.PROMPT_INJECTION, "quoted span contains prompt injection"))
            missing = [term for term in citation.required_terms if term.casefold() not in quote.casefold()]
            if missing:
                out.append(CitationFinding(idx, CitationStatus.REJECT, ResearchMemoryFailureCode.UNSUPPORTED_CLAIM, "missing support terms: " + ",".join(missing)))
        if citation.support_scope != "DIRECT":
            out.append(CitationFinding(idx, CitationStatus.REJECT, ResearchMemoryFailureCode.OVER_EXTRAPOLATED, "citation is background-only"))
        if re.search(r"\b(proves?|guarantees?|official|validated|promoted|risk approved)\b", citation.claim, re.I):
            out.append(CitationFinding(idx, CitationStatus.REJECT, ResearchMemoryFailureCode.OVER_EXTRAPOLATED, "claim over-extrapolates document evidence"))
        if not out:
            out.append(CitationFinding(idx, CitationStatus.PASS, None, "citation verified"))
        return out


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
    decision_time: datetime
    requires_time_claim: bool = True


@dataclass(frozen=True)
class MemoryRecord:
    record_id: str
    redacted_hypothesis: str
    citations: tuple[Citation, ...]
    factor_draft_hash: str
    train_validation_evidence_summary: Mapping[str, Any]
    public_failure_reason: str | None
    lineage: tuple[str, ...]
    created_at: datetime
    decision_time: datetime
    available_at: datetime
    schema_version: str = SCHEMA_VERSION


class ResearchMemoryStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.records_path = self.root / "records.jsonl"

    def build_record(
        self,
        hypothesis: EvidenceGroundedHypothesis,
        *,
        factor_draft_hash: str,
        train_validation_evidence_summary: Mapping[str, Any],
        public_failure_reason: str | None,
        lineage: tuple[str, ...],
        created_at: datetime,
        decision_time: datetime,
        available_at: datetime,
    ) -> MemoryRecord:
        record_id = "mem_" + artifact_hash(
            {
                "hypothesis": hypothesis.hypothesis_hash,
                "factor": factor_draft_hash,
                "citations": tuple(artifact_hash(item) for item in hypothesis.citations),
                "available_at": available_at,
            }
        ).removeprefix("sha256:")[:24]
        return MemoryRecord(
            record_id=record_id,
            redacted_hypothesis=redact_sensitive(hypothesis.hypothesis),
            citations=hypothesis.citations,
            factor_draft_hash=factor_draft_hash,
            train_validation_evidence_summary=dict(train_validation_evidence_summary),
            public_failure_reason=public_failure_reason,
            lineage=lineage,
            created_at=created_at,
            decision_time=decision_time,
            available_at=available_at,
        )

    def append(self, record: MemoryRecord) -> MemoryRecord:
        payload = normalize(record)
        assert_no_forbidden_memory_payload(payload)
        line = canonical_json(payload)
        with self.records_path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(line + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        return record

    def query_as_of(self, decision_time: datetime) -> tuple[MemoryRecord, ...]:
        cutoff = _dt(decision_time, "decision_time")
        out: list[MemoryRecord] = []
        for record in self.all_records():
            if record.available_at <= cutoff and record.decision_time <= cutoff:
                out.append(record)
        return tuple(out)

    def all_records(self) -> tuple[MemoryRecord, ...]:
        if not self.records_path.exists():
            return tuple()
        records: list[MemoryRecord] = []
        for line in self.records_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                payload = json.loads(line)
                assert_no_forbidden_memory_payload(payload)
                records.append(_record_from_payload(payload))
        return tuple(records)


class Retriever:
    def __init__(self, gateways: Mapping[SourceKind, SourceGateway], document_store: FrozenDocumentStore) -> None:
        self.gateways = dict(gateways)
        self.document_store = document_store

    def retrieve(self, requests: tuple[SourceRequest, ...]) -> tuple[FrozenDocument, ...]:
        docs: list[FrozenDocument] = []
        for request in requests:
            gateway = self.gateways.get(request.source_kind)
            if gateway is None:
                _fail(ResearchMemoryFailureCode.DAG_VALIDATION_FAILED, "missing gateway")
            docs.append(self.document_store.freeze(gateway.fetch(request)))
        return tuple(docs)


class HypothesisSynthesizer:
    def synthesize(self, brief: ResearchBrief, documents: tuple[FrozenDocument, ...]) -> EvidenceGroundedHypothesis:
        if not documents:
            _fail(ResearchMemoryFailureCode.DAG_VALIDATION_FAILED, "at least one document is required")
        doc = documents[0]
        safe_chunk = next((chunk for chunk in doc.chunks if not detect_prompt_injection(chunk.text)), None)
        if safe_chunk is None:
            _fail(ResearchMemoryFailureCode.PROMPT_INJECTION, "no safe chunk available")
        quote = _first_sentence(safe_chunk.text)
        start = safe_chunk.text.find(quote)
        mapping = FactorDraftMapping(
            factor_name=brief.factor_name,
            draft_expression=brief.factor_expression,
            feature_names=brief.feature_names,
            rationale="DRAFT mapping from frozen document citation; FactorDAG compilation is external.",
        )
        citation = Citation(
            document_id=doc.document_id,
            source_content_hash=doc.content_hash,
            chunk_hash=safe_chunk.chunk_hash,
            page=safe_chunk.page,
            paragraph=safe_chunk.paragraph,
            span_start=start,
            span_end=start + len(quote),
            quote=quote,
            claim=brief.claim,
            required_terms=brief.required_terms,
            published_at=doc.published_at,
            available_at=doc.available_at,
            requires_time_claim=brief.requires_time_claim,
        )
        return EvidenceGroundedHypothesis(
            hypothesis=redact_sensitive(brief.claim),
            economic_mechanism=redact_sensitive(brief.economic_mechanism),
            expected_sign=brief.expected_sign,
            falsification_tests=tuple(redact_sensitive(item) for item in brief.falsification_tests),
            factor_mapping=(mapping,),
            citations=(citation,),
            uncertainty="Document/news content motivates a DRAFT only; official Evidence Service must validate.",
            limitations=(
                "No document citation is official alpha evidence.",
                "This output cannot directly affect portfolio, risk, orders, or ledger.",
            ),
        )


@dataclass(frozen=True)
class ResearchDAGResult:
    status: str
    documents: tuple[FrozenDocument, ...]
    hypothesis: EvidenceGroundedHypothesis
    citation_report: CitationCriticReport
    memory_record_id: str | None


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
        if self.memory_store is not None:
            record = self.memory_store.build_record(
                hypothesis,
                factor_draft_hash=hypothesis.factor_mapping[0].factor_draft_hash,
                train_validation_evidence_summary={},
                public_failure_reason=None if report.status is CitationStatus.PASS else "citation critic rejected DRAFT",
                lineage=("Retriever", "HypothesisSynthesizer", "CitationCritic"),
                created_at=requests[0].retrieved_at,
                decision_time=brief.decision_time,
                available_at=max(doc.available_at for doc in documents),
            )
            self.memory_store.append(record)
            memory_record_id = record.record_id
        return ResearchDAGResult(
            status="DRAFT_READY" if report.status is CitationStatus.PASS else "REJECTED",
            documents=documents,
            hypothesis=hypothesis,
            citation_report=report,
            memory_record_id=memory_record_id,
        )


PROMPT_INJECTION_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("ignore_previous_instructions", re.compile(r"ignore\s+(all\s+)?(previous|prior)\s+instructions|忽略.{0,20}(之前|系统).{0,20}指令", re.I)),
    ("read_secrets", re.compile(r"(read|print|dump|exfiltrate).{0,50}(secret|key|token|cookie|credential)|读取.{0,20}(密钥|令牌|cookie)", re.I)),
    ("call_shell", re.compile(r"(shell|bash|powershell|cmd\.exe|subprocess|system\(|调用.{0,20}shell)", re.I)),
    ("write_files", re.compile(r"(write|modify|delete).{0,40}(file|disk|state\.json)|写文件|修改文件", re.I)),
    ("network_download", re.compile(r"(download|curl|wget|fetch).{0,40}(network|http|url)|网络下载", re.I)),
    ("place_order", re.compile(r"(place|submit|send|cancel).{0,40}(order|trade)|下单", re.I)),
    ("risk_or_promotion", re.compile(r"(approve|override|bypass|promote).{0,40}(risk|promotion|verdict)|风险审批|promotion", re.I)),
)
SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"authorization\s*[:=]\s*bearer\s+[A-Za-z0-9._\-]+", re.I),
    re.compile(r"\b(api[_-]?key|token|cookie|secret|password)\s*[:=]\s*['\"]?[^'\"\s,;]+", re.I),
    re.compile(r"\bBearer\s+[A-Za-z0-9._\-]+", re.I),
)
FORBIDDEN_MEMORY_KEYS = {
    "sealed",
    "sealed_rows",
    "sealed_metrics",
    "sealed_ranking",
    "test_rows",
    "test_metrics",
    "test_ranking",
    "promotion_private_reason",
    "api_key",
    "token",
    "cookie",
    "authorization",
    "raw_content",
    "unfrozen_web_content",
    "orders",
    "target_weights",
    "risk_approval",
}


def detect_prompt_injection(text: str) -> tuple[str, ...]:
    return tuple(sorted({name for name, pattern in PROMPT_INJECTION_RULES if pattern.search(text or "")}))


def redact_sensitive(text: str) -> str:
    redacted = text
    for pattern in SECRET_PATTERNS:
        redacted = pattern.sub("[REDACTED_SECRET]", redacted)
    return redacted


def assert_no_forbidden_memory_payload(value: Any, *, path: str = "$") -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            key_text = str(key).strip().lower()
            if key_text in FORBIDDEN_MEMORY_KEYS:
                _fail(ResearchMemoryFailureCode.FORBIDDEN_MEMORY_PAYLOAD, f"forbidden memory key at {path}.{key_text}")
            assert_no_forbidden_memory_payload(item, path=f"{path}.{key_text}")
    elif isinstance(value, list):
        for idx, item in enumerate(value):
            assert_no_forbidden_memory_payload(item, path=f"{path}[{idx}]")
    elif isinstance(value, str):
        lower = value.lower()
        if "sealed metric" in lower or "sealed ranking" in lower or "test row" in lower:
            _fail(ResearchMemoryFailureCode.FORBIDDEN_MEMORY_PAYLOAD, f"forbidden sealed/test text at {path}")
        if any(pattern.search(value) for pattern in SECRET_PATTERNS):
            _fail(ResearchMemoryFailureCode.SECRET_DETECTED, f"secret-like text at {path}")


def _published_status(metadata: Mapping[str, Any]) -> PublishedAtStatus:
    raw = str(metadata.get("published_at_status") or "KNOWN").upper()
    return PublishedAtStatus.UNKNOWN if raw == "UNKNOWN" else PublishedAtStatus.KNOWN


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _url_allowed(locator: str, allowlist: tuple[str, ...]) -> bool:
    parsed = urlsplit(locator)
    host = (parsed.hostname or "").lower()
    lowered = locator.lower()
    return bool(host) and any(host == item or host.endswith("." + item) or lowered == item for item in allowlist)


def _chunk_body(*, document_id: str, body: str, body_hash: str) -> tuple[FrozenDocumentChunk, ...]:
    chunks: list[FrozenDocumentChunk] = []
    page = 1
    paragraph = 0
    current: list[str] = []
    start = 0
    offset = 0

    def flush(end: int) -> None:
        nonlocal paragraph, current, start
        text = "".join(current).strip()
        if text:
            paragraph += 1
            chunks.append(
                FrozenDocumentChunk(
                    document_id=document_id,
                    chunk_index=len(chunks),
                    page=page,
                    paragraph=paragraph,
                    span_start=start,
                    span_end=end,
                    text=text,
                    chunk_hash=artifact_hash(
                        {
                            "document_id": document_id,
                            "content_hash": body_hash,
                            "page": page,
                            "paragraph": paragraph,
                            "text": text,
                        }
                    ),
                )
            )
        current = []

    for line in body.splitlines(keepends=True):
        line_start = offset
        offset += len(line)
        marker = re.match(r"^\s*-{3,}\s*Page\s+(\d+)\s*-{3,}\s*$", line.strip(), re.I)
        if marker:
            flush(line_start)
            page = int(marker.group(1))
            paragraph = 0
            start = offset
            continue
        if not line.strip():
            flush(line_start)
            start = offset
            continue
        if not current:
            start = line_start
        current.append(line)
    flush(len(body))
    if not chunks:
        _fail(ResearchMemoryFailureCode.INVALID_SOURCE, "document body produced no chunks")
    return tuple(chunks)


def _write_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def _parse_dt(value: Any) -> datetime:
    if isinstance(value, datetime):
        return _dt(value, "datetime")
    if not isinstance(value, str):
        _fail(ResearchMemoryFailureCode.INVALID_VALUE, "datetime must be serialized as text")
    return _dt(datetime.fromisoformat(value.replace("Z", "+00:00")), "datetime")


def _parse_dt_or_none(value: Any) -> datetime | None:
    return None if value is None else _parse_dt(value)


def _record_from_payload(payload: Mapping[str, Any]) -> MemoryRecord:
    citations = tuple(
        Citation(
            document_id=str(item["document_id"]),
            source_content_hash=str(item["source_content_hash"]),
            chunk_hash=str(item["chunk_hash"]),
            page=int(item["page"]),
            paragraph=int(item["paragraph"]),
            span_start=int(item["span_start"]),
            span_end=int(item["span_end"]),
            quote=str(item["quote"]),
            claim=str(item["claim"]),
            required_terms=tuple(str(term) for term in item.get("required_terms", [])),
            published_at=_parse_dt_or_none(item.get("published_at")),
            available_at=_parse_dt(item["available_at"]),
            requires_time_claim=bool(item.get("requires_time_claim", True)),
            support_scope=str(item.get("support_scope") or "DIRECT"),
        )
        for item in payload.get("citations", [])
    )
    return MemoryRecord(
        record_id=str(payload["record_id"]),
        redacted_hypothesis=str(payload["redacted_hypothesis"]),
        citations=citations,
        factor_draft_hash=str(payload["factor_draft_hash"]),
        train_validation_evidence_summary=dict(payload.get("train_validation_evidence_summary") or {}),
        public_failure_reason=payload.get("public_failure_reason"),
        lineage=tuple(str(item) for item in payload.get("lineage", ())),
        created_at=_parse_dt(payload["created_at"]),
        decision_time=_parse_dt(payload["decision_time"]),
        available_at=_parse_dt(payload["available_at"]),
    )


def _first_sentence(text: str) -> str:
    stripped = " ".join(text.split())
    if "." in stripped:
        return stripped.split(".", 1)[0] + "."
    return stripped[:240]
