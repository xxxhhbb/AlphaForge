"""Research question -> deterministic dynamic data plan compiler."""

from __future__ import annotations

from dataclasses import replace

from .catalog import THEME_DATASETS, requirement_catalog
from .models import (
    DataPlanningRequest,
    DynamicDataPlan,
    HistorySpec,
    ResearchTheme,
    SCHEMA_VERSION,
    TimeRangeSpec,
    UniverseKind,
    UniverseSpec,
)


AUTHORITY_RESTRICTIONS = (
    "LLM cannot access provider credentials or data-source secrets",
    "LLM cannot calculate or assert official evidence",
    "LLM cannot approve candidate promotion",
    "LLM cannot access sealed evaluation artifacts",
)


class DynamicDataRequirementCompiler:
    """Compile user semantics; never fetch data or silently shrink scope."""

    def compile(
        self,
        request: DataPlanningRequest | str,
        research_mode: object | None = None,
        **overrides: object,
    ) -> DynamicDataPlan:
        if isinstance(request, str):
            if research_mode is None:
                raise ValueError("research_mode is required when request is question text")
            request = DataPlanningRequest(
                question=request,
                research_mode=getattr(research_mode, "value", research_mode),
                **overrides,
            )
        elif research_mode is not None or overrides:
            raise ValueError("research_mode/overrides are only valid with question text")
        if not isinstance(request, DataPlanningRequest):
            raise ValueError("request must be DataPlanningRequest or question text")

        themes = infer_themes(request.question, request.research_mode)
        catalog = requirement_catalog()
        dataset_ids = _ordered_union(THEME_DATASETS[theme] for theme in themes)
        requirements = tuple(
            _select_minimum_apis(
                _adapt_requirement(
                    _promote_question_fields(catalog[dataset_id], request.question),
                    request.universe,
                ),
                request.question,
            )
            for dataset_id in dataset_ids
        )
        warmup = request.factor_lookback_sessions or _default_warmup(themes)
        history = HistorySpec(
            warmup_sessions=warmup,
            train_sessions=request.train_sessions,
            validation_sessions=request.validation_sessions,
            sealed_sessions=request.sealed_sessions,
            label_sessions=request.label_horizon_sessions,
            buffer_sessions=request.buffer_sessions,
        )
        universe = _resolve_universe(request.universe, themes)
        api_names = _ordered_union(item.api_names for item in requirements)
        requested_fields = _ordered_union(
            item.required_fields + item.optional_fields for item in requirements
        )
        return DynamicDataPlan(
            schema_version=SCHEMA_VERSION,
            question=request.question.strip(),
            research_mode=request.research_mode,
            themes=themes,
            universe=universe,
            time_range=TimeRangeSpec(
                start_date=request.start_date,
                end_date=request.end_date,
                derived_minimum_sessions=history.minimum_total_sessions,
            ),
            history=history,
            requirements=requirements,
            requested_api_names=api_names,
            requested_fields=requested_fields,
            authority_restrictions=AUTHORITY_RESTRICTIONS,
        )


def infer_themes(question: str, research_mode: str) -> tuple[ResearchTheme, ...]:
    text = question.casefold()
    mode = getattr(research_mode, "value", research_mode)
    scores = {theme: 0 for theme in ResearchTheme}
    keyword_map = {
        ResearchTheme.MOMENTUM: (
            "动量", "反转", "趋势", "momentum", "reversal", "price", "return", "收益率",
        ),
        ResearchTheme.FUNDAMENTAL: (
            "基本面", "盈利", "利润", "营收", "估值", "质量", "roe", "fundamental", "earnings", "value",
        ),
        ResearchTheme.EVENT: (
            "事件", "公告", "业绩预告", "财报发布", "event", "announcement", "forecast", "express",
        ),
        ResearchTheme.MONEY_FLOW: (
            "资金流", "北向", "大单", "流动性", "成交额", "money flow", "northbound", "liquidity",
        ),
        ResearchTheme.MACRO: (
            "宏观", "利率", "shibor", "市场状态", "regime", "macro", "指数环境",
        ),
        ResearchTheme.PORTFOLIO_INCREMENTAL: (
            "组合", "增量", "已有因子", "相关性", "portfolio", "incremental", "factor gap",
        ),
    }
    for theme, keywords in keyword_map.items():
        scores[theme] += sum(2 for keyword in keywords if keyword in text)

    mode_map = {
        "NEW_ALPHA_EXPLORATION": ResearchTheme.MOMENTUM,
        "VALIDATE_IDEA": ResearchTheme.MOMENTUM,
        "IMPROVE_EXISTING_FACTOR": ResearchTheme.PORTFOLIO_INCREMENTAL,
        "PORTFOLIO_GAP_SEARCH": ResearchTheme.PORTFOLIO_INCREMENTAL,
        "DIAGNOSE_FACTOR_PORTFOLIO": ResearchTheme.PORTFOLIO_INCREMENTAL,
        "EVENT_MARKET_TOPIC": ResearchTheme.EVENT,
        "PAPER_STRATEGY_REPLICATION": ResearchTheme.MOMENTUM,
        "NEW_DATA_SOURCE_EVALUATION": ResearchTheme.MOMENTUM,
        "PRELAUNCH_REVIEW": ResearchTheme.PORTFOLIO_INCREMENTAL,
        "MONITOR_DECAY_RETIREMENT": ResearchTheme.PORTFOLIO_INCREMENTAL,
    }
    scores[mode_map.get(str(mode), ResearchTheme.MOMENTUM)] += 1
    selected = [theme for theme, score in scores.items() if score > 0]
    selected.sort(key=lambda theme: (-scores[theme], theme.value))
    return tuple(selected)


