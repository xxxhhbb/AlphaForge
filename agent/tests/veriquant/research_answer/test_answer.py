from __future__ import annotations

from types import SimpleNamespace
from pathlib import Path

import pytest

from veriquant.agent_research_product.collaboration import EvidenceCollaborationAggregator
from veriquant.agent_research_product.contracts import (
    ProductExperimentResult,
    ProductExperimentStatus,
    ResearchResultBundle,
)
from veriquant.agent_research_sandbox.process_runner import canonical_hash
from veriquant.agent_research_sandbox.report import (
    BoundMetric,
    ChineseResearchReportComposer,
    CustomerResearchReport,
    ResearchConclusion,
)
from veriquant.research_answer.adapters import AnswerAdapterRegistry
from veriquant.research_answer.assembler import ResearchAnswerAssembler
from veriquant.research_answer.source_summary import summarize_reviewed_source
from veriquant.research_answer.contracts import (
    AnswerStatus,
    CandidateDefinition,
    CandidateDisposition,
    EvidenceAuthority,
    QuestionRequirement,
    ResearchAnswerSpec,
)
from veriquant.research_results import ResearchResultKind
from veriquant.agent_research_product.planner import ResearchTaskPlanner
from veriquant.research500_harness import load_catalog


def h(char: str) -> str:
    return "sha256:" + char * 64


KINDS = (
    "DatasetAssessmentTable",
    "DiagnosticTable",
    "EventTable",
    "RegimeComparisonTable",
    "RegressionTable",
    "ScoreTable",
)

ROOT = Path(__file__).resolve().parents[4]
CATALOG = ROOT / ".autobuild" / "veriquant" / "factor_question_500" / "catalog.json"


def request(*kinds: str):
    tasks = tuple(
        SimpleNamespace(
            output_kind=kind,
            artifact_hash=h(hex(index)[2:]),
            specialist_id=f"specialist_{index}",
            question="研究一个可证伪的 Alpha 问题",
        )
        for index, kind in enumerate(kinds, start=1)
    )
    return SimpleNamespace(
        question_id="q_answer",
        question="研究一个可证伪的 Alpha 问题",
        tasks=tasks,
        manifest=SimpleNamespace(run_id="run_answer", data_snapshot_hash=h("d")),
    )


def experiment(index: int, kind: str, outcome: str = "VALIDATED_RESEARCH"):
    evidence_hash = h(hex(index + 6)[2:])
    return ProductExperimentResult(
        experiment_id=f"candidate_{index:02d}",
        specialist_id=f"specialist_{index}",
        source_hash=h("f"),
        status=ProductExperimentStatus.OFFICIAL_EVIDENCE,
        official_evidence_hash=evidence_hash,
        official_metrics=(("observation_count", "120"), ("signal_metric", "0.12")),
        court_outcome=outcome,
        output_kind=kind,
    )


def candidate(index: int, kind: str, evidence_hash: str):
    roles = {
        "ScoreTable": "ALPHA_SIGNAL",
        "RegressionTable": "STATISTICAL_TEST",
        "RegimeComparisonTable": "CONDITION_ANALYSIS",
        "EventTable": "EVENT_STUDY",
        "DiagnosticTable": "DIAGNOSTIC",
        "DatasetAssessmentTable": "DATA_ASSESSMENT",
    }
    return CandidateDefinition(
        candidate_id=f"candidate_{index:02d}",
        specialist_id=f"specialist_{index}",
        source_hash=h("f"),
        display_name=f"研究候选 {index}",
        hypothesis="该候选对未来收益具有可证伪关系。",
        method_summary=(
            "使用冻结 close 与 turnover_rate 字段，分别按训练样本做 Z-score 标准化后等权相加，"
            "形成预测未来20日收益的横截面评分；分数越高表示预期未来收益排序越高。"
        ),
        output_kind=kind,
        result_role=roles[kind],
        implementation_summary=(
            "输入字段：close、turnover_rate；源码评分表达式：close_z + turnover_z；"
            "分数越高表示未来收益排序越高。"
            if kind == "ScoreTable" else None
        ),
        input_fields=(("close", "turnover_rate") if kind == "ScoreTable" else ()),
        data_snapshot_hash=h("d"),
        evidence_hashes=(evidence_hash,),
    )


