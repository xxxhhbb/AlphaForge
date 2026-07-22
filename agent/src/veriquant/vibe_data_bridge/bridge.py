"""Read-only normalized bridge for frozen Vibe market research material."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from types import MappingProxyType
from typing import Any, ClassVar, Mapping, Sequence

from ..capability_catalog import (
    CapabilityAvailability,
    ResearchCapabilityCatalog,
    build_default_research_capability_catalog,
)
from ..contracts import CanonicalContract, FailureCode
from ..contracts.base import (
    fail,
    freeze_mapping,
    require_aware_datetime,
    require_hash,
    require_identifier,
    require_nonempty_tuple,
    require_text,
)

from ..research_tool_contracts import (
    ResearchToolFailureCode,
    ResearchToolRequest,
)
from ..vibe_market_adapters.adapters import (
    FinancialStatementsResearchAdapter,
    FrozenMarketAdapter,
    FundamentalsResearchAdapter,
    InMemoryMarketResearchProvider,
    MarketScreenerResearchAdapter,
)
from ..vibe_market_adapters.models import FrozenMarketResponse


VIBE_DATA_BRIDGE_SCHEMA_VERSION = "alphaforge.vibe-data-bridge/v1"


class VibeDataBridgeStatus(str, Enum):
    AVAILABLE = "AVAILABLE"
    UNAVAILABLE = "UNAVAILABLE"


class VibeDataBridgeFailureCode(str, Enum):
    CAPABILITY_UNAVAILABLE = "CAPABILITY_UNAVAILABLE"
    ONLINE_REQUIRED = "ONLINE_REQUIRED"
    ALLOWED_DATA_MISMATCH = "ALLOWED_DATA_MISMATCH"
    REQUIRED_FIELD_UNAVAILABLE = "REQUIRED_FIELD_UNAVAILABLE"
    PROVIDER_REJECTED = "PROVIDER_REJECTED"
    SECRET_OR_PRIVILEGED_CONTENT = "SECRET_OR_PRIVILEGED_CONTENT"
    DATA_NOT_AVAILABLE_AS_OF = "DATA_NOT_AVAILABLE_AS_OF"


_ADAPTERS: Mapping[str, type[FrozenMarketAdapter]] = {
    "frozen_market_summary": FrozenMarketAdapter,
    "fundamentals_summary": FundamentalsResearchAdapter,
    "financial_statements_summary": FinancialStatementsResearchAdapter,
    "market_screener_summary": MarketScreenerResearchAdapter,
}

_SECRET_FAILURES = {
    ResearchToolFailureCode.SECRET_LEAKAGE,
    ResearchToolFailureCode.FORBIDDEN_AUTHORITY_FIELD,
    ResearchToolFailureCode.PATH_OR_URL_LEAKAGE,
}


def _text_tuple(values: Sequence[str], field: str, *, allow_empty: bool = False) -> tuple[str, ...]:
    if not isinstance(values, (tuple, list)) or (not values and not allow_empty):
        fail(FailureCode.MISSING_REQUIRED_FIELD, f"{field} is required")
    result: list[str] = []
    for item in values:
        require_identifier(str(item), field)
        result.append(str(item))
    return tuple(sorted(dict.fromkeys(result)))


def _safe_symbol(payload: Mapping[str, Any]) -> str:
    for key in ("symbol", "code", "ticker", "ts_code"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip().upper()
    return "UNKNOWN"


def _field_values(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    excluded = {"symbol", "code", "ticker", "ts_code", "trade_date", "date", "event_time", "available_at"}
    values: dict[str, Any] = {}
    for key, value in payload.items():
        name = str(key)
        if name.lower() not in excluded:
            values[name] = value
    return MappingProxyType(dict(sorted(values.items())))


@dataclass(frozen=True, kw_only=True)
class VibeDataBridgeRequest(CanonicalContract):
    SCHEMA_VERSION: ClassVar[str] = VIBE_DATA_BRIDGE_SCHEMA_VERSION

    request_id: str
    campaign_id: str
    campaign_hash: str
    research_group_id: str
    capability_id: str
    decision_time: datetime
    allowed_data: tuple[str, ...]
    required_fields: tuple[str, ...] = ()
    frozen_universe: tuple[str, ...] = ()
    offline: bool = True

    def __post_init__(self) -> None:
        require_identifier(self.request_id, "request_id")
        require_identifier(self.campaign_id, "campaign_id")
        require_hash(self.campaign_hash, "campaign_hash")
        require_identifier(self.research_group_id, "research_group_id")
        require_identifier(self.capability_id, "capability_id")
        require_aware_datetime(self.decision_time, "decision_time")
        object.__setattr__(self, "allowed_data", _text_tuple(self.allowed_data, "allowed_data", allow_empty=True))
        object.__setattr__(self, "required_fields", _text_tuple(self.required_fields, "required_fields", allow_empty=True))
        object.__setattr__(self, "frozen_universe", _text_tuple(self.frozen_universe, "frozen_universe", allow_empty=True))
        if type(self.offline) is not bool:
            fail(FailureCode.INVALID_VALUE, "offline must be boolean")


@dataclass(frozen=True, kw_only=True)
class VibeDataRow(CanonicalContract):
    SCHEMA_VERSION: ClassVar[str] = VIBE_DATA_BRIDGE_SCHEMA_VERSION

    row_id: str
    symbol: str
    provider_source_label: str
    source_class: str
    event_time: datetime
    published_at: datetime
    available_at: datetime
    retrieved_at: datetime
    fields: Mapping[str, Any]
    source_hash: str
    content_hash: str
    manifest_hash: str

    def __post_init__(self) -> None:
        require_identifier(self.row_id, "row_id")
        require_identifier(self.symbol, "symbol")
        require_identifier(self.provider_source_label, "provider_source_label")
        if "OFFICIAL_TUSHARE" in self.provider_source_label:
            fail(FailureCode.AUXILIARY_MASQUERADE, "approved proxy must not be labeled official Tushare")
        require_identifier(self.source_class, "source_class")
        for name in ("event_time", "published_at", "available_at", "retrieved_at"):
            require_aware_datetime(getattr(self, name), name)
        if self.event_time > self.published_at or self.published_at > self.available_at or self.available_at > self.retrieved_at:
            fail(FailureCode.INVALID_TIME_ORDER, "row time order is invalid")
        object.__setattr__(self, "fields", freeze_mapping(self.fields, "fields"))
        for field_name in ("source_hash", "content_hash", "manifest_hash"):
            require_hash(getattr(self, field_name), field_name)


@dataclass(frozen=True, kw_only=True)
class VibeDataBridgeResult(CanonicalContract):
    SCHEMA_VERSION: ClassVar[str] = VIBE_DATA_BRIDGE_SCHEMA_VERSION

    request_hash: str
    status: VibeDataBridgeStatus
    provider_source_label: str
    rows: tuple[VibeDataRow, ...] = ()
    coverage_by_field: Mapping[str, float] = field(default_factory=dict)
    failure_code: VibeDataBridgeFailureCode | None = None
    message_zh: str = ""

    def __post_init__(self) -> None:
        require_hash(self.request_hash, "request_hash")
        if not isinstance(self.status, VibeDataBridgeStatus):
            object.__setattr__(self, "status", VibeDataBridgeStatus(self.status))
        require_identifier(self.provider_source_label, "provider_source_label")
        if "OFFICIAL_TUSHARE" in self.provider_source_label:
            fail(FailureCode.AUXILIARY_MASQUERADE, "approved proxy must not be labeled official Tushare")
        if not isinstance(self.rows, tuple):
            fail(FailureCode.INVALID_VALUE, "rows must be tuple")
        coverage: dict[str, float] = {}
        for key, value in self.coverage_by_field.items():
            require_identifier(key, "coverage_by_field")
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0.0 <= float(value) <= 1.0:
                fail(FailureCode.INVALID_VALUE, "coverage must be in [0, 1]")
            coverage[key] = float(value)
        object.__setattr__(self, "coverage_by_field", MappingProxyType(dict(sorted(coverage.items()))))
        if self.failure_code is not None and not isinstance(self.failure_code, VibeDataBridgeFailureCode):
            object.__setattr__(self, "failure_code", VibeDataBridgeFailureCode(self.failure_code))
        if self.status is VibeDataBridgeStatus.AVAILABLE:
            if not self.rows or self.failure_code is not None:
                fail(FailureCode.INVALID_STATE, "available bridge result requires rows and no failure")
        elif self.failure_code is None:
            fail(FailureCode.INVALID_STATE, "unavailable bridge result requires typed failure")
        if self.message_zh:
            require_text(self.message_zh, "message_zh")
            lowered = self.message_zh.lower()
            if any(token in lowered for token in ("token", "api_key", "authorization", "sealed", "promotion", "order", "broker")):
                fail(FailureCode.UNAUTHORIZED_CAPABILITY, "message contains forbidden content")


class NormalizedVibeDataBridge:
    """Normalize frozen provider material into research-visible rows."""

    def __init__(self, catalog: ResearchCapabilityCatalog | None = None) -> None:
        self.catalog = catalog or build_default_research_capability_catalog()

    def query(
        self,
        request: VibeDataBridgeRequest,
        frozen_response: FrozenMarketResponse | Mapping[str, Any],
    ) -> VibeDataBridgeResult:
        capability = self.catalog.require(request.capability_id)
        provider_label = capability.provider_source_label.value
        if capability.availability is CapabilityAvailability.UNAVAILABLE:
            return self._unavailable(request, provider_label, VibeDataBridgeFailureCode.CAPABILITY_UNAVAILABLE, "研究材料能力不可用")
        if capability.online_required and request.offline:
            return self._unavailable(request, provider_label, VibeDataBridgeFailureCode.ONLINE_REQUIRED, "该材料需要显式在线获取")
        if capability.availability is not CapabilityAvailability.AVAILABLE:
            return self._unavailable(request, provider_label, VibeDataBridgeFailureCode.CAPABILITY_UNAVAILABLE, "研究材料当前不可离线使用")
        if set(capability.allowed_data) - set(request.allowed_data):
            return self._unavailable(request, provider_label, VibeDataBridgeFailureCode.ALLOWED_DATA_MISMATCH, "研究授权数据范围不足")
        adapter_cls = _ADAPTERS.get(capability.tool_name)
        if adapter_cls is None:
            return self._unavailable(request, provider_label, VibeDataBridgeFailureCode.CAPABILITY_UNAVAILABLE, "该能力尚无冻结材料桥")
        tool_request = ResearchToolRequest(
            campaign_id=request.campaign_id,
            campaign_hash=request.campaign_hash,
            desk_id=request.research_group_id,
            decision_time=request.decision_time,
            allowed_data=request.allowed_data,
            tool_name=capability.tool_name,
            request_id=request.request_id,
            offline=request.offline,
        )
        adapter = adapter_cls(
            InMemoryMarketResearchProvider(provider_id="frozen_vibe_material", response=frozen_response),
            frozen_universe=request.frozen_universe,
        )
        result = adapter.run(tool_request, query={})
        if result.failure_code is not None:
            return self._unavailable(
                request,
                provider_label,
                self._map_failure(result.failure_code),
                "冻结材料未通过可见性或时间检查",
            )
        rows = tuple(
            self._row_from_artifact(
                artifact,
                provider_source_label=provider_label,
                decision_time=request.decision_time,
            )
            for artifact in result.artifacts
        )
        missing = tuple(field for field in request.required_fields if not any(field in row.fields for row in rows))
        if missing:
            return self._unavailable(
                request,
                provider_label,
                VibeDataBridgeFailureCode.REQUIRED_FIELD_UNAVAILABLE,
                "冻结材料缺少必要字段",
            )
        coverage = self._coverage(rows, capability.fields or request.required_fields)
        return VibeDataBridgeResult(
            request_hash=request.artifact_hash,
            status=VibeDataBridgeStatus.AVAILABLE,
            provider_source_label=provider_label,
            rows=rows,
            coverage_by_field=coverage,
            message_zh="冻结研究材料已标准化",
        )

    def _row_from_artifact(self, artifact: Any, *, provider_source_label: str, decision_time: datetime) -> VibeDataRow:
        provenance = artifact.provenance
        if provenance.available_at > decision_time:
            fail(FailureCode.INVALID_TIME_ORDER, "artifact is not available as of decision_time")
        payload = dict(artifact.payload)
        return VibeDataRow(
            row_id=artifact.artifact_id,
            symbol=_safe_symbol(payload),
            provider_source_label=provider_source_label,
            source_class=provenance.source_class,
            event_time=provenance.event_time,
            published_at=provenance.published_at,
            available_at=provenance.available_at,
            retrieved_at=provenance.retrieved_at,
            fields=_field_values(payload),
            source_hash=provenance.source_hash,
            content_hash=provenance.content_hash,
            manifest_hash=provenance.manifest_hash,
        )

    def _coverage(self, rows: tuple[VibeDataRow, ...], fields: Sequence[str]) -> Mapping[str, float]:
        wanted = tuple(field for field in fields if field)
        if not rows or not wanted:
            return MappingProxyType({})
        result: dict[str, float] = {}
        for field_name in wanted:
            present = sum(1 for row in rows if field_name in row.fields and row.fields[field_name] is not None)
            result[field_name] = present / len(rows)
        return MappingProxyType(dict(sorted(result.items())))

    def _map_failure(self, code: ResearchToolFailureCode) -> VibeDataBridgeFailureCode:
        if code is ResearchToolFailureCode.DATA_NOT_AVAILABLE_AS_OF:
            return VibeDataBridgeFailureCode.DATA_NOT_AVAILABLE_AS_OF
        if code is ResearchToolFailureCode.ALLOWED_DATA_MISMATCH:
            return VibeDataBridgeFailureCode.ALLOWED_DATA_MISMATCH
        if code in _SECRET_FAILURES:
            return VibeDataBridgeFailureCode.SECRET_OR_PRIVILEGED_CONTENT
        return VibeDataBridgeFailureCode.PROVIDER_REJECTED

    def _unavailable(
        self,
        request: VibeDataBridgeRequest,
        provider_label: str,
        failure: VibeDataBridgeFailureCode,
        message: str,
    ) -> VibeDataBridgeResult:
        return VibeDataBridgeResult(
            request_hash=request.artifact_hash,
            status=VibeDataBridgeStatus.UNAVAILABLE,
            provider_source_label=provider_label,
            failure_code=failure,
            message_zh=message,
        )
