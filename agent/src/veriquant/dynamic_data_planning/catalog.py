"""Deterministic dataset and research-group catalogs."""

from __future__ import annotations

from dataclasses import dataclass

from .models import DataRequirement, RequirementLevel, ResearchTheme


def requirement_catalog() -> dict[str, DataRequirement]:
    """Return new immutable requirement values on every call."""

    return {
        "market_daily": DataRequirement(
            dataset_id="market_daily",
            api_names=("daily", "adj_factor", "trade_cal"),
            required_fields=("ts_code", "trade_date", "close", "vol", "adj_factor"),
            optional_fields=("open", "high", "low", "amount"),
            time_semantics="event_time=trade_date close; available_at is after market close",
            purpose="PIT returns, labels, momentum, liquidity, and event response",
        ),
        "historical_universe": DataRequirement(
            dataset_id="historical_universe",
            api_names=("stock_basic", "index_weight", "trade_cal"),
            required_fields=("ts_code", "list_date", "delist_date"),
            optional_fields=("index_code", "in_date", "out_date", "industry"),
            time_semantics="membership is resolved as-of each historical decision date",
            purpose="prevent survivorship bias without imposing a symbol-count limit",
        ),
        "market_valuation": DataRequirement(
            dataset_id="market_valuation",
            api_names=("daily_basic",),
            required_fields=("ts_code", "trade_date", "total_mv"),
            optional_fields=("circ_mv", "pe_ttm", "pb", "turnover_rate"),
            time_semantics="available_at is after the corresponding market close",
            purpose="size, liquidity, and valuation controls",
        ),
        "fundamentals": DataRequirement(
            dataset_id="fundamentals",
            api_names=("income", "balancesheet", "cashflow", "fina_indicator"),
            required_fields=("ts_code", "end_date", "ann_date", "f_ann_date"),
            optional_fields=(
                "revenue", "n_income", "roe", "grossprofit_margin",
                "n_cashflow_act", "netprofit_yoy", "or_yoy",
            ),
            time_semantics="report_period=end_date; usable only at conservative available_at from announcement clocks",
            purpose="PIT profitability, quality, growth, and valuation research",
        ),
        "corporate_events": DataRequirement(
            dataset_id="corporate_events",
            api_names=("forecast", "express"),
            required_fields=("ts_code", "end_date", "ann_date"),
            optional_fields=("type", "p_change_min", "p_change_max", "n_income"),
            time_semantics="event_time and publication/available_at are distinct; revisions are versioned",
            purpose="earnings forecast, express report, and announcement event studies",
        ),
        "money_flow": DataRequirement(
            dataset_id="money_flow",
            api_names=("moneyflow",),
            required_fields=("ts_code", "trade_date"),
            optional_fields=("buy_lg_amount", "sell_lg_amount", "net_mf_amount"),
            time_semantics="available_at is after the corresponding market close",
            purpose="capital-flow and order-size-bucket hypotheses",
        ),
        "macro_regime": DataRequirement(
            dataset_id="macro_regime",
            api_names=("shibor", "index_daily"),
            required_fields=("date",),
            optional_fields=("on", "1w", "close", "pct_chg", "vol"),
            time_semantics="each series has explicit release time and first tradable decision time",
            purpose="interest-rate and broad-market regime conditioning",
        ),
        "registered_research": DataRequirement(
            dataset_id="registered_research",
            api_names=(),
            required_fields=("registered_factor_summary",),
            optional_fields=("public_validation_summary", "factor_exposure", "portfolio_weight"),
            time_semantics="only public train/validation artifacts visible before the decision time",
            purpose="incrementality and portfolio-gap analysis without sealed-evidence access",
        ),
    }


@dataclass(frozen=True)
class ResearchGroupSpec:
    group_id: str
    themes: tuple[ResearchTheme, ...]
    core_datasets: tuple[str, ...]
    useful_fields: tuple[str, ...]


def research_group_catalog() -> tuple[ResearchGroupSpec, ...]:
    return (
        ResearchGroupSpec(
            "market_structure",
            (ResearchTheme.MOMENTUM, ResearchTheme.MONEY_FLOW),
            ("market_daily", "historical_universe"),
            ("close", "vol", "amount", "turnover_rate"),
        ),
        ResearchGroupSpec(
            "fundamental",
            (ResearchTheme.FUNDAMENTAL,),
            ("market_daily", "fundamentals"),
            ("revenue", "n_income", "roe", "total_mv"),
        ),
        ResearchGroupSpec(
            "expectations_events",
            (ResearchTheme.EVENT, ResearchTheme.FUNDAMENTAL),
            ("market_daily", "corporate_events"),
            ("ann_date", "f_ann_date", "p_change_min", "p_change_max"),
        ),
        ResearchGroupSpec(
            "flow_liquidity",
            (ResearchTheme.MONEY_FLOW, ResearchTheme.MOMENTUM),
            ("market_daily", "money_flow"),
            ("net_mf_amount", "turnover_rate", "amount"),
        ),
        ResearchGroupSpec(
            "macro_regime",
            (ResearchTheme.MACRO,),
            ("market_daily", "macro_regime"),
            ("on", "1w", "pct_chg"),
        ),
        ResearchGroupSpec(
            "meta_alpha",
            (ResearchTheme.PORTFOLIO_INCREMENTAL, ResearchTheme.MOMENTUM),
            ("market_daily", "registered_research"),
            ("registered_factor_summary", "factor_exposure", "portfolio_weight"),
        ),
    )


THEME_DATASETS: dict[ResearchTheme, tuple[str, ...]] = {
    ResearchTheme.MOMENTUM: ("market_daily", "historical_universe", "market_valuation"),
    ResearchTheme.FUNDAMENTAL: ("market_daily", "historical_universe", "market_valuation", "fundamentals"),
    ResearchTheme.EVENT: ("market_daily", "historical_universe", "corporate_events"),
    ResearchTheme.MONEY_FLOW: ("market_daily", "historical_universe", "market_valuation", "money_flow"),
    ResearchTheme.MACRO: ("market_daily", "historical_universe", "macro_regime"),
    ResearchTheme.PORTFOLIO_INCREMENTAL: (
        "market_daily",
        "historical_universe",
        "registered_research",
    ),
}
