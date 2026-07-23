"""Deterministic assembly of question-facing answers from recorded Evidence."""

from __future__ import annotations

import json
import re
from typing import Any, Mapping

from ..agent_research_sandbox.process_runner import canonical_hash
from ..agent_research_product.collaboration import EvidenceCollaborationResult
from ..agent_research_product.contracts import (
    ProductExperimentResult,
    ProductExperimentStatus,
    ProductRunRequest,
    ResearchResultBundle,
)
from ..research_agent_runtime import ResearchSwarmResult
from .contracts import (
    AnswerResearchVerdict,
    AnswerStatus,
    CandidateDecision,
    CandidateDefinition,
    CandidateDisposition,
    EvidenceAuthority,
    EvidenceBoundFact,
    QuestionPartAnswer,
    QuestionRequirement,
    ResearchAnswer,
    ResearchAnswerSpec,
)
from .adapters import AnswerAdapterRegistry
from .compiler import ResearchAnswerContractCompiler
from .coverage import CoverageStatus, EvidenceCoverageGate, RequirementCoverage
from .source_summary import summarize_reviewed_source


_METHODS: Mapping[str, str] = {
    "ScoreTable": "构造横截面评分，并由独立服务检查预测方向、成本、稳定性和增量性。",
    "RegressionTable": "在明确控制变量下估计关系，并检查不确定性和共线诊断。",
    "RegimeComparisonTable": "按决策时可知状态分组，比较各状态样本和效应差异。",
    "EventTable": "按事件发布与可交易时间构造事件窗、对照组和安慰剂检验。",
    "DiagnosticTable": "对候选根因形成排序、反证和下一项可证伪修复实验。",
    "DatasetAssessmentTable": "检查数据覆盖、时间语义、偏差和研究适用范围。",
}

_RESULT_ROLES: Mapping[str, str] = {
    "ScoreTable": "ALPHA_SIGNAL",
    "RegressionTable": "STATISTICAL_TEST",
    "RegimeComparisonTable": "CONDITION_ANALYSIS",
    "EventTable": "EVENT_STUDY",
    "DiagnosticTable": "DIAGNOSTIC",
    "DatasetAssessmentTable": "DATA_ASSESSMENT",
}

_RESULT_LABELS: Mapping[str, str] = {
    "ScoreTable": "Alpha候选",
    "RegressionTable": "统计检验",
    "RegimeComparisonTable": "状态分析",
    "EventTable": "事件研究",
    "DiagnosticTable": "诊断结果",
    "DatasetAssessmentTable": "数据评估",
}

_GENERIC_METHODS = frozenset(_METHODS.values())
_IDENTIFIER = re.compile(r"\b[a-z][a-z0-9_]{2,}\b")
_DEFINITION_TRANSFORMS = (
    "评分", "组合", "标准化", "z-score", "zscore", "排序", "分位数", "回归",
    "差值", "比率", "变化率", "均值", "中位数", "取负", "中性化", "残差",
)
_DEFINITION_DIRECTIONS = (
    "正相关", "负相关", "分数越高", "分数越低", "预期更高", "预期更低",
    "预测", "排序", "方向", "多头", "空头",
)
_NON_FIELD_IDENTIFIERS = frozenset({
    "alpha", "draft", "scoretable", "train_predict_v2", "run_research",
    "train_rows", "prediction_rows", "target_value", "decision_time", "symbol",
    "source_available_at", "label_horizon_sessions",
})

_METRIC_LABELS: Mapping[str, str] = {
    "pearson_ic": "Pearson IC", "rank_ic": "RankIC", "ic_mean": "IC 均值",
    "icir": "ICIR", "net_return": "成本后收益", "gross_return": "成本前收益",
    "turnover": "换手率", "max_drawdown": "最大回撤",
    "subperiod_positive_fraction": "子时期正向比例",
    "regime_positive_fraction": "状态正向比例",
    "incremental_ic_mean": "增量 IC 均值",
    "max_registered_abs_correlation": "与已登记因子最高绝对相关性",
    "observation_count": "有效观测数", "period_count": "评价期数",
}


