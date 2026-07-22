"""Deterministic, code-backed catalog of research material capabilities.

The catalog is intentionally one level above the existing data and tool
implementations.  It records what AlphaForge can expose to proposal workers as
read-only research material, whether the implementation is actually importable,
which fields it can support, whether it is PIT-safe, and whether using it would
require an online provider.
"""

from __future__ import annotations

import importlib
import inspect
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Any, ClassVar, Mapping, Sequence

from ..contracts import CanonicalContract, FailureCode
from ..contracts.base import (
    canonicalize,
    fail,
    require_hash,
    require_identifier,
    require_nonempty_tuple,
    require_text,
)


CAPABILITY_CATALOG_SCHEMA_VERSION = "alphaforge.research-capability-catalog/v1"


class CapabilityAvailability(str, Enum):
    AVAILABLE = "AVAILABLE"
    ONLINE_REQUIRED = "ONLINE_REQUIRED"
    UNAVAILABLE = "UNAVAILABLE"


class CapabilitySourceClass(str, Enum):
    IMPLEMENTED_CODE = "IMPLEMENTED_CODE"
    MISSING_CODE = "MISSING_CODE"


class DataSourceLabel(str, Enum):
    APPROVED_PROXY_DATA_VENDOR = "APPROVED_PROXY_DATA_VENDOR"
    DETERMINISTIC_OFFLINE_FIXTURE = "DETERMINISTIC_OFFLINE_FIXTURE"
    VIBE_LOADER = "VIBE_LOADER"
    FROZEN_VIBE_MATERIAL = "FROZEN_VIBE_MATERIAL"
    AUXILIARY_ONLY = "AUXILIARY_ONLY"


_OFFICIAL_TUSHARE = "OFFICIAL_TUSHARE"
_SECRET_TOKENS = (
    "authorization",
    "api_key",
    "apikey",
    "access_token",
    "refresh_token",
    "token",
    "secret",
    "cookie",
)


def _text_tuple(values: Sequence[str], field: str, *, allow_empty: bool = False) -> tuple[str, ...]:
    if not isinstance(values, (tuple, list)) or (not values and not allow_empty):
        fail(FailureCode.MISSING_REQUIRED_FIELD, f"{field} must be a sequence")
    result: list[str] = []
    for item in values:
        require_identifier(str(item), field)
        result.append(str(item))
    return tuple(sorted(dict.fromkeys(result)))


def _assert_public_text(value: str, field: str) -> None:
    require_text(value, field)
    lowered = value.lower()
    if _OFFICIAL_TUSHARE.lower() in lowered:
        fail(FailureCode.AUXILIARY_MASQUERADE, "approved proxy must not be labeled official Tushare")
    if any(token in lowered for token in _SECRET_TOKENS):
        fail(FailureCode.UNAUTHORIZED_CAPABILITY, "secret-bearing text is not catalog-visible")


def _file_hash(path: Path) -> str:
    try:
        data = path.read_bytes()
    except OSError:
        fail(FailureCode.ADAPTER_UNAVAILABLE, "capability source file is unavailable")
    import hashlib

    return "sha256:" + hashlib.sha256(data).hexdigest()


@dataclass(frozen=True, kw_only=True)
class ResearchCapabilitySpec:
    """Static probe target.  Specs are not claims until probed from code."""

    capability_id: str
    title_zh: str
    module_path: str
    symbol_name: str
    tool_name: str
    provider_source_label: DataSourceLabel
    allowed_data: tuple[str, ...]
    fields: tuple[str, ...]
    pit_safe: bool
    online_required: bool
    provenance_required: bool = True
    notes_zh: str = ""

    def __post_init__(self) -> None:
        require_identifier(self.capability_id, "capability_id")
        _assert_public_text(self.title_zh, "title_zh")
        require_text(self.module_path, "module_path")
        require_identifier(self.symbol_name, "symbol_name")
        require_identifier(self.tool_name, "tool_name")
        if not isinstance(self.provider_source_label, DataSourceLabel):
            object.__setattr__(self, "provider_source_label", DataSourceLabel(self.provider_source_label))
        object.__setattr__(self, "allowed_data", _text_tuple(self.allowed_data, "allowed_data", allow_empty=True))
        object.__setattr__(self, "fields", _text_tuple(self.fields, "fields", allow_empty=True))
        for name in ("pit_safe", "online_required", "provenance_required"):
            if type(getattr(self, name)) is not bool:
                fail(FailureCode.INVALID_VALUE, f"{name} must be boolean")
        if self.notes_zh:
            _assert_public_text(self.notes_zh, "notes_zh")


