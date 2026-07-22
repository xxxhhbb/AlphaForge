"""Explicit API/field allowlist over the existing approved proxy SDK client."""

from __future__ import annotations

import math
from numbers import Real
from typing import Any, Mapping, Protocol

from ..tushare_sdk_proxy import TushareProxyError


API_FIELD_ALLOWLIST = {
    "trade_cal": frozenset({"exchange", "cal_date", "is_open", "pretrade_date"}),
    "index_weight": frozenset({"index_code", "con_code", "trade_date", "weight"}),
    "daily": frozenset({"ts_code", "trade_date", "open", "high", "low", "close", "vol", "amount"}),
    "adj_factor": frozenset({"ts_code", "trade_date", "adj_factor"}),
    "daily_basic": frozenset({
        "ts_code", "trade_date", "close", "turnover_rate",
        "turnover_rate_f", "volume_ratio", "pe", "pe_ttm", "pb", "ps",
        "ps_ttm", "dv_ratio", "dv_ttm", "total_share", "float_share",
        "free_share", "total_mv", "circ_mv",
    }),
    "stock_basic": frozenset({
        "ts_code", "industry", "list_date", "delist_date", "list_status",
        "industry_effective_date", "industry_available_date",
    }),
    # Historical industry membership endpoints are read-only and only expose
    # effective interval fields.  They are intentionally separate from
    # ``stock_basic.industry`` so a current classification can never be
    # mistaken for a point-in-time history.
    "index_member_all": frozenset({
        "l1_code", "l1_name", "l2_code", "l2_name", "l3_code", "l3_name",
        "ts_code", "name", "in_date", "out_date", "is_new",
    }),
    "income": frozenset({
        "ts_code", "ann_date", "f_ann_date", "end_date", "report_type",
        "comp_type", "basic_eps", "total_revenue", "revenue", "n_income",
        "n_income_attr_p", "update_flag",
    }),
    "balancesheet": frozenset({
        "ts_code", "ann_date", "f_ann_date", "end_date", "report_type",
        "comp_type", "total_assets", "total_liab", "total_hldr_eqy_exc_min_int",
        "money_cap", "accounts_receiv", "inventories", "update_flag",
    }),
    "cashflow": frozenset({
        "ts_code", "ann_date", "f_ann_date", "end_date", "report_type",
        "comp_type", "n_cashflow_act", "n_cashflow_inv_act",
        "n_cash_flows_fnc_act", "free_cashflow", "update_flag",
    }),
    "fina_indicator": frozenset({
        "ts_code", "ann_date", "end_date", "eps", "dt_eps", "roe", "roe_waa",
        "roa", "grossprofit_margin", "netprofit_margin", "debt_to_assets",
        "current_ratio", "quick_ratio", "assets_turn", "inv_turn",
        "or_yoy", "netprofit_yoy", "ocf_to_or", "bps", "update_flag",
    }),
    "forecast": frozenset({
        "ts_code", "ann_date", "end_date", "type", "p_change_min",
        "p_change_max", "net_profit_min", "net_profit_max", "summary",
        "change_reason",
    }),
    "express": frozenset({
        "ts_code", "ann_date", "end_date", "revenue", "operate_profit",
        "total_profit", "n_income", "total_assets", "total_hldr_eqy_exc_min_int",
        "diluted_eps", "diluted_roe", "yoy_sales", "yoy_net_profit",
    }),
    "moneyflow": frozenset({
        "ts_code", "trade_date", "buy_sm_vol", "buy_sm_amount", "sell_sm_vol",
        "sell_sm_amount", "buy_md_vol", "buy_md_amount", "sell_md_vol",
        "sell_md_amount", "buy_lg_vol", "buy_lg_amount", "sell_lg_vol",
        "sell_lg_amount", "buy_elg_vol", "buy_elg_amount", "sell_elg_vol",
        "sell_elg_amount", "net_mf_vol", "net_mf_amount",
    }),
    "index_daily": frozenset({
        "ts_code", "trade_date", "open", "high", "low", "close", "pre_close",
        "change", "pct_chg", "vol", "amount",
    }),
    "shibor": frozenset({
        "date", "on", "1w", "2w", "1m", "3m", "6m", "9m", "1y",
    }),
}


class ProxyScaleQueryClient(Protocol):
    offline_fixture: bool

    def query(self, api_name: str, *, params: Mapping[str, Any], fields: tuple[str, ...]) -> list[dict[str, Any]]: ...


class AllowlistedProxyScaleClient:
    """Wrap a client created by ``tushare_sdk_proxy.create_proxy_client``.

    The wrapped SDK owns its credential internally. This adapter never accepts,
    reads, logs or serializes a token.
    """

    offline_fixture = False

    def __init__(self, sdk_client: Any) -> None:
        self._sdk_client = sdk_client

    def query(self, api_name: str, *, params: Mapping[str, Any], fields: tuple[str, ...]) -> list[dict[str, Any]]:
        _validate_query(api_name, params=params, fields=fields)
        try:
            table = getattr(self._sdk_client, api_name)(**dict(params), fields=",".join(fields))
        except Exception as exc:
            raise TushareProxyError(f"approved proxy API unavailable ({api_name}:{type(exc).__name__})") from exc
        return _rows(table)


def _validate_query(api_name: str, *, params: Mapping[str, Any], fields: tuple[str, ...]) -> None:
    allowed = API_FIELD_ALLOWLIST.get(api_name)
    if allowed is None:
        raise TushareProxyError("API outside approved scale acquisition allowlist")
    if not fields or not set(fields).issubset(allowed):
        raise TushareProxyError("field outside approved scale acquisition allowlist")
    forbidden_params = {"token", "api_key", "authorization", "url", "endpoint"}
    if any(str(key).lower() in forbidden_params for key in params):
        raise TushareProxyError("credential or endpoint injection is forbidden")


def _rows(table: Any) -> list[dict[str, Any]]:
    if isinstance(table, list):
        return [_clean_row(row) for row in table if isinstance(row, Mapping)]
    if hasattr(table, "to_dict"):
        try:
            return [_clean_row(row) for row in table.to_dict(orient="records")]
        except Exception:
            return []
    return []


def _clean_row(row: Mapping[str, Any]) -> dict[str, Any]:
    return {str(key): _clean_scalar(value) for key, value in row.items()}


def _clean_scalar(value: Any) -> Any:
    """Normalize table-library missing scalars at the provider boundary."""

    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, Real):
        return value if math.isfinite(float(value)) else None
    if type(value).__name__ in {"NAType", "NaTType"}:
        return None
    item = getattr(value, "item", None)
    if callable(item):
        return _clean_scalar(item())
    return value


__all__ = ["API_FIELD_ALLOWLIST", "AllowlistedProxyScaleClient", "ProxyScaleQueryClient"]