def bundle(experiments, kind=ResearchResultKind.EVALUATED_RESEARCH_RESULT):
    experiments = tuple(experiments)
    return ResearchResultBundle(
        run_id="run_answer",
        result_kind=kind,
        experiments=experiments,
        failure_codes=(),
        branch_statuses=("COMPLETED",),
        official_evidence_hashes=tuple(sorted({
            item.official_evidence_hash for item in experiments if item.official_evidence_hash
        })),
    )


def test_registry_explicitly_covers_all_six_standard_outputs() -> None:
    registry = AnswerAdapterRegistry()
    assert registry.output_kinds == KINDS
    assert all(registry.get(kind).dimensions for kind in KINDS)


def test_reviewed_source_summary_exposes_fields_and_score_without_execution() -> None:
    source = """
def run_research(train_rows, prediction_rows, seed):
    feature_fields = ['net_moneyflow_amount', 'turnover_rate', 'shibor_1w']
    z_scores = [1.0, 2.0, 3.0]
    score = (z_scores[0] + z_scores[1] - z_scores[2]) / 3.0
    return {'rows': []}
"""
    summary = summarize_reviewed_source(source, output_kind="ScoreTable")
    assert summary is not None
    assert summary.input_fields == ("net_moneyflow_amount", "shibor_1w", "turnover_rate")
    assert "按源码顺序）：net_moneyflow_amount、turnover_rate、shibor_1w" in summary.implementation_summary
    assert "Z(net_moneyflow_amount) + Z(turnover_rate) - Z(shibor_1w)" in summary.implementation_summary
    assert "分数越高" in summary.implementation_summary
    assert summary.additive_fields == (
        "net_moneyflow_amount", "shibor_1w", "turnover_rate",
    )
    assert summary.design_warnings == ()


def test_legitimate_large_order_field_is_not_confused_with_order_authority() -> None:
    value = CandidateDefinition(
        candidate_id="candidate_order_flow",
        specialist_id="market_structure",
        source_hash=h("f"),
        display_name="大单结构 Alpha",
        hypothesis="大单失衡可能含有预测信息。",
        method_summary="研究 large_order_imbalance 与未来收益的关系。",
        output_kind="ScoreTable",
        data_snapshot_hash=h("d"),
        implementation_summary="输入字段：large_order_imbalance；分数越高表示净流入越强。",
        input_fields=("large_order_imbalance",),
    )
    assert value.input_fields == ("large_order_imbalance",)
    with pytest.raises(ValueError, match="privileged"):
        CandidateDefinition(
            candidate_id="candidate_forbidden_order",
            specialist_id="market_structure",
            source_hash=h("f"),
            display_name="越权候选",
            hypothesis="尝试写 orders。",
            method_summary="禁止 orders 权限。",
            output_kind="ScoreTable",
            data_snapshot_hash=h("d"),
        )


def test_direct_answer_counts_only_score_output_as_alpha_signal() -> None:
    assembler = ResearchAnswerAssembler()
    req = request("ScoreTable", "RegimeComparisonTable")
    experiments = (
        experiment(1, "ScoreTable"),
        experiment(2, "RegimeComparisonTable"),
    )
    source = (
        "def run_research(train_rows, prediction_rows, seed):\n"
        "    feature_fields = ['close', 'turnover_rate']\n"
        "    score = close_z + turnover_z\n"
        "    return {'rows': []}\n"
    )

    class Repository:
        def get(self, _content_hash):
            return source

    candidates = assembler.build_candidates(
        request=req, swarm=None, experiments=experiments, artifact_repository=Repository()
    )
    assert [item.result_role for item in candidates] == ["ALPHA_SIGNAL", "CONDITION_ANALYSIS"]
    collaboration = EvidenceCollaborationAggregator().summarize_experiments(
        run_id="run_answer", experiments=experiments
    )
    answer = assembler.assemble(
        request=req,
        spec=assembler.compile_spec(req),
        candidates=candidates,
        experiments=experiments,
        result_bundle=bundle(experiments),
        collaboration=collaboration,
        failures=(),
    )
    assert "主要 Alpha" in answer.direct_answer
    assert "实际计算方法" in answer.direct_answer
    assert "close、turnover_rate" in answer.direct_answer
    assert "另有 1 项状态、统计或诊断分析" in answer.direct_answer
    assert "不是额外的 Alpha" in answer.direct_answer