@dataclass(frozen=True, kw_only=True)
class CapabilityProbe(CanonicalContract):
    SCHEMA_VERSION: ClassVar[str] = CAPABILITY_CATALOG_SCHEMA_VERSION

    module_path: str
    symbol_name: str
    implemented: bool
    source_class: CapabilitySourceClass
    source_file_hash: str | None
    reason_code: str

    def __post_init__(self) -> None:
        require_text(self.module_path, "module_path")
        require_identifier(self.symbol_name, "symbol_name")
        if type(self.implemented) is not bool:
            fail(FailureCode.INVALID_VALUE, "implemented must be boolean")
        if not isinstance(self.source_class, CapabilitySourceClass):
            object.__setattr__(self, "source_class", CapabilitySourceClass(self.source_class))
        if self.source_file_hash is not None:
            require_hash(self.source_file_hash, "source_file_hash")
        require_identifier(self.reason_code, "reason_code")
        if self.implemented and self.source_file_hash is None:
            fail(FailureCode.INVALID_HASH, "implemented probe requires source_file_hash")


@dataclass(frozen=True, kw_only=True)
class ResearchCapability(CanonicalContract):
    SCHEMA_VERSION: ClassVar[str] = CAPABILITY_CATALOG_SCHEMA_VERSION

    capability_id: str
    title_zh: str
    tool_name: str
    provider_source_label: DataSourceLabel
    availability: CapabilityAvailability
    allowed_data: tuple[str, ...]
    fields: tuple[str, ...]
    pit_safe: bool
    online_required: bool
    provenance_required: bool
    probe: CapabilityProbe
    notes_zh: str = ""

    def __post_init__(self) -> None:
        require_identifier(self.capability_id, "capability_id")
        _assert_public_text(self.title_zh, "title_zh")
        require_identifier(self.tool_name, "tool_name")
        if not isinstance(self.provider_source_label, DataSourceLabel):
            object.__setattr__(self, "provider_source_label", DataSourceLabel(self.provider_source_label))
        if self.provider_source_label.value == _OFFICIAL_TUSHARE:
            fail(FailureCode.AUXILIARY_MASQUERADE, "approved proxy must not be labeled official Tushare")
        if not isinstance(self.availability, CapabilityAvailability):
            object.__setattr__(self, "availability", CapabilityAvailability(self.availability))
        object.__setattr__(self, "allowed_data", _text_tuple(self.allowed_data, "allowed_data", allow_empty=True))
        object.__setattr__(self, "fields", _text_tuple(self.fields, "fields", allow_empty=True))
        for name in ("pit_safe", "online_required", "provenance_required"):
            if type(getattr(self, name)) is not bool:
                fail(FailureCode.INVALID_VALUE, f"{name} must be boolean")
        if not isinstance(self.probe, CapabilityProbe):
            fail(FailureCode.INVALID_VALUE, "probe must be CapabilityProbe")
        if self.availability is CapabilityAvailability.AVAILABLE and not self.probe.implemented:
            fail(FailureCode.INVALID_STATE, "available capability must have code evidence")
        if self.availability is CapabilityAvailability.AVAILABLE and self.online_required:
            fail(FailureCode.INVALID_STATE, "online-required capability cannot be available in offline catalog")
        if self.notes_zh:
            _assert_public_text(self.notes_zh, "notes_zh")

    @property
    def is_research_material_available(self) -> bool:
        return self.availability is CapabilityAvailability.AVAILABLE


