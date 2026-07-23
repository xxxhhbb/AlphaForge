from __future__ import annotations

import pytest

from veriquant.agent_research_sandbox.process_runner import canonical_hash
from veriquant.agent_research_sandbox.report import (
    BoundMetric,
    ChineseResearchReportComposer,
    CustomerResearchReport,
    ResearchProcessNarrative,
    ResearchConclusion,
    render_persisted_report,
)


HASH = "sha256:" + "a" * 64


def make_report(**overrides):
    question = "高ROE是否能预测未来20日收益？"
    values = {
        "question_id": "F08_T01",
        "question_hash": canonical_hash({"question": question}),
        "question": question,
        "conclusion": ResearchConclusion.REJECTED,
        "specialists": ("基本面研究组", "市场行为研究组"),
        "research_routes": ("预注册假设与证伪条件", "构建决策时点可见的ROE信号"),
        "data_description": ("批准代理数据；2024-01-01至2025-12-31；历史股票池",),
        "experiments": ("行业与市值中性化后的横截面检验",),
        "review_summary": ("审查通过；无未来标签、网络或任意路径访问",),
        "metrics": (BoundMetric("净RankIC", "-0.012", HASH),),
        "robustness_checks": ("延迟可得时间", "提高交易成本"),
        "supporting_evidence": (),
        "opposing_evidence": ("净RankIC方向与预注册假设相反",),
        "limitations": ("样本仅覆盖已冻结区间",),
        "next_steps": ("检查盈利质量代理并扩展冻结历史",),
        "artifact_hashes": (HASH,),
    }
    values.update(overrides)
    return CustomerResearchReport(**values)


def test_customer_report_is_stable_and_customer_first() -> None:
    report = make_report()
    assert report.artifact_hash == make_report().artifact_hash
    markdown = ChineseResearchReportComposer().render(report)
    assert markdown.startswith("# AlphaForge（千因研究院）研究报告\n\n## 一句话结论")
    assert "## 研究问题" in markdown
    assert "## 复现与关联产物" in markdown
    assert "复现要求" in markdown
    assert "当前机器证据不支持" in markdown
    assert "实际执行的实验" in markdown
    assert "净RankIC" in markdown
    assert "Evidence Service" in markdown
    assert "sha256:" not in markdown


def test_customer_report_round_trip_preserves_identity() -> None:
    report = make_report()
    rebuilt = CustomerResearchReport.from_dict(report.to_dict())
    assert rebuilt == report
    assert rebuilt.artifact_hash == report.artifact_hash


def test_persisted_report_can_be_atomically_rerendered_without_changing_identity(tmp_path) -> None:
    import json

    report = make_report()
    source = tmp_path / "report.json"
    destination = tmp_path / "report.md"
    source.write_text(json.dumps(report.to_dict(), ensure_ascii=False), encoding="utf-8")
    assert render_persisted_report(source, destination) == report.artifact_hash
    first = destination.read_text(encoding="utf-8")
    assert render_persisted_report(source, destination) == report.artifact_hash
    assert destination.read_text(encoding="utf-8") == first


def test_customer_report_explains_the_actual_research_process() -> None:
    report = make_report(
        research_process=(ResearchProcessNarrative(
            specialist="基本面研究组",
            status="COMPLETED",
            plan="先检验 ROE 与未来收益的关系，再控制行业和市值暴露。",
            investigations=("读取冻结字段覆盖率。",),
            attempts=("运行横截面回归。",),
            review_findings=("机械审查通过。",),
            evidence_summary=("形成机器复核研究证据。",),
            artifact_hashes=(HASH,),
        ),),
    )
    markdown = ChineseResearchReportComposer().render(report)
    assert "## 本轮研究是怎样完成的" in markdown
    assert "研究猜想与方法" in markdown
    assert "先检验 ROE" in markdown
    assert "调查了什么" in markdown
    assert "实际尝试" in markdown
    assert "审查与修订" in markdown