def test_positive_metric_cannot_be_presented_as_retained_alpha_without_definition() -> None:
    assembler = ResearchAnswerAssembler()
    req = request("ScoreTable")
    experiments = (experiment(1, "ScoreTable"),)
    incomplete = CandidateDefinition(
        candidate_id="candidate_01",
        specialist_id="specialist_1",
        source_hash=h("f"),
        display_name="未定义候选",
        hypothesis="尝试预测未来收益。",
        method_summary="构造一个横截面评分。",
        output_kind="ScoreTable",
        data_snapshot_hash=h("d"),
        evidence_hashes=(experiments[0].official_evidence_hash,),
    )
    collaboration = EvidenceCollaborationAggregator().summarize_experiments(
        run_id="run_answer", experiments=experiments
    )
    answer = assembler.assemble(
        request=req,
        spec=assembler.compile_spec(req),
        candidates=(incomplete,),
        experiments=experiments,
        result_bundle=bundle(experiments),
        collaboration=collaboration,
        failures=(),
    )
    assert answer.candidate_decisions[0].disposition is CandidateDisposition.REVISE
    assert answer.candidate_decisions[0].reason_codes == ("CANDIDATE_DEFINITION_INCOMPLETE",)
    assert "未定义候选" not in answer.direct_answer


def test_market_wide_additive_term_cannot_be_retained_as_cross_sectional_alpha() -> None:
    assembler = ResearchAnswerAssembler()
    req = request("ScoreTable")
    experiments = (experiment(1, "ScoreTable"),)
    candidate_with_warning = CandidateDefinition(
        candidate_id="candidate_01",
        specialist_id="specialist_1",
        source_hash=h("f"),
        display_name="资金流与 Shibor 等权候选",
        hypothesis="资金流与利率可能共同预测未来收益。",
        method_summary=(
            "将 net_moneyflow_amount 与 shibor_1w 标准化后相减形成横截面评分，"
            "分数越高表示预期未来20日收益越高。"
        ),
        output_kind="ScoreTable",
        data_snapshot_hash=h("d"),
        implementation_summary=(
            "输入字段：net_moneyflow_amount、shibor_1w；"
            "具名评分公式：Z(net_moneyflow_amount) - Z(shibor_1w)。"
        ),
        input_fields=("net_moneyflow_amount", "shibor_1w"),
        design_warnings=("CROSS_SECTION_CONSTANT_ADDITIVE_TERM",),
        evidence_hashes=(experiments[0].official_evidence_hash,),
    )
    collaboration = EvidenceCollaborationAggregator().summarize_experiments(
        run_id="run_answer", experiments=experiments
    )
    answer = assembler.assemble(
        request=req,
        spec=assembler.compile_spec(req),
        candidates=(candidate_with_warning,),
        experiments=experiments,
        result_bundle=bundle(experiments),
        collaboration=collaboration,
        failures=(),
    )
    decision = answer.candidate_decisions[0]
    assert decision.disposition is CandidateDisposition.REVISE
    assert decision.reason_codes == ("CROSS_SECTION_CONSTANT_ADDITIVE_TERM",)
    assert "没有形成可直接计算的 Alpha" not in answer.direct_answer
    assert "尚无候选达到保留条件" in answer.direct_answer


