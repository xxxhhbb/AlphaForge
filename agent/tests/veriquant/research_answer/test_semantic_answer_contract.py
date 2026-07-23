from __future__ import annotations

from types import SimpleNamespace

from veriquant.agent_research_product.collaboration import EvidenceCollaborationAggregator
from veriquant.agent_research_product.contracts import (
    ProductExperimentResult,
    ProductExperimentStatus,
    ResearchResultBundle,
)
from veriquant.agent_research_sandbox.process_runner import canonical_hash
from veriquant.research_answer.assembler import ResearchAnswerAssembler
from veriquant.research_answer.contracts import (
    AnswerStatus,
    CandidateDefinition,
)
from veriquant.research_results import ResearchResultKind
from veriquant.agent_research_product.planner import ResearchTaskPlanner


def h(char: str) -> str:
    return "sha256:" + char * 64


def semantic_request(question: str, kinds: tuple[str, ...]):
    tasks = tuple(
        SimpleNamespace(
            output_kind=kind,
            artifact_hash=canonical_hash({"index": index, "kind": kind}),
            specialist_id=f"specialist_{index}",
            question=question,
            answer_requirement_ids=("R10_PRIMARY_CONCLUSION",),
        )
        for index, kind in enumerate(kinds, start=1)
    )
    return SimpleNamespace(
        question_id="semantic_question",
        question=question,
        tasks=tasks,
        manifest=SimpleNamespace(run_id="semantic_run", data_snapshot_hash=h("d")),
    )


def result(index: int, kind: str, verdict: str, metrics: tuple[tuple[str, str], ...]):
    return ProductExperimentResult(
        experiment_id=f"experiment_{index}",
        specialist_id=f"specialist_{index}",
        source_hash=h(hex(index + 1)[2:]),
        status=ProductExperimentStatus.OFFICIAL_EVIDENCE,
        official_evidence_hash=h(hex(index + 7)[2:]),
        official_metrics=metrics,
        court_outcome=verdict,
        output_kind=kind,
    )


def definition(index: int, kind: str, evidence_hash: str):
    role = {
        "ScoreTable": "ALPHA_SIGNAL",
        "RegressionTable": "STATISTICAL_TEST",
        "EventTable": "EVENT_STUDY",
    }[kind]
    return CandidateDefinition(
        candidate_id=f"experiment_{index}",
        specialist_id=f"specialist_{index}",
        source_hash=h(hex(index + 1)[2:]),
        display_name=f"研究定义 {index}",
        hypothesis="检验用户提出的关系。",
        method_summary="使用冻结字段构造可证伪实验并独立评价统计关系。",
        output_kind=kind,
        result_role=role,
        implementation_summary=(
            "输入字段：momentum_50d、turnover_rate；计算二者标准化交互项，分数越高表示未来收益预期越高。"
            if kind == "ScoreTable" else None
        ),
        input_fields=("momentum_50d", "turnover_rate") if kind == "ScoreTable" else (),
        data_snapshot_hash=h("d"),
        evidence_hashes=(evidence_hash,),
    )


def assemble(question: str, experiments):
    kinds = tuple(item.output_kind for item in experiments)
    request = semantic_request(question, kinds)
    assembler = ResearchAnswerAssembler()
    spec = assembler.compile_spec(request)
    candidates = tuple(
        definition(index, item.output_kind, item.official_evidence_hash)
        for index, item in enumerate(experiments, start=1)
    )
    collaboration = EvidenceCollaborationAggregator().summarize_experiments(
        run_id="semantic_run", experiments=tuple(experiments)
    )
    bundle = ResearchResultBundle(
        run_id="semantic_run",
        result_kind=ResearchResultKind.EVALUATED_RESEARCH_RESULT,
        experiments=tuple(experiments),
        failure_codes=(),
        branch_statuses=("COMPLETED",),
        official_evidence_hashes=tuple(sorted(item.official_evidence_hash for item in experiments)),
    )
    answer = assembler.assemble(
        request=request,
        spec=spec,
        candidates=candidates,
        experiments=tuple(experiments),
        result_bundle=bundle,
        collaboration=collaboration,
        failures=(),
    )
    return spec, answer


