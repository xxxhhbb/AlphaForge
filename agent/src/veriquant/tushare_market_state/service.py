"""Restricted SDK acquisition and deterministic market-state normalization."""

from __future__ import annotations

import json
from datetime import datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Protocol
from zoneinfo import ZoneInfo

from ..contracts.base import canonicalize
from ..data.bitemporal import canonical_content_hash
from .models import (
    FrozenMarketStateManifest,
    MarketStateCapture,
    MarketStateError,
    MarketStateFailureCode,
    MarketStateRequest,
    NormalizedMarketStateRow,
    SdkCallCapture,
    market_state_fail,
    parse_date,
)


SHANGHAI = ZoneInfo("Asia/Shanghai")
_API_FIELDS = {
    "stock_basic": ("ts_code", "list_status", "list_date", "delist_date"),
    "suspend_d": ("ts_code", "suspend_date", "resume_date"),
    "stk_limit": ("ts_code", "trade_date", "pre_close", "up_limit", "down_limit"),
    "adj_factor": ("ts_code", "trade_date", "adj_factor"),
}
_SECRET_KEYS = frozenset(
    {"token", "authorization", "api_key", "apikey", "access_token", "secret", "password"}
)


class MarketStateSdk(Protocol):
    """Minimal raw SDK port. The service selects every method and parameter."""

    def stock_basic(self, **params: Any) -> Any: ...

    def suspend_d(self, **params: Any) -> Any: ...

    def stk_limit(self, **params: Any) -> Any: ...

    def adj_factor(self, **params: Any) -> Any: ...


def _table_rows(table: Any) -> list[dict[str, Any]]:
    if table is None:
        return []
    if isinstance(table, list):
        if any(not isinstance(item, Mapping) for item in table):
            market_state_fail(MarketStateFailureCode.INVALID_RECORD, "SDK list contains a non-object row")
        return [dict(item) for item in table]
    if hasattr(table, "to_dict"):
        try:
            converted = table.to_dict(orient="records")
        except Exception as exc:
            raise MarketStateError(
                MarketStateFailureCode.INVALID_RECORD,
                "SDK table conversion failed",
                details={"error_type": type(exc).__name__},
            ) from exc
        if not isinstance(converted, list) or any(not isinstance(item, Mapping) for item in converted):
            market_state_fail(MarketStateFailureCode.INVALID_RECORD, "SDK table conversion is invalid")
        return [dict(item) for item in converted]
    market_state_fail(MarketStateFailureCode.INVALID_RECORD, "SDK response is not a supported table")


def _safe_scalar(value: Any, field_name: str) -> str | int | float | bool | None:
    if value is None or type(value) in (str, int, float, bool):
        if isinstance(value, float) and (value != value or value in (float("inf"), float("-inf"))):
            market_state_fail(MarketStateFailureCode.INVALID_RECORD, f"{field_name} is non-finite")
        return value
    if hasattr(value, "item"):
        try:
            return _safe_scalar(value.item(), field_name)
        except Exception as exc:
            raise MarketStateError(
                MarketStateFailureCode.INVALID_RECORD,
                f"{field_name} scalar conversion failed",
                details={"error_type": type(exc).__name__},
            ) from exc
    market_state_fail(MarketStateFailureCode.INVALID_RECORD, f"{field_name} is not a scalar")


def _capture_rows(api_name: str, table: Any) -> tuple[Mapping[str, Any], ...]:
    allowed = _API_FIELDS[api_name]
    output: list[dict[str, Any]] = []
    for raw in _table_rows(table):
        lowered = {str(key).lower() for key in raw}
        if lowered & _SECRET_KEYS:
            market_state_fail(MarketStateFailureCode.SECRET_MATERIAL, "SDK response contains secret-like fields")
        output.append({field: _safe_scalar(raw.get(field), field) for field in allowed})
    return tuple(
        sorted(
            output,
            key=lambda row: json.dumps(canonicalize(row), sort_keys=True, separators=(",", ":")),
        )
    )


class RestrictedMarketStateClient:
    """Fixed capability adapter that records only allowlisted response fields."""

    def __init__(self, sdk: MarketStateSdk) -> None:
        self._sdk = sdk
        self._calls: list[SdkCallCapture] = []

    @property
    def calls(self) -> tuple[SdkCallCapture, ...]:
        return tuple(self._calls)

    def _call(self, api_name: str, **params: Any) -> tuple[Mapping[str, Any], ...]:
        if api_name not in _API_FIELDS:
            market_state_fail(MarketStateFailureCode.API_OUTSIDE_ALLOWLIST, "SDK API is outside allowlist")
        try:
            table = getattr(self._sdk, api_name)(**params)
        except Exception as exc:
            raise MarketStateError(
                MarketStateFailureCode.SDK_FAILURE,
                f"SDK call failed for {api_name}",
                details={"api_name": api_name, "error_type": type(exc).__name__},
            ) from exc
        rows = _capture_rows(api_name, table)
        self._calls.append(SdkCallCapture(api_name=api_name, params=params, rows=rows))
        return rows

    def stock_basic(self, **params: Any) -> tuple[Mapping[str, Any], ...]:
        return self._call("stock_basic", **params)

    def suspend_d(self, **params: Any) -> tuple[Mapping[str, Any], ...]:
        return self._call("suspend_d", **params)

    def stk_limit(self, **params: Any) -> tuple[Mapping[str, Any], ...]:
        return self._call("stk_limit", **params)

    def adj_factor(self, **params: Any) -> tuple[Mapping[str, Any], ...]:
        return self._call("adj_factor", **params)


