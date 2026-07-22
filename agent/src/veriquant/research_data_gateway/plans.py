"""Deterministic plans for complete A-share research-data acquisition."""

from __future__ import annotations

from datetime import datetime

from ..dynamic_data_planning import (
    AUTHORITY_RESTRICTIONS,
    DataRequirement,
    DynamicDataPlan,
    HistorySpec,
    RequirementLevel,
    ResearchTheme,
    SCHEMA_VERSION as PLAN_SCHEMA_VERSION,
    TimeRangeSpec,
    UniverseKind,
    UniverseSpec,
)


OFFICIAL_API_NAMES = (
    "adj_factor", "balancesheet", "cashflow", "daily", "daily_basic",
    "express", "fina_indicator", "forecast", "income", "index_daily",
    "moneyflow", "shibor", "stock_basic", "trade_cal",
)


def build_reference_plan(*, start_date: str, end_date: str) -> DynamicDataPlan:
    return _plan(
        question="freeze full historical A-share universe and SSE/SZSE calendars",
        start_date=start_date,
        end_date=end_date,
        requirements=(
            DataRequirement(
                dataset_id="historical_universe",
                api_names=("stock_basic", "trade_cal"),
                required_fields=("ts_code", "list_status", "list_date", "cal_date", "is_open"),
                optional_fields=("delist_date", "industry", "pretrade_date"),
                level=RequirementLevel.REQUIRED,
                time_semantics="LISTING_INTERVAL_AND_EXCHANGE_SESSION",
                purpose="discover the complete historical SSE/SZSE research population",
            ),
        ),
        themes=(ResearchTheme.MOMENTUM,),
    )


def build_comprehensive_plan(*, start_date: str, end_date: str) -> DynamicDataPlan:
    requirements = (
        DataRequirement(dataset_id="historical_universe", api_names=("stock_basic", "trade_cal"), required_fields=("ts_code", "list_date", "cal_date", "is_open"), optional_fields=("delist_date", "industry"), time_semantics="LISTING_INTERVAL_AND_EXCHANGE_SESSION", purpose="PIT universe and calendars"),
        DataRequirement(dataset_id="market", api_names=("daily", "adj_factor", "daily_basic"), required_fields=("open", "high", "low", "close", "vol", "amount", "adj_factor", "total_mv"), optional_fields=("turnover_rate", "pe_ttm", "pb", "circ_mv"), time_semantics="POST_CLOSE_AVAILABLE", purpose="market spine, adjusted prices, valuation and liquidity"),
        DataRequirement(dataset_id="fundamentals", api_names=("fina_indicator", "income", "balancesheet", "cashflow"), required_fields=("ann_date", "end_date"), optional_fields=("roe", "revenue", "n_income", "total_assets", "total_liab", "n_cashflow_act"), time_semantics="ANNOUNCEMENT_AVAILABLE_WITH_REVISIONS", purpose="PIT financial statements and indicators"),
        DataRequirement(dataset_id="flow_macro", api_names=("moneyflow", "index_daily", "shibor"), required_fields=("trade_date",), optional_fields=("net_mf_amount", "pct_chg", "on"), time_semantics="MARKET_OR_CALENDAR_PUBLICATION", purpose="capital flow, index and money-market regime"),
        DataRequirement(dataset_id="corporate_events", api_names=("forecast", "express"), required_fields=("ann_date", "end_date"), optional_fields=("p_change_min", "p_change_max", "n_income", "yoy_net_profit"), time_semantics="ANNOUNCEMENT_EVENT_CLOCK", purpose="earnings forecasts and performance express reports"),
    )
    return _plan(
        question="freeze comprehensive A-share research data",
        start_date=start_date,
        end_date=end_date,
        requirements=requirements,
        themes=(ResearchTheme.MOMENTUM, ResearchTheme.FUNDAMENTAL, ResearchTheme.EVENT, ResearchTheme.MONEY_FLOW, ResearchTheme.MACRO),
    )


def build_financial_event_canary_plan(*, start_date: str, end_date: str) -> DynamicDataPlan:
    """Bounded representative-symbol plan; never implies full-market coverage."""

    requirements = (
        DataRequirement(
            dataset_id="fundamentals_canary",
            api_names=("fina_indicator", "income", "balancesheet", "cashflow"),
            required_fields=("ts_code", "ann_date", "end_date"),
            optional_fields=("roe", "revenue", "n_income", "total_assets", "n_cashflow_act"),
            time_semantics="ANNOUNCEMENT_AVAILABLE_WITH_REVISIONS",
            purpose="verify approved-proxy per-symbol financial acquisition",
        ),
        DataRequirement(
            dataset_id="corporate_events_canary",
            api_names=("forecast", "express"),
            required_fields=("ts_code", "ann_date", "end_date"),
            optional_fields=("p_change_min", "p_change_max", "n_income", "yoy_net_profit"),
            time_semantics="ANNOUNCEMENT_EVENT_CLOCK",
            purpose="verify approved-proxy per-symbol event acquisition",
        ),
    )
    return _plan(
        question="verify bounded per-symbol financial and event acquisition",
        start_date=start_date,
        end_date=end_date,
        requirements=requirements,
        themes=(ResearchTheme.FUNDAMENTAL, ResearchTheme.EVENT),
    )


def _plan(*, question: str, start_date: str, end_date: str, requirements, themes) -> DynamicDataPlan:
    start = datetime.strptime(start_date, "%Y%m%d").date()
    end = datetime.strptime(end_date, "%Y%m%d").date()
    apis = tuple(sorted({api for item in requirements for api in item.api_names}))
    fields = tuple(sorted({field for item in requirements for field in item.required_fields + item.optional_fields}))
    return DynamicDataPlan(
        schema_version=PLAN_SCHEMA_VERSION,
        question=question,
        research_mode="NEW_DATA_SOURCE_EVALUATION",
        themes=themes,
        universe=UniverseSpec(kind=UniverseKind.HISTORICAL_ALL_A_SHARE),
        time_range=TimeRangeSpec(start_date=start, end_date=end, derived_minimum_sessions=1),
        history=HistorySpec(warmup_sessions=0, train_sessions=0, validation_sessions=0, sealed_sessions=0, label_sessions=1, buffer_sessions=0),
        requirements=requirements,
        requested_api_names=apis,
        requested_fields=fields,
        authority_restrictions=AUTHORITY_RESTRICTIONS,
    )


__all__ = [
    "OFFICIAL_API_NAMES",
    "build_comprehensive_plan",
    "build_financial_event_canary_plan",
    "build_reference_plan",
]
