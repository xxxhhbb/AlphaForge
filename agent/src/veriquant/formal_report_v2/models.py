"""Stable formal research report v2 contracts and renderer."""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, ClassVar, Mapping

from ..contracts import CanonicalContract, EvidenceClass, FailureCode
from ..contracts.base import fail, freeze_mapping, require_hash, require_identifier, require_text
from ..data_planning import DataPlanningResult, FieldAvailability
from ..research_closure_contracts import (
    CandidateResearchOutcome,
    CampaignResearchOutcome,
    OfficialMetricRecord,
    ResearchOutcomeStatus,
)
from ..research_os import AnswerLevel, ResearchQuestionAssessment


FORMAL_REPORT_V2_SCHEMA_VERSION = "alphaforge.formal-report-v2/v1"
_FORBIDDEN_PUBLIC_TERMS = ("Desk", "Router", "router", "Swarm", "swarm", "pipeline", "Pipeline")
_FORBIDDEN_SENSITIVE_TERMS = (
    "api_key",
    "authorization",
    "bearer ",
    "token=",
    "sealed",
    "secret",
    "OFFICIAL_TUSHARE",
)


def _reject_public_text(value: str, field_name: str) -> None:
    require_text(value, field_name)
    lowered = value.lower()
    for token in _FORBIDDEN_PUBLIC_TERMS:
        if token in value:
            fail(FailureCode.UNAUTHORIZED_CAPABILITY, "formal report exposes engineering wording", field=field_name)
    for token in _FORBIDDEN_SENSITIVE_TERMS:
        if token.lower() in lowered:
            fail(FailureCode.UNAUTHORIZED_CAPABILITY, "formal report exposes sensitive or sealed wording", field=field_name)


def _text_tuple(value: tuple[str, ...], field_name: str, *, nonempty: bool = False) -> None:
    if not isinstance(value, tuple) or (nonempty and not value):
        fail(FailureCode.INVALID_VALUE, f"{field_name} must be a tuple")
    for item in value:
        _reject_public_text(item, field_name)


def _hash_tuple(value: tuple[str, ...], field_name: str, *, nonempty: bool = False) -> None:
    _text_tuple(value, field_name, nonempty=nonempty)
    for item in value:
        require_hash(item, field_name)


@dataclass(frozen=True, kw_only=True)
class ResearchabilitySectionV2(CanonicalContract):
    SCHEMA_VERSION: ClassVar[str] = FORMAL_REPORT_V2_SCHEMA_VERSION

    strongest_answer_level: AnswerLevel
    answerable_levels: tuple[AnswerLevel, ...]
    official_evaluation_ready: bool
    official_blockers: tuple[str, ...] = ()
    clarification_questions: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.strongest_answer_level, AnswerLevel):
            object.__setattr__(self, "strongest_answer_level", AnswerLevel(self.strongest_answer_level))
        if not isinstance(self.answerable_levels, tuple) or not self.answerable_levels:
            fail(FailureCode.MISSING_REQUIRED_FIELD, "answerable_levels are required")
        for level in self.answerable_levels:
            if not isinstance(level, AnswerLevel):
                AnswerLevel(level)
        if not isinstance(self.official_evaluation_ready, bool):
            fail(FailureCode.INVALID_VALUE, "official_evaluation_ready must be boolean")
        _text_tuple(self.official_blockers, "official_blockers")
        _text_tuple(self.clarification_questions, "clarification_questions")
        if not self.official_evaluation_ready and not self.official_blockers:
            fail(FailureCode.MISSING_REQUIRED_FIELD, "blocked official evaluation requires reasons")