def _provider_date(value: Any, field_name: str, *, optional: bool = False):
    if value in (None, "") and optional:
        return None
    if not isinstance(value, str) or len(value) != 8 or not value.isdigit():
        market_state_fail(MarketStateFailureCode.INVALID_RECORD, f"{field_name} must be YYYYMMDD")
    try:
        return datetime.strptime(value, "%Y%m%d").date()
    except ValueError as exc:
        raise MarketStateError(
            MarketStateFailureCode.INVALID_RECORD,
            f"{field_name} is not a valid date",
        ) from exc


def _positive_decimal(value: Any, field_name: str) -> Decimal:
    if isinstance(value, bool) or value is None:
        market_state_fail(MarketStateFailureCode.INVALID_RECORD, f"{field_name} is required")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise MarketStateError(
            MarketStateFailureCode.INVALID_RECORD,
            f"{field_name} must be numeric",
        ) from exc
    if not parsed.is_finite() or parsed <= 0:
        market_state_fail(MarketStateFailureCode.INVALID_RECORD, f"{field_name} must be positive and finite")
    return parsed


def _one_by_key(
    rows: tuple[Mapping[str, Any], ...],
    *,
    api_name: str,
    symbol: str,
    date_field: str | None = None,
) -> dict[str, Mapping[str, Any]]:
    output: dict[str, Mapping[str, Any]] = {}
    for row in rows:
        if row.get("ts_code") != symbol:
            market_state_fail(MarketStateFailureCode.INVALID_RECORD, f"{api_name} returned an out-of-scope symbol")
        key = symbol if date_field is None else str(row.get(date_field) or "")
        if not key or key in output:
            market_state_fail(MarketStateFailureCode.INVALID_RECORD, f"{api_name} has a missing or duplicate key")
        output[key] = row
    return output


