"""Fail-closed Tushare-compatible source normalizers and time policies."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Any, Iterable, Mapping
from zoneinfo import ZoneInfo

from .models import (
    BitemporalError,
    EvidenceEligibility,
    NormalizationBatch,
    NormalizationIssue,
    NormalizedRecord,
    SourceKind,
    UnknownTimePolicy,
    canonical_hash,
)


SHANGHAI = ZoneInfo("Asia/Shanghai")

_MARKET_FIELDS: dict[SourceKind, dict[str, str]] = {
    SourceKind.DAILY: {
        "open": "open",
        "high": "high",
        "low": "low",
        "close": "close",
        "pre_close": "pre_close",
        "change": "price_change",
        "pct_chg": "pct_change",
        "vol": "volume",
        "amount": "amount",
    },
    SourceKind.INDEX_DAILY: {
        "open": "index_open",
        "high": "index_high",
        "low": "index_low",
        "close": "index_close",
        "pre_close": "index_pre_close",
        "change": "index_price_change",
        "pct_chg": "index_pct_change",
        "vol": "index_volume",
        "amount": "index_amount",
    },
    SourceKind.DAILY_BASIC: {
        "turnover_rate": "turnover_rate",
        "turnover_rate_f": "free_float_turnover_rate",
        "volume_ratio": "volume_ratio",
        "pe": "pe",
        "pe_ttm": "pe_ttm",
        "pb": "pb",
        "ps": "ps",
        "ps_ttm": "ps_ttm",
        "dv_ratio": "dividend_yield",
        "dv_ttm": "dividend_yield_ttm",
        "total_share": "total_shares",
        "float_share": "float_shares",
        "free_share": "free_float_shares",
        "total_mv": "market_cap",
        "circ_mv": "float_market_cap",
    },
    SourceKind.ADJ_FACTOR: {
        "adj_factor": "adjustment_factor",
    },
    SourceKind.MONEYFLOW: {
        "buy_sm_vol": "buy_small_volume",
        "buy_sm_amount": "buy_small_amount",
        "sell_sm_vol": "sell_small_volume",
        "sell_sm_amount": "sell_small_amount",
        "buy_md_vol": "buy_medium_volume",
        "buy_md_amount": "buy_medium_amount",
        "sell_md_vol": "sell_medium_volume",
        "sell_md_amount": "sell_medium_amount",
        "buy_lg_vol": "buy_large_volume",
        "buy_lg_amount": "buy_large_amount",
        "sell_lg_vol": "sell_large_volume",
        "sell_lg_amount": "sell_large_amount",
        "buy_elg_vol": "buy_extra_large_volume",
        "buy_elg_amount": "buy_extra_large_amount",
        "sell_elg_vol": "sell_extra_large_volume",
        "sell_elg_amount": "sell_extra_large_amount",
        "net_mf_vol": "net_moneyflow_volume",
        "net_mf_amount": "net_moneyflow_amount",
    },
}

_FINANCIAL_FIELDS: dict[SourceKind, dict[str, str]] = {
    SourceKind.INCOME: {
        "revenue": "revenue",
        "operate_profit": "operating_profit",
        "total_profit": "total_profit",
        "n_income": "net_income",
        "n_income_attr_p": "net_income_parent",
        "basic_eps": "basic_eps",
        "diluted_eps": "diluted_eps",
    },
    SourceKind.BALANCESHEET: {
        "total_assets": "total_assets",
        "total_liab": "total_liabilities",
        "total_hldr_eqy_exc_min_int": "book_value",
        "money_cap": "cash_and_equivalents",
        "accounts_receiv": "accounts_receivable",
        "inventories": "inventories",
    },
    SourceKind.CASHFLOW: {
        "n_cashflow_act": "operating_cashflow",
        "n_cashflow_inv_act": "investing_cashflow",
        "n_cash_flows_fnc_act": "financing_cashflow",
        "c_cash_equ_end_period": "cash_end_period",
    },
    SourceKind.FINA_INDICATOR: {
        "roe": "roe",
        "roa": "roa",
        "grossprofit_margin": "gross_margin",
        "netprofit_margin": "net_margin",
        "debt_to_assets": "debt_to_assets",
        "current_ratio": "current_ratio",
        "quick_ratio": "quick_ratio",
        "assets_turn": "asset_turnover",
        "or_yoy": "revenue_yoy",
        "netprofit_yoy": "net_income_yoy",
        "ocf_to_or": "operating_cashflow_to_revenue",
        "eps": "eps",
        "bps": "book_value_per_share",
    },
    SourceKind.FORECAST: {
        "p_change_min": "forecast_profit_change_min",
        "p_change_max": "forecast_profit_change_max",
        "net_profit_min": "forecast_net_profit_min",
        "net_profit_max": "forecast_net_profit_max",
        "last_parent_net": "forecast_last_parent_net_income",
    },
    SourceKind.EXPRESS: {
        "revenue": "express_revenue",
        "operate_profit": "express_operating_profit",
        "total_profit": "express_total_profit",
        "n_income": "express_net_income",
        "total_assets": "express_total_assets",
        "diluted_eps": "express_diluted_eps",
        "diluted_roe": "express_diluted_roe",
        "yoy_sales": "express_revenue_yoy",
        "yoy_net_profit": "express_net_income_yoy",
    },
}

_SHIBOR_FIELDS = {
    "on": "shibor_overnight",
    "1w": "shibor_1w",
    "2w": "shibor_2w",
    "1m": "shibor_1m",
    "3m": "shibor_3m",
    "6m": "shibor_6m",
    "9m": "shibor_9m",
    "1y": "shibor_1y",
}


def normalize_rows(
    source_name: str,
    rows: Iterable[Mapping[str, Any]],
    *,
    decision_time: datetime | None = None,
    trading_sessions: Iterable[date] | None = None,
    unknown_policy: UnknownTimePolicy = UnknownTimePolicy.AUXILIARY_ONLY,
) -> NormalizationBatch:
    """Normalize source rows without silently inventing unknown time semantics."""

    try:
        source = SourceKind(source_name)
    except ValueError:
        if unknown_policy is UnknownTimePolicy.FAIL_CLOSED:
            raise BitemporalError(f"unknown time semantics for source: {source_name}")
        issue = NormalizationIssue(
            source_name=source_name,
            row_number=-1,
            code="UNKNOWN_TIME_SEMANTICS",
            message="source is AUXILIARY_ONLY and produced no formal feature records",
        )
        return NormalizationBatch(
            source_name=source_name,
            records=(),
            eligibility=EvidenceEligibility.AUXILIARY_ONLY,
            issues=(issue,),
        )
    if decision_time is not None:
        _require_aware(decision_time, "decision_time")
    frozen_sessions = (
        None
        if trading_sessions is None
        else tuple(sorted(set(trading_sessions)))
    )

    output: list[NormalizedRecord] = []
    for row_number, row in enumerate(rows):
        if not isinstance(row, Mapping):
            raise BitemporalError(f"{source_name}[{row_number}] must be a mapping")
        output.append(
            _normalize(
                source,
                row,
                decision_time=decision_time,
                trading_sessions=frozen_sessions,
            )
        )
    ordered = tuple(
        sorted(
            output,
            key=lambda item: (
                item.entity_id,
                item.event_time,
                item.available_at,
                item.revision_id,
            ),
        )
    )
    return NormalizationBatch(
        source_name=source.value,
        records=ordered,
        eligibility=EvidenceEligibility.FEATURE_ELIGIBLE,
    )


def normalize_row(
    source_name: str,
    row: Mapping[str, Any],
    *,
    decision_time: datetime | None = None,
    trading_sessions: Iterable[date] | None = None,
) -> NormalizedRecord:
    batch = normalize_rows(
        source_name,
        (row,),
        decision_time=decision_time,
        trading_sessions=trading_sessions,
        unknown_policy=UnknownTimePolicy.FAIL_CLOSED,
    )
    return batch.records[0]


def _normalize(
    source: SourceKind,
    row: Mapping[str, Any],
    *,
    decision_time: datetime | None,
    trading_sessions: tuple[date, ...] | None,
) -> NormalizedRecord:
    raw_hash = canonical_hash(dict(row))
    if source in (
        SourceKind.DAILY,
        SourceKind.DAILY_BASIC,
        SourceKind.ADJ_FACTOR,
        SourceKind.INDEX_DAILY,
    ):
        entity = _text(row, "ts_code")
        session = _date(row, "trade_date")
        event = _at(session, time(15, 0))
        published = _at(session, time(15, 30))
        available = _at(session, time(16, 0))
        values = _numeric_values(row, _MARKET_FIELDS[source])
        if source is SourceKind.DAILY_BASIC:
            values.update(_valuation_reciprocals(values))
    elif source is SourceKind.MONEYFLOW:
        entity = _text(row, "ts_code")
        session = _date(row, "trade_date")
        event = _at(session, time(15, 0))
        published = _at(session, time(23, 59, 59, 999999))
        available = _next_market_open(session, trading_sessions)
        values = _numeric_values(row, _MARKET_FIELDS[source])
    elif source in _FINANCIAL_FIELDS:
        entity = _text(row, "ts_code")
        period = _date(row, "end_date")
        event = _at(period, time(0, 0))
        actual = _optional_date(row.get("f_ann_date"))
        announced = _date(row, "ann_date")
        publication_day = max(item for item in (actual, announced) if item is not None)
        published = _at(publication_day, time(23, 59, 59, 999999))
        available = _next_market_open(publication_day, trading_sessions)
        values = _numeric_values(row, _FINANCIAL_FIELDS[source])
    elif source is SourceKind.SHIBOR:
        entity = "MARKET:CNY"
        session = _date(row, "date")
        event = _at(session, time(0, 0))
        published = _at(session, time(23, 59, 59, 999999))
        available = _next_market_open(session, trading_sessions)
        values = _numeric_values(row, _SHIBOR_FIELDS)
    else:  # pragma: no cover - SourceKind makes this unreachable
        raise BitemporalError(f"no time policy for {source.value}")

    if decision_time is not None and available > decision_time:
        raise BitemporalError(
            f"{source.value} record is unavailable at decision_time"
        )
    revision_id = _revision_id(source, row, raw_hash)
    return NormalizedRecord(
        source=source,
        entity_id=entity,
        event_time=event,
        published_at=published,
        available_at=available,
        decision_time=decision_time,
        revision_id=revision_id,
        values=values,
        raw_hash=raw_hash,
    )


def _revision_id(source: SourceKind, row: Mapping[str, Any], raw_hash: str) -> str:
    explicit = row.get("revision_id")
    if explicit is not None and str(explicit).strip():
        return str(explicit).strip()
    update = str(row.get("update_flag", "0")).strip() or "0"
    return f"{source.value}:{update}:{raw_hash[7:23]}"


def _numeric_values(
    row: Mapping[str, Any], mapping: Mapping[str, str]
) -> dict[str, int | float | None]:
    values: dict[str, int | float | None] = {}
    for source_field, output_field in mapping.items():
        raw = row.get(source_field)
        if raw is None or raw == "":
            values[output_field] = None
            continue
        if isinstance(raw, bool):
            raise BitemporalError(f"{source_field} must be numeric")
        try:
            value = float(raw)
        except (TypeError, ValueError) as exc:
            raise BitemporalError(f"{source_field} must be numeric") from exc
        if value != value or value in (float("inf"), float("-inf")):
            raise BitemporalError(f"{source_field} must be finite")
        values[output_field] = value
    return values


def _valuation_reciprocals(
    values: Mapping[str, int | float | None],
) -> dict[str, float | None]:
    def reciprocal(field: str) -> float | None:
        value = values.get(field)
        if value is None or float(value) <= 0:
            return None
        return 1.0 / float(value)

    return {
        "earnings_yield": reciprocal("pe_ttm"),
        "book_to_market": reciprocal("pb"),
    }


def _text(row: Mapping[str, Any], field: str) -> str:
    value = row.get(field)
    if not isinstance(value, str) or not value.strip():
        raise BitemporalError(f"{field} is required")
    return value.strip()


def _date(row: Mapping[str, Any], field: str) -> date:
    value = _optional_date(row.get(field))
    if value is None:
        raise BitemporalError(f"{field} is required")
    return value


def _optional_date(value: Any) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    candidate = str(value).strip()
    try:
        if len(candidate) == 8 and candidate.isdigit():
            return datetime.strptime(candidate, "%Y%m%d").date()
        return date.fromisoformat(candidate)
    except ValueError as exc:
        raise BitemporalError(f"invalid date: {candidate}") from exc


def _at(value: date, clock: time) -> datetime:
    return datetime.combine(value, clock, tzinfo=SHANGHAI)


def _next_day_open(value: date) -> datetime:
    # A source row alone cannot identify exchange holidays, but it can safely
    # avoid treating weekends as tradable. A supplied exchange calendar remains
    # the authority for holiday-specific decision anchors.
    candidate = value + timedelta(days=1)
    while candidate.weekday() >= 5:
        candidate += timedelta(days=1)
    return _at(candidate, time(9, 30))


def _next_market_open(
    value: date,
    trading_sessions: tuple[date, ...] | None,
) -> datetime:
    if trading_sessions is None:
        return _next_day_open(value)
    candidate = next((item for item in trading_sessions if item > value), None)
    if candidate is None:
        raise BitemporalError(
            "frozen trading calendar does not contain the next session"
        )
    return _at(candidate, time(9, 30))


def _require_aware(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise BitemporalError(f"{name} must be timezone-aware")
