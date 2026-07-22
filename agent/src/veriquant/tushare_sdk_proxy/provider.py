"""Bounded providers backed by the reviewed Tushare-compatible SDK client."""

from __future__ import annotations

from typing import Any, Mapping

from ..data.ashare.models import AShareDataRequest
from .client import TushareProxyError


def _rows(table: Any) -> list[dict[str, Any]]:
    if table is None:
        return []
    if isinstance(table, list):
        return [dict(item) for item in table if isinstance(item, Mapping)]
    if hasattr(table, "to_dict"):
        try:
            return [dict(item) for item in table.to_dict(orient="records")]
        except Exception:
            return []
    return []


class RecordingProxyClient:
    """Record canonical SDK results for later hashing, never request credentials."""

    def __init__(self, client: Any) -> None:
        self._client = client
        self.captures: list[dict[str, Any]] = []

    def call(self, api_name: str, **params: Any) -> Any:
        if api_name not in {"daily", "adj_factor", "index_weight", "trade_cal"}:
            raise TushareProxyError("SDK API is outside the acquisition allowlist")
        try:
            result = getattr(self._client, api_name)(**params)
        except Exception as exc:
            raise TushareProxyError(
                f"Tushare SDK call failed ({api_name}:{type(exc).__name__})"
            ) from exc
        self.captures.append(
            {"api_name": api_name, "params": dict(sorted(params.items())), "rows": _rows(result)}
        )
        return result

    def index_weight(self, **params: Any) -> Any:
        return self.call("index_weight", **params)


class ProxyMarketDataProvider:
    provider_name = "tushare-compatible-proxy-sdk"
    offline_fixture = False

    def __init__(self, client: RecordingProxyClient) -> None:
        self.client = client

    def fetch(self, request: AShareDataRequest) -> Mapping[str, Any]:
        if not isinstance(request, AShareDataRequest):
            raise TushareProxyError("typed A-share request is required")
        if request.adjustment != "raw" or request.frequency != "1D":
            raise TushareProxyError("proxy smoke supports raw daily bars only")
        start = request.start_time.strftime("%Y%m%d")
        end = request.end_time.strftime("%Y%m%d")
        output: dict[str, list[dict[str, Any]]] = {}
        for code in request.codes:
            table = self.client.call(
                "daily",
                ts_code=code,
                start_date=start,
                end_date=end,
                fields="ts_code,trade_date,open,high,low,close,vol,amount",
            )
            normalized: list[dict[str, Any]] = []
            for row in _rows(table):
                normalized.append(
                    {
                        "trade_date": row.get("trade_date"),
                        "open": row.get("open"),
                        "high": row.get("high"),
                        "low": row.get("low"),
                        "close": row.get("close"),
                        "volume": row.get("vol"),
                        "amount": row.get("amount"),
                    }
                )
            output[code] = normalized
        return output
