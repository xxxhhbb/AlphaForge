"""Thin, content-addressed adapter over Vibe's existing read-only research tools.

The model receives Vibe's real tool schemas and may choose among them through
the normal AgentLoop.  Tool outputs are always frozen as AUXILIARY research
artifacts; a later deterministic data gateway may independently qualify a
source for official PIT evaluation.  This module never upgrades authority.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
import json
import math
import re
from typing import Any

from src.agent.tools import ToolRegistry

from ..contracts import FailureCode
from ..contracts.base import fail
from ..llm_desks.safety import canonical_hash as runtime_hash
from ..research_agent_runtime.contracts import InvestigationReceipt, ResearchAgentTask
from ..research_agent_runtime.toolbox import ResearchToolResult, ResearchToolboxProtocol
from ..research_memory.security import detect_prompt_injection
from .contracts import canonical_hash
from .handles import ContentAddressedHandleStore


# One broad research profile shared by all specialists.  Specialization belongs
# in the prompt and research plan, not in a tiny hard-coded field gate.
VIBE_READONLY_RESEARCH_TOOLS: tuple[str, ...] = tuple(sorted({
    "alpha_compare",
    "alpha_zoo",
    "get_block_trades",
    "get_dragon_tiger",
    "get_financial_statements",
    "get_fund_flow",
    "get_fundamentals",
    "get_lockup_expiry",
    "get_macro_series",
    "get_margin_trading",
    "get_market_data",
    "get_northbound_flow",
    "get_research_reports",
    "get_sector_info",
    "get_shareholder_count",
    "get_stock_news",
    "get_stock_profile",
    "screen_market",
    "search_symbol",
}))


def available_vibe_research_registry(
    tool_names: Sequence[str] | None = None,
) -> tuple[ToolRegistry, tuple[str, ...], tuple[str, ...]]:
    """Return the usable Vibe read-only subset plus explicit unavailable names.

    Optional tool dependencies must not prevent an otherwise productive Agent
    from starting. The missing names are returned for a typed data-gap receipt;
    they are never silently represented as successful tools.
    """

    requested = tuple(sorted(tool_names or VIBE_READONLY_RESEARCH_TOOLS))
    if not requested or not set(requested).issubset(VIBE_READONLY_RESEARCH_TOOLS):
        fail(FailureCode.UNAUTHORIZED_CAPABILITY, "invalid Vibe research tool selection")
    from src.tools import build_filtered_registry

    registry = build_filtered_registry(list(requested), include_shell_tools=False)
    available = tuple(sorted(registry.tool_names))
    unavailable = tuple(sorted(set(requested) - set(available)))
    return registry, available, unavailable

_NETWORK_TOOLS = frozenset({
    "get_block_trades", "get_dragon_tiger", "get_financial_statements",
    "get_fund_flow", "get_fundamentals", "get_lockup_expiry",
    "get_macro_series", "get_margin_trading", "get_market_data",
    "get_northbound_flow", "get_research_reports", "get_sector_info",
    "get_shareholder_count", "get_stock_news", "get_stock_profile",
    "screen_market", "search_symbol",
})
_FORBIDDEN_TOOL_TOKENS = (
    "account", "bash", "broker", "cancel", "edit", "env", "execute", "file",
    "ledger", "mandate", "order", "portfolio", "risk", "shell", "sql", "trade",
    "write",
)
_FORBIDDEN_KEYS = frozenset({
    "api_key", "apikey", "authorization", "cookie", "credentials", "env",
    "environment", "headers", "password", "private_key", "secret", "token",
})
_SECRET = re.compile(r"(?i)(?:authorization\s*:|bearer\s+\S+|api[_ -]?key\s*[:=]|token\s*[:=]|password\s*[:=])")
_LOCAL_PATH = re.compile(r"(?:^[A-Za-z]:[\\/]|^~[\\/]|(?:^|[\\/])\.\.(?:[\\/]|$))")


class VibeResearchToolbox(ResearchToolboxProtocol):
    """Expose a selected subset of Vibe tools with frozen, pathless receipts."""

    def __init__(
        self,
        *,
        task: ResearchAgentTask,
        handle_store: ContentAddressedHandleStore,
        tool_names: Sequence[str] | None = None,
        registry: ToolRegistry | None = None,
        allow_network: bool = False,
        clock: Callable[[], datetime] | None = None,
        preview_items: int = 5,
        max_result_bytes: int = 8_000_000,
    ) -> None:
        selected_names = tuple(sorted(tool_names if tool_names is not None else task.allowed_tools))
        if not selected_names or len(selected_names) != len(set(selected_names)):
            fail(FailureCode.INVALID_VALUE, "Vibe research tool selection must be non-empty and unique")
        if not set(selected_names).issubset(task.allowed_tools):
            fail(FailureCode.UNAUTHORIZED_CAPABILITY, "Vibe tools must be a subset of the task whitelist")
        unknown = set(selected_names) - set(VIBE_READONLY_RESEARCH_TOOLS)
        if unknown:
            fail(FailureCode.UNAUTHORIZED_CAPABILITY, "task requests a non-research Vibe tool")
        if any(any(token in name.casefold() for token in _FORBIDDEN_TOOL_TOKENS) for name in selected_names):
            fail(FailureCode.UNAUTHORIZED_CAPABILITY, "task requests a privileged Vibe capability")
        if not 0 <= preview_items <= 20:
            raise ValueError("preview_items must be 0..20")
        if not 1_024 <= max_result_bytes <= 32_000_000:
            raise ValueError("max_result_bytes must be 1024..32000000")
        if registry is None:
            registry, available, _unavailable = available_vibe_research_registry(selected_names)
            if available != selected_names:
                fail(FailureCode.ADAPTER_UNAVAILABLE, "one or more requested Vibe research tools are unavailable")
        if tuple(sorted(registry.tool_names)) != selected_names:
            fail(FailureCode.ADAPTER_UNAVAILABLE, "one or more requested Vibe research tools are unavailable")
        for name in selected_names:
            tool = registry.get(name)
            if tool is None or getattr(tool, "is_readonly", None) is not True:
                fail(FailureCode.UNAUTHORIZED_CAPABILITY, "Vibe research tools must be read-only")
        self.task = task
        self._names = selected_names
        self._registry = registry
        self._store = handle_store
        self._allow_network = bool(allow_network)
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._preview_items = preview_items
        self._max_result_bytes = max_result_bytes
        self._receipts: list[InvestigationReceipt] = []

    @property
    def names(self) -> tuple[str, ...]:
        return self._names

    @property
    def definitions(self) -> tuple[Mapping[str, object], ...]:
        definitions: list[Mapping[str, object]] = []
        for name in self.names:
            tool = self._registry.get(name)
            assert tool is not None
            schema = tool.to_openai_schema()["function"]
            parameters = dict(schema.get("parameters") or {"type": "object", "properties": {}})
            parameters.setdefault("additionalProperties", False)
            definitions.append({
                "name": name,
                "description": str(schema.get("description") or "Read research data."),
                "parameters": parameters,
                "readonly": True,
            })
        return tuple(definitions)

    @property
    def recorded_receipts(self) -> tuple[InvestigationReceipt, ...]:
        return tuple(sorted(self._receipts, key=lambda item: (item.tool_name, item.arguments_hash, item.result_hash)))

    def call(self, name: str, arguments: Mapping[str, Any]) -> ResearchToolResult:
        if name not in self.names:
            fail(FailureCode.UNAUTHORIZED_CAPABILITY, "undeclared Vibe research tool")
        if name in _NETWORK_TOOLS and not self._allow_network:
            fail(FailureCode.UNAUTHORIZED_CAPABILITY, "network research tools are disabled for this run")
        if not isinstance(arguments, Mapping):
            fail(FailureCode.INVALID_VALUE, "tool arguments must be an object")
        _reject_request_material(arguments)
        definition = next(item for item in self.definitions if item["name"] == name)
        _validate_schema(dict(arguments), definition["parameters"])
        tool = self._registry.get(name)
        assert tool is not None
        try:
            raw = tool.execute(**dict(arguments))
        except Exception:
            fail(FailureCode.ADAPTER_EXECUTION_FAILED, "Vibe research tool failed closed")
        try:
            payload = json.loads(raw) if isinstance(raw, str) else raw
        except (TypeError, json.JSONDecodeError):
            fail(FailureCode.PROVIDER_SCHEMA_REJECTED, "Vibe research tool returned non-JSON output")
        if not isinstance(payload, (Mapping, list, tuple)):
            fail(FailureCode.PROVIDER_SCHEMA_REJECTED, "Vibe research tool returned an unsupported result")
        if isinstance(payload, Mapping) and (
            payload.get("ok") is False or str(payload.get("status", "")).casefold() in {"error", "failed"}
        ):
            fail(FailureCode.ADAPTER_EXECUTION_FAILED, "Vibe research tool returned a typed failure")
        safe_payload, removed = _sanitize_result(payload)
        rendered = json.dumps(safe_payload, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))
        if len(rendered.encode("utf-8")) > self._max_result_bytes:
            fail(FailureCode.PROVIDER_BUDGET_EXCEEDED, "Vibe research tool result exceeds its size budget")
        handle = self._store.put_json(safe_payload)
        fetched_at = self._clock()
        if fetched_at.tzinfo is None or fetched_at.utcoffset() is None:
            raise ValueError("research tool clock must return a timezone-aware datetime")
        envelope = {
            "schema_version": "alphaforge.vibe-research-tool-receipt/v1",
            "authority": "AUXILIARY_RESEARCH_EVIDENCE",
            "official_eligible": False,
            "tool_name": name,
            "arguments_hash": canonical_hash(dict(arguments)),
            "result_hash": handle.handle_id,
            "fetched_at": fetched_at.astimezone(timezone.utc).isoformat(),
            "artifact": {
                "handle_id": handle.handle_id,
                "media_type": handle.media_type,
                "size_bytes": handle.size_bytes,
            },
            "preview": _preview(safe_payload, self._preview_items),
            "prompt_injection_items_removed": removed,
            "permission_delta": "NONE",
            "tools_granted": [],
        }
        # Freeze the complete sanitized envelope as well as the raw sanitized
        # payload.  The InvestigationReceipt now points to this envelope, so a
        # recovery/data-lineage Harness can deterministically recover the exact
        # payload handle without enumerating directories or trusting a trace.
        envelope_handle = self._store.put_json(envelope)
        if envelope_handle.handle_id != runtime_hash(envelope):
            raise ValueError("Vibe research envelope canonical hash mismatch")
        result = ResearchToolResult(payload=envelope, available_at=fetched_at.astimezone(timezone.utc))
        self._receipts.append(InvestigationReceipt(
            tool_name=name,
            arguments_hash=runtime_hash(dict(arguments)),
            result_hash=envelope_handle.handle_id,
            available_at=result.available_at,
        ))
        return result


def _reject_request_material(value: Any) -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            if str(key).casefold() in _FORBIDDEN_KEYS or str(key).casefold() == "run_dir":
                fail(FailureCode.UNAUTHORIZED_CAPABILITY, "secret or runtime-path tool arguments are forbidden")
            _reject_request_material(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            _reject_request_material(child)
    elif isinstance(value, str) and (_SECRET.search(value) or _LOCAL_PATH.search(value)):
        fail(FailureCode.UNAUTHORIZED_CAPABILITY, "secret-like or local-path tool argument is forbidden")


def _validate_schema(value: Any, schema: Mapping[str, Any], path: str = "arguments") -> None:
    kind = schema.get("type")
    if "enum" in schema and value not in schema["enum"]:
        fail(FailureCode.INVALID_VALUE, f"{path} is outside the declared enum")
    if kind == "object":
        if not isinstance(value, Mapping):
            fail(FailureCode.INVALID_VALUE, f"{path} must be an object")
        properties = schema.get("properties", {})
        if not isinstance(properties, Mapping):
            fail(FailureCode.INVALID_VALUE, f"{path} schema is invalid")
        if schema.get("additionalProperties") is False and set(value) - set(properties):
            fail(FailureCode.INVALID_VALUE, f"{path} contains undeclared fields")
        missing = set(schema.get("required", ())) - set(value)
        if missing:
            fail(FailureCode.MISSING_REQUIRED_FIELD, f"{path} is missing required fields")
        for key, child in value.items():
            if key in properties:
                _validate_schema(child, properties[key], f"{path}.{key}")
    elif kind == "array":
        if not isinstance(value, list):
            fail(FailureCode.INVALID_VALUE, f"{path} must be an array")
        if len(value) < int(schema.get("minItems", 0)) or len(value) > int(schema.get("maxItems", 2**31)):
            fail(FailureCode.INVALID_VALUE, f"{path} array length is invalid")
        if schema.get("uniqueItems"):
            identities = [json.dumps(item, ensure_ascii=False, sort_keys=True, default=str) for item in value]
            if len(identities) != len(set(identities)):
                fail(FailureCode.INVALID_VALUE, f"{path} items must be unique")
        for child in value:
            _validate_schema(child, schema.get("items", {}), path)
    elif kind == "string":
        if not isinstance(value, str) or len(value) > int(schema.get("maxLength", 100_000)):
            fail(FailureCode.INVALID_VALUE, f"{path} must be bounded text")
    elif kind == "integer":
        if isinstance(value, bool) or not isinstance(value, int):
            fail(FailureCode.INVALID_VALUE, f"{path} must be an integer")
        if value < schema.get("minimum", value) or value > schema.get("maximum", value):
            fail(FailureCode.INVALID_VALUE, f"{path} is outside its numeric bounds")
    elif kind == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
            fail(FailureCode.INVALID_VALUE, f"{path} must be a finite number")
    elif kind == "boolean" and not isinstance(value, bool):
        fail(FailureCode.INVALID_VALUE, f"{path} must be boolean")


def _sanitize_result(value: Any) -> tuple[Any, int]:
    removed = 0

    def visit(item: Any) -> Any:
        nonlocal removed
        if isinstance(item, Mapping):
            result = {}
            for key, child in sorted(item.items(), key=lambda pair: str(pair[0])):
                folded = str(key).casefold()
                if folded in _FORBIDDEN_KEYS or "sealed" in folded:
                    removed += 1
                    continue
                result[str(key)] = visit(child)
            return result
        if isinstance(item, (list, tuple)):
            return [visit(child) for child in item]
        if isinstance(item, float) and not math.isfinite(item):
            return None
        if isinstance(item, str):
            if _SECRET.search(item) or _LOCAL_PATH.search(item):
                removed += 1
                return "[SENSITIVE_VALUE_REDACTED]"
            if detect_prompt_injection(item):
                removed += 1
                return "[UNTRUSTED_INSTRUCTION_REDACTED]"
        if item is None or isinstance(item, (str, int, float, bool)):
            return item
        return str(item)

    return visit(value), removed


def _preview(value: Any, limit: int) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _preview(child, limit) for key, child in list(value.items())[: max(1, limit)]}
    if isinstance(value, list):
        return [_preview(child, limit) for child in value[:limit]]
    return value


__all__ = [
    "VIBE_READONLY_RESEARCH_TOOLS", "VibeResearchToolbox",
    "available_vibe_research_registry",
]
