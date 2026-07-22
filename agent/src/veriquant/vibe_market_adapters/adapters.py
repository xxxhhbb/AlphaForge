"""Adapters that wrap Vibe market-data style outputs as frozen research tools.

The classes in this module do not call the original network-backed Vibe tools.
They consume frozen provider responses, strip privileged surfaces, and emit
desk-visible ResearchToolResult envelopes only.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping, Protocol, Sequence

from ..research_tool_contracts import (
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

from .models import FrozenMarketRecord, FrozenMarketResponse, canonical_time, normalize_datetime


_SAFE_ID_RE = re.compile(r"[^A-Za-z0-9._:/-]+")
_FORBIDDEN_PAYLOAD_KEYS = {
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
    "official",
    "official_evidence",
    "official_metrics",
    "sealed",
    "sealed_metrics",
    "promotion",
    "promotion_decision",
    "risk",
    "risk_approval",
    "target_weights",
    "weights",
    "orders",
    "order",
    "ledger",
    "fills",
    "broker",
}
_SECRET_OR_LOCATION_TEXT = re.compile(
    r"(?i)(https?://|ftp://|[a-z]:\\|\\\\|(?:^|[\s'\"(])(?:\.\./|/[^/\s]+/)|"
    r"authorization\s*[:=]|bearer\s+[a-z0-9._\-]{8,}|api[_-]?key\s*[:=]|"
    r"token\s*[:=]|secret\s*[:=])"
)


class MarketResearchProvider(Protocol):
    """Provider Protocol used by frozen market adapters."""

    provider_id: str

    def fetch_market_research(
        self,
        request: ResearchToolRequest,
        query: Mapping[str, Any],
    ) -> FrozenMarketResponse | Mapping[str, Any]:
        """Return frozen rows or a raw response that can be frozen locally."""


@dataclass
class InMemoryMarketResearchProvider:
    """Deterministic fake provider for offline tests and local demos."""

    provider_id: str
    response: FrozenMarketResponse | Mapping[str, Any]
    calls: list[tuple[str, Mapping[str, Any]]] = field(default_factory=list)

    def fetch_market_research(
        self,
        request: ResearchToolRequest,
        query: Mapping[str, Any],
    ) -> FrozenMarketResponse | Mapping[str, Any]:
        self.calls.append((request.request_id, dict(query)))
        return self.response


def _safe_id(value: Any, fallback: str = "item") -> str:
    text = _SAFE_ID_RE.sub("_", str(value or fallback).strip())
    text = text.strip("._:/-")
    return text[:180] or fallback


def _failure_result(
    request: ResearchToolRequest,
    *,
    adapter_id: str,
    capability: ResearchToolCapability,
    visibility: ResearchToolVisibility,
    code: ResearchToolFailureCode,
) -> ResearchToolResult:
    payload = {
        "adapter": adapter_id,
        "status": "unavailable",
        "reason_code": code.value,
    }
    result_id = _safe_id(f"{adapter_id}:{request.request_id}:{code.value.lower()}")
    return ResearchToolResult(
        request=request,
        result_id=result_id,
        visibility=visibility,
        capability=capability,
        payload=payload,
        failure_code=code,
        message="typed unavailable",
    )


def _visible_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _visible_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_visible_value(item) for item in value]
    if isinstance(value, datetime):
        return canonical_time(value)
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return str(value)


def _assert_visible_payload(value: Any) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            normalized = str(key).lower()
            if normalized in _FORBIDDEN_PAYLOAD_KEYS:
                raise ResearchToolContractError(
                    ResearchToolFailureCode.SECRET_LEAKAGE
                    if normalized in {"authorization", "auth", "api_key", "apikey", "access_token", "refresh_token", "token", "secret", "cookie", "headers", "provider_url", "url", "uri", "path", "filepath", "file_path"}
                    else ResearchToolFailureCode.FORBIDDEN_AUTHORITY_FIELD,
                    "provider row contains a non-desk-visible field",
                )
            _assert_visible_payload(item)
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            _assert_visible_payload(item)
        return
    if isinstance(value, str) and _SECRET_OR_LOCATION_TEXT.search(value):
        raise ResearchToolContractError(
            ResearchToolFailureCode.SECRET_LEAKAGE,
            "provider row contains a secret-like or location-like value",
        )


def _coerce_frozen_response(
    raw: FrozenMarketResponse | Mapping[str, Any],
    *,
    default_provider: str,
    default_source_class: str,
) -> FrozenMarketResponse:
    if isinstance(raw, FrozenMarketResponse):
        return raw
    if not isinstance(raw, Mapping):
        raise ResearchToolContractError(ResearchToolFailureCode.PROVIDER_UNAVAILABLE, "provider response is invalid")

    provider = _safe_id(raw.get("provider") or default_provider, "provider")
    source_class = _safe_id(raw.get("source_class") or default_source_class, default_source_class)
    manifest = raw.get("manifest") if isinstance(raw.get("manifest"), Mapping) else {}
    rows = raw.get("records")
    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
        raise ResearchToolContractError(ResearchToolFailureCode.INVALID_PAYLOAD, "provider records must be a list")

    records: list[FrozenMarketRecord] = []
    manifest_hash = content_hash(manifest)
    for index, row in enumerate(rows):
        if not isinstance(row, Mapping):
            raise ResearchToolContractError(ResearchToolFailureCode.INVALID_PAYLOAD, "provider record must be an object")
        payload = row.get("payload")
        if not isinstance(payload, Mapping):
            raise ResearchToolContractError(ResearchToolFailureCode.INVALID_PAYLOAD, "provider payload must be an object")
        visible_payload = _visible_value(payload)
        _assert_visible_payload(visible_payload)
        source_payload = row.get("source_payload")
        if not isinstance(source_payload, Mapping):
            source_payload = dict(row)
        records.append(
            FrozenMarketRecord(
                record_id=_safe_id(row.get("record_id") or row.get("id") or index, f"record_{index}"),
                provider=provider,
                source_class=source_class,
                event_time=normalize_datetime(row.get("event_time"), "event_time"),
                published_at=normalize_datetime(row.get("published_at"), "published_at"),
                available_at=normalize_datetime(row.get("available_at"), "available_at"),
                retrieved_at=normalize_datetime(row.get("retrieved_at"), "retrieved_at"),
                payload=visible_payload,
                source_payload=source_payload,
                manifest_hash=manifest_hash,
            )
        )
    return FrozenMarketResponse(provider=provider, source_class=source_class, records=tuple(records), manifest=manifest)


def _payload_symbol(payload: Mapping[str, Any]) -> str | None:
    for key in ("symbol", "code", "ticker", "ts_code"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip().upper()
    return None


class FrozenMarketAdapter:
    """Convert frozen provider rows into O0 ResearchToolResult envelopes."""

    adapter_id = "frozen_market_summary"
    source_class = "market_research"
    artifact_type = "market_research"
    capability = ResearchToolCapability.READ_FROZEN_DATA
    default_visibility = ResearchToolVisibility.DRAFT_SUPPORT
    required_allowed_data: tuple[str, ...] = ()
    filter_to_frozen_universe = False

    def __init__(
        self,
        provider: MarketResearchProvider,
        *,
        frozen_universe: Sequence[str] = (),
    ) -> None:
        self.provider = provider
        self.frozen_universe = tuple(sorted({item.strip().upper() for item in frozen_universe if item.strip()}))

    def run(self, request: ResearchToolRequest, query: Mapping[str, Any] | None = None) -> ResearchToolResult:
        query = dict(query or {})
        visibility = self._visibility_for(query)
        if request.tool_name != self.adapter_id:
            return _failure_result(
                request,
                adapter_id=self.adapter_id,
                capability=self.capability,
                visibility=visibility,
                code=ResearchToolFailureCode.UNAUTHORIZED_TOOL,
            )
        if set(self.required_allowed_data) - set(request.allowed_data):
            return _failure_result(
                request,
                adapter_id=self.adapter_id,
                capability=self.capability,
                visibility=visibility,
                code=ResearchToolFailureCode.ALLOWED_DATA_MISMATCH,
            )
        try:
            fetched = self.provider.fetch_market_research(request, query)
            response = _coerce_frozen_response(
                fetched,
                default_provider=getattr(self.provider, "provider_id", "provider"),
                default_source_class=self.source_class,
            )
            records, excluded = self._select_records(response.records, request)
            artifacts = tuple(self._to_artifact(record, response.manifest_hash) for record in records)
        except ResearchToolContractError as exc:
            return _failure_result(
                request,
                adapter_id=self.adapter_id,
                capability=self.capability,
                visibility=visibility,
                code=exc.code,
            )
        except Exception:
            return _failure_result(
                request,
                adapter_id=self.adapter_id,
                capability=self.capability,
                visibility=visibility,
                code=ResearchToolFailureCode.PROVIDER_UNAVAILABLE,
            )

        payload = {
            "adapter": self.adapter_id,
            "status": "ok",
            "source_class": self.source_class,
            "visibility": visibility.value,
            "row_count": len(artifacts),
            "excluded_universe_count": excluded,
            "artifact_hashes": tuple(artifact.artifact_hash for artifact in artifacts),
            "rows": tuple(artifact.payload for artifact in artifacts),
        }
        result_id = _safe_id(f"{self.adapter_id}:{request.request_id}:{content_hash(payload)[7:23]}")
        return ResearchToolResult(
            request=request,
            result_id=result_id,
            visibility=visibility,
            capability=self.capability,
            payload=payload,
            artifacts=artifacts,
        )

    def _visibility_for(self, query: Mapping[str, Any]) -> ResearchToolVisibility:
        return self.default_visibility

    def _select_records(
        self,
        records: Sequence[FrozenMarketRecord],
        request: ResearchToolRequest,
    ) -> tuple[tuple[FrozenMarketRecord, ...], int]:
        if any(record.available_at > request.decision_time for record in records):
            raise ResearchToolContractError(
                ResearchToolFailureCode.DATA_NOT_AVAILABLE_AS_OF,
                "provider response contains rows unavailable at decision_time",
            )
        selected = list(records)
        excluded = 0
        if self.filter_to_frozen_universe:
            if not self.frozen_universe:
                raise ResearchToolContractError(
                    ResearchToolFailureCode.MISSING_REQUIRED_FIELD,
                    "market screener requires a frozen universe",
                )
            allowed = set(self.frozen_universe)
            kept: list[FrozenMarketRecord] = []
            for record in selected:
                symbol = _payload_symbol(record.payload)
                if symbol is None or symbol in allowed:
                    kept.append(record)
                else:
                    excluded += 1
            selected = kept
        selected.sort(key=lambda record: (record.available_at, record.payload_hash, record.record_id))
        return tuple(selected), excluded

    def _to_artifact(self, record: FrozenMarketRecord, response_manifest_hash: str) -> FrozenResearchArtifact:
        manifest_hash = record.manifest_hash or response_manifest_hash
        provenance = SourceProvenance(
            event_time=record.event_time,
            published_at=record.published_at,
            available_at=record.available_at,
            retrieved_at=record.retrieved_at,
            provider=_safe_id(record.provider, "provider"),
            source_class=_safe_id(record.source_class, self.source_class),
            provider_artifact_id=_safe_id(record.record_id, "record"),
            source_hash=record.source_hash,
            content_hash=record.payload_hash,
            manifest_hash=manifest_hash,
        )
        artifact_id = _safe_id(f"{self.adapter_id}:{record.record_id}:{record.payload_hash[7:19]}")
        return FrozenResearchArtifact(
            artifact_id=artifact_id,
            artifact_type=self.artifact_type,
            provenance=provenance,
            payload=record.payload,
            payload_hash=record.payload_hash,
        )


class FundamentalsResearchAdapter(FrozenMarketAdapter):
    adapter_id = "fundamentals_summary"
    source_class = "fundamental_record"
    artifact_type = "fundamental_summary"
    required_allowed_data = ("fundamentals",)


class FinancialStatementsResearchAdapter(FrozenMarketAdapter):
    adapter_id = "financial_statements_summary"
    source_class = "financial_statement"
    artifact_type = "financial_statement_summary"
    required_allowed_data = ("fundamentals",)


class StockProfileResearchAdapter(FrozenMarketAdapter):
    adapter_id = "stock_profile_summary"
    source_class = "stock_profile"
    artifact_type = "stock_profile_summary"
    required_allowed_data = ("fundamentals",)


class MarketScreenerResearchAdapter(FrozenMarketAdapter):
    adapter_id = "market_screener_summary"
    source_class = "market_screener"
    artifact_type = "market_screener_summary"
    required_allowed_data = ("price_volume",)
    filter_to_frozen_universe = True


class ShareholderCountResearchAdapter(FrozenMarketAdapter):
    adapter_id = "shareholder_count_summary"
    source_class = "shareholder_count"
    artifact_type = "shareholder_count_summary"
    required_allowed_data = ("fundamentals",)


class LockupEventResearchAdapter(FrozenMarketAdapter):
    adapter_id = "lockup_event_summary"
    source_class = "lockup_event"
    artifact_type = "lockup_event_summary"
    required_allowed_data = ("events",)


class MarginResearchAdapter(FrozenMarketAdapter):
    adapter_id = "margin_summary"
    source_class = "margin_trading"
    artifact_type = "margin_summary"
    required_allowed_data = ("market_structure",)


class NorthboundResearchAdapter(FrozenMarketAdapter):
    adapter_id = "northbound_summary"
    source_class = "northbound_flow"
    artifact_type = "northbound_summary"
    required_allowed_data = ("market_structure",)


class FundFlowResearchAdapter(FrozenMarketAdapter):
    adapter_id = "fund_flow_summary"
    source_class = "fund_flow"
    artifact_type = "fund_flow_summary"
    required_allowed_data = ("market_structure",)

    def _visibility_for(self, query: Mapping[str, Any]) -> ResearchToolVisibility:
        if str(query.get("period", "")).lower() == "min":
            return ResearchToolVisibility.AUXILIARY_RESEARCH_EVIDENCE
        return self.default_visibility


class DragonTigerResearchAdapter(FrozenMarketAdapter):
    adapter_id = "dragon_tiger_summary"
    source_class = "dragon_tiger"
    artifact_type = "dragon_tiger_summary"
    required_allowed_data = ("events", "market_structure")


class BlockTradeResearchAdapter(FrozenMarketAdapter):
    adapter_id = "block_trade_summary"
    source_class = "block_trade"
    artifact_type = "block_trade_summary"
    required_allowed_data = ("events", "market_structure")


class FinancialRigorAdapter(FrozenMarketAdapter):
    adapter_id = "financial_rigor_lint"
    source_class = "financial_audit"
    artifact_type = "financial_audit_summary"
    capability = ResearchToolCapability.LINT_DRAFT
    required_allowed_data = ("fundamentals",)
