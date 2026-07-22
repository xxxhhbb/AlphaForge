from __future__ import annotations

import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MD_PATH = ROOT / "docs" / "veriquant" / "FACTOR_RESEARCH_500_QUESTION_COVERAGE.md"
JSON_PATH = ROOT / ".autobuild" / "veriquant" / "factor_question_500" / "catalog.json"


FAMILIES = (
    ("F01", "短期反转", "过去1至10日超跌或超涨形成的短期反转信号", ("daily", "adj_factor"), "market_structure", "IMPLEMENTED_NOT_FULLY_RUN"),
    ("F02", "中期动量", "过去20至120日相对强弱形成的中期动量信号", ("daily", "adj_factor", "daily_basic"), "market_structure", "IMPLEMENTED_NOT_FULLY_RUN"),
    ("F03", "长期趋势与长期反转", "长期累计收益、趋势持续性与长期反转信号", ("daily", "adj_factor"), "market_structure", "IMPLEMENTED_NOT_FULLY_RUN"),
    ("F04", "跳空与日内价格结构", "隔夜跳空、开收盘关系和日内高低价结构信号", ("daily", "trade_cal"), "market_structure", "IMPLEMENTED_NOT_FULLY_RUN"),
    ("F05", "低波动", "历史总波动率和波动率变化形成的低波动信号", ("daily", "index_daily"), "market_structure", "IMPLEMENTED_NOT_FULLY_RUN"),
    ("F06", "特质与下行风险", "特质波动率、下行波动率和尾部风险信号", ("daily", "index_daily"), "market_structure", "IMPLEMENTED_NOT_FULLY_RUN"),
    ("F07", "Beta与残差风险", "市场Beta、残差收益和系统性风险暴露变化信号", ("daily", "index_daily"), "market_structure", "IMPLEMENTED_NOT_FULLY_RUN"),
    ("F08", "成交量确认", "成交量、成交额与价格方向共同形成的量价确认信号", ("daily",), "market_structure", "IMPLEMENTED_NOT_FULLY_RUN"),
    ("F09", "换手与交易活跃度", "换手率、量比和交易活跃度变化信号", ("daily", "daily_basic"), "market_structure", "IMPLEMENTED_NOT_FULLY_RUN"),
    ("F10", "流动性与Amihud", "Amihud非流动性、成交额市值比和流动性变化信号", ("daily", "daily_basic"), "market_structure", "IMPLEMENTED_NOT_FULLY_RUN"),
    ("F11", "微观结构与价格冲击", "振幅、价量冲击、极端成交和短期交易结构信号", ("daily", "daily_basic"), "market_structure", "IMPLEMENTED_NOT_FULLY_RUN"),
    ("F12", "估值", "PE、PB、PS和股息率构成的估值信号", ("daily_basic",), "fundamental", "IMPLEMENTED_NOT_FULLY_RUN"),
    ("F13", "盈利能力与质量", "ROE、ROA、毛利率和资产周转率构成的质量信号", ("fina_indicator", "stock_basic"), "fundamental", "IMPLEMENTED_NOT_FULLY_RUN"),
    ("F14", "杠杆与资产负债表", "资产负债率、流动比率和资产负债表改善信号", ("balancesheet", "fina_indicator"), "fundamental", "IMPLEMENTED_NOT_FULLY_RUN"),
    ("F15", "现金流与应计项目", "经营现金流、利润现金含量和应计项目质量信号", ("cashflow", "income", "balancesheet"), "fundamental", "IMPLEMENTED_NOT_FULLY_RUN"),
    ("F16", "成长与基本面加速度", "营收、利润增长及其加速度形成的成长信号", ("income", "fina_indicator"), "fundamental", "IMPLEMENTED_NOT_FULLY_RUN"),
    ("F17", "盈利预期与业绩变化", "业绩预告、快报、盈利修正和预期差信号", ("forecast", "express", "income"), "expectations_events", "IMPLEMENTED_NOT_FULLY_RUN"),
    ("F18", "公司事件与公告", "财报、分红、回购、指数调整和公司公告事件信号", ("forecast", "express", "documents"), "expectations_events", "EVENT_DATA_CONDITIONAL"),
    ("F19", "个股资金流", "大单、超大单和个股净资金流持续性信号", ("moneyflow", "daily"), "market_structure", "IMPLEMENTED_NOT_FULLY_RUN"),
    ("F20", "北向与龙虎榜", "北向资金状态、龙虎榜事件和特殊交易资金信号", ("northbound", "dragon_tiger", "daily"), "expectations_events", "AUXILIARY_ONLY"),
    ("F21", "新闻研报与关注度", "新闻、研报覆盖、关注度和文本情绪形成的信息信号", ("stock_news", "research_reports", "daily"), "alternative_data", "AUXILIARY_ONLY"),
    ("F22", "利率与宏观流动性", "Shibor、利率期限结构和宏观流动性状态信号", ("shibor", "index_daily", "daily"), "macro_regime", "IMPLEMENTED_NOT_FULLY_RUN"),
    ("F23", "市场状态与宽度", "指数波动、市场宽度、风险偏好和流动性状态信号", ("index_daily", "daily", "daily_basic"), "macro_regime", "IMPLEMENTED_NOT_FULLY_RUN"),
    ("F24", "季节性与日历效应", "月末、节前节后、财报季和交易日历相关信号", ("daily", "trade_cal"), "expectations_events", "IMPLEMENTED_NOT_FULLY_RUN"),
    ("F25", "复合因子与组合增量", "多因子组合、残差Alpha、低相关性和组合增量信号", ("registered_factor_summary", "registered_evidence", "portfolio_returns"), "meta_alpha", "REGISTERED_CONTEXT_REQUIRED"),
)


