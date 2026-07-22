from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol
from urllib.parse import urlsplit

from .models import (
    FailureCode,
    GatewayResult,
    PublishedAtStatus,
    ResearchMemoryError,
    SourceKind,
    SourceRequest,
)


class SourceGateway(Protocol):
    def fetch(self, request: SourceRequest) -> GatewayResult:
        ...


def _published_status(metadata: dict[str, Any]) -> PublishedAtStatus:
    raw = str(metadata.get("published_at_status") or "KNOWN").strip().upper()
    if raw == PublishedAtStatus.UNKNOWN.value:
        return PublishedAtStatus.UNKNOWN
    return PublishedAtStatus.KNOWN


def _gateway_result_from_metadata(
    request: SourceRequest,
    *,
    body: str,
    locator_kind: str,
) -> GatewayResult:
    metadata = dict(request.metadata)
    source_url = request.locator if locator_kind == "url" else metadata.get("source_url")
    local_locator = request.locator if locator_kind == "local" else metadata.get("local_locator")
    return GatewayResult(
        source_kind=request.source_kind,
        locator=request.locator,
        body=body,
        title=str(metadata.get("title") or Path(request.locator).name or request.locator),
        author=metadata.get("author"),
        institution=metadata.get("institution"),
        published_at=metadata.get("published_at"),
        published_at_status=_published_status(metadata),
        retrieved_at=request.retrieved_at,
        available_at=request.available_at,
        language=str(metadata.get("language") or "en"),
        license_note=str(metadata.get("license_note") or "unspecified"),
        parser_version=str(metadata.get("parser_version") or "research-memory-parser/v1"),
        source_url=str(source_url) if source_url else None,
        local_locator=str(local_locator) if local_locator else None,
    )


@dataclass
class LocalDocumentGateway:
    allowed_roots: tuple[Path, ...]

    def __init__(self, allowed_roots: tuple[str | Path, ...]) -> None:
        if not allowed_roots:
            raise ResearchMemoryError(FailureCode.INVALID_SOURCE.value, "allowed_roots is required")
        self.allowed_roots = tuple(Path(root).resolve() for root in allowed_roots)

    def fetch(self, request: SourceRequest) -> GatewayResult:
        if request.source_kind is not SourceKind.LOCAL_DOCUMENT:
            raise ResearchMemoryError(FailureCode.INVALID_SOURCE.value, "LocalDocumentGateway requires LOCAL_DOCUMENT")
        path = Path(request.locator).resolve()
        if not any(_is_relative_to(path, root) for root in self.allowed_roots):
            raise ResearchMemoryError(FailureCode.INVALID_SOURCE.value, "local document escapes allowed roots")
        if not path.is_file():
            raise ResearchMemoryError(FailureCode.DOCUMENT_NOT_FOUND.value, f"document not found: {request.locator}")
        body = path.read_text(encoding=str(request.metadata.get("encoding") or "utf-8"))
        return _gateway_result_from_metadata(request, body=body, locator_kind="local")


@dataclass
class FrozenWebFixtureGateway:
    fixtures: dict[str, GatewayResult | dict[str, Any]]

    def fetch(self, request: SourceRequest) -> GatewayResult:
        if request.source_kind is not SourceKind.FROZEN_WEB_FIXTURE:
            raise ResearchMemoryError(FailureCode.INVALID_SOURCE.value, "FrozenWebFixtureGateway requires FROZEN_WEB_FIXTURE")
        fixture = self.fixtures.get(request.locator)
        if fixture is None:
            raise ResearchMemoryError(FailureCode.DOCUMENT_NOT_FOUND.value, "frozen fixture not found")
        if isinstance(fixture, GatewayResult):
            return fixture
        body = str(fixture.get("body") or "")
        metadata = dict(fixture)
        metadata.pop("body", None)
        merged = dict(request.metadata)
        merged.update(metadata)
        return _gateway_result_from_metadata(
            SourceRequest(
                source_kind=request.source_kind,
                locator=request.locator,
                retrieved_at=request.retrieved_at,
                available_at=request.available_at,
                metadata=merged,
            ),
            body=body,
            locator_kind="url",
        )


VibeToolExecutor = Callable[[str, dict[str, Any]], str]


@dataclass
class OptionalVibeReadOnlyGateway:
    enable_research_network: str = ""
    source_allowlist: tuple[str, ...] = ()
    network_budget: int = 0
    timeout_seconds: float = 0.0
    vibe_executor: VibeToolExecutor | None = None
    calls_used: int = 0

    def fetch(self, request: SourceRequest) -> GatewayResult:
        if request.source_kind is not SourceKind.OPTIONAL_VIBE_READONLY:
            raise ResearchMemoryError(FailureCode.INVALID_SOURCE.value, "OptionalVibeReadOnlyGateway requires OPTIONAL_VIBE_READONLY")
        self._check_enabled(request.locator)
        assert self.vibe_executor is not None
        self.calls_used += 1
        raw = self.vibe_executor(
            "read_url",
            {
                "url": request.locator,
                "no_cache": True,
                "timeout_seconds": self.timeout_seconds,
            },
        )
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ResearchMemoryError(FailureCode.INVALID_SOURCE.value, "Vibe tool returned non-JSON") from exc
        if payload.get("status") != "ok":
            raise ResearchMemoryError(
                FailureCode.INVALID_SOURCE.value,
                f"Vibe tool failed: {payload.get('error', 'unknown error')}",
            )
        body = str(payload.get("content") or "")
        metadata = dict(request.metadata)
        metadata.setdefault("title", payload.get("title") or request.locator)
        metadata.setdefault("source_url", request.locator)
        return _gateway_result_from_metadata(request, body=body, locator_kind="url")

    def _check_enabled(self, locator: str) -> None:
        if self.enable_research_network != "1":
            raise ResearchMemoryError(FailureCode.NETWORK_DISABLED.value, "ENABLE_RESEARCH_NETWORK=1 was not explicitly passed")
        if self.timeout_seconds <= 0:
            raise ResearchMemoryError(FailureCode.NETWORK_DISABLED.value, "positive timeout is required")
        if self.network_budget <= 0 or self.calls_used >= self.network_budget:
            raise ResearchMemoryError(FailureCode.NETWORK_BUDGET_EXCEEDED.value, "network budget exhausted")
        if self.vibe_executor is None:
            raise ResearchMemoryError(FailureCode.NETWORK_DISABLED.value, "no Vibe read-only executor was supplied")
        if not self.source_allowlist:
            raise ResearchMemoryError(FailureCode.SOURCE_NOT_ALLOWLISTED.value, "source allowlist is required")
        parsed = urlsplit(locator)
        host = (parsed.hostname or "").lower()
        allowed = {item.lower().strip() for item in self.source_allowlist if item.strip()}
        if host not in allowed and locator.lower() not in allowed:
            raise ResearchMemoryError(FailureCode.SOURCE_NOT_ALLOWLISTED.value, "source is not allowlisted")


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False
