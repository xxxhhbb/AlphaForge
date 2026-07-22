from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from .canonical import artifact_hash, canonical_json, isoformat_utc


SCHEMA_VERSION = "veriquant.research-memory/v1"


class ResearchMemoryError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class FailureCode(str, Enum):
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
    PROMPT_INJECTION = "PROMPT_INJECTION"
    FORBIDDEN_MEMORY_PAYLOAD = "FORBIDDEN_MEMORY_PAYLOAD"


class SourceKind(str, Enum):
    LOCAL_DOCUMENT = "LOCAL_DOCUMENT"
    FROZEN_WEB_FIXTURE = "FROZEN_WEB_FIXTURE"
    OPTIONAL_VIBE_READONLY = "OPTIONAL_VIBE_READONLY"


class PublishedAtStatus(str, Enum):
    KNOWN = "KNOWN"
    UNKNOWN = "UNKNOWN"


class HypothesisState(str, Enum):
    DRAFT = "DRAFT"


@dataclass(frozen=True)
class SourceRequest:
    source_kind: SourceKind
    locator: str
    retrieved_at: str
    available_at: str
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.locator.strip():
            raise ResearchMemoryError(FailureCode.INVALID_SOURCE.value, "locator is required")
        isoformat_utc(self.retrieved_at, field_name="retrieved_at")
        retrieved = isoformat_utc(self.retrieved_at, field_name="retrieved_at")
        available = isoformat_utc(self.available_at, field_name="available_at")
        object.__setattr__(self, "retrieved_at", retrieved)
        object.__setattr__(self, "available_at", available)


@dataclass(frozen=True)
class GatewayResult:
    source_kind: SourceKind
    locator: str
    body: str
    title: str
    retrieved_at: str
    available_at: str
    author: str | None = None
    institution: str | None = None
    published_at: str | None = None
    published_at_status: PublishedAtStatus = PublishedAtStatus.KNOWN
    language: str = "en"
    license_note: str = "unspecified"
    parser_version: str = "research-memory-parser/v1"
    source_url: str | None = None
    local_locator: str | None = None

    def __post_init__(self) -> None:
        if not self.body:
            raise ResearchMemoryError(FailureCode.INVALID_SOURCE.value, "document body is required")
        if not self.title.strip():
            raise ResearchMemoryError(FailureCode.INVALID_SOURCE.value, "title is required")
        retrieved = isoformat_utc(self.retrieved_at, field_name="retrieved_at")
        available = isoformat_utc(self.available_at, field_name="available_at")
        object.__setattr__(self, "retrieved_at", retrieved)
        object.__setattr__(self, "available_at", available)
        if self.published_at_status is PublishedAtStatus.KNOWN:
            if not self.published_at:
                raise ResearchMemoryError(FailureCode.INVALID_SOURCE.value, "published_at is required when known")
            object.__setattr__(
                self,
                "published_at",
                isoformat_utc(self.published_at, field_name="published_at"),
            )
        else:
            object.__setattr__(self, "published_at", None)


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
    retrieved_at: str
    available_at: str
    author: str | None = None
    institution: str | None = None
    published_at: str | None = None
    published_at_status: PublishedAtStatus = PublishedAtStatus.KNOWN
    language: str = "en"
    license_note: str = "unspecified"
    parser_version: str = "research-memory-parser/v1"
    source_url: str | None = None
    local_locator: str | None = None

    @property
    def document_hash(self) -> str:
        return artifact_hash(self.to_manifest())

    def to_manifest(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "document_id": self.document_id,
            "content_hash": self.content_hash,
            "source_kind": self.source_kind.value,
            "locator": self.locator,
            "title": self.title,
            "author": self.author,
            "institution": self.institution,
            "published_at": self.published_at,
            "published_at_status": self.published_at_status.value,
            "retrieved_at": self.retrieved_at,
            "available_at": self.available_at,
            "language": self.language,
            "license_note": self.license_note,
            "parser_version": self.parser_version,
            "source_url": self.source_url,
            "local_locator": self.local_locator,
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


@dataclass(frozen=True)
class Citation:
    document_id: str
    chunk_hash: str
    page: int
    paragraph: int
    span_start: int
    span_end: int
    quote: str
    claim: str
    required_terms: tuple[str, ...] = ()
    requires_time_claim: bool = False


@dataclass(frozen=True)
class FactorDraftMapping:
    factor_name: str
    draft_expression: str
    feature_names: tuple[str, ...]
    rationale: str

    @property
    def factor_draft_hash(self) -> str:
        return artifact_hash(
            {
                "factor_name": self.factor_name,
                "draft_expression": self.draft_expression,
                "feature_names": self.feature_names,
                "rationale": self.rationale,
            }
        )


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
    state: HypothesisState = HypothesisState.DRAFT
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.state is not HypothesisState.DRAFT:
            raise ResearchMemoryError(FailureCode.INVALID_SOURCE.value, "research memory hypotheses must stay DRAFT")
        if self.expected_sign not in {"positive", "negative", "mixed", "unknown"}:
            raise ResearchMemoryError(FailureCode.INVALID_SOURCE.value, "expected_sign is invalid")
        if not self.hypothesis.strip() or not self.economic_mechanism.strip():
            raise ResearchMemoryError(FailureCode.INVALID_SOURCE.value, "hypothesis and mechanism are required")

    @property
    def hypothesis_hash(self) -> str:
        return artifact_hash(self)

    def canonical_json(self) -> str:
        return canonical_json(self)


@dataclass(frozen=True)
class CitationFinding:
    citation_index: int
    status: str
    code: str | None
    message: str


@dataclass(frozen=True)
class CitationCriticReport:
    status: str
    findings: tuple[CitationFinding, ...]


@dataclass(frozen=True)
class MemoryRecord:
    record_id: str
    redacted_hypothesis: str
    citations: tuple[Citation, ...]
    factor_draft_hash: str
    train_validation_evidence_summary: dict[str, Any]
    public_failure_reason: str | None
    lineage: tuple[str, ...]
    created_at: str
    decision_time: str
    available_at: str
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "created_at", isoformat_utc(self.created_at, field_name="created_at"))
        object.__setattr__(self, "decision_time", isoformat_utc(self.decision_time, field_name="decision_time"))
        object.__setattr__(self, "available_at", isoformat_utc(self.available_at, field_name="available_at"))
