from __future__ import annotations

from veriquant.research_quality_delivery_harness.acceptance import _positive
from veriquant.research_quality_delivery_harness.evaluators import OutputEvaluatorRegistry
from veriquant.research_quality_delivery_harness.reports import build_report_v2


def test_report_v2_is_deterministic_and_every_metric_is_bound():
    table, context = _positive("ScoreTable")
    evidence = OutputEvaluatorRegistry().evaluate(table=table, context=context, evidence_id="report_canary")
    kwargs = dict(
        question_id="Q_REPORT", question="过去五日信号是否预测未来收益？", evidence=evidence,
        specialists=("market_structure",), tested=("冻结样本横截面评价",),
        not_tested=("封闭测试",), methods=("相关性和成本后分位差",), baselines=("零信号",),
        controls=("交易成本",), contributions=({"group": "market_structure", "role": "primary"},),
        conflicts=(), associated_artifacts=(table["code_identity_hash"],),
        next_experiment="扩大到未重叠的可见验证子时期",
    )
    left, left_md = build_report_v2(**kwargs)
    right, right_md = build_report_v2(**kwargs)
    assert left["report_hash"] == right["report_hash"]
    assert left_md == right_md
    assert all(item["evidence_hash"].startswith("sha256:") for item in left["metrics"])
    assert "一句话机器结论" in left_md
    assert "实际测试了什么" in left_md
    assert "没有测试什么" in left_md


def test_auxiliary_evidence_cannot_be_labeled_as_qualified_report():
    table, context = _positive("DiagnosticTable")
    evidence = OutputEvaluatorRegistry().evaluate(table=table, context=context, evidence_id="diagnostic")
    assert evidence.authority.value == "OFFICIAL_EVIDENCE"