def acquire_and_normalize(
    request: MarketStateRequest,
    *,
    sdk: MarketStateSdk,
) -> tuple[MarketStateCapture, tuple[NormalizedMarketStateRow, ...], FrozenMarketStateManifest]:
    """Acquire four fixed datasets and build complete deterministic row state."""

    if not isinstance(request, MarketStateRequest):
        market_state_fail(MarketStateFailureCode.INVALID_REQUEST, "typed MarketStateRequest is required")
    client = RestrictedMarketStateClient(sdk)
    stock_by_symbol: dict[str, Mapping[str, Any]] = {}
    suspensions_by_symbol: dict[str, tuple[Mapping[str, Any], ...]] = {}
    limits: dict[tuple[str, str], Mapping[str, Any]] = {}
    adjustments: dict[tuple[str, str], Mapping[str, Any]] = {}
    provider_start = parse_date(request.start_date, "start_date").strftime("%Y%m%d")
    provider_end = parse_date(request.end_date, "end_date").strftime("%Y%m%d")
    suspend_start = (parse_date(request.start_date, "start_date") - timedelta(days=366)).strftime("%Y%m%d")

    for symbol in request.codes:
        stock_rows = client.stock_basic(
            ts_code=symbol,
            fields=",".join(_API_FIELDS["stock_basic"]),
        )
        indexed_stock = _one_by_key(stock_rows, api_name="stock_basic", symbol=symbol)
        if set(indexed_stock) != {symbol}:
            market_state_fail(MarketStateFailureCode.INCOMPLETE_METADATA, "stock_basic must return exactly one row per code")
        stock_by_symbol[symbol] = indexed_stock[symbol]

        suspension_rows = client.suspend_d(
            ts_code=symbol,
            start_date=suspend_start,
            end_date=provider_end,
            fields=",".join(_API_FIELDS["suspend_d"]),
        )
        _one_by_key(suspension_rows, api_name="suspend_d", symbol=symbol, date_field="suspend_date")
        suspensions_by_symbol[symbol] = suspension_rows

        limit_rows = client.stk_limit(
            ts_code=symbol,
            start_date=provider_start,
            end_date=provider_end,
            fields=",".join(_API_FIELDS["stk_limit"]),
        )
        for key, value in _one_by_key(
            limit_rows, api_name="stk_limit", symbol=symbol, date_field="trade_date"
        ).items():
            limits[(symbol, key)] = value

        adj_rows = client.adj_factor(
            ts_code=symbol,
            start_date=provider_start,
            end_date=provider_end,
            fields=",".join(_API_FIELDS["adj_factor"]),
        )
        for key, value in _one_by_key(
            adj_rows, api_name="adj_factor", symbol=symbol, date_field="trade_date"
        ).items():
            adjustments[(symbol, key)] = value

    capture = MarketStateCapture(request_hash=request.artifact_hash, calls=client.calls)
    capture_hash = capture.artifact_hash
    expected_keys = {(row.symbol, row.trade_date.replace("-", "")) for row in request.rows}
    if set(limits) != expected_keys or set(adjustments) != expected_keys:
        market_state_fail(
            MarketStateFailureCode.INCOMPLETE_METADATA,
            "stk_limit and adj_factor must cover every frozen bar row exactly",
        )

    normalized: list[NormalizedMarketStateRow] = []
    for input_row in request.rows:
        symbol = input_row.symbol
        day = parse_date(input_row.trade_date, "trade_date")
        basic = stock_by_symbol[symbol]
        list_date = _provider_date(basic.get("list_date"), "list_date")
        delist_date = _provider_date(basic.get("delist_date"), "delist_date", optional=True)
        status = basic.get("list_status")
        if status not in {"L", "D", "P"}:
            market_state_fail(MarketStateFailureCode.INVALID_RECORD, "stock_basic list_status is unknown")
        if status == "D" and delist_date is None:
            market_state_fail(MarketStateFailureCode.INCOMPLETE_METADATA, "delisted stock lacks delist_date")
        if day < list_date:
            market_state_fail(MarketStateFailureCode.INVALID_RECORD, "bar row precedes listing date")

        suspended = False
        suspension_material: list[Mapping[str, Any]] = []
        for suspension in suspensions_by_symbol[symbol]:
            suspend_date = _provider_date(suspension.get("suspend_date"), "suspend_date")
            resume_date = _provider_date(suspension.get("resume_date"), "resume_date", optional=True)
            if resume_date is not None and resume_date < suspend_date:
                market_state_fail(MarketStateFailureCode.INVALID_RECORD, "resume_date precedes suspend_date")
            if suspend_date <= day and (resume_date is None or day < resume_date):
                suspended = True
                suspension_material.append(suspension)

        limit_row = limits[(symbol, input_row.trade_date.replace("-", ""))]
        adj_row = adjustments[(symbol, input_row.trade_date.replace("-", ""))]
        up_limit = _positive_decimal(limit_row.get("up_limit"), "up_limit")
        down_limit = _positive_decimal(limit_row.get("down_limit"), "down_limit")
        close = _positive_decimal(input_row.close, "close")
        adj_factor = _positive_decimal(adj_row.get("adj_factor"), "adj_factor")
        if down_limit >= up_limit or close > up_limit or close < down_limit:
            market_state_fail(MarketStateFailureCode.INVALID_RECORD, "close lies outside daily price limits")

        published_at = datetime.combine(day, time(18, 0), SHANGHAI)
        available_at = published_at
        if available_at > request.decision_time:
            market_state_fail(MarketStateFailureCode.FUTURE_DATA, "market-state metadata follows decision_time")
        listed_at = datetime.combine(list_date, time.min, SHANGHAI)
        revision = canonical_content_hash(
            {
                "schema_version": "veriquant.tushare-market-state-row-revision/v1",
                "basic": basic,
                "suspension": suspension_material,
                "limit": limit_row,
                "adjustment": adj_row,
            }
        )
        normalized.append(
            NormalizedMarketStateRow(
                symbol=symbol,
                trade_date=input_row.trade_date,
                event_time=input_row.event_time,
                listed_at=listed_at,
                published_at=published_at,
                available_at=available_at,
                revision=revision,
                suspended=suspended,
                delisted=delist_date is not None and day > delist_date,
                limit_up=close == up_limit,
                limit_down=close == down_limit,
                up_limit=float(up_limit),
                down_limit=float(down_limit),
                adj_factor=float(adj_factor),
            )
        )

    rows = tuple(normalized)
    records_hash = canonical_content_hash(rows)
    revision = canonical_content_hash(
        {
            "schema_version": "veriquant.tushare-market-state-revision/v1",
            "capture_hash": capture_hash,
            "records_hash": records_hash,
        }
    )
    identity = {
        "schema_version": "veriquant.tushare-market-state-vintage-content/v1",
        "dataset_id": request.dataset_id,
        "request_hash": request.artifact_hash,
        "provider_capture_hash": capture_hash,
        "source_snapshot_hash": request.source_snapshot_hash,
        "calendar_hash": request.calendar_hash,
        "universe_hash": request.universe_hash,
        "bar_capture_hash": request.bar_capture_hash,
        "records_hash": records_hash,
        "revision": revision,
        "decision_time": request.decision_time,
    }
    vintage_hash = canonical_content_hash(identity)
    manifest = FrozenMarketStateManifest(
        dataset_id=request.dataset_id,
        request_hash=request.artifact_hash,
        provider_capture_hash=capture_hash,
        source_snapshot_hash=request.source_snapshot_hash,
        calendar_hash=request.calendar_hash,
        universe_hash=request.universe_hash,
        bar_capture_hash=request.bar_capture_hash,
        records_hash=records_hash,
        vintage_hash=vintage_hash,
        revision=revision,
        decision_time=request.decision_time,
        row_count=len(rows),
        api_names=tuple(sorted(_API_FIELDS)),
    )
    return capture, rows, manifest