def test_mixed_question_is_compiled_as_semantics_not_one_table_kind() -> None:
    question = "成交量是否增强50日动量，并比较最佳Alpha的成本、换手和稳定性？"
    common = (
        ("ic_mean", "0.02"), ("max_drawdown", "0.03"),
        ("net_return", "0.01"), ("turnover", "0.10"),
    )
    experiments = (
        result(1, "ScoreTable", "VALIDATED_RESEARCH", common),
        result(2, "ScoreTable", "REJECT", common),
        result(3, "RegressionTable", "VALIDATED_RESEARCH", (
            ("coefficient.volume_interaction", "0.12"),
            ("p_value_normal.volume_interaction", "0.03"),
        )),
    )
    spec, answer = assemble(question, experiments)
    assert all(item.output_kind is None for item in spec.requirements)
    assert {item.requirement_id for item in spec.requirements} >= {
        "R10_PRIMARY_CONCLUSION", "R20_CANDIDATE_COMPARISON",
        "R30_TRADING_QUALITY", "R90_IMPLEMENTABLE_DEFINITION",
    }
    assert all(item.answer_status is not AnswerStatus.UNANSWERABLE_WITH_REASON for item in answer.question_parts)
    assert "研究定义 1" in answer.direct_answer


def test_all_evaluated_candidates_rejected_is_an_explicit_negative_answer() -> None:
    experiments = (result(
        1, "RegressionTable", "REJECT",
        (("coefficient.roe", "-0.01"), ("p_value_normal.roe", "0.61")),
    ),)
    _spec, answer = assemble("ROE是否能预测未来20日收益？", experiments)
    assert answer.answer_status is AnswerStatus.NEGATIVE_ANSWER
    assert "不支持原问题" in answer.direct_answer
    assert "不是‘没有结果’" in answer.direct_answer


def test_event_question_requires_timing_outcome_control_and_placebo() -> None:
    metrics = (
        ("event_count", "82"), ("event_window_start", "1"),
        ("event_window_end", "20"), ("mean_car", "0.013"),
        ("mean_control_adjusted_car", "0.008"), ("mean_placebo_car", "0.001"),
    )
    _spec, answer = assemble(
        "业绩快报公告后是否存在20日漂移？",
        (result(1, "EventTable", "VALIDATED_RESEARCH", metrics),),
    )
    by_id = {item.requirement_id: item for item in answer.question_parts}
    assert by_id["R60_EVENT_DESIGN"].answer_status is AnswerStatus.ANSWERED
    assert by_id["R61_EVENT_OUTCOME"].answer_status is AnswerStatus.ANSWERED
    assert "事件数=82" in by_id["R61_EVENT_OUTCOME"].direct_answer


def test_event_labels_without_outcomes_fail_coverage_closed() -> None:
    _spec, answer = assemble(
        "业绩快报公告后是否存在20日漂移？",
        (result(1, "EventTable", "VALIDATED_RESEARCH", (("event_count", "82"),)),),
    )
    by_id = {item.requirement_id: item for item in answer.question_parts}
    assert by_id["R61_EVENT_OUTCOME"].answer_status is AnswerStatus.PARTIALLY_ANSWERED
    assert any("尚未覆盖" in item for item in by_id["R61_EVENT_OUTCOME"].limitations)


def test_planner_exposes_a_mixed_evaluator_portfolio_for_mixed_questions() -> None:
    planner = ResearchTaskPlanner()
    mixed = planner.compile("成交量是否增强50日动量，并比较最佳Alpha的成本、换手和稳定性？")
    assert {kind for _specialist, kind in mixed.specialist_output_kinds} >= {
        "ScoreTable", "RegressionTable",
    }
    event = planner.compile("业绩快报公告后是否存在20日漂移？")
    assert "EventTable" in {kind for _specialist, kind in event.specialist_output_kinds}
    regime = planner.compile("寻找在高低Shibor环境中稳定的新增Alpha")
    assert "RegimeComparisonTable" in {
        kind for _specialist, kind in regime.specialist_output_kinds
    }
