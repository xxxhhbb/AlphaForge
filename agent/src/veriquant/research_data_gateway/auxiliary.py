"""Allowlisted freezing wrapper for Vibe read-only investigation tools."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import json
import os
from pathlib import Path
import tempfile
from typing import Any, Mapping, Protocol

from .models import DataAuthority, DataSourceReceipt, GatewayFailureCode, canonical_hash
from ..research_memory.security import detect_prompt_injection


AUXILIARY_TOOL_IDS = ("dragon_tiger", "northbound", "research_reports", "stock_news")
_SECRET_KEYS = frozenset({"token", "api_key", "apikey", "authorization", "cookie", "secret"})


class ReadOnlyTool(Protocol):
    def execute(self, **kwargs: Any) -> str: ...


class AuxiliaryArtifactFreezer:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def freeze(
        self,
        *,
        tools: Mapping[str, ReadOnlyTool],
        queries: Mapping[str, Mapping[str, Any]],
    ) -> tuple[tuple[DataSourceReceipt, ...], str]:
        unexpected = (set(tools) | set(queries)) - set(AUXILIARY_TOOL_IDS)
        if unexpected:
            raise ValueError("auxiliary tool is not allowlisted")
        receipts: list[DataSourceReceipt] = []
        artifact_hashes: dict[str, str] = {}
        for tool_id in AUXILIARY_TOOL_IDS:
            if tool_id not in queries:
                continue
            query = dict(queries[tool_id])
            _reject_secrets(query)
            tool = tools.get(tool_id)
            if tool is None:
                receipts.append(DataSourceReceipt(source_id=tool_id, authority=DataAuthority.AUXILIARY_RESEARCH_EVIDENCE, status="UNAVAILABLE", row_count=0, content_hash=None, failure_code=GatewayFailureCode.AUXILIARY_TOOL_UNAVAILABLE))
                continue
            try:
                raw = tool.execute(**query)
                value = json.loads(raw)
                if not isinstance(value, Mapping):
                    raise ValueError("tool response is not an object")
                _reject_secrets(value)
                value, redacted_count = _sanitize_untrusted(value)
                ok = value.get("ok", True) is True
                if not ok:
                    # Provider exception strings and URLs are not frozen as
                    # research content.  The typed receipt carries failure.
                    value = {
                        "ok": False,
                        "source": _safe_source(value.get("source")),
                        "failure": "UPSTREAM_RESPONSE_UNAVAILABLE",
                    }
                payload = {
                    "schema_version": "alphaforge.auxiliary-artifact/v1",
                    "tool_id": tool_id,
                    "authority": DataAuthority.AUXILIARY_RESEARCH_EVIDENCE.value,
                    "captured_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
                    "query_hash": canonical_hash(query),
                    "provider_source": _safe_source(value.get("source")),
                    "prompt_injection_redactions": redacted_count,
                    "payload": value,
                }
                # capture time is provenance, but deterministic content identity excludes it.
                content_hash = canonical_hash({key: val for key, val in payload.items() if key != "captured_at"})
                payload["content_hash"] = content_hash
                _atomic_json(self.root / f"{tool_id}.json", payload)
                row_count = _estimated_rows(value)
                if not ok:
                    receipts.append(DataSourceReceipt(
                        source_id=tool_id,
                        authority=DataAuthority.AUXILIARY_RESEARCH_EVIDENCE,
                        status="INVALID",
                        row_count=0,
                        content_hash=None,
                        failure_code=GatewayFailureCode.AUXILIARY_RESPONSE_INVALID,
                    ))
                else:
                    status = "SUCCESS" if row_count > 0 else "EMPTY"
                    receipts.append(DataSourceReceipt(source_id=tool_id, authority=DataAuthority.AUXILIARY_RESEARCH_EVIDENCE, status=status, row_count=row_count, content_hash=content_hash))
                    artifact_hashes[tool_id] = content_hash
            except Exception:  # tool failures are typed and never leak raw provider details
                receipts.append(DataSourceReceipt(source_id=tool_id, authority=DataAuthority.AUXILIARY_RESEARCH_EVIDENCE, status="INVALID", row_count=0, content_hash=None, failure_code=GatewayFailureCode.AUXILIARY_RESPONSE_INVALID))
        manifest_hash = canonical_hash(dict(sorted(artifact_hashes.items())))
        return tuple(sorted(receipts, key=lambda item: item.source_id)), manifest_hash


def default_vibe_tools(*, northbound_proxy_client: Any | None = None) -> dict[str, ReadOnlyTool]:
    """Load only the four reviewed read-only Vibe tools; no ToolRegistry exposure."""
    from src.tools.dragon_tiger_tool import DragonTigerTool
    from src.tools.northbound_tool import NorthboundFlowTool
    from src.tools.research_reports_tool import ResearchReportsTool
    from src.tools.stock_news_tool import StockNewsTool
    return {
        "dragon_tiger": DragonTigerTool(),
        "northbound": HistoricalNorthboundWrapper(
            primary=NorthboundFlowTool(), proxy_client=northbound_proxy_client
        ),
        "research_reports": ResearchReportsTool(),
        "stock_news": StockNewsTool(),
    }


class HistoricalNorthboundWrapper:
    """Recover the last disclosed net-buy history through the approved proxy.

    Eastmoney can return HTTP 200 with an empty ``history`` after the 2024
    disclosure change.  That is not a successful data result.  This wrapper
    first uses the reviewed Vibe tool, then (only when explicitly injected)
    queries the already-approved proxy for the historical series.  It never
    reads configuration or credentials itself.
    """

    def __init__(self, *, primary: ReadOnlyTool, proxy_client: Any | None) -> None:
        self._primary = primary
        self._proxy_client = proxy_client

    def execute(self, **kwargs: Any) -> str:
        lookback_days = _bounded_positive_int(kwargs.get("lookback_days", 30), ceiling=250)
        primary = _parse_tool_object(self._primary.execute(lookback_days=lookback_days))
        if _estimated_rows(primary) > 0:
            return json.dumps(primary, ensure_ascii=False, allow_nan=False)
        if self._proxy_client is None:
            return json.dumps(primary, ensure_ascii=False, allow_nan=False)

        as_of = _parse_optional_date(kwargs.get("as_of_date")) or date.today()
        provider_end = as_of
        provider_start = provider_end - timedelta(days=max(lookback_days * 4, 40))
        try:
            frame = self._proxy_client.moneyflow_hsgt(
                start_date=provider_start.strftime("%Y%m%d"),
                end_date=provider_end.strftime("%Y%m%d"),
            )
            raw_rows = frame.to_dict("records") if hasattr(frame, "to_dict") else frame
            if not isinstance(raw_rows, list):
                raw_rows = []
            history = [_normalize_northbound_row(row) for row in raw_rows if isinstance(row, Mapping)]
            history = [row for row in history if row is not None]
            # Some compatible proxy implementations accept but ignore date
            # parameters.  Enforce the requested interval locally so out-of-
            # window rows can never be frozen under a false query identity.
            lower = provider_start.isoformat()
            upper = provider_end.isoformat()
            history = [row for row in history if lower <= row["trade_date"] <= upper]
            history.sort(key=lambda row: row["trade_date"])
            history = history[-lookback_days:]
        except Exception:
            # No raw SDK/provider exception is returned or frozen.
            return json.dumps(
                {"ok": False, "source": "approved_proxy_data_vendor", "failure": "UPSTREAM_RESPONSE_UNAVAILABLE"},
                ensure_ascii=False,
            )
        latest = history[-1] if history else {}
        return json.dumps(
            {
                "ok": True,
                "market": "China A",
                "source": "approved_proxy_data_vendor",
                "data": {
                    "unit": "10k CNY",
                    "lookback_days": lookback_days,
                    "requested_as_of_date": as_of.isoformat(),
                    "requested_start_date": provider_start.isoformat(),
                    "series_end_date": latest.get("trade_date"),
                    "availability_policy": "APPROVED_PROXY_DAILY_AGGREGATE_AUXILIARY_ONLY",
                    "realtime": {
                        "shanghai_connect": None,
                        "shenzhen_connect": None,
                        "total": None,
                    },
                    "latest_daily": latest,
                    "history": history,
                },
            },
            ensure_ascii=False,
            allow_nan=False,
        )


def _reject_secrets(value: Any) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if str(key).casefold() in _SECRET_KEYS:
                raise ValueError("secret-like field rejected")
            _reject_secrets(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _reject_secrets(item)


def _estimated_rows(value: Mapping[str, Any]) -> int:
    data = value.get("data")
    if isinstance(data, list):
        return len(data)
    if isinstance(data, Mapping):
        sizes = [len(item) for item in data.values() if isinstance(item, list)]
        return max(sizes, default=0)
    return 0


def _sanitize_untrusted(value: Any) -> tuple[Any, int]:
    redacted = 0

    def visit(item: Any) -> Any:
        nonlocal redacted
        if isinstance(item, Mapping):
            return {str(key): visit(child) for key, child in sorted(item.items(), key=lambda pair: str(pair[0]))}
        if isinstance(item, (list, tuple)):
            return [visit(child) for child in item]
        if isinstance(item, str) and detect_prompt_injection(item):
            redacted += 1
            return "[UNTRUSTED_INSTRUCTION_REDACTED]"
        return item

    return visit(value), redacted


def _safe_source(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    folded = value.strip().casefold()
    allowed = {"eastmoney", "eastmoney+ths", "approved_proxy_data_vendor", "tushare"}
    return folded if folded in allowed else None


def _parse_tool_object(raw: str) -> dict[str, Any]:
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("tool response is not an object")
    return value


def _bounded_positive_int(value: Any, *, ceiling: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = 30
    return max(1, min(parsed, ceiling))


def _parse_optional_date(value: Any) -> date | None:
    if value in (None, ""):
        return None
    text = str(value).strip().replace("-", "")
    if len(text) != 8 or not text.isdigit():
        raise ValueError("as_of_date must be YYYYMMDD")
    return datetime.strptime(text, "%Y%m%d").date()


def _normalize_northbound_row(row: Mapping[str, Any]) -> dict[str, Any] | None:
    trade_date = str(row.get("trade_date") or "").strip().replace("-", "")
    if len(trade_date) != 8 or not trade_date.isdigit():
        return None
    shanghai = _finite_float(row.get("hgt"))
    shenzhen = _finite_float(row.get("sgt"))
    total = _finite_float(row.get("north_money"))
    return {
        "trade_date": f"{trade_date[:4]}-{trade_date[4:6]}-{trade_date[6:]}",
        "shanghai_connect": shanghai * 100 if shanghai is not None else None,
        "shenzhen_connect": shenzhen * 100 if shenzhen is not None else None,
        "total": total * 100 if total is not None else None,
    }


def _finite_float(value: Any) -> float | None:
    if value in (None, "", "-"):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed == parsed and parsed not in (float("inf"), float("-inf")) else None


def _atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    handle = tempfile.NamedTemporaryFile("w", encoding="utf-8", newline="\n", delete=False, dir=path.parent)
    temporary = Path(handle.name)
    try:
        json.dump(payload, handle, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
        handle.close()
        os.replace(temporary, path)
    finally:
        if not handle.closed:
            handle.close()
        if temporary.exists():
            temporary.unlink()


__all__ = [
    "AUXILIARY_TOOL_IDS",
    "AuxiliaryArtifactFreezer",
    "HistoricalNorthboundWrapper",
    "ReadOnlyTool",
    "default_vibe_tools",
]
