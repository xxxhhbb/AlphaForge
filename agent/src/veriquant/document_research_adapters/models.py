"""Frozen document/event adapters for AlphaForge research tools.

This package is product-layer and DRAFT-only.  It never creates official
Evidence, promotion decisions, portfolio weights, risk approvals, orders, or
ledger entries.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, fields, is_dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Mapping, Protocol, Sequence

from veriquant.research_memory.security import detect_prompt_injection
from veriquant.research_tool_contracts import (
    CitationBundle,
    FrozenResearchArtifact,
    ResearchToolCapability,
    ResearchToolContractError,
    ResearchToolFailureCode,
    ResearchToolRequest,
    ResearchToolResult,
    ResearchToolVisibility,
    SourceProvenance,
    content_hash,
)


SCHEMA_VERSION = "alphaforge.document-research-adapters/v1"

_HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_SAFE_SEGMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_URL_RE = re.compile(r"(?i)\b(?:https?|ftp)://")
_WINDOWS_PATH_RE = re.compile(r"(?i)(?:[a-z]:\\|\\\\)")
_POSIX_PATH_RE = re.compile(r"(?:^|[\s'\"(])(?:\.\./|/[^/\s]+/)")
_SECRET_VALUE_RE = re.compile(
    r"(?i)(authorization\s*[:=]|bearer\s+[a-z0-9._\-]{8,}|api[_-]?key\s*[:=]|token\s*[:=]|secret\s*[:=]|cookie\s*[:=])"
)
_AUTHORITY_TEXT_RE = re.compile(
    r"(?i)\b("
    r"challenger|official[_ -]?evidence|official[_ -]?metrics?|sealed|holdout|"
    r"promotion|promote|portfolio|positions?|target[_ -]?weights?|orders?|"
    r"risk[_ -]?approval|broker|fills?|ledger"
    r")\b"
)
_DRAFT_SAFE_FIELD_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")

_FORBIDDEN_KEYS = {
    "official",
    "official_evidence",
    "official_metrics",
    "sealed",
    "sealed_metric",
    "sealed_metrics",
    "holdout",
    "promotion",
    "promotion_decision",
    "challenger",
    "portfolio",
    "positions",
    "position",
    "target_weights",
    "weights",
    "orders",
    "order",
    "risk",
    "risk_approval",
    "broker",
    "fills",
    "ledger",
    "raw_content",
    "unfrozen_body",
}

_SECRET_OR_LOCATION_KEYS = {
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
    "provider_url",
    "url",
    "uri",
    "path",
    "filepath",
    "file_path",
    "document_url",
    "source_url",
    "local_locator",
}


class SourceClass(str, Enum):
    DOCUMENT = "document"
    NEWS = "news"
    RESEARCH_REPORT = "research_report"
    FILING = "filing"


class CitationStatus(str, Enum):
    PASS = "PASS"
    REJECT = "REJECT"


def _fail(code: ResearchToolFailureCode, message: str, **details: Any) -> None:
    raise ResearchToolContractError(code, message, details=details)


def _require_hash(value: str, field_name: str) -> None:
    if not isinstance(value, str) or _HASH_RE.fullmatch(value) is None:
        _fail(ResearchToolFailureCode.INVALID_HASH, f"{field_name} must be sha256:<64 lowercase hex>")


def _require_safe_segment(value: str, field_name: str) -> None:
    if not isinstance(value, str) or _SAFE_SEGMENT_RE.fullmatch(value) is None:
        _fail(ResearchToolFailureCode.PATH_OR_URL_LEAKAGE, f"{field_name} must be a safe frozen identifier")
    lowered = value.lower()
    if ".." in lowered or "/" in value or "\\" in value:
        _fail(ResearchToolFailureCode.PATH_OR_URL_LEAKAGE, f"{field_name} must not contain path traversal")


def _require_text(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        _fail(ResearchToolFailureCode.MISSING_REQUIRED_FIELD, f"{field_name} is required")


def _parse_dt(value: datetime | str, field_name: str) -> datetime:
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        text = value.strip()
        candidate = text[:-1] + "+00:00" if text.endswith("Z") else text
        try:
            dt = datetime.fromisoformat(candidate)
        except ValueError as exc:
            raise ResearchToolContractError(
                ResearchToolFailureCode.INVALID_TIMESTAMP,
                f"{field_name} must be ISO-8601",
                details={"field": field_name},
            ) from exc
    else:
        _fail(ResearchToolFailureCode.INVALID_TIMESTAMP, f"{field_name} must be timezone-aware")
    if dt.tzinfo is None or dt.utcoffset() is None:
        _fail(ResearchToolFailureCode.INVALID_TIMESTAMP, f"{field_name} must be timezone-aware")
    return dt.astimezone(timezone.utc)


def _time_order(left_name: str, left: datetime, right_name: str, right: datetime) -> None:
    if left > right:
        _fail(
            ResearchToolFailureCode.INVALID_TIME_ORDER,
            f"{left_name} must not be after {right_name}",
            left=left_name,
            right=right_name,
        )


def _canonical_tree(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
    if is_dataclass(value):
        return {item.name: _canonical_tree(getattr(value, item.name)) for item in fields(value)}
    if isinstance(value, Mapping):
        return {str(key): _canonical_tree(value[key]) for key in sorted(value)}
    if isinstance(value, (tuple, list)):
        return [_canonical_tree(item) for item in value]
    return value


def _iso_dt(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _assert_no_secret_or_location_text(text: str, *, field_name: str) -> None:
    if _SECRET_VALUE_RE.search(text):
        _fail(ResearchToolFailureCode.SECRET_LEAKAGE, f"{field_name} contains secret-like text")
    if _URL_RE.search(text) or _WINDOWS_PATH_RE.search(text) or _POSIX_PATH_RE.search(text) or "../" in text or "..\\" in text:
        _fail(ResearchToolFailureCode.PATH_OR_URL_LEAKAGE, f"{field_name} contains URL, path, or traversal text")


def _assert_no_forbidden_keys(value: Any, *, path: str = "payload") -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            lowered = str(key).strip().lower()
            if lowered in _FORBIDDEN_KEYS:
                _fail(
                    ResearchToolFailureCode.FORBIDDEN_AUTHORITY_FIELD,
                    "privileged authority field is not allowed in frozen research adapters",
                    path=f"{path}.{key}",
                )
            if lowered in _SECRET_OR_LOCATION_KEYS:
                _fail(
                    ResearchToolFailureCode.PATH_OR_URL_LEAKAGE,
                    "URL, file path, provider URL, or secret key is not desk-visible",
                    path=f"{path}.{key}",
                )
            _assert_no_forbidden_keys(item, path=f"{path}.{key}")
        return
    if isinstance(value, (tuple, list)):
        for index, item in enumerate(value):
            _assert_no_forbidden_keys(item, path=f"{path}[{index}]")
        return
    if isinstance(value, str):
        if _SECRET_VALUE_RE.search(value):
            _fail(ResearchToolFailureCode.SECRET_LEAKAGE, "secret-like value is forbidden", path=path)


def _assert_no_authority_text(text: str, *, field_name: str) -> None:
    if _AUTHORITY_TEXT_RE.search(text):
        _fail(
            ResearchToolFailureCode.FORBIDDEN_AUTHORITY_FIELD,
            f"{field_name} contains privileged financial authority text",
        )


def _is_retrieval_safe(text: str) -> bool:
    if detect_prompt_injection(text):
        return False
    if _AUTHORITY_TEXT_RE.search(text):
        return False
    if _SECRET_VALUE_RE.search(text) or _URL_RE.search(text) or _WINDOWS_PATH_RE.search(text) or _POSIX_PATH_RE.search(text):
        return False
    if "../" in text or "..\\" in text:
        return False
    return True


def _safe_summary(text: str, *, max_chars: int = 180) -> str:
    normalized = " ".join(text.split())
    return normalized[:max_chars]


@dataclass(frozen=True, kw_only=True)
class FrozenChunk:
    source_id: str
    chunk_index: int
    span_start: int
    span_end: int
    text: str
    chunk_hash: str
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        _require_safe_segment(self.source_id, "source_id")
        if isinstance(self.chunk_index, bool) or not isinstance(self.chunk_index, int) or self.chunk_index < 0:
            _fail(ResearchToolFailureCode.INVALID_PAYLOAD, "chunk_index must be a non-negative integer")
        if self.span_start < 0 or self.span_end <= self.span_start:
            _fail(ResearchToolFailureCode.INVALID_PAYLOAD, "chunk span is invalid")
        _require_text(self.text, "chunk.text")
        _require_hash(self.chunk_hash, "chunk_hash")


@dataclass(frozen=True, kw_only=True)
class FrozenSource:
    source_id: str
    source_class: SourceClass
    title: str
    body: str
    event_time: datetime
    published_at: datetime
    available_at: datetime
    retrieved_at: datetime
    provider: str
    chunks: tuple[FrozenChunk, ...]
    source_hash: str
    content_hash: str
    manifest_hash: str
    author: str | None = None
    institution: str | None = None
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        _require_safe_segment(self.source_id, "source_id")
        if not isinstance(self.source_class, SourceClass):
            object.__setattr__(self, "source_class", SourceClass(self.source_class))
        for field_name in ("title", "body", "provider"):
            _require_text(getattr(self, field_name), field_name)
        _require_safe_segment(self.provider, "provider")
        for field_name in ("event_time", "published_at", "available_at", "retrieved_at"):
            object.__setattr__(self, field_name, _parse_dt(getattr(self, field_name), field_name))
        _time_order("event_time", self.event_time, "published_at", self.published_at)
        _time_order("published_at", self.published_at, "available_at", self.available_at)
        _time_order("available_at", self.available_at, "retrieved_at", self.retrieved_at)
        if not self.chunks:
            _fail(ResearchToolFailureCode.INVALID_PAYLOAD, "frozen source must contain chunks")
        for chunk in self.chunks:
            if chunk.source_id != self.source_id:
                _fail(ResearchToolFailureCode.INVALID_PAYLOAD, "chunk belongs to another source")
        for field_name in ("source_hash", "content_hash", "manifest_hash"):
            _require_hash(getattr(self, field_name), field_name)

    @property
    def provenance(self) -> SourceProvenance:
        return SourceProvenance(
            event_time=self.event_time,
            published_at=self.published_at,
            available_at=self.available_at,
            retrieved_at=self.retrieved_at,
            provider=self.provider,
            source_class=self.source_class.value,
            provider_artifact_id=self.source_id,
            source_hash=self.source_hash,
            content_hash=self.content_hash,
            manifest_hash=self.manifest_hash,
        )

    def to_artifact(self) -> FrozenResearchArtifact:
        safe_chunk = next((chunk for chunk in self.chunks if _is_retrieval_safe(chunk.text)), None)
        payload = {
            "source_id": self.source_id,
            "source_class": self.source_class.value,
            "title": self.title,
            "summary": _safe_summary(safe_chunk.text) if safe_chunk is not None else "unsafe frozen source content redacted",
            "source_hash": self.source_hash,
            "chunk_count": len(self.chunks),
            "prompt_injection_chunk_count": sum(1 for chunk in self.chunks if detect_prompt_injection(chunk.text)),
        }
        return FrozenResearchArtifact(
            artifact_id=self.source_id,
            artifact_type=f"frozen_{self.source_class.value}",
            provenance=self.provenance,
            payload=payload,
            payload_hash=content_hash(payload),
        )


class FrozenSourceProvider(Protocol):
    """Read-only provider of pre-frozen sources; no network or filesystem reads."""

    def get_source(self, source_id: str) -> FrozenSource: ...

    def list_sources(self, *, source_class: SourceClass | str | None = None) -> tuple[FrozenSource, ...]: ...


class FrozenFixtureSourceProvider:
    """In-memory frozen fixture provider used by offline tests and demos."""

    def __init__(self, sources: Sequence[FrozenSource]) -> None:
        by_id: dict[str, FrozenSource] = {}
        for source in sources:
            if source.source_id in by_id:
                _fail(ResearchToolFailureCode.INVALID_PAYLOAD, "duplicate frozen source id")
            by_id[source.source_id] = source
        self._sources = dict(sorted(by_id.items()))

    def get_source(self, source_id: str) -> FrozenSource:
        _require_safe_segment(source_id, "source_id")
        source = self._sources.get(source_id)
        if source is None:
            _fail(ResearchToolFailureCode.PROVIDER_UNAVAILABLE, "frozen source is unavailable", source_id=source_id)
        return source

    def list_sources(self, *, source_class: SourceClass | str | None = None) -> tuple[FrozenSource, ...]:
        if source_class is None:
            return tuple(self._sources.values())
        expected = source_class if isinstance(source_class, SourceClass) else SourceClass(source_class)
        return tuple(source for source in self._sources.values() if source.source_class is expected)


@dataclass(frozen=True, kw_only=True)
class FrozenCitation:
    source_id: str
    source_class: SourceClass
    source_hash: str
    chunk_hash: str
    span_start: int
    span_end: int
    quote: str
    claim: str
    event_time: datetime
    published_at: datetime
    available_at: datetime
    bundle_hash: str
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        _require_safe_segment(self.source_id, "source_id")
        if not isinstance(self.source_class, SourceClass):
            object.__setattr__(self, "source_class", SourceClass(self.source_class))
        for field_name in ("source_hash", "chunk_hash", "bundle_hash"):
            _require_hash(getattr(self, field_name), field_name)
        if self.span_start < 0 or self.span_end <= self.span_start:
            _fail(ResearchToolFailureCode.INVALID_PAYLOAD, "citation span is invalid")
        for field_name in ("quote", "claim"):
            _require_text(getattr(self, field_name), field_name)
        for field_name in ("event_time", "published_at", "available_at"):
            object.__setattr__(self, field_name, _parse_dt(getattr(self, field_name), field_name))
        _assert_no_secret_or_location_text(self.quote, field_name="citation.quote")
        _assert_no_secret_or_location_text(self.claim, field_name="citation.claim")

    @property
    def bundle(self) -> CitationBundle:
        return CitationBundle(
            source_hash=self.source_hash,
            chunk_hash=self.chunk_hash,
            span_start=self.span_start,
            span_end=self.span_end,
            available_at=self.available_at,
            claim=self.claim,
        )

    def to_visible_payload(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "source_class": self.source_class.value,
            "source_hash": self.source_hash,
            "chunk_hash": self.chunk_hash,
            "span_start": self.span_start,
            "span_end": self.span_end,
            "event_time": _iso_dt(self.event_time),
            "published_at": _iso_dt(self.published_at),
            "available_at": _iso_dt(self.available_at),
            "quote": self.quote,
            "claim": self.claim,
            "bundle_hash": self.bundle_hash,
        }


@dataclass(frozen=True, kw_only=True)
class CitationValidationFinding:
    status: CitationStatus
    code: ResearchToolFailureCode | None
    message: str
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if not isinstance(self.status, CitationStatus):
            object.__setattr__(self, "status", CitationStatus(self.status))
        if self.code is not None and not isinstance(self.code, ResearchToolFailureCode):
            object.__setattr__(self, "code", ResearchToolFailureCode(self.code))
        _require_text(self.message, "message")


@dataclass(frozen=True, kw_only=True)
class CitationValidationReport:
    status: CitationStatus
    citation: FrozenCitation
    findings: tuple[CitationValidationFinding, ...]
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if not isinstance(self.status, CitationStatus):
            object.__setattr__(self, "status", CitationStatus(self.status))
        if not isinstance(self.citation, FrozenCitation):
            _fail(ResearchToolFailureCode.INVALID_PAYLOAD, "citation report needs FrozenCitation")
        if not self.findings:
            _fail(ResearchToolFailureCode.MISSING_REQUIRED_FIELD, "citation findings are required")

    @property
    def artifact_hash(self) -> str:
        return content_hash(_canonical_tree(self))


@dataclass(frozen=True, kw_only=True)
class ProxyDraft:
    proxy_id: str
    state: str
    claim: str
    proxy_expression: str
    required_fields: tuple[str, ...]
    citation_bundle_hashes: tuple[str, ...]
    rationale: str
    limitations: tuple[str, ...]
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        _require_safe_segment(self.proxy_id, "proxy_id")
        if self.state != "DRAFT":
            _fail(ResearchToolFailureCode.UNAUTHORIZED_CAPABILITY, "claim mapper can only emit DRAFT")
        _require_text(self.claim, "claim")
        _require_text(self.proxy_expression, "proxy_expression")
        _require_text(self.rationale, "rationale")
        _assert_no_authority_text(self.claim, field_name="claim")
        _assert_no_secret_or_location_text(self.claim, field_name="claim")
        if not self.required_fields:
            _fail(ResearchToolFailureCode.MISSING_REQUIRED_FIELD, "required_fields is required")
        for field_name in self.required_fields:
            if not isinstance(field_name, str) or _DRAFT_SAFE_FIELD_RE.fullmatch(field_name) is None:
                _fail(ResearchToolFailureCode.INVALID_IDENTIFIER, "required field is not FactorDAG-safe")
            lowered = field_name.lower()
            if any(term in lowered for term in ("future", "label", "return_", "sealed")):
                _fail(ResearchToolFailureCode.FORBIDDEN_AUTHORITY_FIELD, "future/sealed fields are forbidden")
        if not self.citation_bundle_hashes:
            _fail(ResearchToolFailureCode.MISSING_REQUIRED_FIELD, "citation_bundle_hashes is required")
        for item in self.citation_bundle_hashes:
            _require_hash(item, "citation_bundle_hash")
        if not self.limitations:
            _fail(ResearchToolFailureCode.MISSING_REQUIRED_FIELD, "limitations are required")

    @property
    def artifact_hash(self) -> str:
        return content_hash(_canonical_tree(self))


def build_frozen_source(
    *,
    source_id: str,
    source_class: SourceClass,
    title: str,
    body: str,
    event_time: datetime | str,
    published_at: datetime | str,
    available_at: datetime | str,
    retrieved_at: datetime | str,
    provider: str = "frozen_fixture",
    author: str | None = None,
    institution: str | None = None,
) -> FrozenSource:
    _require_safe_segment(source_id, "source_id")
    _require_text(title, "title")
    _require_text(body, "body")
    _require_safe_segment(provider, "provider")
    _assert_no_secret_or_location_text(title, field_name="title")
    event_dt = _parse_dt(event_time, "event_time")
    published_dt = _parse_dt(published_at, "published_at")
    available_dt = _parse_dt(available_at, "available_at")
    retrieved_dt = _parse_dt(retrieved_at, "retrieved_at")
    _time_order("event_time", event_dt, "published_at", published_dt)
    _time_order("published_at", published_dt, "available_at", available_dt)
    _time_order("available_at", available_dt, "retrieved_at", retrieved_dt)
    if source_class is SourceClass.DOCUMENT:
        _assert_no_authority_text(body, field_name="document body")

    body_hash = content_hash(body)
    manifest = {
        "source_id": source_id,
        "source_class": source_class.value,
        "title": title,
        "event_time": event_dt,
        "published_at": published_dt,
        "available_at": available_dt,
        "retrieved_at": retrieved_dt,
        "provider": provider,
        "author": author,
        "institution": institution,
        "body_hash": body_hash,
    }
    source_hash = content_hash({"manifest": manifest, "body": body})
    chunks = _chunk_body(source_id=source_id, source_hash=source_hash, body=body)
    return FrozenSource(
        source_id=source_id,
        source_class=source_class,
        title=title,
        body=body,
        event_time=event_dt,
        published_at=published_dt,
        available_at=available_dt,
        retrieved_at=retrieved_dt,
        provider=provider,
        chunks=chunks,
        source_hash=source_hash,
        content_hash=body_hash,
        manifest_hash=content_hash(manifest),
        author=author,
        institution=institution,
    )


def _chunk_body(*, source_id: str, source_hash: str, body: str) -> tuple[FrozenChunk, ...]:
    chunks: list[FrozenChunk] = []
    offset = 0
    for paragraph in body.split("\n\n"):
        start = body.find(paragraph, offset)
        end = start + len(paragraph)
        offset = end
        text = paragraph.strip()
        if not text:
            continue
        chunk_hash = content_hash(
            {
                "source_id": source_id,
                "source_hash": source_hash,
                "chunk_index": len(chunks),
                "text": text,
                "span_start": 0,
                "span_end": len(text),
            }
        )
        chunks.append(
            FrozenChunk(
                source_id=source_id,
                chunk_index=len(chunks),
                span_start=0,
                span_end=len(text),
                text=text,
                chunk_hash=chunk_hash,
            )
        )
    if not chunks:
        _fail(ResearchToolFailureCode.INVALID_PAYLOAD, "body produced no chunks")
    return tuple(chunks)