def test_answer_spec_is_stable_and_preserves_each_output_requirement() -> None:
    assembler = ResearchAnswerAssembler()
    first = assembler.compile_spec(request(*KINDS))
    second = assembler.compile_spec(request(*KINDS))
    assert first.artifact_hash == second.artifact_hash
    assert {item.output_kind for item in first.requirements} == set(KINDS)
    assert len(first.requirements) == 6


def test_all_500_catalog_questions_compile_to_stable_answer_specs() -> None:
    catalog = load_catalog(CATALOG)
    planner = ResearchTaskPlanner(catalog_path=CATALOG)
    assembler = ResearchAnswerAssembler()
    seen = set()
    for item in catalog.questions:
        plan = planner.compile(item.question)
        tasks = tuple(
            SimpleNamespace(
                output_kind=plan.output_kind_for(specialist),
                artifact_hash=canonical_hash({
                    "plan_hash": plan.artifact_hash, "specialist": specialist,
                }),
                specialist_id=specialist,
                question=item.question,
            )
            for specialist in plan.selected_specialists
        )
        req = SimpleNamespace(
            question_id=item.question_id,
            question=item.question,
            tasks=tasks,
            manifest=SimpleNamespace(run_id="static_audit", data_snapshot_hash=h("d")),
        )
        first = assembler.compile_spec(req)
        second = assembler.compile_spec(req)
        assert first.artifact_hash == second.artifact_hash
        assert {requirement.output_kind for requirement in first.requirements} == {
            task.output_kind for task in tasks
        }
        seen.add(item.question_id)
    assert len(seen) == 500


def test_all_six_outputs_produce_explicit_question_answers() -> None:
    assembler = ResearchAnswerAssembler()
    req = request(*KINDS)
    spec = assembler.compile_spec(req)
    experiments = tuple(experiment(index, kind) for index, kind in enumerate(KINDS, start=1))
    candidates = tuple(
        candidate(index, kind, experiments[index - 1].official_evidence_hash)
        for index, kind in enumerate(KINDS, start=1)
    )
    collaboration = EvidenceCollaborationAggregator().summarize_experiments(
        run_id="run_answer", experiments=experiments
    )
    answer = assembler.assemble(
        request=req,
        spec=spec,
        candidates=candidates,
        experiments=experiments,
        result_bundle=bundle(experiments),
        collaboration=collaboration,
        failures=(),
    )
    assert answer.answer_status is AnswerStatus.ANSWERED
    assert len(answer.question_parts) == 6
    assert all(part.direct_answer and part.supporting_facts for part in answer.question_parts)
    assert answer.artifact_hash == assembler.assemble(
        request=req,
        spec=spec,
        candidates=candidates,
        experiments=experiments,
        result_bundle=bundle(experiments),
        collaboration=collaboration,
        failures=(),
    ).artifact_hash


def test_valid_candidate_is_not_erased_by_rejected_branch() -> None:
    assembler = ResearchAnswerAssembler()
    req = request("ScoreTable", "ScoreTable")
    spec = assembler.compile_spec(req)
    experiments = (
        experiment(1, "ScoreTable", "VALIDATED_RESEARCH"),
        experiment(2, "ScoreTable", "REJECT"),
    )
    candidates = tuple(
        candidate(index, "ScoreTable", item.official_evidence_hash)
        for index, item in enumerate(experiments, start=1)
    )
    collaboration = EvidenceCollaborationAggregator().summarize_experiments(
        run_id="run_answer", experiments=experiments
    )
    answer = assembler.assemble(
        request=req, spec=spec, candidates=candidates, experiments=experiments,
        result_bundle=bundle(experiments), collaboration=collaboration, failures=(),
    )
    dispositions = {item.candidate_id: item.disposition for item in answer.candidate_decisions}
    assert answer.answer_status is AnswerStatus.CONDITIONALLY_ANSWERED
    assert dispositions["candidate_01"] is CandidateDisposition.RETAIN_WITH_CONDITIONS
    assert dispositions["candidate_02"] is CandidateDisposition.REJECT
    assert "研究候选 1" in answer.direct_answer


