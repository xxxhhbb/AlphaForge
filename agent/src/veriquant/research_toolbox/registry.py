"""Static, non-executable toolbox and data capability registry."""

from __future__ import annotations

from dataclasses import dataclass
import re
from types import MappingProxyType
from typing import Any, Mapping


# All specialists receive the same broad, read-only research surface. Their
# expertise comes from the prompt and plan, not from arbitrary data silos. The
# adapter below still enforces frozen identities, PIT clocks, no labels/sealed
# data and no write/trading capability for every call.
BROAD_READONLY_RESEARCH_TOOLS: tuple[str, ...] = tuple(sorted({
    "describe_available_data", "load_asof_financial_data", "load_asof_market_data",
    "load_event_data", "load_macro_data", "load_moneyflow_data", "query_data_schema",
    "query_dragon_tiger", "query_factor_library", "query_frozen_documents",
    "query_northbound", "query_portfolio_summary", "query_registered_factors",
    "query_research_reports", "query_stock_news", "query_visible_evidence",
}))

SPECIALIST_TOOL_ALLOWLISTS: Mapping[str, tuple[str, ...]] = MappingProxyType({
    specialist_id: BROAD_READONLY_RESEARCH_TOOLS
    for specialist_id in (
        "alternative_data", "expectations_events", "fundamental",
        "macro_regime", "market_structure", "meta_alpha",
    )
})


OFFICIAL_DATA_IDS = frozenset({
    "adj_factor", "balancesheet", "cashflow", "daily", "daily_basic", "express",
    "fina_indicator", "forecast", "income", "index_daily", "moneyflow", "shibor",
    "stock_basic", "trade_cal",
})
AUXILIARY_DATA_IDS = frozenset({
    "documents", "dragon_tiger", "northbound", "research_reports", "stock_news",
})
REGISTERED_CONTEXT_DATA_IDS = frozenset({
    "portfolio_returns", "registered_evidence", "registered_factor_summary",
})
REQUIRED_DATA_IDS = OFFICIAL_DATA_IDS | AUXILIARY_DATA_IDS | REGISTERED_CONTEXT_DATA_IDS

REQUIRED_DATA_TO_TOOL: Mapping[str, str] = MappingProxyType({
    "adj_factor": "load_asof_market_data",
    "balancesheet": "load_asof_financial_data",
    "cashflow": "load_asof_financial_data",
    "daily": "load_asof_market_data",
    "daily_basic": "load_asof_market_data",
    "documents": "query_frozen_documents",
    "dragon_tiger": "query_dragon_tiger",
    "express": "load_event_data",
    "fina_indicator": "load_asof_financial_data",
    "forecast": "load_event_data",
    "income": "load_asof_financial_data",
    "index_daily": "load_macro_data",
    "moneyflow": "load_moneyflow_data",
    "northbound": "query_northbound",
    "portfolio_returns": "query_portfolio_summary",
    "registered_evidence": "query_visible_evidence",
    "registered_factor_summary": "query_registered_factors",
    "research_reports": "query_research_reports",
    "shibor": "load_macro_data",
    "stock_basic": "load_asof_market_data",
    "stock_news": "query_stock_news",
    "trade_cal": "load_asof_market_data",
})

_FORBIDDEN_KEYS = frozenset({
    "api_key", "apikey", "authorization", "base_url", "cookie", "endpoint",
    "file", "file_path", "filename", "password", "path", "secret", "token", "url",
})
_PATHISH = re.compile(r"(?:^[A-Za-z]:[\\/]|^/|^~[\\/]|(?:^|[\\/])\.\.(?:[\\/]|$))")
_SECRET_TEXT = re.compile(r"(?i)(?:bearer\s+\S+|authorization\s*:|api[_-]?key\s*[:=]|token\s*[:=])")


@dataclass(frozen=True, slots=True)
class AuthorizedToolCall:
    specialist_id: str
    tool_id: str
    parameters: Mapping[str, Any]


class ResearchToolRegistry:
    """Authorize names and inert parameters; it never executes a Vibe tool."""

    def tools_for(self, specialist_id: str) -> tuple[str, ...]:
        try:
            return SPECIALIST_TOOL_ALLOWLISTS[specialist_id]
        except KeyError as exc:
            raise ValueError("unknown specialist") from exc

    def authorize(
        self,
        *,
        specialist_id: str,
        tool_id: str,
        parameters: Mapping[str, Any] | None = None,
    ) -> AuthorizedToolCall:
        if tool_id not in self.tools_for(specialist_id):
            raise PermissionError("tool is not declared for this specialist")
        if not isinstance(parameters or {}, Mapping):
            raise ValueError("tool parameters must be a mapping")
        clean = dict(parameters or {})
        _reject_unsafe(clean)
        return AuthorizedToolCall(specialist_id, tool_id, MappingProxyType(clean))


def validate_required_data(values: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    result = tuple(sorted(set(values)))
    unknown = set(result) - REQUIRED_DATA_IDS
    if unknown:
        raise ValueError("required_data contains an unmapped capability")
    return result


def _reject_unsafe(value: Any) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if str(key).casefold() in _FORBIDDEN_KEYS:
                raise PermissionError("secret, endpoint and path parameters are forbidden")
            _reject_unsafe(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _reject_unsafe(item)
    elif isinstance(value, str) and (_PATHISH.search(value) or _SECRET_TEXT.search(value)):
        raise PermissionError("path-shaped or secret-like parameter value is forbidden")


__all__ = [
    "AUXILIARY_DATA_IDS", "AuthorizedToolCall", "BROAD_READONLY_RESEARCH_TOOLS", "OFFICIAL_DATA_IDS",
    "REGISTERED_CONTEXT_DATA_IDS", "REQUIRED_DATA_IDS", "REQUIRED_DATA_TO_TOOL",
    "ResearchToolRegistry", "SPECIALIST_TOOL_ALLOWLISTS", "validate_required_data",
]