def test_score_report_answers_question_and_hides_internal_experiment_ids() -> None:
    experiment = "task_alternative_data_abc123"
    report = make_report(
        question="寻找资金流与流动性 Alpha，比较成本、Shibor 状态和相对已有动量的增量性。",
        question_hash=canonical_hash({"question": "寻找资金流与流动性 Alpha，比较成本、Shibor 状态和相对已有动量的增量性。"}),
        conclusion=ResearchConclusion.REVISE,
        metrics=(
            BoundMetric(f"{experiment} · IC 均值", "0.032", HASH),
            BoundMetric(f"{experiment} · RankIC", "0.195", HASH),
            BoundMetric(f"{experiment} · Pearson IC", "0.150", HASH),
            BoundMetric(f"{experiment} · ICIR", "5.25", HASH),
            BoundMetric(f"{experiment} · 成本前收益", "0.0024", HASH),
            BoundMetric(f"{experiment} · 成本后收益", "0.0016", HASH),
            BoundMetric(f"{experiment} · 换手率", "0.33", HASH),
            BoundMetric(f"{experiment} · 最弱子时期 IC", "-0.007", HASH),
            BoundMetric(f"{experiment} · 子时期正向比例", "0.5", HASH),
            BoundMetric(f"{experiment} · 增量 IC 均值", "0.032", HASH),
            BoundMetric(f"{experiment} · 与已登记因子最高相关性", "0", HASH),
            BoundMetric("辅助研究 · task_macro_1 · sign_stability.mean return", "0", HASH),
        ),
        research_process=(ResearchProcessNarrative(
            specialist="另类数据研究组",
            status="COMPLETED",
            plan="使用冻结资金流与流动性数据构建横截面评分。",
            investigations=("检查资金流字段覆盖率。",),
            attempts=(f"提交实验 {experiment}：OFFICIAL_EVIDENCE，评价为 VALIDATED_RESEARCH。",),
            review_findings=(f"{experiment}：机械审查通过。",),
            evidence_summary=(f"{experiment} 形成正式 Evidence。",),
            artifact_hashes=(HASH,),
        ),),
        limitations=(f"RUNNER_BLOCKED:{experiment}",),
    )
    markdown = ChineseResearchReportComposer().render(report)
    assert "候选评分较高的股票随后收益整体更高" in markdown
    assert "扣除成本后" in markdown
    assert "时间稳定性尚未完全成立" in markdown
    assert "低相关性结论仍需核实" in markdown
    assert "高低 Shibor 状态下的效果没有保持稳定" in markdown
    assert "| 本轮实际答案 | 关键依据 | 判断 |" in markdown
    assert experiment not in markdown
    assert "task_" not in markdown


def test_machine_conclusion_cannot_exist_without_bound_metric() -> None:
    with pytest.raises(ValueError, match="bound metrics"):
        make_report(metrics=())


def test_external_approval_report_is_complete_without_fake_metrics() -> None:
    report = make_report(
        conclusion=ResearchConclusion.NEEDS_EXTERNAL_APPROVAL,
        metrics=(),
        next_steps=("请批准一个包含公告时间语义的数据源和相应字段权限。",),
    )
    markdown = ChineseResearchReportComposer().render(report)
    assert "需要用户批准" in markdown
    assert "没有可冒充的官方指标" in markdown


@pytest.mark.parametrize(
    "text",
    ("Authorization: Bearer abc", "api_key=abc", "sealed_metric=0.1", "target_weight=0.2"),
)
def test_report_redacts_privileged_or_secret_material(text: str) -> None:
    with pytest.raises(ValueError, match="privileged or secret"):
        make_report(limitations=(text,))


def test_report_allows_legitimate_order_flow_research_fields() -> None:
    report = make_report(
        research_routes=(
            "使用 large_order_imbalance 与 net_moneyflow_amount 研究大单交易结构。",
        ),
    )
    markdown = ChineseResearchReportComposer().render(report)
    assert "large_order_imbalance" in markdown