@dataclass(frozen=True, kw_only=True)
class ResearchCapabilityCatalog(CanonicalContract):
    SCHEMA_VERSION: ClassVar[str] = CAPABILITY_CATALOG_SCHEMA_VERSION

    catalog_id: str
    capabilities: tuple[ResearchCapability, ...]
    offline_default: bool = True

    def __post_init__(self) -> None:
        require_identifier(self.catalog_id, "catalog_id")
        require_nonempty_tuple(self.capabilities, "capabilities")
        ordered = tuple(sorted(self.capabilities, key=lambda item: item.capability_id))
        ids = tuple(item.capability_id for item in ordered)
        if len(set(ids)) != len(ids):
            fail(FailureCode.ARTIFACT_COLLISION, "duplicate capability_id")
        if type(self.offline_default) is not bool:
            fail(FailureCode.INVALID_VALUE, "offline_default must be boolean")
        object.__setattr__(self, "capabilities", ordered)

    @property
    def by_id(self) -> Mapping[str, ResearchCapability]:
        return MappingProxyType({item.capability_id: item for item in self.capabilities})

    def require(self, capability_id: str) -> ResearchCapability:
        require_identifier(capability_id, "capability_id")
        try:
            return self.by_id[capability_id]
        except KeyError as exc:
            fail(FailureCode.ADAPTER_UNAVAILABLE, "capability is not registered", capability_id=capability_id)
            raise exc

    def available_for_data(self, allowed_data: Sequence[str]) -> tuple[ResearchCapability, ...]:
        allowed = set(_text_tuple(tuple(allowed_data), "allowed_data", allow_empty=True))
        return tuple(
            item
            for item in self.capabilities
            if item.availability is CapabilityAvailability.AVAILABLE and set(item.allowed_data) <= allowed
        )

    def to_public_manifest(self) -> dict[str, Any]:
        """Return a stable user-safe view with product language."""

        return {
            "catalog_id": self.catalog_id,
            "catalog_hash": self.artifact_hash,
            "offline_default": self.offline_default,
            "research_materials": tuple(
                {
                    "capability_id": item.capability_id,
                    "name_zh": item.title_zh,
                    "availability": item.availability.value,
                    "provider_source_label": item.provider_source_label.value,
                    "allowed_data": item.allowed_data,
                    "fields": item.fields,
                    "pit_safe": item.pit_safe,
                    "online_required": item.online_required,
                    "source_file_hash": item.probe.source_file_hash,
                    "notes_zh": item.notes_zh,
                }
                for item in self.capabilities
            ),
        }


def probe_spec(spec: ResearchCapabilitySpec, *, offline_default: bool = True) -> ResearchCapability:
    probe = _probe_import(spec.module_path, spec.symbol_name)
    if not probe.implemented:
        availability = CapabilityAvailability.UNAVAILABLE
    elif spec.online_required:
        availability = CapabilityAvailability.ONLINE_REQUIRED
    else:
        availability = CapabilityAvailability.AVAILABLE
    return ResearchCapability(
        capability_id=spec.capability_id,
        title_zh=spec.title_zh,
        tool_name=spec.tool_name,
        provider_source_label=spec.provider_source_label,
        availability=availability,
        allowed_data=spec.allowed_data,
        fields=spec.fields,
        pit_safe=spec.pit_safe,
        online_required=spec.online_required,
        provenance_required=spec.provenance_required,
        probe=probe,
        notes_zh=spec.notes_zh,
    )


def _probe_import(module_path: str, symbol_name: str) -> CapabilityProbe:
    try:
        module = importlib.import_module(module_path)
        symbol = getattr(module, symbol_name)
        source_file = inspect.getsourcefile(symbol) or getattr(module, "__file__", None)
        if not source_file:
            return CapabilityProbe(
                module_path=module_path,
                symbol_name=symbol_name,
                implemented=False,
                source_class=CapabilitySourceClass.MISSING_CODE,
                source_file_hash=None,
                reason_code="SOURCE_FILE_UNAVAILABLE",
            )
        source_hash = _file_hash(Path(source_file))
        return CapabilityProbe(
            module_path=module_path,
            symbol_name=symbol_name,
            implemented=True,
            source_class=CapabilitySourceClass.IMPLEMENTED_CODE,
            source_file_hash=source_hash,
            reason_code="IMPLEMENTED",
        )
    except Exception:
        return CapabilityProbe(
            module_path=module_path,
            symbol_name=symbol_name,
            implemented=False,
            source_class=CapabilitySourceClass.MISSING_CODE,
            source_file_hash=None,
            reason_code="IMPORT_OR_SYMBOL_UNAVAILABLE",
        )


