"""The 24 product scenarios are acceptance data, never execution branches."""

from __future__ import annotations

from dataclasses import dataclass

from ..research_campaign.models import ResearchMode


@dataclass(frozen=True)
class ProductScenario:
    scenario_id: str
    question: str
    mode: ResearchMode
    expected_lookbacks: tuple[int, ...]
    expected_horizons: tuple[int, ...]
    required_data_any: tuple[str, ...]
    ambiguous_question: str


PRODUCT_SCENARIOS = (
    ProductScenario("S01", "挖掘过去50日动量对未来5日收益的预测能力", ResearchMode.NEW_ALPHA_EXPLORATION, (50,), (5,), ("close",), "我想挖一个股票Alpha，但窗口和预测周期都没想好"),
    ProductScenario("S02", "最近5日下跌是否存在未来3日反转", ResearchMode.VALIDATE_IDEA, (5,), (3,), ("close",), "股票下跌之后是否反转，周期还没确定"),
    ProductScenario("S03", "低波动股票是否具有未来20日超额收益", ResearchMode.VALIDATE_IDEA, (), (20,), ("close",), "低波动股票是否有超额收益，预测周期未定"),
    ProductScenario("S04", "放量上涨是否比普通动量更有效", ResearchMode.VALIDATE_IDEA, (), (), ("volume", "amount"), "放量上涨是否有效，回看和预测周期未定"),
    ProductScenario("S05", "换手率下降后的价格突破是否更稳定", ResearchMode.VALIDATE_IDEA, (), (), ("turnover_rate",), "换手和突破的股票收益关系是否存在，周期未定"),
    ProductScenario("S06", "隔夜跳空能否预测次日或未来5日收益", ResearchMode.VALIDATE_IDEA, (), (1, 5), ("open", "close"), "隔夜跳空能否预测股票收益，预测周期未定"),
    ProductScenario("S07", "低PB高盈利收益率能否预测未来收益", ResearchMode.VALIDATE_IDEA, (), (), ("pb", "pe"), "估值能否预测股票收益，预测周期未定"),
    ProductScenario("S08", "高ROE低负债公司是否有质量溢价", ResearchMode.VALIDATE_IDEA, (), (), ("roe", "debt_to_assets"), "质量因子是否有效，但具体指标和周期未定"),
    ProductScenario("S09", "营收和利润增速加速是否形成Alpha", ResearchMode.VALIDATE_IDEA, (), (), ("revenue", "net_income"), "成长是否形成Alpha，指标与周期未定"),
    ProductScenario("S10", "经营现金流优于利润是否更可靠", ResearchMode.VALIDATE_IDEA, (), (), ("operating_cashflow",), "现金流质量是否预测股票收益，周期未定"),
    ProductScenario("S11", "盈利预测上调能否预测未来20日收益", ResearchMode.VALIDATE_IDEA, (), (20,), ("earnings_forecast",), "盈利预测上调能否预测收益，周期未定"),
    ProductScenario("S12", "业绩超预期后是否存在公告后漂移", ResearchMode.EVENT_MARKET_TOPIC, (), (), ("event_time", "available_at"), "研究一个公告事件是否影响股票收益，事件和周期待确认"),
    ProductScenario("S13", "指数纳入和剔除是否存在可交易效应", ResearchMode.EVENT_MARKET_TOPIC, (), (), ("event_time",), "研究一个指数事件，具体指数和窗口待确认"),
    ProductScenario("S14", "分红回购公告后是否存在条件化收益", ResearchMode.EVENT_MARKET_TOPIC, (), (), ("event_time",), "研究一个公司公告事件，事件类型和周期待确认"),
    ProductScenario("S15", "高利率状态下动量是否衰减", ResearchMode.VALIDATE_IDEA, (), (), ("shibor_1w",), "宏观利率是否影响股票因子，因子和周期待确认"),
    ProductScenario("S16", "高波动市场中短期反转是否增强", ResearchMode.VALIDATE_IDEA, (), (), ("benchmark_return",), "市场状态是否影响反转，窗口待确认"),
    ProductScenario("S17", "大单资金流入是否具有短期预测力", ResearchMode.VALIDATE_IDEA, (), (), ("moneyflow_net",), "资金流是否预测股票收益，周期未定"),
    ProductScenario("S18", "新闻情绪数据是否值得接入研究系统", ResearchMode.NEW_DATA_SOURCE_EVALUATION, (), (), ("documents", "entity_mapping"), "评估一个新数据源，但来源与字段未说明"),
    ProductScenario("S19", "把现有动量因子的换手降低一半", ResearchMode.IMPROVE_EXISTING_FACTOR, (), (), ("close",), "请改进这个因子，但parent_factor_id未提供"),
    ProductScenario("S20", "找一个与动量和价值低相关的Alpha", ResearchMode.PORTFOLIO_GAP_SEARCH, (), (), ("registered_factor_summary",), "找一个组合缺口Alpha，但factor_id和portfolio_id未提供"),
    ProductScenario("S21", "最近动量因子为什么失效", ResearchMode.DIAGNOSE_FACTOR_PORTFOLIO, (), (), ("registered_evidence",), "最近这个因子为什么失效，但factor_id未提供"),
    ProductScenario("S22", "最近组合回撤主要来自什么", ResearchMode.DIAGNOSE_FACTOR_PORTFOLIO, (), (), ("registered_evidence",), "诊断组合回撤，但portfolio_id未提供"),
    ProductScenario("S23", "复现一篇论文中的质量因子", ResearchMode.PAPER_STRATEGY_REPLICATION, (), (), ("source_document",), "复现一个论文因子，但论文来源未提供"),
    ProductScenario("S24", "某因子是否适合上线或继续保留", ResearchMode.PRELAUNCH_REVIEW, (), (), ("registered_evidence",), "某因子是否适合上线，但factor_id未提供"),
)


def scenario_cases() -> tuple[dict[str, object], ...]:
    rows: list[dict[str, object]] = []
    for scenario in PRODUCT_SCENARIOS:
        variants = (
            ("canonical", scenario.question),
            ("synonym_1", f"请系统研究：{scenario.question}"),
            ("synonym_2", f"在A股历史股票池中，{scenario.question}"),
            ("synonym_3", f"请把这个问题做成可证伪研究：{scenario.question}"),
            ("ambiguous", scenario.ambiguous_question),
            ("irrelevant_number", f"报告编号20260720，预算说明100元；真正的问题是：{scenario.question}"),
        )
        for variant, question in variants:
            rows.append(
                {
                    "case_id": f"{scenario.scenario_id}.{variant}",
                    "scenario_id": scenario.scenario_id,
                    "variant": variant,
                    "question": question,
                    "expected_mode": scenario.mode.value,
                    "expected_lookbacks": scenario.expected_lookbacks,
                    "expected_horizons": scenario.expected_horizons,
                    "required_data_any": scenario.required_data_any,
                }
            )
    return tuple(rows)


__all__ = ["PRODUCT_SCENARIOS", "ProductScenario", "scenario_cases"]