def test_missing_evidence_returns_answer_with_reason_and_recovery() -> None:
    assembler = ResearchAnswerAssembler()
    req = request("EventTable")
    spec = assembler.compile_spec(req)
    empty_bundle = bundle((), ResearchResultKind.DATA_OR_CAPABILITY_GAP_REPORT)
    collaboration = EvidenceCollaborationAggregator().summarize_experiments(
        run_id="run_answer", experiments=()
    )
    answer = assembler.assemble(
        request=req, spec=spec, candidates=(), experiments=(),
        result_bundle=empty_bundle, collaboration=collaboration,
        failures=("EVENT_TIME_UNAVAILABLE",),
    )
    assert answer.answer_status is AnswerStatus.UNANSWERABLE_WITH_REASON
    assert answer.question_parts[0].unanswered_reason
    assert answer.question_parts[0].recovery_requirement


def test_v3_report_renders_answer_not_internal_ids_or_hashes() -> None:
    assembler = ResearchAnswerAssembler()
    req = request("ScoreTable", "ScoreTable")
    spec = assembler.compile_spec(req)
    experiments = (
        experiment(1, "ScoreTable", "VALIDATED_RESEARCH"),
        experiment(2, "ScoreTable", "REJECT"),
    )
    candidates = tuple(
        candidate(index, "ScoreTable", item.official_evidence_hash)
        for index, item in enumerate(experiments, start=1)
    )
    collaboration = EvidenceCollaborationAggregator().summarize_experiments(
        run_id="run_answer", experiments=experiments
    )
    answer = assembler.assemble(
        request=req, spec=spec, candidates=candidates, experiments=experiments,
        result_bundle=bundle(experiments), collaboration=collaboration, failures=(),
    )
    report = CustomerResearchReport(
        question_id="q_answer",
        question_hash=canonical_hash({"question": req.question}),
        question=req.question,
        conclusion=ResearchConclusion.REVISE,
        specialists=("市场行为与交易可行性研究组",),
        research_routes=("受控研究主链",),
        data_description=("冻结数据",),
        experiments=("两个候选",),
        review_summary=("机械审查通过",),
        metrics=(BoundMetric("机器指标", "0.12", h("7")),),
        robustness_checks=("冲突检查",),
        supporting_evidence=(),
        opposing_evidence=(),
        limitations=("RUNNER_BLOCKED:task_market_structure_internal", "样本外尚未验证"),
        next_steps=("重放",),
        artifact_hashes=(h("7"),),
        research_answer=answer.to_dict(),
        schema_version="alphaforge.customer-research-report/v3",
    )
    markdown = ChineseResearchReportComposer().render(report)
    assert "## 一句话答案" in markdown
    assert "研究候选 1" in markdown
    assert "有条件保留" in markdown
    assert "实际计算公式与方向" in markdown
    assert "验证进度" in markdown
    assert "证据等级" not in markdown
    assert "逐项回答" in markdown
    assert "candidate_01" not in markdown
    assert "task_market_structure_internal" not in markdown
    assert "sha256:" not in markdown


def test_answered_part_cannot_exist_without_evidence() -> None:
    from veriquant.research_answer.contracts import QuestionPartAnswer

    with pytest.raises(ValueError, match="Evidence-bound facts"):
        QuestionPartAnswer(
            requirement_id="R01",
            answer_status=AnswerStatus.ANSWERED,
            direct_answer="已回答。",
            judgment="支持。",
            evidence_authority=EvidenceAuthority.OFFICIAL_EVIDENCE,
            candidate_ids=(),
        )


def test_spec_rejects_question_hash_mismatch() -> None:
    with pytest.raises(ValueError, match="does not bind"):
        ResearchAnswerSpec(
            question_id="q",
            question="问题",
            question_hash=h("a"),
            requirements=(QuestionRequirement("R01", "回答问题", "ScoreTable", ("prediction",)),),
            task_hashes=(h("b"),),
        )