def default_research_capability_specs() -> tuple[ResearchCapabilitySpec, ...]:
    return (
        ResearchCapabilitySpec(
            capability_id="frozen.market.summary",
            title_zh="冻结行情摘要",
            module_path="src.veriquant.vibe_market_adapters.adapters",
            symbol_name="FrozenMarketAdapter",
            tool_name="frozen_market_summary",
            provider_source_label=DataSourceLabel.FROZEN_VIBE_MATERIAL,
            allowed_data=("price_volume",),
            fields=("amount", "close", "high", "low", "open", "volume"),
            pit_safe=True,
            online_required=False,
            notes_zh="只读取已经冻结并带有可得时间的市场材料。",
        ),
        ResearchCapabilitySpec(
            capability_id="frozen.fundamentals.summary",
            title_zh="冻结基本面摘要",
            module_path="src.veriquant.vibe_market_adapters.adapters",
            symbol_name="FundamentalsResearchAdapter",
            tool_name="fundamentals_summary",
            provider_source_label=DataSourceLabel.FROZEN_VIBE_MATERIAL,
            allowed_data=("fundamentals",),
            fields=("eps", "net_profit", "roe", "revenue"),
            pit_safe=True,
            online_required=False,
            notes_zh="只读取冻结基本面材料；不能替代正式证据计算。",
        ),
        ResearchCapabilitySpec(
            capability_id="frozen.market.screener",
            title_zh="冻结股票池筛选摘要",
            module_path="src.veriquant.vibe_market_adapters.adapters",
            symbol_name="MarketScreenerResearchAdapter",
            tool_name="market_screener_summary",
            provider_source_label=DataSourceLabel.FROZEN_VIBE_MATERIAL,
            allowed_data=("price_volume",),
            fields=("market_cap", "symbol", "turnover", "volume"),
            pit_safe=True,
            online_required=False,
            notes_zh="会按冻结历史股票池过滤，不暴露未来成员。",
        ),
        ResearchCapabilitySpec(
            capability_id="visible.evidence.reader",
            title_zh="可公开验证摘要",
            module_path="src.veriquant.research_explorer_tools.tools",
            symbol_name="VisibleEvidenceToolbox",
            tool_name="query_visible_evidence",
            provider_source_label=DataSourceLabel.AUXILIARY_ONLY,
            allowed_data=(),
            fields=("cost_bps", "incrementality", "net_icir", "stability"),
            pit_safe=True,
            online_required=False,
            notes_zh="只能读取训练和验证期允许公开的摘要。",
        ),
        ResearchCapabilitySpec(
            capability_id="approved.proxy.daily",
            title_zh="批准代理日线获取",
            module_path="src.veriquant.tushare_sdk_proxy.provider",
            symbol_name="ProxyMarketDataProvider",
            tool_name="approved_proxy_daily",
            provider_source_label=DataSourceLabel.APPROVED_PROXY_DATA_VENDOR,
            allowed_data=("price_volume",),
            fields=("amount", "close", "high", "low", "open", "volume"),
            pit_safe=False,
            online_required=True,
            notes_zh="需要显式联网与凭证；来源只能标记为批准代理数据源。",
        ),
        ResearchCapabilitySpec(
            capability_id="vibe.loader.market_data",
            title_zh="Vibe 行情加载器",
            module_path="src.tools.market_data_tool",
            symbol_name="MarketDataTool",
            tool_name="get_market_data",
            provider_source_label=DataSourceLabel.VIBE_LOADER,
            allowed_data=("price_volume",),
            fields=("close", "high", "low", "open", "volume"),
            pit_safe=False,
            online_required=True,
            notes_zh="原始加载器可能联网；正式路径必须先冻结成 PIT 材料。",
        ),
    )


def build_default_research_capability_catalog(
    *,
    offline_default: bool = True,
    extra_specs: Sequence[ResearchCapabilitySpec] = (),
) -> ResearchCapabilityCatalog:
    specs = (*default_research_capability_specs(), *tuple(extra_specs))
    return ResearchCapabilityCatalog(
        catalog_id="alphaforge.research_materials.default",
        capabilities=tuple(probe_spec(spec, offline_default=offline_default) for spec in specs),
        offline_default=offline_default,
    )


def canonical_manifest_hash(manifest: Mapping[str, Any]) -> str:
    import hashlib
    import json

    return "sha256:" + hashlib.sha256(
        json.dumps(canonicalize(manifest), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
