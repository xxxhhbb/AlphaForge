"""Least-privilege adapter from frozen data to ``ResearchToolboxProtocol``.

Trusted integration code loads frozen rows and manifests.  This reader never
constructs Vibe's global ToolRegistry, accesses a caller path, reads the
environment, performs network I/O, or grants Evidence/trading authority.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timezone
import json
import re
from time import monotonic
from types import MappingProxyType
from typing import Any, Mapping, Sequence

from ..contracts import FailureCode
from ..contracts.base import fail
from ..llm_desks.safety import canonical_hash as runtime_hash
from ..research_agent_runtime.contracts import InvestigationReceipt, ResearchAgentTask
from ..research_agent_runtime.toolbox import ResearchToolResult, ResearchToolboxProtocol
from ..research_data_gateway.models import DataAuthority, GatewayStatus, ResearchDataGatewayManifest
from ..research_memory.security import detect_prompt_injection
from .contracts import ReadOnlyArtifactHandle, ResearchAuthority, canonical_hash
from .handles import ContentAddressedHandleStore
from .registry import AUXILIARY_DATA_IDS, OFFICIAL_DATA_IDS, REGISTERED_CONTEXT_DATA_IDS, ResearchToolRegistry


_GROUPS = MappingProxyType({
    "load_asof_market_data": frozenset({"adj_factor", "daily", "daily_basic", "stock_basic", "trade_cal"}),
    "load_asof_financial_data": frozenset({"balancesheet", "cashflow", "fina_indicator", "income"}),
    "load_event_data": frozenset({"express", "forecast"}),
    "load_macro_data": frozenset({"index_daily", "shibor"}),
    "load_moneyflow_data": frozenset({"moneyflow"}),
})
_AUX_TO_DATA = MappingProxyType({
    "query_dragon_tiger": "dragon_tiger", "query_frozen_documents": "documents",
    "query_northbound": "northbound", "query_research_reports": "research_reports",
    "query_stock_news": "stock_news",
})
_CONTEXT_TO_DATA = MappingProxyType({
    "query_factor_library": "registered_factor_summary",
    "query_registered_factors": "registered_factor_summary",
    "query_visible_evidence": "registered_evidence",
    "query_portfolio_summary": "portfolio_returns",
})
_ID = re.compile(r"^[A-Za-z0-9_.:-]+$")
_HASH = re.compile(r"^sha256:[0-9a-f]{64}$")
_PATH = re.compile(r"(?:^[A-Za-z]:[\\/]|^/|^~[\\/]|(?:^|[\\/])\.\.(?:[\\/]|$))")
_SECRET = re.compile(r"(?i)(?:authorization\s*:|bearer\s+\S+|api[_-]?key\s*[:=]|token\s*[:=]|password\s*[:=])")
_FORBIDDEN_KEYS = frozenset({
    "api_key", "apikey", "authorization", "broker", "cookie", "endpoint", "env",
    "environment", "file", "filename", "ledger", "order", "password", "path",
    "promotion", "risk_approval", "sealed", "secret", "token", "url",
})
_LABEL_KEYS = frozenset({"future_return", "label", "target", "y"})


@dataclass(frozen=True, slots=True)
class FrozenResearchArtifact:
    data_id: str
    authority: ResearchAuthority
    content_hash: str
    available_at: datetime
    payload: Any
    status: str = "READY"

    def __post_init__(self) -> None:
        if not _ID.fullmatch(self.data_id):
            raise ValueError("data_id must be a non-path identifier")
        if not isinstance(self.authority, ResearchAuthority):
            raise ValueError("invalid research authority")
        if not _HASH.fullmatch(self.content_hash) or canonical_hash(self.payload) != self.content_hash:
            raise ValueError("frozen artifact content hash mismatch")
        if self.available_at.tzinfo is None or self.available_at.utcoffset() is None:
            raise ValueError("available_at must be timezone-aware")
        if self.status not in {"READY", "SUCCESS", "EMPTY", "UNAVAILABLE"}:
            raise ValueError("invalid frozen artifact status")
        _reject_privileged(self.payload)


class ControlledResearchToolbox(ResearchToolboxProtocol):
    """A deterministic toolbox bound to one DRAFT-only specialist task."""

    def __init__(
        self, *, task: ResearchAgentTask, gateway_manifest: ResearchDataGatewayManifest,
        official_rows: Mapping[str, Sequence[Mapping[str, Any]]],
        handle_store: ContentAddressedHandleStore,
        auxiliary_artifacts: Mapping[str, FrozenResearchArtifact] | None = None,
        registered_artifacts: Mapping[str, FrozenResearchArtifact] | None = None,
        max_rows: int = 10_000, max_result_bytes: int = 2_000_000,
        call_timeout_seconds: float = 5.0, preview_rows: int = 5,
    ) -> None:
        if gateway_manifest.status is not GatewayStatus.READY:
            fail(FailureCode.INCOMPLETE_RESULT, "frozen Gateway manifest is not READY")
        if gateway_manifest.source_label != "APPROVED_PROXY_DATA_VENDOR" or gateway_manifest.official_tushare:
            fail(FailureCode.UNAUTHORIZED_CAPABILITY, "unapproved data source identity")
        if not gateway_manifest.official_freeze_hash or not gateway_manifest.campaign_dataset_hash:
            fail(FailureCode.INCOMPLETE_RESULT, "frozen provenance is incomplete")
        declared = set(ResearchToolRegistry().tools_for(task.specialist_id))
        if not set(task.allowed_tools).issubset(declared):
            fail(FailureCode.UNAUTHORIZED_CAPABILITY, "task grants an undeclared specialist tool")
        if isinstance(max_rows, bool) or not 1 <= max_rows <= 100_000:
            raise ValueError("max_rows must be 1..100000")
        if isinstance(max_result_bytes, bool) or not 1_024 <= max_result_bytes <= 32_000_000:
            raise ValueError("max_result_bytes must be 1024..32000000")
        if not 0.01 <= float(call_timeout_seconds) <= 60.0 or not 0 <= preview_rows <= 20:
            raise ValueError("invalid toolbox resource budget")
        self.task = task
        self.gateway_manifest = gateway_manifest
        self._store = handle_store
        self._max_rows, self._max_bytes = max_rows, max_result_bytes
        self._timeout, self._preview = float(call_timeout_seconds), preview_rows
        self._receipts: list[InvestigationReceipt] = []
        self._rows = self._verify_rows(official_rows)
        self._aux = self._verify_artifacts(auxiliary_artifacts or {}, ResearchAuthority.AUXILIARY_RESEARCH_EVIDENCE)
        self._context = self._verify_artifacts(registered_artifacts or {}, ResearchAuthority.REGISTERED_VISIBLE_CONTEXT)

    @property
    def names(self) -> tuple[str, ...]:
        return self.task.allowed_tools

    @property
    def definitions(self) -> tuple[Mapping[str, object], ...]:
        return tuple(_definition(name) for name in self.names)

    @property
    def recorded_receipts(self) -> tuple[InvestigationReceipt, ...]:
        return tuple(sorted(self._receipts, key=lambda item: (item.tool_name, item.arguments_hash)))

    def call(self, name: str, arguments: Mapping[str, Any]) -> ResearchToolResult:
        started = monotonic()
        if name not in self.names:
            fail(FailureCode.UNAUTHORIZED_CAPABILITY, "undeclared research tool")
        if not isinstance(arguments, Mapping):
            fail(FailureCode.INVALID_VALUE, "tool arguments must be an object")
        _reject_privileged(arguments)
        definition = _definition(name)
        _validate(dict(arguments), definition["parameters"])
        try:
            if name == "describe_available_data":
                result = self._describe(arguments)
            elif name == "query_data_schema":
                result = self._schema(arguments)
            elif name in _GROUPS:
                result = self._load(name, arguments)
            elif name in _AUX_TO_DATA:
                result = self._aux_receipt(name, _AUX_TO_DATA[name])
            elif name in _CONTEXT_TO_DATA:
                result = self._visible_context(name, arguments, _CONTEXT_TO_DATA[name])
            else:
                fail(FailureCode.UNAUTHORIZED_CAPABILITY, "tool adapter is not registered")
        except (PermissionError, ValueError):
            raise
        except Exception:
            fail(FailureCode.ADAPTER_EXECUTION_FAILED, "research tool failed closed")
        if monotonic() - started > self._timeout:
            fail(FailureCode.PROVIDER_TIMEOUT, "research tool exceeded its fixed time budget")
        rendered = json.dumps(result.payload, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"), default=str)
        if len(rendered.encode("utf-8")) > self._max_bytes:
            fail(FailureCode.PROVIDER_BUDGET_EXCEEDED, "research tool result exceeds its size budget")
        _reject_privileged(result.payload)
        if result.available_at > self.task.decision_time:
            fail(FailureCode.INVALID_TIME_ORDER, "research result was unavailable at decision time")
        self._receipts.append(InvestigationReceipt(
            tool_name=name, arguments_hash=runtime_hash(dict(arguments)),
            result_hash=runtime_hash(dict(result.payload)), available_at=result.available_at,
        ))
        return result

    def _verify_rows(self, values: Mapping[str, Sequence[Mapping[str, Any]]]) -> Mapping[str, tuple[Mapping[str, Any], ...]]:
        receipts = {item.source_id: item for item in self.gateway_manifest.receipts}
        output: dict[str, tuple[Mapping[str, Any], ...]] = {}
        for data_id, rows in values.items():
            receipt = receipts.get(data_id)
            if data_id not in OFFICIAL_DATA_IDS or receipt is None or receipt.authority is not DataAuthority.OFFICIAL_INPUT or receipt.status not in {"READY", "SUCCESS"} or not receipt.content_hash:
                raise ValueError("official rows lack a successful Gateway receipt")
            normalized = tuple(_stable_row(item) for item in rows)
            if canonical_hash([dict(item) for item in normalized]) != receipt.content_hash:
                raise ValueError("official rows do not match Gateway receipt")
            output[data_id] = normalized
        return MappingProxyType(output)

    @staticmethod
    def _verify_artifacts(values: Mapping[str, FrozenResearchArtifact], authority: ResearchAuthority) -> Mapping[str, FrozenResearchArtifact]:
        allowed = AUXILIARY_DATA_IDS if authority is ResearchAuthority.AUXILIARY_RESEARCH_EVIDENCE else REGISTERED_CONTEXT_DATA_IDS
        output = {}
        for data_id, artifact in values.items():
            if data_id != artifact.data_id or artifact.authority is not authority or data_id not in allowed:
                raise ValueError("frozen artifact identity or authority mismatch")
            output[data_id] = artifact
        return MappingProxyType(output)

    def _describe(self, arguments: Mapping[str, Any]) -> ResearchToolResult:
        requested = arguments.get("data_ids") or sorted(set(self._rows) | set(self._aux) | set(self._context))
        entries = []
        for data_id in requested:
            if data_id in self._rows:
                receipt = self._gateway_receipt(data_id)
                entries.append({"data_id": data_id, "authority": "OFFICIAL_INPUT", "status": "READY", "row_count": len(self._rows[data_id]), "content_hash": receipt.content_hash})
            elif data_id in self._aux or data_id in self._context:
                artifact = self._aux[data_id] if data_id in self._aux else self._context[data_id]
                entries.append({"data_id": data_id, "authority": artifact.authority.value, "status": artifact.status, "content_hash": artifact.content_hash})
            else:
                entries.append({"data_id": data_id, "authority": "NONE", "status": "UNAVAILABLE"})
        return ResearchToolResult(self._envelope("describe_available_data", entries, "MIXED_READ_ONLY"), self.task.decision_time)

    def _schema(self, arguments: Mapping[str, Any]) -> ResearchToolResult:
        data_id = str(arguments["data_id"])
        rows = self._rows.get(data_id, ())
        body = {"data_id": data_id, "fields": sorted({str(key) for row in rows for key in row}), "row_count": len(rows)}
        authority = "OFFICIAL_INPUT" if data_id in self._rows else "UNAVAILABLE"
        return ResearchToolResult(self._envelope("query_data_schema", body, authority), self.task.decision_time)

    def _load(self, name: str, arguments: Mapping[str, Any]) -> ResearchToolResult:
        data_ids = tuple(arguments["data_ids"])
        if not set(data_ids).issubset(_GROUPS[name]):
            fail(FailureCode.UNAUTHORIZED_CAPABILITY, "incompatible data source for tool")
        limit = min(int(arguments.get("limit", self._max_rows)), self._max_rows)
        symbols, fields = set(arguments.get("symbols", ())), set(arguments.get("fields", ()))
        start = _parse_time(arguments["start_time"]) if arguments.get("start_time") else None
        end = _parse_time(arguments["end_time"]) if arguments.get("end_time") else self.task.decision_time
        if end > self.task.decision_time or (start and start > end):
            fail(FailureCode.INVALID_TIME_ORDER, "invalid as-of interval")
        collected, source_hashes = [], []
        for data_id in data_ids:
            if data_id not in self._rows:
                fail(FailureCode.INCOMPLETE_RESULT, "requested frozen source is unavailable")
            source_hashes.append(str(self._gateway_receipt(data_id).content_hash))
            for row in self._rows[data_id]:
                available = _available_at(row)
                event = _event_time(row, available)
                symbol = str(row.get("symbol") or row.get("ts_code") or "")
                if available > self.task.decision_time or event > end or (start and event < start) or (symbols and symbol not in symbols):
                    continue
                clean = dict(row)
                clean["data_id"] = data_id
                if fields:
                    clean = {key: value for key, value in clean.items() if key in fields | {"data_id", "available_at"}}
                _reject_labels(clean)
                collected.append(clean)
                if len(collected) >= limit:
                    break
            if len(collected) >= limit:
                break
        collected.sort(key=_sort_key)
        handle = self._store.put_json(collected)
        body = {"artifact": _handle(handle), "data_ids": list(data_ids), "row_count": len(collected), "source_hashes": sorted(source_hashes), "preview": collected[: self._preview], "truncated": len(collected) >= limit}
        latest = max((_available_at(row) for row in collected), default=self.task.decision_time)
        return ResearchToolResult(self._envelope(name, body, "OFFICIAL_INPUT"), latest)

    def _aux_receipt(self, name: str, data_id: str) -> ResearchToolResult:
        artifact = self._aux.get(data_id)
        if artifact is None or artifact.status not in {"READY", "SUCCESS"}:
            fail(FailureCode.INCOMPLETE_RESULT, "frozen auxiliary receipt is unavailable")
        if artifact.available_at > self.task.decision_time:
            fail(FailureCode.INVALID_TIME_ORDER, "auxiliary receipt is from the future")
        visible, removed = _sanitize_untrusted_auxiliary(artifact.payload)
        receipt = {"data_id": data_id, "authority": artifact.authority.value, "content_hash": artifact.content_hash, "status": artifact.status, "available_at": artifact.available_at.isoformat()}
        visible_handle = self._store.put_json(visible)
        body = {
            "receipt": receipt,
            "artifact": _handle(visible_handle),
            "preview": visible[: self._preview] if isinstance(visible, list) else visible,
            "prompt_injection_items_removed": removed,
            "permission_delta": "NONE",
            "tools_granted": [],
        }
        return ResearchToolResult(self._envelope(name, body, artifact.authority.value), artifact.available_at)

    def _visible_context(self, name: str, arguments: Mapping[str, Any], data_id: str) -> ResearchToolResult:
        artifact = self._context.get(data_id)
        if artifact is None or artifact.status not in {"READY", "SUCCESS"}:
            fail(FailureCode.INCOMPLETE_RESULT, "registered visible context is unavailable")
        if artifact.available_at > self.task.decision_time:
            fail(FailureCode.INVALID_TIME_ORDER, "registered context is from the future")
        _reject_privileged(artifact.payload)
        records = artifact.payload if isinstance(artifact.payload, list) else [artifact.payload]
        query, limit = str(arguments.get("query", "")).casefold(), int(arguments.get("limit", 100))
        selected = [item for item in records if not query or query in json.dumps(item, ensure_ascii=False, sort_keys=True).casefold()][:limit]
        body = {"data_id": data_id, "artifact": _handle(self._store.put_json(selected)), "row_count": len(selected), "preview": selected[: self._preview], "source_hash": artifact.content_hash}
        return ResearchToolResult(self._envelope(name, body, artifact.authority.value), artifact.available_at)

    def _gateway_receipt(self, data_id: str):
        return next(item for item in self.gateway_manifest.receipts if item.source_id == data_id)

    def _envelope(self, tool: str, result: Any, authority: str) -> dict[str, Any]:
        return {"schema_version": "alphaforge.controlled-research-toolbox/v1", "tool_id": tool, "authority": authority, "gateway_manifest_hash": self.gateway_manifest.artifact_hash, "official_freeze_hash": self.gateway_manifest.official_freeze_hash, "campaign_dataset_hash": self.gateway_manifest.campaign_dataset_hash, "source_label": self.gateway_manifest.source_label, "decision_time": self.task.decision_time.isoformat(), "result": result}


def _definition(name: str) -> Mapping[str, object]:
    base = {"type": "object", "additionalProperties": False}
    if name == "describe_available_data":
        parameters = {**base, "properties": {"data_ids": _array(sorted(OFFICIAL_DATA_IDS | AUXILIARY_DATA_IDS | REGISTERED_CONTEXT_DATA_IDS))}}
    elif name == "query_data_schema":
        parameters = {**base, "properties": {"data_id": {"type": "string", "enum": sorted(OFFICIAL_DATA_IDS)}}, "required": ["data_id"]}
    elif name in _GROUPS:
        parameters = {**base, "properties": {"data_ids": _array(sorted(_GROUPS[name]), 1), "symbols": _array(), "start_time": {"type": "string", "format": "date-time"}, "end_time": {"type": "string", "format": "date-time"}, "fields": _array(), "limit": {"type": "integer", "minimum": 1, "maximum": 100000}}, "required": ["data_ids"]}
    elif name in _AUX_TO_DATA:
        parameters = {**base, "properties": {"query": {"type": "string", "maxLength": 512}}}
    elif name in _CONTEXT_TO_DATA:
        parameters = {**base, "properties": {"query": {"type": "string", "maxLength": 512}, "limit": {"type": "integer", "minimum": 1, "maximum": 1000}}}
    else:
        raise ValueError("unknown controlled research tool")
    return {"name": name, "description": "Read an approved frozen as-of view through a pathless content handle.", "parameters": parameters, "readonly": True}


def _array(enum: Sequence[str] | None = None, minimum: int = 0) -> dict[str, Any]:
    item: dict[str, Any] = {"type": "string"}
    if enum is not None:
        item["enum"] = list(enum)
    return {"type": "array", "items": item, "uniqueItems": True, "minItems": minimum, "maxItems": 100}


def _validate(value: Any, schema: Mapping[str, Any], path: str = "arguments") -> None:
    kind = schema.get("type")
    if kind == "object":
        if not isinstance(value, Mapping):
            fail(FailureCode.INVALID_VALUE, f"{path} must be an object")
        props = schema.get("properties", {})
        if set(value) - set(props) or set(schema.get("required", ())) - set(value):
            fail(FailureCode.INVALID_VALUE, f"{path} fields do not match schema")
        for key, item in value.items():
            _validate(item, props[key], f"{path}.{key}")
    elif kind == "array":
        if not isinstance(value, list) or not int(schema.get("minItems", 0)) <= len(value) <= int(schema.get("maxItems", 2**31)):
            fail(FailureCode.INVALID_VALUE, f"{path} must be a bounded array")
        rendered = [json.dumps(item, sort_keys=True, default=str) for item in value]
        if schema.get("uniqueItems") and len(rendered) != len(set(rendered)):
            fail(FailureCode.INVALID_VALUE, f"{path} must be unique")
        for item in value:
            _validate(item, schema["items"], path)
    elif kind == "string":
        if not isinstance(value, str) or len(value) > int(schema.get("maxLength", 4096)) or ("enum" in schema and value not in schema["enum"]):
            fail(FailureCode.INVALID_VALUE, f"{path} is invalid")
        if schema.get("format") == "date-time":
            _parse_time(value)
    elif kind == "integer":
        if isinstance(value, bool) or not isinstance(value, int) or value < schema.get("minimum", value) or value > schema.get("maximum", value):
            fail(FailureCode.INVALID_VALUE, f"{path} is invalid")


def _stable_row(row: Mapping[str, Any]) -> Mapping[str, Any]:
    if not isinstance(row, Mapping):
        raise ValueError("frozen row must be a mapping")
    clean = dict(row)
    _reject_privileged(clean)
    _reject_labels(clean)
    json.dumps(clean, ensure_ascii=False, allow_nan=False, sort_keys=True, default=_json_default)
    return MappingProxyType(clean)


def _reject_privileged(value: Any) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            folded = str(key).casefold()
            if folded in _FORBIDDEN_KEYS or "sealed" in folded:
                raise PermissionError("privileged research material is forbidden")
            _reject_privileged(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _reject_privileged(item)
    elif isinstance(value, str) and (_SECRET.search(value) or _PATH.search(value)):
        raise PermissionError("secret-like or path-shaped material is forbidden")


def _reject_labels(row: Mapping[str, Any]) -> None:
    if any(str(key).casefold() in _LABEL_KEYS or str(key).casefold().startswith("future_") for key in row):
        fail(FailureCode.UNAUTHORIZED_CAPABILITY, "future labels cannot enter research tools")


def _sanitize_untrusted_auxiliary(value: Any) -> tuple[Any, int]:
    """Project frozen untrusted text without allowing instructions to survive."""

    removed = 0

    def visit(item: Any) -> Any:
        nonlocal removed
        if isinstance(item, Mapping):
            return {str(key): visit(child) for key, child in sorted(item.items(), key=lambda pair: str(pair[0]))}
        if isinstance(item, (list, tuple)):
            return [visit(child) for child in item]
        if isinstance(item, str) and detect_prompt_injection(item):
            removed += 1
            return "[UNTRUSTED_INSTRUCTION_REDACTED]"
        return item

    return visit(value), removed


def _available_at(row: Mapping[str, Any]) -> datetime:
    if row.get("available_at") is None:
        fail(FailureCode.INVALID_TIME_ORDER, "frozen row lacks available_at")
    return _parse_time(row["available_at"])


def _event_time(row: Mapping[str, Any], fallback: datetime) -> datetime:
    for key in ("event_time", "decision_time", "trade_date", "ann_date", "end_date"):
        if row.get(key) not in (None, ""):
            return _parse_time(row[key])
    return fallback


def _parse_time(value: Any) -> datetime:
    if isinstance(value, datetime):
        result = value
    elif isinstance(value, date):
        result = datetime.combine(value, time.min, tzinfo=timezone.utc)
    elif isinstance(value, str):
        text = value.strip()
        try:
            if len(text) == 8 and text.isdigit():
                result = datetime.strptime(text, "%Y%m%d").replace(tzinfo=timezone.utc)
            elif len(text) == 10 and text[4] == "-":
                result = datetime.fromisoformat(text).replace(tzinfo=timezone.utc)
            else:
                result = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            fail(FailureCode.INVALID_TIMESTAMP, "invalid date-time")
    else:
        fail(FailureCode.INVALID_TIMESTAMP, "invalid date-time")
    if result.tzinfo is None or result.utcoffset() is None:
        fail(FailureCode.INVALID_TIMESTAMP, "date-time must be timezone-aware")
    return result.astimezone(timezone.utc)


def _sort_key(row: Mapping[str, Any]) -> tuple[str, str, str]:
    return (_event_time(row, _available_at(row)).isoformat(), str(row.get("symbol") or row.get("ts_code") or ""), canonical_hash(dict(row)))


def _handle(value: ReadOnlyArtifactHandle) -> dict[str, Any]:
    return {"handle_id": value.handle_id, "media_type": value.media_type, "size_bytes": value.size_bytes}


def _json_default(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    raise TypeError("unsupported frozen value")


__all__ = ["ControlledResearchToolbox", "FrozenResearchArtifact"]