TESTS = (
    ("T01", "短周期预测", "{signal}是否能稳定预测未来5个交易日收益？", "CROSS_SECTIONAL_VALIDATION"),
    ("T02", "中周期预测", "{signal}是否能稳定预测未来20个交易日收益？", "CROSS_SECTIONAL_VALIDATION"),
    ("T03", "单调性", "{signal}与未来收益之间是否存在稳定的横截面单调关系？", "QUANTILE_MONOTONICITY"),
    ("T04", "非线性", "{signal}是否存在阈值、U形或其他非线性预测关系？", "NONLINEAR_DIAGNOSTIC"),
    ("T05", "行业中性", "行业中性化后，{signal}是否仍具有预测能力？", "NEUTRALIZED_VALIDATION"),
    ("T06", "风格控制", "控制市值、Beta和常见风格暴露后，{signal}是否仍有增量解释力？", "RESIDUAL_INCREMENTAL"),
    ("T07", "波动状态", "在高波动和低波动市场状态下，{signal}的效果是否显著不同？", "REGIME_CONDITIONING"),
    ("T08", "流动性状态", "在高流动性和低流动性股票中，{signal}是否同样有效且可交易？", "LIQUIDITY_CONDITIONING"),
    ("T09", "市场分层", "在主板、创业板、科创板及不同市值层中，{signal}是否具有一致性？", "UNIVERSE_ROBUSTNESS"),
    ("T10", "子时期稳定性", "在不同年份和市场子时期中，{signal}的方向与强度是否稳定？", "SUBPERIOD_STABILITY"),
    ("T11", "时间语义", "将相关数据的available_at按真实可交易时间延迟后，{signal}是否仍然成立？", "PIT_DELAY_TEST"),
    ("T12", "异常样本", "排除极端收益、低价股、涨跌停和异常缺失样本后，{signal}是否仍然稳健？", "OUTLIER_ROBUSTNESS"),
    ("T13", "成本后表现", "计入点差、冲击和手续费后，{signal}是否仍有正向净研究收益？", "COST_STRESS"),
    ("T14", "换手控制", "通过平滑、滞后或降低调仓频率后，{signal}能否降低换手并保留预测力？", "TURNOVER_OPTIMIZATION"),
    ("T15", "容量", "随着资金规模和ADV参与率提高，{signal}的容量和成本是否仍可接受？", "CAPACITY_STRESS"),
    ("T16", "相关性", "{signal}与已登记动量、价值、质量和低波动因子的相关性有多高？", "REGISTERED_CORRELATION"),
    ("T17", "增量性", "控制已登记因子后，{signal}是否仍具有显著的增量IC或组合价值？", "RESIDUAL_INCREMENTAL"),
    ("T18", "反证与Placebo", "经过符号翻转、标签错位和Placebo检验后，{signal}是否仍表现为真实效应而非偶然？", "ADVERSARIAL_FALSIFICATION"),
    ("T19", "参数稳健性", "改变观察窗口、持有期和标准化方法后，{signal}是否对单一参数不敏感？", "PARAMETER_ROBUSTNESS"),
    ("T20", "衰减与监控", "{signal}近期是否出现衰减，其变化更可能来自数据、市场状态、成本还是拥挤代理？", "DIAGNOSTIC_MONITORING"),
)