class ResearchAnswerAssembler:
    """Build an answer without recomputing metrics or upgrading authority."""

    def __init__(self, registry: AnswerAdapterRegistry | None = None) -> None:
        self.registry = registry or AnswerAdapterRegistry()
        self.contract_compiler = ResearchAnswerContractCompiler()
        self.coverage_gate = EvidenceCoverageGate(self.registry)

    def compile_spec(self, request: ProductRunRequest) -> ResearchAnswerSpec:
        # Old immutable receipts predate semantic requirements.  Preserve
        # their exact table-based identity for replay; every current product
        # task carries answer_requirement_ids and uses the semantic compiler.
        if not all(getattr(task, "answer_requirement_ids", ()) for task in request.tasks):
            output_kinds = tuple(sorted({task.output_kind for task in request.tasks}))
            requirements = tuple(
                QuestionRequirement(
                    requirement_id=f"R{index:02d}_{kind}",
                    question_text=self.registry.get(kind).requirement_text,
                    output_kind=kind,
                    dimensions=tuple(sorted(self.registry.get(kind).dimensions)),
                )
                for index, kind in enumerate(output_kinds, start=1)
            )
            return ResearchAnswerSpec(
                question_id=request.question_id,
                question=request.question,
                question_hash=canonical_hash({"question": request.question}),
                requirements=requirements,
                task_hashes=tuple(sorted(task.artifact_hash for task in request.tasks)),
                rule_version="alphaforge.answer-spec/v1",
            )
        return self.contract_compiler.compile(
            question_id=request.question_id,
            question=request.question,
            task_hashes=(task.artifact_hash for task in request.tasks),
        )

    def build_candidates(
        self,
        *,
        request: ProductRunRequest,
        swarm: ResearchSwarmResult | None,
        experiments: tuple[ProductExperimentResult, ...],
        artifact_repository: Any = None,
    ) -> tuple[CandidateDefinition, ...]:
        tasks = {task.specialist_id: task for task in request.tasks}
        outcomes = {} if swarm is None else {item.specialist_id: item for item in swarm.outcomes}
        counters: dict[str, int] = {}
        candidates = []
        for item in sorted(experiments, key=lambda value: value.experiment_id):
            task = tasks.get(item.specialist_id)
            if task is None:
                continue
            counters[item.specialist_id] = counters.get(item.specialist_id, 0) + 1
            outcome = outcomes.get(item.specialist_id)
            plan_hash = None if outcome is None else outcome.plan_hash
            plan = self._read_plan(plan_hash, artifact_repository)
            source_summary = self._read_source_summary(
                item.source_hash,
                item.output_kind or task.output_kind,
                artifact_repository,
            )
            evidence_hashes = tuple(sorted({
                value for value in (item.official_evidence_hash, item.research_evidence_hash)
                if value is not None
            }))
            candidates.append(CandidateDefinition(
                candidate_id=item.experiment_id,
                specialist_id=item.specialist_id,
                source_hash=item.source_hash,
                display_name=self._candidate_display_name(
                    specialist_id=item.specialist_id,
                    output_kind=item.output_kind or task.output_kind,
                    input_fields=(() if source_summary is None else source_summary.input_fields),
                    ordinal=counters[item.specialist_id],
                ),
                hypothesis=task.question,
                method_summary=plan or _METHODS[task.output_kind],
                output_kind=item.output_kind or task.output_kind,
                result_role=_RESULT_ROLES[item.output_kind or task.output_kind],
                implementation_summary=(
                    None if source_summary is None else source_summary.implementation_summary
                ),
                input_fields=(() if source_summary is None else source_summary.input_fields),
                design_warnings=(() if source_summary is None else source_summary.design_warnings),
                design_advisories=item.formula_advisory_codes,
                revision_of_source_hash=item.revision_of_source_hash,
                revision_reason_codes=item.revision_reason_codes,
                data_snapshot_hash=request.manifest.data_snapshot_hash,
                plan_hash=plan_hash,
                evidence_hashes=evidence_hashes,
            ))
        return tuple(sorted(candidates, key=lambda item: item.candidate_id))

    def assemble(
        self,
        *,
        request: ProductRunRequest,
        spec: ResearchAnswerSpec,
        candidates: tuple[CandidateDefinition, ...],
        experiments: tuple[ProductExperimentResult, ...],
        result_bundle: ResearchResultBundle,
        collaboration: EvidenceCollaborationResult,
        failures: tuple[str, ...],
    ) -> ResearchAnswer:
        by_id = {item.experiment_id: item for item in experiments}
        candidate_by_id = {item.candidate_id: item for item in candidates}
        decisions = tuple(
            self._candidate_decision(candidate, by_id[candidate.candidate_id], collaboration)
            for candidate in candidates
        )
        parts = []
        for requirement in spec.requirements:
            candidate_pool = tuple(
                item for item in candidates
                if requirement.output_kind is None or item.output_kind == requirement.output_kind
            )
            coverage = self.coverage_gate.assess(requirement, candidate_pool, by_id)
            matched = tuple(
                item for item in candidate_pool if item.candidate_id in coverage.candidate_ids
            )
            parts.append(self._answer_requirement(
                requirement, matched, by_id, collaboration, coverage
            ))
        parts = tuple(parts)
        statuses = {item.answer_status for item in parts}
        if statuses == {AnswerStatus.UNANSWERABLE_WITH_REASON}:
            answer_status = AnswerStatus.UNANSWERABLE_WITH_REASON
        elif statuses & {AnswerStatus.UNANSWERABLE_WITH_REASON, AnswerStatus.PARTIALLY_ANSWERED}:
            answer_status = AnswerStatus.PARTIALLY_ANSWERED
        elif AnswerStatus.CONDITIONALLY_ANSWERED in statuses:
            answer_status = AnswerStatus.CONDITIONALLY_ANSWERED
        elif statuses == {AnswerStatus.NEGATIVE_ANSWER}:
            answer_status = AnswerStatus.NEGATIVE_ANSWER
        else:
            answer_status = AnswerStatus.ANSWERED

        retained = tuple(
            candidate_by_id[item.candidate_id].display_name
            for item in decisions
            if item.disposition in {CandidateDisposition.RETAIN, CandidateDisposition.RETAIN_WITH_CONDITIONS}
        )
        rejected = tuple(
            candidate_by_id[item.candidate_id].display_name
            for item in decisions if item.disposition is CandidateDisposition.REJECT
        )
        direct_answer = self._direct_answer(
            answer_status, decisions, candidate_by_id, rejected, parts
        )
        verdict = self._research_verdict(answer_status, decisions)
        authority = self._overall_authority(parts)
        tested = tuple(sorted(
            item.question_text for item in spec.requirements
            if next(part for part in parts if part.requirement_id == item.requirement_id).answer_status
            is not AnswerStatus.UNANSWERABLE_WITH_REASON
        ))
        untested = tuple(sorted(
            item.question_text for item in spec.requirements
            if next(part for part in parts if part.requirement_id == item.requirement_id).answer_status
            is AnswerStatus.UNANSWERABLE_WITH_REASON
        ))
        limitations = set(failures)
        for item in experiments:
            if "UNFROZEN_DYNAMIC_TOOL_INPUT" in item.review_feedback_codes:
                limitations.add(
                    "动态调查结果尚未冻结进本次数据身份；相关结论仅作为辅助研究。"
                )
        if authority in {EvidenceAuthority.AUXILIARY_ONLY, EvidenceAuthority.MIXED}:
            limitations.add("部分结论来自机器复核研究证据，尚未升级为独立官方 Evidence。")
        limitations.update(
            limitation for part in parts for limitation in part.limitations
        )
        source_hashes = {
            spec.artifact_hash,
            collaboration.artifact_hash,
            result_bundle.artifact_hash,
            *(item.artifact_hash for item in candidates),
            *(value for item in candidates for value in item.evidence_hashes),
        }
        return ResearchAnswer(
            run_id=request.manifest.run_id,
            question_id=request.question_id,
            question_hash=spec.question_hash,
            answer_spec_hash=spec.artifact_hash,
            answer_status=answer_status,
            research_verdict=verdict,
            direct_answer=direct_answer,
            evidence_authority=authority,
            question_parts=parts,
            candidate_definitions=candidates,
            candidate_decisions=decisions,
            conflicts=tuple(sorted(set(collaboration.conflict_codes))),
            tested_scope=tested,
            untested_scope=untested,
            limitations=tuple(sorted(limitations)),
            next_experiment=self._next_experiment(answer_status, parts),
            source_artifact_hashes=tuple(sorted(source_hashes)),
        )

    def _answer_requirement(
        self,
        requirement: QuestionRequirement,
        candidates: tuple[CandidateDefinition, ...],
        experiments: Mapping[str, ProductExperimentResult],
        collaboration: EvidenceCollaborationResult,
        coverage: RequirementCoverage,
    ) -> QuestionPartAnswer:
        available = tuple(
            experiments[item.candidate_id] for item in candidates
            if item.candidate_id in experiments
        )
        facts = tuple(
            fact for item in available for fact in self._facts(item)
        )
        definitions = {item.candidate_id: item for item in candidates}
        incomplete_positive = tuple(
            item for item in available
            if self._is_positive(item)
            and not self._candidate_retainable(definitions[item.experiment_id])
        )
        positive = tuple(
            item for item in available
            if self._is_positive(item)
            and self._candidate_retainable(definitions[item.experiment_id])
        )
        negative = tuple(item for item in available if self._is_negative(item))
        revisions = tuple(
            item for item in available
            if self._is_revision(item) or item in incomplete_positive
        )
        authority = self._authority_for(available)
        candidate_ids = tuple(sorted(item.candidate_id for item in candidates))
        conflicts = bool(collaboration.conflict_codes) or bool(positive and negative)

        if not facts or coverage.status is CoverageStatus.MISSING:
            return QuestionPartAnswer(
                requirement_id=requirement.requirement_id,
                answer_status=AnswerStatus.UNANSWERABLE_WITH_REASON,
                direct_answer=f"本轮尚不能回答“{requirement.question_text}”。",
                judgment="没有形成与该问题部分匹配的机器 Evidence。",
                evidence_authority=EvidenceAuthority.NONE,
                candidate_ids=candidate_ids,
                limitations=("没有形成覆盖该问题语义的可评价非空结果。",),
                unanswered_reason="缺少匹配问题要求的实验结果或 Evidence。",
                recovery_requirement=(
                    "补做以下缺失项并从原问题恢复运行："
                    + "、".join((*coverage.missing_semantics, *coverage.missing_metrics) or coverage.reason_codes)
                ),
                question_text=requirement.question_text,
            )
        if coverage.status is CoverageStatus.PARTIAL:
            status = AnswerStatus.PARTIALLY_ANSWERED
            judgment = "已有机器结果，但用户要求的关键语义、指标或候选比较尚未完整覆盖。"
        elif positive and (
            negative or revisions or conflicts
            or authority is not EvidenceAuthority.OFFICIAL_EVIDENCE
        ):
            status = AnswerStatus.CONDITIONALLY_ANSWERED
            judgment = "存在可保留线索，但候选冲突、稳定性、定义完整性或部分验证仍有限制。"
        elif positive:
            status = AnswerStatus.ANSWERED
            judgment = "至少一个候选获得机器 Evidence 支持。"
        elif negative and len(negative) == len(available):
            status = AnswerStatus.NEGATIVE_ANSWER
            judgment = "已完成评价，但当前证据不支持保留这些候选。"
        else:
            status = AnswerStatus.PARTIALLY_ANSWERED
            judgment = "已形成机器结果，但不足以给出完整肯定或否定答案。"
        base_answer = (
            f"针对“{requirement.question_text}”，机器共评价 {len(available)} 项实验："
            f"{len(positive)} 项获得支持、{len(negative)} 项被否定、{len(revisions)} 项需要修订。"
        )
        summaries = []
        for kind in sorted({item.output_kind for item in available if item.output_kind}):
            representative = next(item for item in (*positive, *negative, *available) if item.output_kind == kind)
            summaries.append(self.registry.get(kind).summarize_metrics(
                representative.official_metrics or representative.research_metrics
            ))
        metric_summary = " ".join(summaries)
        names = {item.candidate_id: item.display_name for item in candidates}
        selected = tuple(names[item.experiment_id] for item in positive)
        rejected = tuple(names[item.experiment_id] for item in negative)
        revised = tuple(names[item.experiment_id] for item in revisions)
        details = []
        if selected:
            details.append("获得支持：" + "、".join(selected))
        if rejected:
            details.append("被拒绝：" + "、".join(rejected))
        if revised:
            details.append("需要修订：" + "、".join(revised))
        return QuestionPartAnswer(
            requirement_id=requirement.requirement_id,
            answer_status=status,
            direct_answer=(
                base_answer
                + (" " + "；".join(details) + "。" if details else "")
                + " " + metric_summary
            ),
            judgment=judgment,
            evidence_authority=authority,
            candidate_ids=candidate_ids,
            supporting_facts=tuple(fact for item in positive for fact in self._facts(item)),
            opposing_facts=tuple(fact for item in (*negative, *revisions) for fact in self._facts(item)),
            limitations=tuple(sorted({
                "不同研究路线存在方向或结论冲突。"
                for _ in (0,) if conflicts
            } | {
                "尚未覆盖：" + "、".join((*coverage.missing_semantics, *coverage.missing_metrics))
                for _ in (0,) if coverage.status is CoverageStatus.PARTIAL
            })),
            question_text=requirement.question_text,
        )

    def _candidate_decision(
        self,
        candidate: CandidateDefinition,
        experiment: ProductExperimentResult,
        collaboration: EvidenceCollaborationResult,
    ) -> CandidateDecision:
        evidence = candidate.evidence_hashes
        if self._is_positive(experiment) and not self._candidate_definition_complete(candidate):
            disposition = CandidateDisposition.REVISE
            reasons = ("CANDIDATE_DEFINITION_INCOMPLETE",)
        elif self._is_positive(experiment) and candidate.design_warnings:
            disposition = CandidateDisposition.REVISE
            reasons = candidate.design_warnings
        elif self._is_positive(experiment):
            disposition = (
                CandidateDisposition.RETAIN_WITH_CONDITIONS
                if collaboration.conflict_codes or experiment.status is ProductExperimentStatus.AUXILIARY_ONLY
                else CandidateDisposition.RETAIN
            )
            reasons = ("MACHINE_EVIDENCE_SUPPORTS",) if disposition is CandidateDisposition.RETAIN else ("CONDITIONAL_MACHINE_SUPPORT",)
        elif self._is_negative(experiment):
            disposition, reasons = CandidateDisposition.REJECT, ("MACHINE_EVIDENCE_REJECTS",)
        elif self._is_revision(experiment):
            disposition = CandidateDisposition.REVISE
            reasons = experiment.review_feedback_codes or ("MACHINE_REVIEW_REQUIRES_REVISION",)
        else:
            disposition = CandidateDisposition.NOT_EVALUATED
            reasons = (experiment.failure_code or "NO_MACHINE_VERDICT",)
        return CandidateDecision(
            candidate_id=candidate.candidate_id,
            disposition=disposition,
            reason_codes=tuple(sorted(reasons)),
            evidence_hashes=evidence,
        )

    @staticmethod
    def _facts(item: ProductExperimentResult) -> tuple[EvidenceBoundFact, ...]:
        evidence_hash = item.official_evidence_hash or item.research_evidence_hash
        metrics = item.official_metrics or item.research_metrics
        if evidence_hash is None:
            return ()
        authority = (
            EvidenceAuthority.OFFICIAL_EVIDENCE
            if item.official_evidence_hash is not None
            else EvidenceAuthority.AUXILIARY_ONLY
        )
        prioritized = sorted(metrics, key=lambda value: (
            0 if value[0] in _METRIC_LABELS else 1, value[0]
        ))[:12]
        return tuple(EvidenceBoundFact(
            text=f"{_METRIC_LABELS.get(name, name)}：{value}",
            evidence_hash=evidence_hash,
            authority=authority,
        ) for name, value in prioritized)

    @staticmethod
    def _is_positive(item: ProductExperimentResult) -> bool:
        return (item.court_outcome or item.research_verdict) in {
            "VALIDATED_RESEARCH", "SUPPORTED", "CONDITIONALLY_SUPPORTED"
        }

    @staticmethod
    def _is_negative(item: ProductExperimentResult) -> bool:
        return (item.court_outcome or item.research_verdict) in {"REJECT", "REJECTED"}

    @staticmethod
    def _is_revision(item: ProductExperimentResult) -> bool:
        return (
            (item.court_outcome or item.research_verdict) == "REVISE"
            or item.failure_code == "FORMULA_SEMANTIC_REVIEW_REQUIRED"
        )

    @staticmethod
    def _authority_for(items: tuple[ProductExperimentResult, ...]) -> EvidenceAuthority:
        official = any(item.official_evidence_hash for item in items)
        auxiliary = any(item.research_evidence_hash for item in items)
        if official and auxiliary:
            return EvidenceAuthority.MIXED
        if official:
            return EvidenceAuthority.OFFICIAL_EVIDENCE
        if auxiliary:
            return EvidenceAuthority.AUXILIARY_ONLY
        return EvidenceAuthority.NONE

    @staticmethod
    def _overall_authority(parts: tuple[QuestionPartAnswer, ...]) -> EvidenceAuthority:
        values = {item.evidence_authority for item in parts} - {EvidenceAuthority.NONE}
        if not values:
            return EvidenceAuthority.NONE
        if len(values) == 1:
            return next(iter(values))
        return EvidenceAuthority.MIXED

    @classmethod
    def _direct_answer(cls, status, decisions, candidates, rejected, parts) -> str:
        if status is AnswerStatus.UNANSWERABLE_WITH_REASON:
            return "本轮没有形成可回答原问题的机器 Evidence；报告已明确缺口和恢复条件。"
        if status is AnswerStatus.NEGATIVE_ANSWER:
            return (
                "本轮机器证据不支持原问题提出的预测关系或候选定义；"
                f"已完成并拒绝 {len(rejected)} 个候选，而不是‘没有结果’。"
            )
        retained = tuple(
            candidates[item.candidate_id]
            for item in decisions
            if item.disposition in {
                CandidateDisposition.RETAIN, CandidateDisposition.RETAIN_WITH_CONDITIONS,
            }
        )
        alpha = tuple(item for item in retained if item.result_role == "ALPHA_SIGNAL")
        analysis = tuple(item for item in retained if item.result_role != "ALPHA_SIGNAL")
        if alpha:
            primary = alpha[0]
            text = (
                f"本轮保留的主要 Alpha 是“{primary.display_name}”。"
                f"它的实际计算方法是：{cls._candidate_definition_text(primary)}"
            )
            if len(alpha) > 1:
                text += " 其余保留信号为：" + "、".join(item.display_name for item in alpha[1:3]) + "。"
            if analysis:
                text += f"另有 {len(analysis)} 项状态、统计或诊断分析用于限定该 Alpha 的适用条件；它们不是额外的 Alpha。"
            if status in {AnswerStatus.CONDITIONALLY_ANSWERED, AnswerStatus.PARTIALLY_ANSWERED}:
                text += "部分要求仍需补充验证。"
            return text
        if analysis:
            return (
                f"本轮形成 {len(analysis)} 项获得支持的研究分析，但没有形成可直接计算的 Alpha 评分信号；"
                "这些结果只能用于解释或限定条件。"
            )
        completed = sum(item.answer_status is not AnswerStatus.UNANSWERABLE_WITH_REASON for item in parts)
        if rejected:
            return (
                f"本轮已评价并否定 {len(rejected)} 个候选，回答了 {completed}/{len(parts)} 个研究要求；"
                "未回答部分已列出具体缺失实验，不能把否定结论写成‘没有答案’。"
            )
        return f"本轮回答了 {completed}/{len(parts)} 个研究要求，但尚无候选达到保留条件。"

    @staticmethod
    def _candidate_definition_text(candidate: CandidateDefinition) -> str:
        value = candidate.implementation_summary or candidate.method_summary
        value = " ".join(value.split())
        if len(value) <= 360:
            return value
        return value[:357].rstrip("，；。 ") + "……"

    @staticmethod
    def _candidate_definition_complete(candidate: CandidateDefinition) -> bool:
        """A retained Alpha must tell the customer exactly what is calculated.

        Source inspection is preferred.  Detailed legacy plans remain acceptable
        for receipt replay, but a generic method label can never masquerade as an
        Alpha definition.
        """

        if candidate.result_role != "ALPHA_SIGNAL":
            return True
        if candidate.implementation_summary and candidate.input_fields:
            return True
        method = " ".join(candidate.method_summary.lower().split())
        if not method or candidate.method_summary in _GENERIC_METHODS or len(method) < 60:
            return False
        identifiers = {
            item for item in _IDENTIFIER.findall(method)
            if item not in _NON_FIELD_IDENTIFIERS
        }
        has_transform = any(token in method for token in _DEFINITION_TRANSFORMS)
        has_direction = any(token in method for token in _DEFINITION_DIRECTIONS)
        return bool(identifiers and has_transform and has_direction)

    @classmethod
    def _candidate_retainable(cls, candidate: CandidateDefinition) -> bool:
        return cls._candidate_definition_complete(candidate) and not candidate.design_warnings

    @classmethod
    def _candidate_display_name(
        cls,
        *,
        specialist_id: str,
        output_kind: str,
        input_fields: tuple[str, ...],
        ordinal: int,
    ) -> str:
        specialist = cls._specialist_name(specialist_id)
        themes = []
        field_set = set(input_fields)
        theme_rules = (
            ("资金流", lambda: any("moneyflow" in item or "net_flow" in item for item in field_set)),
            ("大单结构", lambda: any("large" in item or "extra_large" in item for item in field_set)),
            ("估值", lambda: bool(field_set & {"pe", "pe_ttm", "pb", "book_to_market", "earnings_yield"})),
            ("盈利质量", lambda: bool(field_set & {"roe", "roa", "gross_margin", "cashflow_quality"})),
            ("动量与价格", lambda: any("momentum" in item or item in {"close", "return"} for item in field_set)),
            ("交易活跃度", lambda: bool(field_set & {"turnover_rate", "volume", "amount"})),
            ("Shibor状态", lambda: any(item.startswith("shibor_") for item in field_set)),
        )
        for label, predicate in theme_rules:
            if predicate():
                themes.append(label)
        subject = "、".join(themes[:3])
        if output_kind == "ScoreTable" and subject:
            return f"{subject}综合 Alpha（{specialist}）"
        if output_kind != "ScoreTable" and subject:
            suffix = _RESULT_LABELS[output_kind]
            if subject.endswith("状态") and suffix == "状态分析":
                suffix = "分析"
            return f"{subject}{suffix}（{specialist}）"
        return f"{specialist}{_RESULT_LABELS[output_kind]} {ordinal}"

    @staticmethod
    def _research_verdict(status, decisions) -> AnswerResearchVerdict:
        if status is AnswerStatus.UNANSWERABLE_WITH_REASON:
            return AnswerResearchVerdict.DATA_UNAVAILABLE
        dispositions = {item.disposition for item in decisions}
        if CandidateDisposition.RETAIN in dispositions and status is AnswerStatus.ANSWERED:
            return AnswerResearchVerdict.SUPPORTED
        if dispositions & {CandidateDisposition.RETAIN, CandidateDisposition.RETAIN_WITH_CONDITIONS}:
            return AnswerResearchVerdict.CONDITIONALLY_SUPPORTED
        if dispositions and dispositions <= {CandidateDisposition.REJECT}:
            return AnswerResearchVerdict.REJECTED
        return AnswerResearchVerdict.INSUFFICIENT_EVIDENCE

    @staticmethod
    def _next_experiment(status, parts) -> str:
        if status is AnswerStatus.UNANSWERABLE_WITH_REASON:
            first = next(item for item in parts if item.recovery_requirement)
            return str(first.recovery_requirement)
        if status is AnswerStatus.NEGATIVE_ANSWER:
            return "保留原问题和数据口径，针对反对证据设计新的候选机制并重新评价。"
        if status in {AnswerStatus.CONDITIONALLY_ANSWERED, AnswerStatus.PARTIALLY_ANSWERED}:
            return "对当前保留候选补做尚未回答的要求和冲突项，并使用相同冻结口径重放。"
        return "在独立冻结样本上重放保留候选，验证结论是否保持一致。"

    @staticmethod
    def _read_plan(plan_hash: str | None, repository: Any) -> str | None:
        if plan_hash is None or repository is None:
            return None
        raw = repository.get(plan_hash)
        if raw is None:
            return None
        try:
            value = json.loads(raw)
        except (TypeError, ValueError):
            return None
        plan = value.get("plan") if isinstance(value, dict) else None
        return plan.strip() if isinstance(plan, str) and plan.strip() else None

    @staticmethod
    def _read_source_summary(source_hash: str, output_kind: str, repository: Any):
        if repository is None:
            return None
        raw = repository.get(source_hash)
        if raw is None:
            return None
        return summarize_reviewed_source(raw, output_kind=output_kind)

    @staticmethod
    def _specialist_name(value: str) -> str:
        return {
            "alternative_data": "另类数据研究组",
            "expectations_events": "预期与事件研究组",
            "fundamental": "基本面研究组",
            "macro_regime": "宏观状态研究组",
            "market_structure": "市场行为与交易可行性研究组",
            "meta_alpha": "组合增量研究组",
        }.get(value, value)


__all__ = ["ResearchAnswerAssembler"]
