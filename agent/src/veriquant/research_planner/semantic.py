"""Composable, deterministic natural-language semantics for A-share research."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable

from ..research_campaign.models import ResearchDepth, ResearchMode
from .models_v2 import IntentResolution, ResearchIntentV2


DEFAULT_DECISION_TIME = datetime(2026, 7, 19, 9, 30, tzinfo=timezone.utc)


@dataclass(frozen=True)
class SemanticRule:
    tokens: tuple[str, ...]
    required_data: tuple[str, ...] = ()
    optional_data: tuple[str, ...] = ()
    neutralization: tuple[str, ...] = ()
    costs: tuple[str, ...] = ()

    def matches(self, text: str) -> bool:
        lowered = text.lower()
        return any(token.lower() in lowered for token in self.tokens)


_RULES = (
    # Gap research cannot be evaluated from close-only bars.  This composes
    # with the general price rule below, promoting ``open`` only for questions
    # that explicitly study an overnight/gap mechanism.
    SemanticRule(("隔夜", "跳空"), ("open", "close", "adj_factor"), ("volume",), (), ("turnover", "spread")),
    SemanticRule(("动量", "反转", "波动", "突破", "跳空", "价格"), ("close", "adj_factor", "volume"), ("open", "high", "low"), ("industry", "market_cap"), ("turnover", "spread", "adv")),
    SemanticRule(("放量", "成交量", "成交额"), ("close", "volume", "amount"), (), (), ("turnover", "adv")),
    SemanticRule(("换手", "流动性"), ("close", "volume", "turnover_rate"), ("amount",), (), ("turnover", "spread", "adv")),
    SemanticRule(("pb", "pe", "估值", "盈利收益率"), ("pb", "pe", "available_at"), ("market_cap",), ("industry", "market_cap")),
    SemanticRule(("roe", "负债", "质量"), ("roe", "debt_to_assets", "available_at"), ("gross_margin",), ("industry", "market_cap")),
    SemanticRule(("营收", "利润增速", "成长"), ("revenue", "net_income", "available_at"), ("roe",), ("industry", "market_cap")),
    SemanticRule(("现金流",), ("operating_cashflow", "net_income", "available_at"), ("free_cashflow_proxy",), ("industry", "market_cap")),
    SemanticRule(("盈利预测", "预期", "上调", "分析师"), ("earnings_forecast", "published_at", "available_at", "close"), ("earnings_surprise",), ("industry", "market_cap")),
    SemanticRule(("超预期", "公告后", "财报后", "分红", "回购", "指数纳入", "指数剔除"), ("event_time", "available_at", "close"), ("benchmark_return",), (), ("turnover", "spread")),
    SemanticRule(("利率", "shibor", "宏观"), ("shibor_1w", "close", "available_at"), ("benchmark_return",)),
    SemanticRule(("高波动市场", "市场状态", "牛熊"), ("benchmark_return", "close", "available_at"), ("market_breadth_proxy",)),
    SemanticRule(("大单", "资金流"), ("moneyflow_net", "volume", "close"), ("buy_amount", "sell_amount"), (), ("turnover", "adv")),
    SemanticRule(("新闻", "情绪", "另类数据"), ("documents", "published_at", "available_at", "entity_mapping")),
    SemanticRule(("低相关", "组合缺口", "残差", "增量"), ("registered_factor_summary",), ("portfolio_returns",), (), ("turnover", "capacity")),
    SemanticRule(("失效", "回撤", "诊断", "衰减", "继续保留"), ("registered_evidence",), ("cost_history", "exposure_history", "regime_history")),
    SemanticRule(("论文", "复现", "研报"), ("source_document", "definition", "available_at"), ("field_mapping",)),
    SemanticRule(("上线", "容量", "压力", "监控"), ("registered_evidence", "cost_history"), ("capacity", "stress_scenarios", "drift_history")),
)

_INJECTION = (
    "ignore previous", "system prompt", "developer message", "tool_call", "api_key",
    "official_metrics", "promotion", "risk_approval", "target_weights", "orders", "broker", "ledger",
    "忽略规则", "系统提示", "开发者消息", "调用工具", "官方指标", "晋级", "风险审批", "真实下单", "密钥",
)


class DeterministicResearchIntentCompiler:
    def compile(
        self,
        text: str,
        *,
        decision_time: datetime = DEFAULT_DECISION_TIME,
        universe: tuple[str, ...] = ("CN_ASHARE_APPROVED_PIT_SAMPLE",),
        depth: ResearchDepth | None = None,
        existing_factor_ids: tuple[str, ...] = (),
        portfolio_id: str | None = None,
    ) -> ResearchIntentV2:
        sanitized, removed = sanitize(text)
        mode = detect_mode(sanitized)
        lookbacks = extract_lookbacks(sanitized)
        horizons = extract_forecasts(sanitized)
        relevant = is_research_question(sanitized)
        clarifications: list[str] = []
        assumptions: list[str] = []
        if removed:
            assumptions.append("privileged or prompt-injection instructions were removed")
        if not relevant:
            resolution = IntentResolution.UNSUPPORTED_SAFELY
            clarifications.append("请提出与 Alpha、因子、组合、市场数据或投资研究有关的问题。")
        else:
            if not horizons and mode not in {
                ResearchMode.DIAGNOSE_FACTOR_PORTFOLIO,
                ResearchMode.NEW_DATA_SOURCE_EVALUATION,
                ResearchMode.PAPER_STRATEGY_REPLICATION,
                ResearchMode.PRELAUNCH_REVIEW,
                ResearchMode.MONITOR_DECAY_RETIREMENT,
            }:
                horizons = (5, 20)
                assumptions.append("forecast horizons 5 and 20 are suggestions, not user-confirmed semantics")
                clarifications.append("请确认预测未来 5 日、20 日，或其他收益周期。")
            if mode is ResearchMode.IMPROVE_EXISTING_FACTOR and not existing_factor_ids and not _factor_ids(sanitized):
                clarifications.append("请提供要改进的 parent_factor_id。")
            if mode is ResearchMode.PORTFOLIO_GAP_SEARCH and not (portfolio_id or existing_factor_ids or _named_factors(sanitized)):
                clarifications.append("请提供现有 factor_id 或 portfolio_id 范围。")
            if mode is ResearchMode.DIAGNOSE_FACTOR_PORTFOLIO and not (
                portfolio_id or existing_factor_ids or _named_factors(sanitized)
            ):
                clarifications.append("请提供要诊断的 factor_id 或 portfolio_id。")
            if mode is ResearchMode.PAPER_STRATEGY_REPLICATION and not _has_source_document(sanitized):
                clarifications.append("请提供论文、研报或策略定义的来源标识。")
            if mode is ResearchMode.NEW_DATA_SOURCE_EVALUATION and not _has_data_source_semantics(sanitized):
                clarifications.append("请提供待评估数据源、字段范围及发布时间语义。")
            if mode in {ResearchMode.PRELAUNCH_REVIEW, ResearchMode.MONITOR_DECAY_RETIREMENT} and not (
                existing_factor_ids or _factor_ids(sanitized)
            ):
                clarifications.append("请提供要评审或监控的 factor_id。")
            resolution = IntentResolution.NEEDS_CLARIFICATION if clarifications else IntentResolution.COMPLETE_DRAFT
        required, optional, neutralization, costs = _requirements(sanitized)
        found_factors = existing_factor_ids or _factor_ids(sanitized) or _named_factors(sanitized)
        return ResearchIntentV2(
            question=sanitized,
            mode=mode,
            research_subject=_subject(mode, sanitized),
            factor_lookbacks=lookbacks,
            forecast_horizons=horizons,
            universe=universe,
            decision_time=decision_time,
            target_variable="future_excess_return" if "超额" in sanitized else "future_return",
            required_data=required,
            optional_data=optional,
            existing_factor_ids=found_factors,
            portfolio_id=portfolio_id,
            neutralization_requirements=neutralization,
            cost_capacity_requirements=costs,
            depth=depth or detect_depth(sanitized),
            assumptions=tuple(assumptions),
            clarification_questions=tuple(dict.fromkeys(clarifications))[:3],
            resolution=resolution,
        )


def sanitize(text: str) -> tuple[str, bool]:
    kept: list[str] = []
    removed = False
    for raw in text.replace("\r\n", "\n").split("\n"):
        line = raw.strip()
        lowered = line.lower()
        if not line:
            continue
        if any(token in lowered or token in line for token in _INJECTION):
            removed = True
            continue
        kept.append(line)
    return (" ".join(kept).strip()[:1000] or "我不确定，帮我选择研究问题", removed)


def extract_forecasts(text: str) -> tuple[int, ...]:
    values: set[int] = set()
    if re.search(r"(?:次日|下一交易日|翌日)", text):
        values.add(1)
    patterns = (
        r"(?:未来|预测未来|预测|预判|持有|forward|forecast|horizon)\s*[:=]?\s*(\d{1,6})\s*(?:个?交易日|日|天|d)",
        r"(\d{1,6})\s*(?:日|天|d)\s*(?:收益|回报|超额收益)",
    )
    for pattern in patterns:
        values.update(int(item) for item in re.findall(pattern, text, flags=re.IGNORECASE) if int(item) > 0)
    return tuple(sorted(values))


def extract_lookbacks(text: str) -> tuple[int, ...]:
    values: set[int] = set()
    patterns = (
        r"lookback\s*[:=]\s*(\d{1,6})",
        r"(?:最近|过去|近)\s*(\d{1,6})\s*(?:个?交易日|日|天)",
        r"(?<!未来)(\d{1,6})\s*(?:日|天|d)\s*(?:动量|反转|波动|均线|大动量|因子|alpha)",
    )
    for pattern in patterns:
        values.update(int(item) for item in re.findall(pattern, text, flags=re.IGNORECASE) if int(item) > 0)
    return tuple(sorted(values))


def detect_mode(text: str) -> ResearchMode:
    rules = (
        (("上线前", "适合上线"), ResearchMode.PRELAUNCH_REVIEW),
        (("财报后漂移", "公告后漂移", "公告", "事件", "指数纳入", "指数剔除", "分红", "回购"), ResearchMode.EVENT_MARKET_TOPIC),
        (("继续保留", "监控", "退役", "因子漂移"), ResearchMode.MONITOR_DECAY_RETIREMENT),
        (("论文", "复现", "研报"), ResearchMode.PAPER_STRATEGY_REPLICATION),
        (("新闻情绪数据是否值得", "数据源评估", "新数据源"), ResearchMode.NEW_DATA_SOURCE_EVALUATION),
        (("改进", "降低", "优化", "父因子", "parent_factor_id"), ResearchMode.IMPROVE_EXISTING_FACTOR),
        (("低相关", "组合缺口", "补充组合", "残差", "增量alpha", "增量 alpha"), ResearchMode.PORTFOLIO_GAP_SEARCH),
        # A falsifiable yes/no question remains idea validation even when its
        # subject mentions decay.  Explicit improvement and portfolio-gap
        # requests above retain their more specific modes.
        (("能否", "是否", "验证", "有没有", "可否"), ResearchMode.VALIDATE_IDEA),
        (("为什么", "失效", "回撤", "拥挤", "衰减", "诊断"), ResearchMode.DIAGNOSE_FACTOR_PORTFOLIO),
    )
    lowered = text.lower()
    for tokens, mode in rules:
        if any(token.lower() in lowered for token in tokens):
            return mode
    return ResearchMode.NEW_ALPHA_EXPLORATION


def detect_depth(text: str) -> ResearchDepth:
    if any(token in text for token in ("快速", "先看看", "草案")):
        return ResearchDepth.QUICK_EXPLORATION
    if any(token in text for token in ("严格", "上线前", "封闭测试", "严谨")):
        return ResearchDepth.STRICT_VALIDATION
    return ResearchDepth.STANDARD_RESEARCH


def is_research_question(text: str) -> bool:
    lowered = text.lower()
    vocabulary = (
        "alpha", "因子", "股票", "收益", "动量", "反转", "波动", "成交", "换手", "跳空",
        "pb", "pe", "roe", "负债", "营收", "利润", "现金流", "盈利", "公告", "指数", "分红",
        "回购", "利率", "shibor", "资金流", "新闻", "组合", "回撤", "论文", "上线", "监控", "数据源",
    )
    return any(token in lowered for token in vocabulary)


def _requirements(text: str):
    required: set[str] = set()
    optional: set[str] = set()
    neutralization: set[str] = set()
    costs: set[str] = set()
    for rule in _RULES:
        if rule.matches(text):
            required.update(rule.required_data)
            optional.update(rule.optional_data)
            neutralization.update(rule.neutralization)
            costs.update(rule.costs)
    optional -= required
    return tuple(sorted(required)), tuple(sorted(optional)), tuple(sorted(neutralization)), tuple(sorted(costs))


def _factor_ids(text: str) -> tuple[str, ...]:
    return tuple(sorted(set(re.findall(r"(?:parent_factor_id|factor_id)\s*[:=：]\s*([A-Za-z0-9_.:/-]+)", text))))


def _named_factors(text: str) -> tuple[str, ...]:
    return tuple(name for token, name in (("动量", "momentum"), ("价值", "value"), ("反转", "reversal")) if token in text)


def _has_source_document(text: str) -> bool:
    lowered = text.lower()
    return any(
        re.search(pattern, lowered)
        for pattern in (
            r"\bdoi\s*[:=：]\s*\S+",
            r"\bsource_document\s*[:=：]\s*\S+",
            r"(?:论文|研报|来源)\s*[:=：]\s*\S+",
        )
    )


def _has_data_source_semantics(text: str) -> bool:
    lowered = text.lower()
    has_source = re.search(r"(?:source_id|数据源|来源)\s*[:=：]\s*\S+", lowered) is not None
    has_fields = re.search(r"(?:fields?|字段)\s*[:=：]\s*\S+", lowered) is not None
    has_time = any(token in lowered for token in ("published_at", "available_at", "发布时间", "可得时间"))
    return has_source and has_fields and has_time


def _subject(mode: ResearchMode, text: str) -> str:
    labels = {
        ResearchMode.DIAGNOSE_FACTOR_PORTFOLIO: "因子或组合诊断",
        ResearchMode.EVENT_MARKET_TOPIC: "事件与市场专题",
        ResearchMode.PORTFOLIO_GAP_SEARCH: "组合缺口与增量研究",
        ResearchMode.NEW_DATA_SOURCE_EVALUATION: "数据源评估",
        ResearchMode.PAPER_STRATEGY_REPLICATION: "论文或策略复现",
        ResearchMode.PRELAUNCH_REVIEW: "上线前评审",
        ResearchMode.MONITOR_DECAY_RETIREMENT: "监控、衰减与退役",
    }
    return f"{labels.get(mode, 'Alpha研究')}：{text}"


__all__ = [
    "DeterministicResearchIntentCompiler", "SemanticRule", "detect_depth", "detect_mode",
    "extract_forecasts", "extract_lookbacks", "is_research_question", "sanitize",
]
