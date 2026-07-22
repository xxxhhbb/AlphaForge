from __future__ import annotations

import pytest

from veriquant.agent_research_sandbox.process_runner import canonical_hash
from veriquant.agent_research_sandbox.report import (
    BoundMetric,
    ChineseResearchReportComposer,
    CustomerResearchReport,
    ResearchProcessNarrative,
    ResearchConclusion,
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
