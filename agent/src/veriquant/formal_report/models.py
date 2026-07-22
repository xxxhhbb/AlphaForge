"""Deterministic formal-report composition for AlphaForge."""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import ClassVar, Mapping

from ..contracts import CanonicalContract, FailureCode
from ..contracts.base import (
    fail,
    freeze_mapping,
    require_hash,
    require_identifier,
    require_text,
)
from ..research_closure_contracts import (
    USER_FACING_VOCABULARY,
    CandidateResearchOutcome,
    CampaignResearchOutcome,
    OfficialMetricRecord,
    ResearchOutcomeStatus,
    VerifiedDatasetReceipt,
)


FORMAL_REPORT_SCHEMA_VERSION = "alphaforge.formal-report/v1"
_FORBIDDEN_PUBLIC_TERMS = ("Desk", "Router", "router", "Swarm", "swarm", "pipeline", "Pipeline")
_SECRET_TOKENS = ("api_key", "authorization", "bearer ", "token=", "sealed", "OFFICIAL_TUSHARE")


def _reject_public_text(value: str, field_name: str) -> None:
    require_text(value, field_name)
    for token in _FORBIDDEN_PUBLIC_TERMS:
        if token in value:
            fail(FailureCode.UNAUTHORIZED_CAPABILITY, "formal report contains engineering wording", field=field_name)
    lowered = value.lower()
    for token in _SECRET_TOKENS:
        if token.lower() in lowered:
            fail(FailureCode.UNAUTHORIZED_CAPABILITY, "formal report contains forbidden sensitive wording", field=field_name)


def _text_tuple(value: tuple[str, ...], field_name: str, *, nonempty: bool = False) -> tuple[str, ...]:
    if not isinstance(value, tuple) or (nonempty and not value):
        fail(FailureCode.INVALID_VALUE, f"{field_name} must be a tuple")
    for item in value:
        _reject_public_text(item, field_name)
    return value