def _default_warmup(themes: tuple[ResearchTheme, ...]) -> int:
    defaults = {
        ResearchTheme.MOMENTUM: 20,
        ResearchTheme.FUNDAMENTAL: 4,
        ResearchTheme.EVENT: 20,
        ResearchTheme.MONEY_FLOW: 20,
        ResearchTheme.MACRO: 60,
        ResearchTheme.PORTFOLIO_INCREMENTAL: 20,
    }
    return max(defaults[theme] for theme in themes)


def _resolve_universe(universe: UniverseSpec, themes: tuple[ResearchTheme, ...]) -> UniverseSpec:
    if (
        ResearchTheme.PORTFOLIO_INCREMENTAL in themes
        and universe.kind is UniverseKind.HISTORICAL_ALL_A_SHARE
    ):
        # Do not invent a portfolio id. All-market PIT remains a valid search
        # universe; registered portfolio history is requested only explicitly.
        return universe
    return universe


def _adapt_requirement(requirement, universe: UniverseSpec):
    """Remove universe APIs that do not apply to the requested population."""

    if requirement.dataset_id != "historical_universe":
        return requirement
    if universe.kind is UniverseKind.HISTORICAL_INDEX_CONSTITUENTS:
        api_names = ("index_weight", "trade_cal")
    else:
        api_names = ("stock_basic", "trade_cal")
    return replace(requirement, api_names=api_names)


def _promote_question_fields(requirement, question: str):
    """Promote explicitly researched values; clocks remain provenance fields."""

    text = question.casefold()
    candidates: dict[str, tuple[tuple[tuple[str, ...], str], ...]] = {
        "market_valuation": (
            (("市净率", "pb", "账面价值"), "pb"),
            (("市盈率", "pe", "盈利收益率"), "pe_ttm"),
            (("换手", "turnover"), "turnover_rate"),
        ),
        "fundamentals": (
            (("roe", "净资产收益率", "质量"), "roe"),
            (("盈利", "利润", "net income"), "n_income"),
            (("营收", "收入", "revenue"), "revenue"),
            (("现金流", "cash flow"), "n_cashflow_act"),
            (("毛利", "gross margin"), "grossprofit_margin"),
            (("成长", "增速", "同比"), "netprofit_yoy"),
        ),
        "corporate_events": (
            (("预告", "forecast"), "p_change_min"),
            (("预告", "forecast"), "p_change_max"),
            (("快报", "express"), "n_income"),
        ),
        "money_flow": (
            (("资金流", "大单", "money flow"), "net_mf_amount"),
        ),
        "macro_regime": (
            (("shibor", "利率"), "on"),
            (("市场状态", "指数", "regime"), "pct_chg"),
        ),
    }
    promoted = {
        field
        for keywords, field in candidates.get(requirement.dataset_id, ())
        if any(keyword in text for keyword in keywords)
    }
    if requirement.dataset_id == "fundamentals" and not promoted and "基本面" in text:
        promoted.add("roe")
    if not promoted:
        return requirement
    available = set(requirement.required_fields) | set(requirement.optional_fields)
    promoted &= available
    return replace(
        requirement,
        required_fields=tuple(sorted(set(requirement.required_fields) | promoted)),
        optional_fields=tuple(sorted(set(requirement.optional_fields) - promoted)),
    )


def _select_minimum_apis(requirement, question: str):
    """Avoid turning one requested feature into unrelated mandatory calls.

    Dataset contracts still retain every required field and time semantic.  We
    only remove APIs that cannot supply an explicitly requested value.  Broad
    requests (for example, simply "基本面") deliberately keep the full source
    family so discovery remains broad.
    """

    text = question.casefold()
    if requirement.dataset_id == "fundamentals":
        if "基本面" in text and any(token in text for token in ("结合", "综合", "全面", "多维")):
            return requirement
        selected: set[str] = set()
        if any(token in text for token in ("roe", "roa", "质量", "毛利", "净利率", "同比", "成长", "增速")):
            selected.add("fina_indicator")
        if any(token in text for token in ("营收", "收入", "利润", "盈利", "eps", "income")):
            selected.add("income")
        if any(token in text for token in ("资产", "负债", "杠杆", "账面", "balance")):
            selected.add("balancesheet")
        if any(token in text for token in ("现金流", "cash flow", "cashflow")):
            selected.add("cashflow")
        if selected:
            return replace(requirement, api_names=tuple(sorted(selected)))
    elif requirement.dataset_id == "corporate_events":
        selected = set()
        if any(token in text for token in ("预告", "forecast")):
            selected.add("forecast")
        if any(token in text for token in ("快报", "express")):
            selected.add("express")
        if selected:
            return replace(requirement, api_names=tuple(sorted(selected)))
    elif requirement.dataset_id == "macro_regime":
        selected = set()
        if any(token in text for token in ("shibor", "利率", "资金价格")):
            selected.add("shibor")
        if any(token in text for token in ("指数", "市场状态", "regime", "大盘")):
            selected.add("index_daily")
        if selected:
            return replace(requirement, api_names=tuple(sorted(selected)))
    return requirement


def _ordered_union(groups: object) -> tuple[str, ...]:
    values: set[str] = set()
    for group in groups:
        values.update(group)
    return tuple(sorted(values))