@dataclass(frozen=True, kw_only=True)
class DataCoverageV2(CanonicalContract):
    SCHEMA_VERSION: ClassVar[str] = FORMAL_REPORT_V2_SCHEMA_VERSION

    provider_label: str
    available_sessions: int
    required_sessions: int
    available_symbols: int
    required_symbols: int
    lookback_sessions: int
    label_horizon_sessions: int
    required_fields: tuple[str, ...]
    optional_fields: tuple[str, ...]
    proxy_fields: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    missing_required_fields: tuple[str, ...] = ()
    field_availability_hash: str | None = None

    def __post_init__(self) -> None:
        require_identifier(self.provider_label, "provider_label")
        if self.provider_label == "OFFICIAL_TUSHARE":
            fail(FailureCode.AUXILIARY_MASQUERADE, "approved proxy data must not be called official Tushare")
        for name in (
            "available_sessions",
            "required_sessions",
            "available_symbols",
            "required_symbols",
            "lookback_sessions",
            "label_horizon_sessions",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                fail(FailureCode.INVALID_VALUE, f"{name} must be a non-negative integer")
        _text_tuple(self.required_fields, "required_fields")
        _text_tuple(self.optional_fields, "optional_fields")
        _text_tuple(self.missing_required_fields, "missing_required_fields")
        proxies = freeze_mapping(self.proxy_fields, "proxy_fields")
        normalized: dict[str, tuple[str, ...]] = {}
        for key, values in proxies.items():
            require_identifier(key, "proxy_fields.key")
            if not isinstance(values, tuple):
                values = tuple(values)
            _text_tuple(values, "proxy_fields.values", nonempty=True)
            normalized[key] = values
        object.__setattr__(self, "proxy_fields", MappingProxyType(dict(sorted(normalized.items()))))
        if self.field_availability_hash is not None:
            require_hash(self.field_availability_hash, "field_availability_hash")


@dataclass(frozen=True, kw_only=True)
class ResearchGroupReportV2(CanonicalContract):
    SCHEMA_VERSION: ClassVar[str] = FORMAL_REPORT_V2_SCHEMA_VERSION

    group_id: str
    group_label: str
    hypothesis: str
    candidate_name: str
    candidate_expression: str
    status: str
    source_hashes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        require_identifier(self.group_id, "group_id")
        for name in ("group_label", "hypothesis", "candidate_name", "candidate_expression", "status"):
            _reject_public_text(getattr(self, name), name)
        _hash_tuple(self.source_hashes, "source_hashes")


@dataclass(frozen=True, kw_only=True)
class CandidateReportV2(CanonicalContract):
    SCHEMA_VERSION: ClassVar[str] = FORMAL_REPORT_V2_SCHEMA_VERSION

    candidate_id: str
    group_label: str
    hypothesis: str
    expression: str
    translation_status: str
    pit_status: str
    evidence_status: str
    critic_status: str
    outcome: ResearchOutcomeStatus
    official_metrics: tuple[OfficialMetricRecord, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    attempt_hash: str | None = None
    failure_reasons: tuple[str, ...] = ()
    next_actions: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        require_identifier(self.candidate_id, "candidate_id")
        for name in ("group_label", "hypothesis", "expression", "translation_status", "pit_status", "evidence_status", "critic_status"):
            _reject_public_text(getattr(self, name), name)
        if not isinstance(self.outcome, ResearchOutcomeStatus):
            object.__setattr__(self, "outcome", ResearchOutcomeStatus(self.outcome))
        for metric in self.official_metrics:
            if not isinstance(metric, OfficialMetricRecord):
                fail(FailureCode.INVALID_VALUE, "official_metrics must contain OfficialMetricRecord")
            if metric.evidence_class is not EvidenceClass.OFFICIAL_EVIDENCE:
                fail(FailureCode.AUXILIARY_MASQUERADE, "candidate report cannot include auxiliary metrics as official")
        _hash_tuple(self.evidence_refs, "evidence_refs")
        if self.attempt_hash is not None:
            require_hash(self.attempt_hash, "attempt_hash")
        _text_tuple(self.failure_reasons, "failure_reasons")
        _text_tuple(self.next_actions, "next_actions")
        if self.outcome in {ResearchOutcomeStatus.VALIDATED_RESEARCH, ResearchOutcomeStatus.CHALLENGER} and not self.official_metrics:
            fail(FailureCode.INVALID_STATE, "validated candidates require official metrics")


@dataclass(frozen=True, kw_only=True)
class FormalResearchReportV2(CanonicalContract):
    SCHEMA_VERSION: ClassVar[str] = FORMAL_REPORT_V2_SCHEMA_VERSION

    report_id: str
    campaign_id: str
    run_id: str
    question: str
    executive_answer: str
    research_mode_label: str
    research_depth_label: str
    collaboration_label: str
    researchability: ResearchabilitySectionV2
    data_coverage: DataCoverageV2
    selected_research_groups: tuple[ResearchGroupReportV2, ...]
    candidates: tuple[CandidateReportV2, ...]
    comparison_summary: str
    limitations: tuple[str, ...]
    next_actions: tuple[str, ...]
    artifact_hashes: tuple[str, ...]

    def __post_init__(self) -> None:
        for name in ("report_id", "campaign_id", "run_id"):
            require_identifier(getattr(self, name), name)
        for name in ("question", "executive_answer", "research_mode_label", "research_depth_label", "collaboration_label", "comparison_summary"):
            _reject_public_text(getattr(self, name), name)
        if not isinstance(self.researchability, ResearchabilitySectionV2):
            fail(FailureCode.INVALID_VALUE, "researchability must be ResearchabilitySectionV2")
        if not isinstance(self.data_coverage, DataCoverageV2):
            fail(FailureCode.INVALID_VALUE, "data_coverage must be DataCoverageV2")
        for group in self.selected_research_groups:
            if not isinstance(group, ResearchGroupReportV2):
                fail(FailureCode.INVALID_VALUE, "selected_research_groups must contain ResearchGroupReportV2")
        for candidate in self.candidates:
            if not isinstance(candidate, CandidateReportV2):
                fail(FailureCode.INVALID_VALUE, "candidates must contain CandidateReportV2")
        _text_tuple(self.limitations, "limitations")
        _text_tuple(self.next_actions, "next_actions", nonempty=True)
        _hash_tuple(self.artifact_hashes, "artifact_hashes")


class FormalReportV2Composer:
    """Compose a stable report from planning and machine result contracts."""

    def compose(
        self,
        *,
        report_id: str,
        campaign_id: str,
        run_id: str,
        question_assessment: ResearchQuestionAssessment,
        data_planning: DataPlanningResult,
        selected_groups: tuple[ResearchGroupReportV2, ...],
        candidate_outcomes: tuple[CandidateResearchOutcome, ...] = (),
        campaign_outcome: CampaignResearchOutcome | None = None,
        candidate_expressions: Mapping[str, str] | None = None,
    ) -> FormalResearchReportV2:
        if not isinstance(question_assessment, ResearchQuestionAssessment):
            fail(FailureCode.INVALID_VALUE, "question_assessment must be ResearchQuestionAssessment")
        if not isinstance(data_planning, DataPlanningResult):
            fail(FailureCode.INVALID_VALUE, "data_planning must be DataPlanningResult")
        if campaign_outcome is not None and not isinstance(campaign_outcome, CampaignResearchOutcome):
            fail(FailureCode.INVALID_VALUE, "campaign_outcome must be CampaignResearchOutcome")
        for group in selected_groups:
            if not isinstance(group, ResearchGroupReportV2):
                fail(FailureCode.INVALID_VALUE, "selected_groups must contain ResearchGroupReportV2")
        expressions = dict(candidate_expressions or {})
        candidates = tuple(
            _candidate_from_outcome(item, expression=expressions.get(item.candidate_id))
            for item in candidate_outcomes
        )
        if not candidates:
            candidates = tuple(_candidate_from_group(group) for group in selected_groups)
        outcome = campaign_outcome.research_outcome if campaign_outcome is not None else _aggregate_outcome(candidates, data_planning)
        report = FormalResearchReportV2(
            report_id=report_id,
            campaign_id=campaign_id,
            run_id=run_id,
            question=question_assessment.intent.question,
            executive_answer=_executive_answer(outcome, data_planning, candidates),
            research_mode_label=_mode_label(question_assessment.intent.mode.value),
            research_depth_label=_depth_label(question_assessment.intent.depth.value),
            collaboration_label="标准协作",
            researchability=ResearchabilitySectionV2(
                strongest_answer_level=data_planning.strongest_answer_level,
                answerable_levels=question_assessment.answerable_levels,
                official_evaluation_ready=data_planning.official_evaluation_ready,
                official_blockers=tuple(data_planning.official_blockers),
                clarification_questions=tuple(question_assessment.clarification_questions),
            ),
            data_coverage=_coverage_from_plan(data_planning),
            selected_research_groups=selected_groups,
            candidates=candidates,
            comparison_summary=_comparison_summary(candidates),
            limitations=_limitations(data_planning, campaign_outcome),
            next_actions=_next_actions(outcome, data_planning),
            artifact_hashes=tuple(
                sorted(
                    {
                        question_assessment.artifact_hash,
                        data_planning.artifact_hash,
                        *(group.artifact_hash for group in selected_groups),
                        *(candidate.artifact_hash for candidate in candidates),
                        *((campaign_outcome.artifact_hash,) if campaign_outcome is not None else ()),
                    }
                )
            ),
        )
        _reject_public_text(report.canonical_json(), "formal_report_payload")
        return report


def _coverage_from_plan(data_planning: DataPlanningResult) -> DataCoverageV2:
    fields = data_planning.field_availability
    return DataCoverageV2(
        provider_label=data_planning.availability.provider_label,
        available_sessions=data_planning.availability.available_sessions,
        required_sessions=data_planning.data_plan.minimum_total_sessions,
        available_symbols=data_planning.availability.available_symbols,
        required_symbols=data_planning.data_plan.minimum_symbol_count,
        lookback_sessions=data_planning.data_plan.factor_lookback_sessions,
        label_horizon_sessions=data_planning.data_plan.label_horizon_sessions,
        required_fields=tuple(sorted(item.field_name for item in fields if item.required_for_official)),
        optional_fields=tuple(sorted(item.field_name for item in fields if not item.required_for_official and not item.proxy_for)),
        proxy_fields=_proxy_map(fields),
        missing_required_fields=tuple(sorted(item.field_name for item in fields if item.required_for_official and not item.available)),
        field_availability_hash=data_planning.artifact_hash,
    )


def _proxy_map(fields: tuple[FieldAvailability, ...]) -> Mapping[str, tuple[str, ...]]:
    proxies: dict[str, list[str]] = {}
    for item in fields:
        for source in item.proxy_for:
            proxies.setdefault(source, []).append(item.field_name)
    return MappingProxyType({key: tuple(sorted(values)) for key, values in sorted(proxies.items())})


def _candidate_from_group(group: ResearchGroupReportV2) -> CandidateReportV2:
    return CandidateReportV2(
        candidate_id=f"candidate.{group.group_id}",
        group_label=group.group_label,
        hypothesis=group.hypothesis,
        expression=group.candidate_expression,
        translation_status="待翻译为受限因子表达式",
        pit_status="未启动",
        evidence_status="未启动",
        critic_status="未启动",
        outcome=ResearchOutcomeStatus.NO_OFFICIAL_EVIDENCE,
        failure_reasons=("尚未运行机器证据链。",),
        next_actions=("确认数据与预算后运行机器证据。",),
    )


def _candidate_from_outcome(outcome: CandidateResearchOutcome, *, expression: str | None) -> CandidateReportV2:
    metrics = tuple(outcome.official_metrics)
    failure_reasons = (outcome.reason,) if outcome.reason else _failure_for(outcome)
    return CandidateReportV2(
        candidate_id=outcome.candidate_id,
        group_label="专业研究小组",
        hypothesis="候选由研究小组提交为草案，评审只引用机器证据。",
        expression=expression or "未公开表达式",
        translation_status="已提交" if outcome.attempt else "未提交",
        pit_status="已记录" if outcome.attempt.terminal_event_hash else "未记录",
        evidence_status="已有机器证据" if metrics else "无机器证据",
        critic_status=outcome.outcome.value,
        outcome=outcome.outcome,
        official_metrics=metrics,
        evidence_refs=tuple(sorted({metric.evidence_hash for metric in metrics})),
        attempt_hash=outcome.attempt.artifact_hash,
        failure_reasons=failure_reasons,
        next_actions=outcome.next_actions or _next_actions(outcome.outcome, None),
    )


def _failure_for(outcome: CandidateResearchOutcome) -> tuple[str, ...]:
    if outcome.outcome is ResearchOutcomeStatus.NO_OFFICIAL_EVIDENCE:
        return ("没有可引用的机器证据。",)
    if outcome.outcome is ResearchOutcomeStatus.REJECT:
        return ("机器评审未通过。",)
    if outcome.outcome is ResearchOutcomeStatus.REVISE:
        return ("候选需要修订后重新验证。",)
    return ()


def _aggregate_outcome(
    candidates: tuple[CandidateReportV2, ...],
    data_planning: DataPlanningResult,
) -> ResearchOutcomeStatus:
    if any(item.outcome is ResearchOutcomeStatus.CHALLENGER for item in candidates):
        return ResearchOutcomeStatus.CHALLENGER
    if any(item.outcome is ResearchOutcomeStatus.VALIDATED_RESEARCH for item in candidates):
        return ResearchOutcomeStatus.VALIDATED_RESEARCH
    if any(item.outcome is ResearchOutcomeStatus.REVISE for item in candidates):
        return ResearchOutcomeStatus.REVISE
    if any(item.outcome is ResearchOutcomeStatus.REJECT for item in candidates):
        return ResearchOutcomeStatus.REJECT
    return ResearchOutcomeStatus.NO_OFFICIAL_EVIDENCE


def _executive_answer(
    outcome: ResearchOutcomeStatus,
    data_planning: DataPlanningResult,
    candidates: tuple[CandidateReportV2, ...],
) -> str:
    if not data_planning.official_evaluation_ready:
        return "本次问题可以形成分级研究答复，但官方机器证据层暂时不能启动，原因见数据需求与限制。"
    if outcome is ResearchOutcomeStatus.NO_OFFICIAL_EVIDENCE:
        return "研究准备已完成，但尚未产生可引用的官方机器证据。"
    if outcome is ResearchOutcomeStatus.REJECT:
        return f"本次自动研究完成 {len(candidates)} 个候选检查，机器评审结论为拒绝。"
    if outcome is ResearchOutcomeStatus.REVISE:
        return f"本次自动研究完成 {len(candidates)} 个候选检查，结论是需要修订后再验证。"
    if outcome is ResearchOutcomeStatus.VALIDATED_RESEARCH:
        return f"本次自动研究得到 {len(candidates)} 个可继续复核的研究候选。"
    return "本次自动研究得到可进入挑战池的候选，但后续仍需独立权限复核。"


def _comparison_summary(candidates: tuple[CandidateReportV2, ...]) -> str:
    if not candidates:
        return "没有候选进入比较。"
    official = [item for item in candidates if item.official_metrics]
    if not official:
        return "没有候选具备机器证据，因此不产生胜出者。"
    validated = [item for item in official if item.outcome in {ResearchOutcomeStatus.VALIDATED_RESEARCH, ResearchOutcomeStatus.CHALLENGER}]
    if not validated:
        return "候选均未达到验证或挑战标准，本轮不产生胜出者。"
    return f"{len(validated)} 个候选达到可继续复核状态，排序必须由后续选择政策基于机器证据执行。"


def _limitations(
    data_planning: DataPlanningResult,
    campaign_outcome: CampaignResearchOutcome | None,
) -> tuple[str, ...]:
    limitations: list[str] = []
    limitations.extend(data_planning.official_blockers)
    if campaign_outcome is None:
        limitations.append("尚未接入完整机器证据结果，本报告只展示研究意图、数据需求和候选草案。")
    if data_planning.availability.provider_label == "APPROVED_PROXY_DATA_VENDOR":
        limitations.append("数据来源为已批准代理数据源，不表述为官方 Tushare。")
    return tuple(dict.fromkeys(limitations)) or ("本报告只引用已传入的机器结果，不使用模型补写指标。",)


def _next_actions(
    outcome: ResearchOutcomeStatus,
    data_planning: DataPlanningResult | None,
) -> tuple[str, ...]:
    if data_planning is not None and not data_planning.official_evaluation_ready:
        return ("补齐必需字段或扩大冻结样本后重新运行机器证据。", "保留概念性与来源型结论，不进入验证或挑战。")
    if outcome is ResearchOutcomeStatus.REVISE:
        return ("修订候选表达式或降低交易成本后重新验证。", "把失败原因写入试验记录，避免免费重试。")
    if outcome is ResearchOutcomeStatus.REJECT:
        return ("归档本轮失败并停止同族无依据重复尝试。",)
    if outcome is ResearchOutcomeStatus.NO_OFFICIAL_EVIDENCE:
        return ("运行受限因子表达式、PIT 与机器证据链。",)
    return ("进入人工检查点，确认是否扩大样本或进入下一阶段。",)


def _mode_label(value: str) -> str:
    labels = {
        "NEW_ALPHA_EXPLORATION": "新 Alpha 探索",
        "VALIDATE_IDEA": "验证我的想法",
        "IMPROVE_EXISTING_FACTOR": "改进现有因子",
        "PORTFOLIO_GAP_SEARCH": "补充组合缺口",
        "DIAGNOSE_FACTOR_PORTFOLIO": "诊断因子或组合",
        "EVENT_MARKET_TOPIC": "事件与市场专题",
    }
    return labels.get(value, value)


def _depth_label(value: str) -> str:
    return {
        "QUICK_EXPLORATION": "快速探索",
        "STANDARD_RESEARCH": "标准研究",
        "STRICT_VALIDATION": "严格验证",
    }.get(value, value)


def render_markdown_v2(report: FormalResearchReportV2) -> str:
    """Render deterministic, user-facing Chinese Markdown."""

    if not isinstance(report, FormalResearchReportV2):
        fail(FailureCode.INVALID_VALUE, "report must be FormalResearchReportV2")
    lines = [
        "# AlphaForge（千因研究院）正式研究报告",
        "",
        "## 一页结论",
        "",
        f"- 结论：{report.executive_answer}",
        f"- 评审结论：`{_outcome_cn(report)}`",
        f"- 研究模式：{report.research_mode_label}",
        f"- 研究深度：{report.research_depth_label}",
        f"- 协作方式：{report.collaboration_label}",
        "",
        "## 研究问题",
        "",
        report.question,
        "",
        "## 可研究性判断",
        "",
        f"- 最强可回答等级：`{report.researchability.strongest_answer_level.value}`",
        f"- 可回答等级：{', '.join(level.value for level in report.researchability.answerable_levels)}",
        f"- 官方机器证据是否可启动：{'是' if report.researchability.official_evaluation_ready else '否'}",
    ]
    for blocker in report.researchability.official_blockers:
        lines.append(f"- 阻断原因：{blocker}")
    for question in report.researchability.clarification_questions:
        lines.append(f"- 需要确认：{question}")
    lines.extend(
        [
            "",
            "## 数据需求与覆盖",
            "",
            f"- 数据来源标签：`{report.data_coverage.provider_label}`",
            f"- 因子观察窗口：{report.data_coverage.lookback_sessions} 个交易日",
            f"- 预测标签周期：{report.data_coverage.label_horizon_sessions} 个交易日",
            f"- 样本交易日：{report.data_coverage.available_sessions} / {report.data_coverage.required_sessions}",
            f"- 股票数量：{report.data_coverage.available_symbols} / {report.data_coverage.required_symbols}",
            f"- 必需字段：{', '.join(report.data_coverage.required_fields) or '无'}",
            f"- 可选字段：{', '.join(report.data_coverage.optional_fields) or '无'}",
        ]
    )
    if report.data_coverage.missing_required_fields:
        lines.append(f"- 缺失必需字段：{', '.join(report.data_coverage.missing_required_fields)}")
    for source, proxies in report.data_coverage.proxy_fields.items():
        lines.append(f"- {source} 的可用代理候选：{', '.join(proxies)}")
    lines.extend(["", "## 专业研究小组", ""])
    for group in report.selected_research_groups:
        lines.extend(
            [
                f"### {group.group_label}",
                f"- 假设：{group.hypothesis}",
                f"- 候选：{group.candidate_name}",
                f"- 表达式：`{group.candidate_expression}`",
                f"- 状态：{group.status}",
                "",
            ]
        )
    lines.extend(["## 候选与机器证据", ""])
    for candidate in report.candidates:
        lines.extend(
            [
                f"### {candidate.candidate_id}",
                f"- 来源小组：{candidate.group_label}",
                f"- 假设：{candidate.hypothesis}",
                f"- 表达式：`{candidate.expression}`",
                f"- 翻译状态：{candidate.translation_status}",
                f"- PIT 状态：{candidate.pit_status}",
                f"- 机器证据：{candidate.evidence_status}",
                f"- 评审状态：{candidate.critic_status}",
                f"- 结论：`{candidate.outcome.value}`",
            ]
        )
        if candidate.official_metrics:
            for metric in candidate.official_metrics:
                lines.append(
                    f"- {metric.metric_name} [{metric.split}, {metric.policy_version}] = {metric.value:.6g}; evidence `{metric.evidence_hash}`"
                )
        else:
            lines.append("- 官方指标：无，报告不补写指标。")
        for reason in candidate.failure_reasons:
            lines.append(f"- 原因：{reason}")
        for action in candidate.next_actions:
            lines.append(f"- 下一步：{action}")
        lines.append("")
    lines.extend(
        [
            "## 候选比较",
            "",
            report.comparison_summary,
            "",
            "## 限制",
            "",
        ]
    )
    for limitation in report.limitations:
        lines.append(f"- {limitation}")
    lines.extend(["", "## 下一步", ""])
    for action in report.next_actions:
        lines.append(f"- {action}")
    lines.extend(["", "## 追溯哈希", ""])
    for value in report.artifact_hashes:
        lines.append(f"- `{value}`")
    markdown = "\n".join(lines).rstrip() + "\n"
    _reject_public_text(markdown, "markdown")
    return markdown


def _outcome_cn(report: FormalResearchReportV2) -> str:
    if any(candidate.outcome is ResearchOutcomeStatus.CHALLENGER for candidate in report.candidates):
        return "可进入挑战池"
    if any(candidate.outcome is ResearchOutcomeStatus.VALIDATED_RESEARCH for candidate in report.candidates):
        return "形成可验证研究候选"
    if any(candidate.outcome is ResearchOutcomeStatus.REVISE for candidate in report.candidates):
        return "需要修订"
    if any(candidate.outcome is ResearchOutcomeStatus.REJECT for candidate in report.candidates):
        return "拒绝"
    return "暂无官方机器证据"