@dataclass(frozen=True, kw_only=True)
class DataCoverageSection(CanonicalContract):
    SCHEMA_VERSION: ClassVar[str] = FORMAL_REPORT_SCHEMA_VERSION

    source_label: str
    dataset_receipt_hash: str | None
    calendar_id: str | None
    universe_size: int
    symbol_count: int
    session_count: int
    row_count: int
    fields: tuple[str, ...]
    coverage_by_field: Mapping[str, float]
    time_range: str
    limitation: str = ""

    def __post_init__(self) -> None:
        _reject_public_text(self.source_label, "source_label")
        if self.source_label == "OFFICIAL_TUSHARE":
            fail(FailureCode.AUXILIARY_MASQUERADE, "approved proxy cannot be called official Tushare")
        if self.dataset_receipt_hash is not None:
            require_hash(self.dataset_receipt_hash, "dataset_receipt_hash")
        if self.calendar_id is not None:
            require_identifier(self.calendar_id, "calendar_id")
        for name in ("universe_size", "symbol_count", "session_count", "row_count"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                fail(FailureCode.INVALID_VALUE, f"{name} must be a non-negative integer")
        _text_tuple(self.fields, "fields")
        coverage = freeze_mapping(self.coverage_by_field, "coverage_by_field", numeric_values=True)
        object.__setattr__(self, "coverage_by_field", MappingProxyType(dict(sorted(coverage.items()))))
        _reject_public_text(self.time_range, "time_range")
        if self.limitation:
            _reject_public_text(self.limitation, "limitation")


@dataclass(frozen=True, kw_only=True)
class ResearchGroupSection(CanonicalContract):
    SCHEMA_VERSION: ClassVar[str] = FORMAL_REPORT_SCHEMA_VERSION

    group_id: str
    group_label: str
    hypothesis: str
    candidate_expression: str
    status: str
    artifact_hash: str | None = None

    def __post_init__(self) -> None:
        require_identifier(self.group_id, "group_id")
        for name in ("group_label", "hypothesis", "candidate_expression", "status"):
            _reject_public_text(getattr(self, name), name)
        if self.artifact_hash is not None:
            require_hash(self.artifact_hash, "artifact_hash")


@dataclass(frozen=True, kw_only=True)
class CandidateReportSection(CanonicalContract):
    SCHEMA_VERSION: ClassVar[str] = FORMAL_REPORT_SCHEMA_VERSION

    candidate_id: str
    candidate_label: str
    expression: str
    outcome: ResearchOutcomeStatus
    attempt_hash: str
    factor_dag_hash: str | None
    pit_receipt_hash: str | None
    metrics: tuple[OfficialMetricRecord, ...]
    reason: str
    next_actions: tuple[str, ...]

    def __post_init__(self) -> None:
        require_identifier(self.candidate_id, "candidate_id")
        for name in ("candidate_label", "expression", "reason"):
            _reject_public_text(getattr(self, name), name)
        if isinstance(self.outcome, str):
            object.__setattr__(self, "outcome", ResearchOutcomeStatus(self.outcome))
        if not isinstance(self.outcome, ResearchOutcomeStatus):
            fail(FailureCode.INVALID_VALUE, "outcome must be ResearchOutcomeStatus")
        require_hash(self.attempt_hash, "attempt_hash")
        if self.factor_dag_hash is not None:
            require_hash(self.factor_dag_hash, "factor_dag_hash")
        if self.pit_receipt_hash is not None:
            require_hash(self.pit_receipt_hash, "pit_receipt_hash")
        for metric in self.metrics:
            if not isinstance(metric, OfficialMetricRecord):
                fail(FailureCode.INVALID_VALUE, "metrics must contain OfficialMetricRecord")
        if self.outcome in {ResearchOutcomeStatus.VALIDATED_RESEARCH, ResearchOutcomeStatus.CHALLENGER} and not self.metrics:
            fail(FailureCode.INVALID_STATE, "formal report cannot validate without official metrics")
        _text_tuple(self.next_actions, "next_actions")


@dataclass(frozen=True, kw_only=True)
class ReportOnePageConclusion(CanonicalContract):
    SCHEMA_VERSION: ClassVar[str] = FORMAL_REPORT_SCHEMA_VERSION

    headline: str
    verdict: ResearchOutcomeStatus
    run_status: str
    key_points: tuple[str, ...]
    next_step: str

    def __post_init__(self) -> None:
        _reject_public_text(self.headline, "headline")
        if isinstance(self.verdict, str):
            object.__setattr__(self, "verdict", ResearchOutcomeStatus(self.verdict))
        _reject_public_text(self.run_status, "run_status")
        _text_tuple(self.key_points, "key_points")
        _reject_public_text(self.next_step, "next_step")


@dataclass(frozen=True, kw_only=True)
class NextAction(CanonicalContract):
    SCHEMA_VERSION: ClassVar[str] = FORMAL_REPORT_SCHEMA_VERSION

    action_id: str
    label: str
    reason: str

    def __post_init__(self) -> None:
        require_identifier(self.action_id, "action_id")
        _reject_public_text(self.label, "label")
        _reject_public_text(self.reason, "reason")


@dataclass(frozen=True, kw_only=True)
class FormalResearchReport(CanonicalContract):
    SCHEMA_VERSION: ClassVar[str] = FORMAL_REPORT_SCHEMA_VERSION

    report_id: str
    campaign_id: str
    run_id: str
    question: str
    mode_label: str
    depth_label: str
    interaction_label: str
    conclusion: ReportOnePageConclusion
    data_coverage: DataCoverageSection
    research_groups: tuple[ResearchGroupSection, ...]
    candidates: tuple[CandidateReportSection, ...]
    next_actions: tuple[NextAction, ...]
    artifact_hashes: tuple[str, ...]

    def __post_init__(self) -> None:
        for name in ("report_id", "campaign_id", "run_id"):
            require_identifier(getattr(self, name), name)
        for name in ("question", "mode_label", "depth_label", "interaction_label"):
            _reject_public_text(getattr(self, name), name)
        if not isinstance(self.conclusion, ReportOnePageConclusion):
            fail(FailureCode.INVALID_VALUE, "conclusion must be ReportOnePageConclusion")
        if not isinstance(self.data_coverage, DataCoverageSection):
            fail(FailureCode.INVALID_VALUE, "data_coverage must be DataCoverageSection")
        for group in self.research_groups:
            if not isinstance(group, ResearchGroupSection):
                fail(FailureCode.INVALID_VALUE, "research_groups must contain ResearchGroupSection")
        for candidate in self.candidates:
            if not isinstance(candidate, CandidateReportSection):
                fail(FailureCode.INVALID_VALUE, "candidates must contain CandidateReportSection")
        for action in self.next_actions:
            if not isinstance(action, NextAction):
                fail(FailureCode.INVALID_VALUE, "next_actions must contain NextAction")
        for value in self.artifact_hashes:
            require_hash(value, "artifact_hashes")


class FormalReportComposer:
    """Compose a deterministic Chinese report from machine receipts."""

    def compose(
        self,
        *,
        report_id: str,
        question: str,
        mode_label: str,
        depth_label: str,
        interaction_label: str,
        campaign_outcome: CampaignResearchOutcome,
        dataset_receipt: VerifiedDatasetReceipt | None,
        research_groups: tuple[ResearchGroupSection, ...],
        candidate_expressions: Mapping[str, str] | None = None,
    ) -> FormalResearchReport:
        if not isinstance(campaign_outcome, CampaignResearchOutcome):
            fail(FailureCode.INVALID_VALUE, "campaign_outcome must be CampaignResearchOutcome")
        if dataset_receipt is not None and not isinstance(dataset_receipt, VerifiedDatasetReceipt):
            fail(FailureCode.INVALID_VALUE, "dataset_receipt must be VerifiedDatasetReceipt")
        expressions = dict(candidate_expressions or {})
        candidates = tuple(
            _candidate_section(outcome, expression=expressions.get(outcome.candidate_id, "未公开表达式"))
            for outcome in campaign_outcome.candidate_outcomes
        )
        conclusion = _conclusion(campaign_outcome, candidates)
        return FormalResearchReport(
            report_id=report_id,
            campaign_id=campaign_outcome.campaign_id,
            run_id=campaign_outcome.run_id,
            question=question,
            mode_label=mode_label,
            depth_label=depth_label,
            interaction_label=interaction_label,
            conclusion=conclusion,
            data_coverage=_data_coverage(dataset_receipt),
            research_groups=research_groups,
            candidates=candidates,
            next_actions=_next_actions(campaign_outcome.research_outcome),
            artifact_hashes=tuple(
                sorted(
                    value
                    for value in (
                        dataset_receipt.artifact_hash if dataset_receipt else None,
                        campaign_outcome.artifact_hash,
                        *(candidate.attempt_hash for candidate in candidates),
                    )
                    if value is not None
                )
            ),
        )


def _candidate_section(outcome: CandidateResearchOutcome, *, expression: str) -> CandidateReportSection:
    return CandidateReportSection(
        candidate_id=outcome.candidate_id,
        candidate_label=f"候选 {outcome.candidate_id}",
        expression=expression,
        outcome=outcome.outcome,
        attempt_hash=outcome.attempt.artifact_hash,
        factor_dag_hash=None,
        pit_receipt_hash=outcome.attempt.terminal_event_hash,
        metrics=outcome.official_metrics,
        reason=outcome.reason or _default_reason(outcome.outcome),
        next_actions=outcome.next_actions or _default_candidate_actions(outcome.outcome),
    )


def _default_reason(outcome: ResearchOutcomeStatus) -> str:
    if outcome is ResearchOutcomeStatus.NO_OFFICIAL_EVIDENCE:
        return "尚未取得可复现的官方机器证据，不能正式评审。"
    if outcome is ResearchOutcomeStatus.REJECT:
        return "机器证据未达到研究政策要求。"
    if outcome is ResearchOutcomeStatus.REVISE:
        return "候选需要修订后重新验证。"
    if outcome is ResearchOutcomeStatus.VALIDATED_RESEARCH:
        return "候选通过可见训练与验证证据，但尚未进入挑战池。"
    return "候选取得独立评审收据，可进入挑战池。"


def _default_candidate_actions(outcome: ResearchOutcomeStatus) -> tuple[str, ...]:
    if outcome is ResearchOutcomeStatus.REVISE:
        return ("降低换手", "补充稳健性检验", "重新提交候选")
    if outcome is ResearchOutcomeStatus.REJECT:
        return ("停止本候选", "记录失败原因", "避免同族无成本重试")
    if outcome is ResearchOutcomeStatus.NO_OFFICIAL_EVIDENCE:
        return ("补齐数据版本", "运行 FactorDAG 与机器证据")
    return ("进入人工复核检查点",)


def _conclusion(
    campaign_outcome: CampaignResearchOutcome,
    candidates: tuple[CandidateReportSection, ...],
) -> ReportOnePageConclusion:
    verdict = campaign_outcome.research_outcome
    metric_candidates = sum(1 for item in candidates if item.metrics)
    headline = {
        ResearchOutcomeStatus.NO_OFFICIAL_EVIDENCE: "本次研究尚未形成正式机器证据",
        ResearchOutcomeStatus.REJECT: "本次候选未通过机器评审",
        ResearchOutcomeStatus.REVISE: "本次研究建议修订后复验",
        ResearchOutcomeStatus.VALIDATED_RESEARCH: "本次研究形成可验证候选",
        ResearchOutcomeStatus.CHALLENGER: "本次研究产生挑战池候选",
    }[verdict]
    key_points = (
        f"系统运行状态：{campaign_outcome.run_status.value}",
        f"候选数量：{len(candidates)}",
        f"具备官方机器指标的候选：{metric_candidates}",
    )
    return ReportOnePageConclusion(
        headline=headline,
        verdict=verdict,
        run_status=campaign_outcome.run_status.value,
        key_points=key_points,
        next_step=_next_actions(verdict)[0].label,
    )


def _data_coverage(dataset_receipt: VerifiedDatasetReceipt | None) -> DataCoverageSection:
    if dataset_receipt is None:
        return DataCoverageSection(
            source_label="未绑定正式数据版本",
            dataset_receipt_hash=None,
            calendar_id=None,
            universe_size=0,
            symbol_count=0,
            session_count=0,
            row_count=0,
            fields=(),
            coverage_by_field={},
            time_range="未运行",
            limitation="缺少已验证数据收据，报告只能作为研究草案。",
        )
    return DataCoverageSection(
        source_label=dataset_receipt.provider_source_label,
        dataset_receipt_hash=dataset_receipt.artifact_hash,
        calendar_id=dataset_receipt.calendar_id,
        universe_size=len(dataset_receipt.universe),
        symbol_count=dataset_receipt.symbol_count,
        session_count=dataset_receipt.session_count,
        row_count=dataset_receipt.row_count,
        fields=dataset_receipt.fields,
        coverage_by_field=dataset_receipt.coverage_by_field,
        time_range=f"{dataset_receipt.start_time.date().isoformat()} 至 {dataset_receipt.end_time.date().isoformat()}",
        limitation="样本范围以本次冻结数据版本为准，不代表全市场结论。",
    )


def _next_actions(outcome: ResearchOutcomeStatus) -> tuple[NextAction, ...]:
    if outcome is ResearchOutcomeStatus.REVISE:
        return (
            NextAction(action_id="revise.factor", label="修订候选", reason="当前证据提示需要降低成本或增强稳健性。"),
            NextAction(action_id="rerun.evidence", label="重新验证", reason="修订后必须重新运行机器证据。"),
        )
    if outcome is ResearchOutcomeStatus.NO_OFFICIAL_EVIDENCE:
        return (
            NextAction(action_id="run.evidence", label="运行机器证据", reason="正式结论必须来自冻结输入和确定性计算。"),
        )
    if outcome is ResearchOutcomeStatus.REJECT:
        return (
            NextAction(action_id="archive.failure", label="归档失败", reason="失败也消耗预算并进入审计记录。"),
        )
    return (
        NextAction(action_id="human.checkpoint", label="进入人工检查点", reason="后续验证需要独立评审和权限边界。"),
    )


def render_markdown(report: FormalResearchReport) -> str:
    """Render stable Chinese Markdown without exposing sealed or engineering terms."""

    if not isinstance(report, FormalResearchReport):
        fail(FailureCode.INVALID_VALUE, "report must be FormalResearchReport")
    lines = [
        f"# {USER_FACING_VOCABULARY.report_label}",
        "",
        f"## 一页结论",
        "",
        f"- 结论：{report.conclusion.headline}",
        f"- 评审结论：`{report.conclusion.verdict.value}`",
        f"- 运行状态：`{report.conclusion.run_status}`",
        *[f"- {point}" for point in report.conclusion.key_points],
        f"- 下一步：{report.conclusion.next_step}",
        "",
        "## 研究问题",
        "",
        f"- 问题：{report.question}",
        f"- 研究模式：{report.mode_label}",
        f"- 研究深度：{report.depth_label}",
        f"- 协作方式：{report.interaction_label}",
        "",
        "## 数据版本",
        "",
        f"- 来源：`{report.data_coverage.source_label}`",
        f"- 日历：`{report.data_coverage.calendar_id or '未绑定'}`",
        f"- 股票数量：{report.data_coverage.symbol_count}",
        f"- 交易日数量：{report.data_coverage.session_count}",
        f"- 行数：{report.data_coverage.row_count}",
        f"- 时间范围：{report.data_coverage.time_range}",
        f"- 字段：{', '.join(report.data_coverage.fields) or '未绑定'}",
    ]
    if report.data_coverage.coverage_by_field:
        for field, coverage in report.data_coverage.coverage_by_field.items():
            lines.append(f"- 覆盖率 {field}: {coverage:.2%}")
    if report.data_coverage.limitation:
        lines.append(f"- 限制：{report.data_coverage.limitation}")
    lines.extend(["", "## 研究小组", ""])
    for group in report.research_groups:
        lines.extend(
            [
                f"### {group.group_label}",
                f"- 假设：{group.hypothesis}",
                f"- 候选表达式：`{group.candidate_expression}`",
                f"- 状态：`{group.status}`",
                "",
            ]
        )
    lines.extend(["## 候选与机器证据", ""])
    for candidate in report.candidates:
        lines.extend(
            [
                f"### {candidate.candidate_label}",
                f"- 表达式：`{candidate.expression}`",
                f"- 评审结论：`{candidate.outcome.value}`",
                f"- 尝试收据：`{candidate.attempt_hash}`",
                f"- 原因：{candidate.reason}",
            ]
        )
        if candidate.metrics:
            for metric in candidate.metrics:
                lines.append(
                    f"- {metric.metric_name} [{metric.split}, {metric.policy_version}]: {metric.value:.6g} "
                    f"(evidence `{metric.evidence_hash}`)"
                )
        else:
            lines.append("- 官方机器指标：无")
        for action in candidate.next_actions:
            lines.append(f"- 建议：{action}")
        lines.append("")
    lines.extend(["## 追溯哈希", ""])
    for value in report.artifact_hashes:
        lines.append(f"- `{value}`")
    markdown = "\n".join(lines)
    _reject_public_text(markdown, "markdown")
    return markdown
