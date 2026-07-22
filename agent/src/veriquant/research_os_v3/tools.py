"""Frozen, specialist-scoped read-only research tools for Research OS V3.

This module projects approved frozen artifacts into redacted summaries.  It
does not fetch data, calculate evidence, expose sealed material, or grant any
promotion/downstream authority.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Any, ClassVar, Mapping

from ..contracts import CanonicalContract, FailureCode
from ..contracts.base import fail, freeze_mapping, require_aware_datetime, require_hash, require_identifier, require_text
from ..dynamic_acquisition.models import stable_content_hash
from ..factor_library import build_factor_library_catalog
from ..llm_desks.safety import canonical_hash
from ..llm_desks.tools import ReadOnlyTool
from ..specialist_research import RestrictedResearchToolRegistry, SpecialistAgentProfile


SCHEMA_VERSION = "alphaforge.research-tools-v3/v1"
APPROVED_SOURCE_LABEL = "APPROVED_PROXY_DATA_VENDOR"


class ResearchCapabilityStatus(str, Enum):
    AVAILABLE = "AVAILABLE"
    DATA_GAP = "DATA_GAP"
    CAPABILITY_GAP = "CAPABILITY_GAP"


_API_FIELD_ALIASES: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        "daily": ("open", "high", "low", "close", "volume", "amount"),
        "daily_basic": ("turnover_rate", "volume_ratio", "pe", "pb", "market_cap", "circ_market_cap"),
        "adj_factor": ("adj_factor",),
        "moneyflow": ("moneyflow_net", "buy_amount", "sell_amount"),
        "stock_basic": ("industry", "list_date", "delist_date"),
        "trade_cal": ("trade_date",),
        "fina_indicator": ("roe", "roa", "gross_margin", "debt_to_assets"),
        "income": ("revenue", "operating_profit", "net_income"),
        "balancesheet": ("total_assets", "total_liabilities", "equity"),
        "cashflow": ("operating_cashflow", "free_cashflow_proxy"),
        "forecast": ("earnings_forecast",),
        "express": ("earnings_surprise", "announcement_return_anchor"),
        "shibor": ("shibor_overnight", "shibor_1w", "shibor_1m"),
        "index_daily": ("benchmark_close", "benchmark_return", "market_breadth_proxy"),
    }
)

_SPECIALIST_REQUIREMENTS: Mapping[str, tuple[tuple[str, ...], str]] = MappingProxyType(
    {
        "fundamental": (("fina_indicator", "income", "balancesheet", "cashflow"), "fundamental statements and availability"),
        "expectations_events": (("forecast", "express"), "event or expectations records with time semantics"),
        "market_structure": (("daily", "daily_basic"), "price, volume, liquidity and cost inputs"),
        "alternative_data": (("documents", "entity_mapping"), "approved documents and entity mapping"),
        "macro_regime": (("shibor", "index_daily"), "decision-time macro and market-state inputs"),
        "meta_alpha": (("factor_catalog", "visible_evidence"), "factor catalog and visible evidence"),
    }
)

# These specialists can do useful, honest work when any one approved scope is
# available. Candidate-level field and PIT checks remain fail-closed later.
# Requiring every financial/event/macro table at branch start incorrectly
# blocked valuation research when daily_basic was available, for example.
_SPECIALIST_ANY_OF_REQUIREMENTS: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        "fundamental": ("daily_basic", "fina_indicator", "income", "balancesheet", "cashflow"),
        "expectations_events": ("forecast", "express"),
        "macro_regime": ("shibor", "index_daily"),
    }
)


@dataclass(frozen=True)
class FrozenResearchContext(CanonicalContract):
    context_id: str
    source_label: str
    official_tushare: bool
    decision_time: datetime
    data_start: str
    data_end: str
    dataset_manifest_hash: str
    calendar_hash: str
    universe_hash: str
    api_statuses: Mapping[str, str]
    api_row_counts: Mapping[str, int]
    available_fields: tuple[str, ...]
    field_coverage: Mapping[str, float]
    factor_catalog_hash: str
    factor_catalog_count: int
    visible_evidence_hashes: tuple[str, ...]
    SCHEMA_VERSION: ClassVar[str] = SCHEMA_VERSION

    def __post_init__(self) -> None:
        require_identifier(self.context_id, "context_id")
        require_text(self.source_label, "source_label")
        if self.official_tushare is not False or "OFFICIAL_TUSHARE" in self.source_label.upper():
            fail(FailureCode.INVALID_VALUE, "research context cannot claim official Tushare provenance")
        require_aware_datetime(self.decision_time, "decision_time")
        require_text(self.data_start, "data_start")
        require_text(self.data_end, "data_end")
        for name in ("dataset_manifest_hash", "calendar_hash", "universe_hash", "factor_catalog_hash"):
            require_hash(getattr(self, name), name)
        object.__setattr__(self, "api_statuses", freeze_mapping(self.api_statuses, "api_statuses"))
        object.__setattr__(self, "api_row_counts", freeze_mapping(self.api_row_counts, "api_row_counts"))
        object.__setattr__(self, "field_coverage", freeze_mapping(self.field_coverage, "field_coverage"))
        if not self.available_fields or len(self.available_fields) != len(set(self.available_fields)):
            fail(FailureCode.INVALID_VALUE, "available_fields must be unique and non-empty")
        for field in self.available_fields:
            require_identifier(field, "available_fields")
            if any(token in field.lower() for token in ("future", "label", "target_return")):
                fail(FailureCode.UNAUTHORIZED_CAPABILITY, "future labels are not research-visible")
        if isinstance(self.factor_catalog_count, bool) or self.factor_catalog_count < 0:
            fail(FailureCode.INVALID_VALUE, "factor_catalog_count must be non-negative")
        for item in self.visible_evidence_hashes:
            require_hash(item, "visible_evidence_hashes")

    def capability(self, specialist_id: str) -> tuple[ResearchCapabilityStatus, tuple[str, ...]]:
        requirements = _SPECIALIST_REQUIREMENTS.get(specialist_id)
        if requirements is None:
            fail(FailureCode.INVALID_IDENTIFIER, "unknown specialist capability")
        required, _ = requirements
        any_of = _SPECIALIST_ANY_OF_REQUIREMENTS.get(specialist_id)
        if any_of is not None:
            available = tuple(
                item
                for item in any_of
                if self.api_statuses.get(item) == "SUCCESS" and self.api_row_counts.get(item, 0) > 0
            )
            if available:
                return ResearchCapabilityStatus.AVAILABLE, ()
            return ResearchCapabilityStatus.DATA_GAP, any_of
        missing: list[str] = []
        for item in required:
            if item == "factor_catalog" and self.factor_catalog_count:
                continue
            if item == "visible_evidence" and self.visible_evidence_hashes:
                continue
            if self.api_statuses.get(item) == "SUCCESS" and self.api_row_counts.get(item, 0) > 0:
                continue
            missing.append(item)
        return (ResearchCapabilityStatus.AVAILABLE, ()) if not missing else (ResearchCapabilityStatus.DATA_GAP, tuple(missing))


def load_frozen_research_context(
    manifest_path: str | Path,
    *,
    decision_time: datetime,
    visible_evidence_path: str | Path | None = None,
) -> FrozenResearchContext:
    """Load explicit frozen artifacts and verify their claimed content hashes."""

    path = Path(manifest_path).resolve(strict=True)
    payload = json.loads(path.read_text(encoding="utf-8"))
    claimed = payload.pop("artifact_hash", None)
    if claimed != stable_content_hash(payload):
        fail(FailureCode.INVALID_HASH, "dynamic freeze manifest hash mismatch")
    payload["artifact_hash"] = claimed
    if payload.get("status") != "SUCCESS":
        fail(FailureCode.INVALID_STATE, "dynamic freeze is not complete")
    source_label = str(payload.get("source_label") or "").strip()
    if not source_label or payload.get("official_tushare") is not False or "OFFICIAL_TUSHARE" in source_label.upper():
        fail(FailureCode.INVALID_VALUE, "research context source is missing or claims official Tushare provenance")

    statuses = {str(key): str(value) for key, value in payload.get("api_statuses", {}).items()}
    counts = {str(key): int(value) for key, value in payload.get("api_row_counts", {}).items()}
    fields = sorted({field for api, aliases in _API_FIELD_ALIASES.items() if statuses.get(api) == "SUCCESS" and counts.get(api, 0) > 0 for field in aliases})
    total = max(1, max(counts.values(), default=1))
    coverage = {field: min(1.0, counts[api] / total) for api, aliases in _API_FIELD_ALIASES.items() if statuses.get(api) == "SUCCESS" and counts.get(api, 0) > 0 for field in aliases}

    catalog = build_factor_library_catalog()
    visible_hashes: tuple[str, ...] = ()
    data_start = str(payload.get("requested_start_date", "unknown"))
    data_end = str(payload.get("requested_end_date", "unknown"))
    universe_hash = str(payload.get("universe_reference_hash"))
    if visible_evidence_path is not None:
        evidence = json.loads(Path(visible_evidence_path).resolve(strict=True).read_text(encoding="utf-8"))
        source = evidence.get("official_evidence", {})
        visible_hashes = tuple(sorted({str(item[key]) for item in source.get("factors", ()) for key in ("train_evidence_hash", "validation_evidence_hash") if item.get(key)}))
        snapshot = evidence.get("snapshot", {})
        data_start = str(snapshot.get("data_start", data_start))
        data_end = str(snapshot.get("data_end", data_end))
        universe_hash = str(snapshot.get("universe_hash", universe_hash))

    return FrozenResearchContext(
        context_id="approved-proxy-frozen-research",
        source_label=source_label,
        official_tushare=False,
        decision_time=decision_time,
        data_start=data_start,
        data_end=data_end,
        dataset_manifest_hash=claimed,
        calendar_hash=str(payload["calendar_hash"]),
        universe_hash=universe_hash,
        api_statuses=statuses,
        api_row_counts=counts,
        available_fields=tuple(fields),
        field_coverage=coverage,
        factor_catalog_hash=catalog.catalog_hash,
        factor_catalog_count=catalog.total_records,
        visible_evidence_hashes=visible_hashes,
    )


def build_frozen_research_registry(
    profile: SpecialistAgentProfile,
    *,
    context: FrozenResearchContext,
) -> RestrictedResearchToolRegistry:
    """Build the exact specialist whitelist over one immutable context."""

    schema = {"type": "object", "properties": {"query": {"type": "string"}}, "required": ("query",), "additionalProperties": False}
    status, missing = context.capability(profile.specialist_id)
    catalog = build_factor_library_catalog()
    factor_language = (
        "field, lag, rolling_mean, rolling_std, pct_change, subtract, add, multiply, "
        "divide_safe, winsorize, rank_cross_section, zscore_cross_section, neutralize_by_group"
    )

    def handler(name: str):
        def call(arguments: Mapping[str, Any]) -> Mapping[str, Any]:
            suffix = name.split(".", 1)[1]
            if suffix == "describe_factor_language":
                summary = factor_language
                item_status = "AVAILABLE"
                item_missing: tuple[str, ...] = ()
                coverage = 1.0
            elif suffix in {"query_factor_library", "query_registered_factor_similarity"}:
                records = catalog.records[:8]
                summary = "; ".join(f"{item.factor_id}:{item.qualification_status.value}" for item in records)
                item_status = "AVAILABLE"
                item_missing = ()
                coverage = 1.0 if records else 0.0
            elif suffix in {"query_visible_evidence", "query_visible_exposure_gap"}:
                summary = f"visible evidence references={len(context.visible_evidence_hashes)}; train/validation summaries only"
                item_status = "AVAILABLE" if context.visible_evidence_hashes else "DATA_GAP"
                item_missing = () if context.visible_evidence_hashes else ("visible_evidence",)
                coverage = 1.0 if context.visible_evidence_hashes else 0.0
            else:
                summary = (
                    f"source={context.source_label}; range={context.data_start}..{context.data_end}; "
                    f"fields={','.join(context.available_fields)}; query={arguments['query'][:120]}"
                )
                item_status = status.value
                item_missing = missing
                coverage = min(context.field_coverage.values(), default=0.0)
            material = {"context_hash": context.artifact_hash, "tool": name, "query": arguments["query"], "status": item_status, "missing": item_missing}
            return {
                "summary": summary,
                "coverage": coverage,
                "source_hash": canonical_hash(material),
                "available_at": context.decision_time.isoformat(),
                "status": item_status,
                "missing_fields": ",".join(item_missing),
            }
        return call

    tools = tuple(
        ReadOnlyTool(
            name=name,
            description="Read a redacted summary from approved frozen research artifacts.",
            input_schema=schema,
            result_allowlist=("summary", "coverage", "source_hash", "available_at", "status", "missing_fields"),
            handler=handler(name),
        )
        for name in profile.required_tool_names
    )
    return RestrictedResearchToolRegistry(profile=profile, tools=tools)


__all__ = [
    "APPROVED_SOURCE_LABEL",
    "FrozenResearchContext",
    "ResearchCapabilityStatus",
    "build_frozen_research_registry",
    "load_frozen_research_context",
]