def result_kind(experiment_type: str) -> str:
    if experiment_type == "DIAGNOSTIC_MONITORING":
        return "DIAGNOSTIC_REPORT"
    if experiment_type in {"RESIDUAL_INCREMENTAL", "REGISTERED_CORRELATION"}:
        return "PORTFOLIO_GAP_OR_INCREMENTAL_REPORT"
    return "VALIDATION_CONCLUSION"


def build_catalog() -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    sequence = 0
    for family_id, family_name, signal, data, specialist, readiness in FAMILIES:
        for test_id, dimension, template, experiment_type in TESTS:
            sequence += 1
            rows.append(
                {
                    "question_id": f"Q{sequence:03d}",
                    "family_id": family_id,
                    "family_name": family_name,
                    "test_id": test_id,
                    "research_dimension": dimension,
                    "question": template.format(signal=signal),
                    "required_data": list(data),
                    "lead_specialist": specialist,
                    "experiment_type": experiment_type,
                    "expected_result_kind": result_kind(experiment_type),
                    "agent_sandbox_coverage": "COVERED_BY_TARGET_ARCHITECTURE",
                    "current_repository_readiness": readiness,
                }
            )
    assert len(rows) == 500
    return rows


def stable_hash(value: object) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def render_markdown(rows: list[dict[str, object]], catalog_hash: str) -> str:
    lines = [
        "# AlphaForge 500个因子研究问题与架构覆盖矩阵",
        "",
        "> 生成方法：25个因子家族 × 20个研究维度。问题目录用于研究覆盖和Harness验收，不代表任何因子有效。",
        "",
        f"- 问题数：{len(rows)}",
        f"- Catalog hash：`{catalog_hash}`",
        "- 目标架构覆盖：500/500 可由受控研究Agent规划、编写实验、沙箱运行并生成报告",
        "- 官方机器评价：仍取决于冻结数据、PIT语义和注册上下文是否齐全",
        "",
        "## 覆盖结论",
        "",
        "| 层级 | 结论 |",
        "|---|---|",
        "| 研究理解与实验设计 | 500/500目标可覆盖 |",
        "| 客户可读报告 | 500/500目标可覆盖 |",
        "| 当前仓库直接官方评价 | 未达到500/500；多年真实数据物化、另类数据资格化和注册组合上下文仍有缺口 |",
        "| 产生正向Alpha | 不保证；负面结论和数据缺口同样是合法结果 |",
        "",
        "## 架构要求",
        "",
        "1. Vibe AgentLoop负责规划、调查、实验代码和修订。",
        "2. 专业组只获得独立只读工具和隔离工作区。",
        "3. 审查Agent检查方法；机械安全Gate决定是否允许运行。",
        "4. 代码只能在无网络、只读冻结数据、资源受限沙箱中运行。",
        "5. Evidence Service从标准结果表计算机器指标；LLM不得写官方指标或结论。",
        "6. 每题必须产生评价、负面、数据缺口或部分研究报告之一。",
        "",
    ]
    by_family: dict[str, list[dict[str, object]]] = {}
    for row in rows:
        by_family.setdefault(str(row["family_id"]), []).append(row)
    family_meta = {item[0]: item for item in FAMILIES}
    for family_id, family_rows in by_family.items():
        _, name, _signal, data, specialist, readiness = family_meta[family_id]
        lines.extend(
            [
                f"## {family_id} {name}",
                "",
                f"数据：`{', '.join(data)}` · 主导专业组：`{specialist}` · 当前准备度：`{readiness}`",
                "",
            ]
        )
        for row in family_rows:
            lines.append(
                f"- **{row['question_id']}** `{row['research_dimension']}`：{row['question']}"
            )
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def main() -> None:
    rows = build_catalog()
    catalog_hash = stable_hash(rows)
    document = {
        "schema_version": "alphaforge.factor-research-question-catalog/v1",
        "question_count": len(rows),
        "family_count": len(FAMILIES),
        "dimension_count": len(TESTS),
        "catalog_hash": catalog_hash,
        "questions": rows,
    }
    MD_PATH.parent.mkdir(parents=True, exist_ok=True)
    JSON_PATH.parent.mkdir(parents=True, exist_ok=True)
    MD_PATH.write_text(render_markdown(rows, catalog_hash), encoding="utf-8")
    JSON_PATH.write_text(
        json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({"markdown": str(MD_PATH), "json": str(JSON_PATH), "catalog_hash": catalog_hash}, ensure_ascii=False))


if __name__ == "__main__":
    main()
