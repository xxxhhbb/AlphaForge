"""U5 deterministic Chinese reporting over committed U4 DRAFT artifacts."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path
from typing import ClassVar

from ..contracts import CanonicalContract, FailureCode
from ..contracts.base import fail, require_hash, require_identifier, require_text
from ..llm_desks.safety import canonical_hash
from ..research_campaign import ResearchCampaign
from ..research_os_publication.models import assert_publication_payload_safe, require_safe_run_id
from ..research_run_controller import ResearchRunIdentityStore, ResearchRunLifecycle
from ..specialist_research import SpecialistCheckpointStore
from .multi_models import MultiSpecialistBranchStatus, MultiSpecialistRunResult, MultiSpecialistRunStatus
from .evidence_adapter import EvidenceAdapterStatus, U4EvidenceAdapterResult
from .selection import V3CandidateSelectionResult
from .tools import FrozenResearchContext


SCHEMA_VERSION = "alphaforge.research-os-v3-report/v2"
GROUP_LABELS = {
    "fundamental": "基本面研究组",
    "expectations_events": "预期与事件研究组",
    "market_structure": "市场行为与交易可行性研究组",
    "alternative_data": "另类信息研究组",
    "macro_regime": "宏观状态研究组",
    "meta_alpha": "组合增量研究组",
}


class ResearchReportOutcome(str, Enum):
    EVALUATED_RESEARCH_RESULT = "EVALUATED_RESEARCH_RESULT"
    DRAFT_RESEARCH_RESULT = "DRAFT_RESEARCH_RESULT"
    NEGATIVE_RESEARCH_RESULT = "NEGATIVE_RESEARCH_RESULT"
    DATA_OR_CAPABILITY_GAP_REPORT = "DATA_OR_CAPABILITY_GAP_REPORT"
    PARTIAL_RESEARCH_RESULT = "PARTIAL_RESEARCH_RESULT"
    SYSTEM_FAILURE_REPORT = "SYSTEM_FAILURE_REPORT"
    CANCELLED_RESEARCH_REPORT = "CANCELLED_RESEARCH_REPORT"


@dataclass(frozen=True, kw_only=True)
class ResearchMetricView(CanonicalContract):
    name: str
    value: float
    evidence_hash: str
    SCHEMA_VERSION: ClassVar[str] = SCHEMA_VERSION

    def __post_init__(self) -> None:
        require_identifier(self.name, "metric.name")
        if isinstance(self.value, bool) or not isinstance(self.value, (int, float)):
            fail(FailureCode.INVALID_VALUE, "metric value must be numeric")
        require_hash(self.evidence_hash, "metric.evidence_hash")


@dataclass(frozen=True, kw_only=True)
class ResearchCandidateView(CanonicalContract):
    specialist_id: str
    group_label: str
    candidate_id: str
    # Historical v2 reports did not persist the family. Keep them readable;
    # newly composed reports always supply the real DRAFT family_id.
    family_id: str = "legacy_unclassified"
    candidate_hash: str
    hypothesis: str
    economic_mechanism: str
    expression: str
    lookback_sessions: int
    forecast_horizon_sessions: int
    preflight_status: str
    factor_dag_hash: str | None
    formula_program_hash: str | None = None
    formula_program_kind: str = "LEGACY_FACTOR_DAG"
    formula_manifest_hash: str | None = None
    failure_code: str | None
    machine_evidence_status: str = "NOT_RUN"
    official_metrics: tuple[ResearchMetricView, ...] = ()
    evidence_reason_codes: tuple[str, ...] = ()
    selection_verdict: str = "NOT_EVALUATED"
    selection_score: float | None = None
    selection_reason_codes: tuple[str, ...] = ()
    promotion_status: str = "NOT_REQUESTED"
    SCHEMA_VERSION: ClassVar[str] = SCHEMA_VERSION

    def __post_init__(self) -> None:
        for name in ("specialist_id", "candidate_id", "family_id", "preflight_status", "machine_evidence_status", "selection_verdict", "promotion_status"):
            require_identifier(getattr(self, name), name)
        for name in ("group_label", "hypothesis", "economic_mechanism", "expression"):
            require_text(getattr(self, name), name)
        require_hash(self.candidate_hash, "candidate_hash")
        if self.factor_dag_hash is not None:
            require_hash(self.factor_dag_hash, "factor_dag_hash")
        if self.formula_program_hash is not None:
            require_hash(self.formula_program_hash, "formula_program_hash")
        if self.formula_manifest_hash is not None:
            require_hash(self.formula_manifest_hash, "formula_manifest_hash")
        if self.formula_program_kind not in {"LEGACY_FACTOR_DAG", "CERTIFIED_QLIB_EXPRESSION"}:
            fail(FailureCode.INVALID_VALUE, "unsupported formula program kind")
        if self.formula_program_kind == "CERTIFIED_QLIB_EXPRESSION":
            if self.factor_dag_hash is not None or self.formula_program_hash is None or self.formula_manifest_hash is None:
                fail(FailureCode.INVALID_STATE, "Qlib report record requires program and manifest hashes")
        if self.failure_code is not None:
            require_identifier(self.failure_code, "failure_code")
        if self.lookback_sessions < 1 or self.forecast_horizon_sessions < 1:
            fail(FailureCode.INVALID_VALUE, "candidate horizons must be positive")
        if self.machine_evidence_status == "NOT_RUN" and self.official_metrics:
            fail(FailureCode.AUXILIARY_MASQUERADE, "metrics require official visible Evidence")
        if self.machine_evidence_status == "OFFICIAL_VISIBLE_EVALUATED" and not self.official_metrics:
            fail(FailureCode.MISSING_REQUIRED_FIELD, "evaluated candidate needs official metrics")
        if self.selection_score is not None and (
            isinstance(self.selection_score, bool) or not isinstance(self.selection_score, (int, float))
        ):
            fail(FailureCode.INVALID_VALUE, "selection score must be numeric")
        for value in self.selection_reason_codes:
            require_identifier(value, "selection_reason_codes")
        for value in self.evidence_reason_codes:
            require_identifier(value, "evidence_reason_codes")
        if self.promotion_status != "NOT_REQUESTED":
            fail(FailureCode.UNAUTHORIZED_CAPABILITY, "report cannot grant Promotion")


@dataclass(frozen=True, kw_only=True)
class ResearchGroupView(CanonicalContract):
    specialist_id: str
    group_label: str
    status: str
    candidate_count: int
    accepted_preflight_count: int
    provider_calls: int
    typed_failures: tuple[str, ...]
    missing_capabilities: tuple[str, ...]
    outcome_hash: str | None
    SCHEMA_VERSION: ClassVar[str] = SCHEMA_VERSION

    def __post_init__(self) -> None:
        for name in ("specialist_id", "status"):
            require_identifier(getattr(self, name), name)
        require_text(self.group_label, "group_label")
        for name in ("candidate_count", "accepted_preflight_count", "provider_calls"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                fail(FailureCode.INVALID_VALUE, f"{name} must be non-negative")
        for value in (*self.typed_failures, *self.missing_capabilities):
            require_identifier(value, "group diagnostic")
        if self.outcome_hash is not None:
            require_hash(self.outcome_hash, "outcome_hash")


@dataclass(frozen=True, kw_only=True)
class ResearchOSV3FormalReport(CanonicalContract):
    report_id: str
    run_id: str
    question: str
    campaign_hash: str
    route_hash: str
    context_hash: str
    source_label: str
    data_start: str
    data_end: str
    universe_hash: str
    model_id: str
    outcome: ResearchReportOutcome
    executive_answer: str
    groups: tuple[ResearchGroupView, ...]
    candidates: tuple[ResearchCandidateView, ...]
    candidate_count: int
    accepted_preflight_count: int
    machine_evidence_status: str
    official_evidence_hash: str | None = None
    selection_hash: str | None = None
    best_candidate_hash: str | None = None
    selected_candidate_hashes: tuple[str, ...] = ()
    evaluated_candidate_count: int = 0
    rejected_candidate_count: int = 0
    promotion_hash: None = None
    limitations: tuple[str, ...] = ()
    next_actions: tuple[str, ...] = ()
    SCHEMA_VERSION: ClassVar[str] = SCHEMA_VERSION

    def __post_init__(self) -> None:
        for name in ("report_id", "run_id", "source_label", "model_id", "machine_evidence_status"):
            require_identifier(getattr(self, name), name)
        require_text(self.question, "question")
        require_text(self.executive_answer, "executive_answer")
        for name in ("campaign_hash", "route_hash", "context_hash", "universe_hash"):
            require_hash(getattr(self, name), name)
        if not isinstance(self.outcome, ResearchReportOutcome):
            object.__setattr__(self, "outcome", ResearchReportOutcome(self.outcome))
        if tuple(item.specialist_id for item in self.groups) != tuple(sorted(item.specialist_id for item in self.groups)):
            fail(FailureCode.INVALID_VALUE, "groups must be stably sorted")
        candidate_keys = tuple((item.specialist_id, item.candidate_id) for item in self.candidates)
        if candidate_keys != tuple(sorted(candidate_keys)) or len(candidate_keys) != len(set(candidate_keys)):
            fail(FailureCode.INVALID_VALUE, "candidates must be unique and stably sorted")
        if self.candidate_count != len(self.candidates):
            fail(FailureCode.INVALID_VALUE, "candidate_count mismatch")
        if self.accepted_preflight_count != sum(item.preflight_status == "ACCEPTED" for item in self.candidates):
            fail(FailureCode.INVALID_VALUE, "accepted_preflight_count mismatch")
        if self.official_evidence_hash is not None:
            require_hash(self.official_evidence_hash, "official_evidence_hash")
        if self.selection_hash is not None:
            require_hash(self.selection_hash, "selection_hash")
        if self.best_candidate_hash is not None:
            require_hash(self.best_candidate_hash, "best_candidate_hash")
        for value in self.selected_candidate_hashes:
            require_hash(value, "selected_candidate_hashes")
        if self.best_candidate_hash is not None and (
            not self.selected_candidate_hashes or self.best_candidate_hash != self.selected_candidate_hashes[0]
        ):
            fail(FailureCode.INVALID_STATE, "best candidate must be first in the deterministic shortlist")
        for name in ("evaluated_candidate_count", "rejected_candidate_count"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                fail(FailureCode.INVALID_VALUE, f"{name} must be non-negative")
        if self.evaluated_candidate_count > self.candidate_count or self.rejected_candidate_count > self.candidate_count:
            fail(FailureCode.INVALID_VALUE, "candidate funnel counts exceed candidate_count")
        if self.machine_evidence_status == "OFFICIAL_VISIBLE_EVALUATED" and self.official_evidence_hash is None:
            fail(FailureCode.MISSING_REQUIRED_FIELD, "official visible report needs Evidence hash")
        if self.promotion_hash is not None:
            fail(FailureCode.UNAUTHORIZED_CAPABILITY, "report has no Promotion authority")
        for item in (*self.limitations, *self.next_actions):
            require_text(item, "report narrative")


@dataclass(frozen=True, kw_only=True)
class ResearchOSV3ReportReceipt(CanonicalContract):
    run_id: str
    report_hash: str
    machine_report_hash: str
    markdown_hash: str
    canonical_manifest_hash: str
    publication_root: str
    status: str = "PUBLISHED"
    SCHEMA_VERSION: ClassVar[str] = SCHEMA_VERSION

    def __post_init__(self) -> None:
        require_safe_run_id(self.run_id)
        for name in ("report_hash", "machine_report_hash", "markdown_hash", "canonical_manifest_hash"):
            require_hash(getattr(self, name), name)
        require_text(self.publication_root, "publication_root")
        if self.status != "PUBLISHED":
            fail(FailureCode.INVALID_STATE, "report receipt must be PUBLISHED")


class ResearchOSV3ReportBackend:
    """Verify committed branches, render public artifacts, update one manifest."""

    def __init__(self, *, run_root: str | Path, publication_root: str | Path) -> None:
        self.run_root = Path(run_root).resolve()
        self.publication_root = Path(publication_root).resolve()
        self.identity_store = ResearchRunIdentityStore(self.run_root)

    def compose_and_publish(
        self,
        *,
        campaign: ResearchCampaign,
        context: FrozenResearchContext,
        result: MultiSpecialistRunResult,
        evidence: U4EvidenceAdapterResult | None = None,
        selection: V3CandidateSelectionResult | None = None,
    ) -> tuple[ResearchOSV3FormalReport, ResearchOSV3ReportReceipt]:
        if result.campaign_hash != campaign.artifact_hash or result.context_hash != context.artifact_hash:
            fail(FailureCode.INVALID_HASH, "report inputs belong to different frozen research identities")
        manifest = self.identity_store.load_manifest(result.run_id)
        if manifest is None:
            fail(FailureCode.INVALID_HASH, "U4 result does not match the canonical run manifest")
        base_manifest_hash = replace(
            manifest,
            report_hash=None,
            official_evidence_hash=None,
            trial_count=0,
        ).artifact_hash
        if manifest.artifact_hash != result.manifest_hash and base_manifest_hash != result.manifest_hash:
            historical_terminal_result = (
                manifest.current_stage
                in {
                    ResearchRunLifecycle.FAILED,
                    ResearchRunLifecycle.CANCELLED,
                }
                and tuple(item.specialist_id for item in result.branches)
                == tuple(sorted(manifest.selected_group_ids))
                and result.route_hash == manifest.route_plan_hash
            )
            if not historical_terminal_result:
                fail(FailureCode.INVALID_HASH, "U4 result does not match the canonical run manifest")
        terminals = {}
        if evidence is not None:
            if evidence.run_id != result.run_id or evidence.source_run_hash != result.artifact_hash:
                fail(FailureCode.INVALID_HASH, "Evidence belongs to another research run")
            terminals = {(item.specialist_id, item.candidate_id): item for item in evidence.terminals}
        selection_records = {}
        if selection is not None:
            if evidence is None or selection.run_id != result.run_id or selection.evidence_result_hash != evidence.artifact_hash:
                fail(FailureCode.INVALID_HASH, "selection belongs to another Evidence result")
            selection_records = {item.candidate_hash: item for item in selection.records}
        groups: list[ResearchGroupView] = []
        candidates: list[ResearchCandidateView] = []
        for branch in result.branches:
            outcome = SpecialistCheckpointStore(
                self.run_root / "research_runs", run_id=result.run_id, specialist_id=branch.specialist_id
            ).load_outcome()
            if branch.outcome_hash is None:
                if outcome is not None:
                    fail(FailureCode.INVALID_STATE, "non-running branch unexpectedly has an outcome")
            elif outcome is None or outcome.artifact_hash != branch.outcome_hash:
                fail(FailureCode.INVALID_HASH, "branch outcome hash mismatch")
            refs = {item.candidate_id: item for item in branch.candidate_refs}
            if outcome is not None:
                draft_ids = {item.candidate_id for item in outcome.candidates}
                if draft_ids != set(refs):
                    fail(FailureCode.INCOMPLETE_RESULT, "branch candidate references do not match committed outcome")
                for draft in outcome.candidates:
                    ref = refs[draft.candidate_id]
                    if ref.candidate_hash != draft.artifact_hash:
                        fail(FailureCode.INVALID_HASH, "candidate draft hash mismatch")
                    terminal = terminals.get((branch.specialist_id, draft.candidate_id))
                    metrics = ()
                    evidence_status = "NOT_RUN"
                    evidence_failure = ref.failure_code
                    evidence_reasons = ()
                    if terminal is not None:
                        evidence_status = terminal.status.value
                        evidence_reasons = terminal.failure_codes
                        evidence_failure = terminal.failure_codes[0] if terminal.failure_codes else evidence_failure
                        if terminal.visible_metrics is not None:
                            metrics = tuple(
                                ResearchMetricView(name=name, value=float(value), evidence_hash=terminal.visible_metrics.metric_source_hash)
                                for name, value in sorted(terminal.visible_metrics.metrics.items())
                            )
                    selection_record = selection_records.get(draft.artifact_hash)
                    candidates.append(ResearchCandidateView(
                        specialist_id=branch.specialist_id,
                        group_label=GROUP_LABELS[branch.specialist_id],
                        candidate_id=draft.candidate_id,
                        family_id=draft.family_id,
                        candidate_hash=draft.artifact_hash,
                        hypothesis=draft.hypothesis,
                        economic_mechanism=draft.economic_mechanism,
                        expression=draft.expression,
                        lookback_sessions=draft.lookback_sessions,
                        forecast_horizon_sessions=draft.forecast_horizon_sessions,
                        preflight_status=ref.preflight_status,
                        factor_dag_hash=ref.factor_dag_hash,
                        formula_program_hash=ref.formula_program_hash,
                        formula_program_kind=ref.formula_program_kind,
                        formula_manifest_hash=ref.formula_manifest_hash,
                        failure_code=evidence_failure,
                        machine_evidence_status=evidence_status,
                        official_metrics=metrics,
                        evidence_reason_codes=evidence_reasons,
                        selection_verdict=selection_record.verdict.value if selection_record else "NOT_EVALUATED",
                        selection_score=selection_record.score if selection_record else None,
                        selection_reason_codes=selection_record.reason_codes if selection_record else (),
                    ))
            groups.append(ResearchGroupView(
                specialist_id=branch.specialist_id,
                group_label=GROUP_LABELS[branch.specialist_id],
                status=branch.status.value,
                candidate_count=len(branch.candidate_refs),
                accepted_preflight_count=sum(item.preflight_status == "ACCEPTED" for item in branch.candidate_refs),
                provider_calls=branch.provider_calls,
                typed_failures=branch.typed_failures,
                missing_capabilities=branch.missing_capabilities,
                outcome_hash=branch.outcome_hash,
            ))
        groups_tuple = tuple(sorted(groups, key=lambda item: item.specialist_id))
        candidates_tuple = tuple(sorted(candidates, key=lambda item: (item.specialist_id, item.candidate_id)))
        evaluated = sum(item.machine_evidence_status == EvidenceAdapterStatus.OFFICIAL_VISIBLE_EVALUATED.value for item in candidates_tuple)
        selected_hashes = selection.promotion_review_candidate_hashes if selection is not None else ()
        rejected = sum(item.selection_verdict in {"NOT_ELIGIBLE", "REJECTED", "REVISE"} for item in candidates_tuple)
        outcome = (
            ResearchReportOutcome.CANCELLED_RESEARCH_REPORT
            if manifest.current_stage is ResearchRunLifecycle.CANCELLED
            else ResearchReportOutcome.SYSTEM_FAILURE_REPORT
            if manifest.current_stage is ResearchRunLifecycle.FAILED
            else
            ResearchReportOutcome.EVALUATED_RESEARCH_RESULT
            if evidence is not None and selected_hashes
            else ResearchReportOutcome.NEGATIVE_RESEARCH_RESULT
            if evidence is not None
            else _report_outcome(result)
        )
        report = ResearchOSV3FormalReport(
            report_id="report." + result.run_id,
            run_id=result.run_id,
            question=campaign.question,
            campaign_hash=campaign.artifact_hash,
            route_hash=result.route_hash,
            context_hash=context.artifact_hash,
            source_label=context.source_label,
            data_start=context.data_start,
            data_end=context.data_end,
            universe_hash=context.universe_hash,
            model_id=manifest.model_id,
            outcome=outcome,
            executive_answer=_executive_answer(outcome, result, evaluated=evaluated, selected=len(selected_hashes), rejected=rejected),
            groups=groups_tuple,
            candidates=candidates_tuple,
            candidate_count=len(candidates_tuple),
            accepted_preflight_count=sum(item.preflight_status == "ACCEPTED" for item in candidates_tuple),
            machine_evidence_status="OFFICIAL_VISIBLE_EVALUATED" if evidence and evidence.official_evidence_hash else "COMPLETED_NO_OFFICIAL_EVIDENCE" if evidence else "NOT_RUN",
            official_evidence_hash=evidence.official_evidence_hash if evidence else None,
            selection_hash=selection.artifact_hash if selection else None,
            best_candidate_hash=selected_hashes[0] if selected_hashes else None,
            selected_candidate_hashes=selected_hashes,
            evaluated_candidate_count=evaluated,
            rejected_candidate_count=rejected,
            limitations=(
                "只展示训练/验证可见机器证据；独立封闭评审内容不可见。" if evidence else "本阶段只完成研究草案和受限因子表达式预检，尚未计算机器证据。",
                "研究验证通过不等于进入挑战池，报告不拥有晋级权限。",
                "成本后收益是本次冻结样本、既定分组和成本模型下的研究统计，不是未来收益预测或收益保证。",
                f"本轮机器评价只使用已冻结并登记的字段：{', '.join(context.available_fields)}；不会假设性补齐未登记数据。",
            ),
            next_actions=("根据机器拒绝或修订原因调整候选；通过者可申请独立封闭评审。",) if evidence else ("由确定性服务在冻结输入上执行时间点检查和机器证据计算。",),
        )
        machine = report.to_dict()
        markdown = render_chinese_report(report)
        assert_publication_payload_safe(machine, "research_report")
        assert_publication_payload_safe(markdown, "research_report_markdown")
        run_dir = self._publication_dir(result.run_id)
        machine_bytes = _json_bytes(machine)
        markdown_bytes = markdown.encode("utf-8")
        _archive_superseded_publication(
            run_dir,
            incoming_machine=machine_bytes,
            incoming_markdown=markdown_bytes,
            referenced_report_hash=manifest.report_hash,
        )
        _write_or_replace(run_dir / "machine_report.json", machine_bytes)
        _write_or_replace(run_dir / "report.md", markdown_bytes)
        if (
            manifest.report_hash is not None
            and manifest.report_hash != report.artifact_hash
            and evidence is not None
        ):
            fail(FailureCode.ARTIFACT_COLLISION, "canonical manifest already references another report")
        updated = self.identity_store.ensure_manifest(replace(manifest, report_hash=report.artifact_hash))
        receipt = ResearchOSV3ReportReceipt(
            run_id=result.run_id,
            report_hash=report.artifact_hash,
            machine_report_hash=_sha256(machine_bytes),
            markdown_hash=_sha256(markdown_bytes),
            canonical_manifest_hash=updated.artifact_hash,
            publication_root=result.run_id,
        )
        _write_or_replace(run_dir / "receipt.json", receipt.canonical_bytes())
        return report, receipt

    def _publication_dir(self, run_id: str) -> Path:
        require_safe_run_id(run_id)
        target = (self.publication_root / run_id).resolve()
        try:
            target.relative_to(self.publication_root)
        except ValueError:
            fail(FailureCode.UNAUTHORIZED_CAPABILITY, "report publication path escapes root")
        target.mkdir(parents=True, exist_ok=True)
        return target


def _report_outcome(result: MultiSpecialistRunResult) -> ResearchReportOutcome:
    if result.status is MultiSpecialistRunStatus.CANCELLED:
        return ResearchReportOutcome.CANCELLED_RESEARCH_REPORT
    if result.status is MultiSpecialistRunStatus.FAILED:
        return ResearchReportOutcome.SYSTEM_FAILURE_REPORT
    statuses = {item.status for item in result.branches}
    if statuses <= {MultiSpecialistBranchStatus.DATA_GAP, MultiSpecialistBranchStatus.BUDGET_BLOCKED}:
        return ResearchReportOutcome.DATA_OR_CAPABILITY_GAP_REPORT
    if result.candidate_count and result.accepted_preflight_count == 0:
        return ResearchReportOutcome.NEGATIVE_RESEARCH_RESULT
    if any(
        item in statuses
        for item in (
            MultiSpecialistBranchStatus.FAILED,
            MultiSpecialistBranchStatus.PARTIAL_RESULT,
            MultiSpecialistBranchStatus.DATA_GAP,
            MultiSpecialistBranchStatus.BUDGET_BLOCKED,
        )
    ):
        return ResearchReportOutcome.PARTIAL_RESEARCH_RESULT
    return ResearchReportOutcome.DRAFT_RESEARCH_RESULT


def _executive_answer(
    outcome: ResearchReportOutcome,
    result: MultiSpecialistRunResult,
    *,
    evaluated: int = 0,
    selected: int = 0,
    rejected: int = 0,
) -> str:
    if outcome is ResearchReportOutcome.EVALUATED_RESEARCH_RESULT:
        return f"本次逐项处理 {result.candidate_count} 个候选，{evaluated} 个完成机器评价，{selected} 个进入独立评审短名单，{rejected} 个被拒绝或要求修订。"
    if outcome is ResearchReportOutcome.NEGATIVE_RESEARCH_RESULT and evaluated:
        return f"本次逐项处理 {result.candidate_count} 个候选，{evaluated} 个完成机器评价，但没有候选通过多指标短名单政策；这是完整的负向研究结果。"
    if outcome is ResearchReportOutcome.DATA_OR_CAPABILITY_GAP_REPORT:
        return "本次研究已形成可追溯的数据或能力缺口报告，没有伪造候选或机器结论。"
    if outcome is ResearchReportOutcome.SYSTEM_FAILURE_REPORT:
        return f"研究运行发生基础设施或合同故障；已保留 {result.candidate_count} 个此前提交的草案，并生成故障报告供修复和恢复。"
    if outcome is ResearchReportOutcome.CANCELLED_RESEARCH_REPORT:
        return f"本次研究已按请求取消；已保留 {result.candidate_count} 个取消前提交的草案，未启动或未完成的步骤不会伪造成成功。"
    if outcome is ResearchReportOutcome.NEGATIVE_RESEARCH_RESULT:
        return f"本次形成 {result.candidate_count} 个研究草案，但没有候选通过受限因子表达式预检。"
    if outcome is ResearchReportOutcome.PARTIAL_RESEARCH_RESULT:
        return f"本次保留 {result.candidate_count} 个可追溯草案；部分研究组异常，独立分支结果仍有效。"
    return (
        f"本次多专业研究形成 {result.candidate_count} 个草案，其中 "
        f"{result.accepted_preflight_count} 个通过受限因子表达式预检，等待机器证据计算。"
    )


def render_chinese_report(report: ResearchOSV3FormalReport) -> str:
    best = next(
        (item for item in report.candidates if item.candidate_hash == report.best_candidate_hash),
        None,
    )
    lines = [
        "# AlphaForge（千因研究院）研究报告",
        "",
        "## 一页结论",
        "",
        f"**结论：{_outcome_label(report.outcome)}。** {report.executive_answer}",
        "",
        f"- 用户问题：{report.question}",
        f"- 本轮建议：{_customer_action(report)}",
        f"- 最佳候选：{best.candidate_id if best else '本轮没有达到短名单标准的候选'}",
        f"- 研究范围：{report.data_start} 至 {report.data_end}；{_source_label(report.source_label)}",
        f"- 候选进度：提出 {report.candidate_count} 个，安全预检通过 {report.accepted_preflight_count} 个，机器评价 {report.evaluated_candidate_count} 个，进入独立评审短名单 {len(report.selected_candidate_hashes)} 个。",
        f"- 搜索覆盖：{len({item.family_id for item in report.candidates})} 个候选家族；这是有预算上限的系统搜索，不等于穷尽所有可能公式。",
        "",
        "## 为什么得到这个结论",
        "",
    ]
    if best is not None:
        lines.extend([
            f"### 当前领先候选：{best.candidate_id}",
            f"- 来自：{best.group_label}",
            f"- 研究假设：{best.hypothesis}",
            f"- 经济机制：{best.economic_mechanism}",
            f"- 机器结论：{_selection_label(best.selection_verdict)}",
            f"- 主要理由：{_reason_text(best.selection_reason_codes)}",
            "",
        ])
    elif report.evaluated_candidate_count:
        lines.extend([
            "本轮已有候选完成机器评价，但没有候选同时满足成本后表现、稳定性、增量性和研究预算要求。",
            "这是一项有效的负向研究结论，不代表系统没有运行。",
            "",
            *_key_observation_lines(report),
        ])

    lines.extend(["## 各专业研究组做了什么", ""])
    for group in report.groups:
        lines.extend([
            f"### {group.group_label}",
            f"- 状态：{_group_status_label(group.status)}",
            f"- 候选：{group.candidate_count}",
            f"- 安全表达式预检通过：{group.accepted_preflight_count}",
            f"- 数据或能力缺口：{_gap_text(group.missing_capabilities)}",
            f"- 运行说明：{_group_diagnostic(group)}",
            "",
        ])
    lines.extend([
        "## 机器结果总览",
        "",
        "表中指标是多个 walk-forward 验证折的汇总；机器裁决使用最后一个可见验证折。因而汇总收益为正时，若最新验证折未达政策，候选仍会被拒绝。",
        "",
        *_candidate_summary_table(report),
        "",
        "## 候选明细",
        "",
    ])
    for candidate in report.candidates:
        lines.extend([
            f"### {candidate.group_label} / {candidate.candidate_id}",
            f"- 候选家族：{candidate.family_id}",
            f"- 假设：{candidate.hypothesis}",
            f"- 机制：{candidate.economic_mechanism}",
            f"- Qlib 研究表达式：`{candidate.expression}`",
            f"- 观察窗口：{candidate.lookback_sessions} 个交易日",
            f"- 预测周期：{candidate.forecast_horizon_sessions} 个交易日",
            f"- 安全预检：{_preflight_label(candidate.preflight_status)}",
            f"- 机器验证：{_evidence_label(candidate.machine_evidence_status)}",
            f"- 当前判断：{_selection_label(candidate.selection_verdict)}",
            f"- 判断依据：{_reason_text(_candidate_reason_codes(candidate))}",
            *_public_metric_lines(candidate),
            "",
        ])
    lines.extend(["## 下一轮怎么改", ""])
    lines.extend(f"- {item}" for item in _next_research_actions(report))
    lines.extend(["## 限制与建议", ""])
    lines.extend(f"- {item}" for item in report.limitations)
    lines.extend(f"- 下一步：{item}" for item in report.next_actions)
    lines.extend([
        "",
        "## 审计附录",
        "",
        f"- 研究编号：`{report.run_id}`",
        f"- 报告哈希：`{report.artifact_hash}`",
        f"- 官方可见证据：`{report.official_evidence_hash or '未生成'}`",
        f"- 选择记录：`{report.selection_hash or '未生成'}`",
        "- 独立封闭评审的内容、指标和排名不在本报告中公开。",
        "",
    ])
    return "\n".join(lines)


def _outcome_label(outcome: ResearchReportOutcome) -> str:
    return {
        ResearchReportOutcome.EVALUATED_RESEARCH_RESULT: "已有候选通过可见研究筛选，可申请独立评审",
        ResearchReportOutcome.NEGATIVE_RESEARCH_RESULT: "本轮没有候选达到研究短名单标准",
        ResearchReportOutcome.DATA_OR_CAPABILITY_GAP_REPORT: "当前数据或能力不足以完成所请求的机器评价",
        ResearchReportOutcome.PARTIAL_RESEARCH_RESULT: "已保留可用研究结果，但部分分支未完成",
        ResearchReportOutcome.DRAFT_RESEARCH_RESULT: "已形成研究草案，等待机器评价",
        ResearchReportOutcome.SYSTEM_FAILURE_REPORT: "研究运行失败，已生成可恢复的故障报告",
        ResearchReportOutcome.CANCELLED_RESEARCH_REPORT: "研究已取消，已保留取消前的有效产物",
    }[outcome]


def _customer_action(report: ResearchOSV3FormalReport) -> str:
    if report.selected_candidate_hashes:
        return "审阅领先候选，并决定是否提交独立封闭评审。"
    if report.evaluated_candidate_count:
        return "查看拒绝或修订原因，调整机制、数据或成本约束后再研究。"
    if report.outcome is ResearchReportOutcome.DATA_OR_CAPABILITY_GAP_REPORT:
        return "补充报告列出的数据或时间语义后重新运行。"
    if report.outcome is ResearchReportOutcome.SYSTEM_FAILURE_REPORT:
        return "根据报告中的类型化故障修复运行环境，然后从受控检查点恢复。"
    if report.outcome is ResearchReportOutcome.CANCELLED_RESEARCH_REPORT:
        return "如需继续，可从已保存检查点恢复；恢复不会重复已提交阶段。"
    return "等待完成安全预检和机器评价，不应据此形成交易决策。"


def _source_label(value: str) -> str:
    return {"APPROVED_PROXY_DATA_VENDOR": "已批准代理数据源", "DETERMINISTIC_OFFLINE_FIXTURE": "确定性离线数据"}.get(value, "已登记研究数据源")


def _group_status_label(value: str) -> str:
    return {"COMPLETED_DRAFT": "已提交草案", "NEGATIVE_RESULT": "负向结果", "DATA_GAP": "数据缺口", "BUDGET_BLOCKED": "预算已用尽", "PARTIAL_RESULT": "部分完成", "FAILED": "运行异常", "CANCELLED": "已取消"}.get(value, value)


def _gap_text(values: tuple[str, ...]) -> str:
    return "无" if not values else "；".join(values)


def _preflight_label(value: str) -> str:
    return {"ACCEPTED": "通过", "REVISE": "需要修订", "CAPABILITY_REQUEST": "存在能力缺口", "NOT_EVALUATED": "尚未执行"}.get(value, value)


def _evidence_label(value: str) -> str:
    return {"OFFICIAL_VISIBLE_EVALUATED": "已生成官方可见机器证据", "NOT_RUN": "尚未执行", "BLOCKED": "已阻断"}.get(value, value)


def _selection_label(value: str) -> str:
    return {"SELECTED_FOR_PROMOTION_REVIEW": "进入独立评审短名单", "NOT_ELIGIBLE": "不符合当前选择条件", "REJECTED": "拒绝", "REVISE": "需要修订", "NOT_EVALUATED": "尚未进入机器选择"}.get(value, value)


def _candidate_reason_codes(candidate: ResearchCandidateView) -> tuple[str, ...]:
    informational = {
        "PIT_VALID",
        "STATISTICS_PASS",
        "COST_CAPACITY_PASS",
        "INCREMENTALITY_PASS",
    }
    detailed = tuple(code for code in candidate.evidence_reason_codes if code not in informational)
    combined = (*detailed, *candidate.selection_reason_codes)
    return tuple(dict.fromkeys(combined))


def _reason_text(values: tuple[str, ...]) -> str:
    labels = {
        "PIT_NOT_VALIDATED": "时点一致性尚未通过",
        "OFFICIAL_EVIDENCE_MISSING": "缺少官方可见机器证据",
        "CRITIC_NOT_PASSED": "机器审查未通过",
        "VISIBLE_SELECTION_METRICS_MISSING": "缺少可比较的公开指标",
        "FAMILY_BUDGET_EXHAUSTED": "候选家族预算已用尽",
        "NET_ICIR_BELOW_POLICY": "成本后信息比率不足",
        "STABILITY_BELOW_POLICY": "跨时期或市场状态稳定性不足",
        "COST_CAPACITY_BELOW_POLICY": "成本或容量条件不足",
        "NOVELTY_BELOW_POLICY": "与既有研究过于相似",
        "INCREMENTALITY_BELOW_POLICY": "对既有因子的增量贡献不足",
        "CANDIDATE_SELECTION_BUDGET_LIMIT": "短名单名额已用尽",
        "PIT_EVIDENCE_CRITIC_VISIBLE_SELECTION_PASS": "通过时点、机器证据与可见选择条件",
        "WEAK_STATISTICAL_EVIDENCE": "统计证据偏弱",
        "NET_RETURN_FAIL": "成本后收益未通过机器政策",
        "NO_INCREMENTAL_EVIDENCE": "对已登记因子的增量证据不足",
        "DRAFT_REVIEW_REJECTED": "候选草案安全审查未通过",
        "DRAFT_TRANSLATION_REJECTED": "候选表达式无法安全转换",
        "EXPRESSION_REJECTED": "候选表达式未通过受限执行检查",
        "PREFLIGHT_STATUS_MISMATCH": "候选预检状态不一致，已关闭评价",
    }
    return "尚未进入机器选择" if not values else "；".join(labels.get(item, item) for item in values)


def _metric_label(value: str) -> str:
    return {
        "train_icir": "训练期 ICIR",
        "validation_icir": "验证期 ICIR",
        "net_icir": "成本后 ICIR",
        "stability_score": "稳定性",
        "turnover": "换手率",
        "net_return": "成本后收益",
        "quantile_spread": "分位数组合收益差",
        "incrementality": "增量性",
    }.get(value, value.replace("_", " "))


def _public_metric_lines(candidate) -> list[str]:
    if not candidate.official_metrics:
        return ["- 正式指标：无；报告不会补写或猜测指标。"]
    metrics = {metric.name: metric for metric in candidate.official_metrics}
    wanted = (
        ("validation_icir", "验证期 ICIR", "number"),
        ("validation_rank_ic", "验证 RankIC", "number"),
        ("validation_net_return", "验证成本后收益", "percent"),
        ("validation_turnover", "验证换手", "percent"),
        ("validation_max_registered_abs_correlation", "与已登记因子最高相关", "number"),
        ("train_icir", "训练期 ICIR", "number"),
    )
    lines: list[str] = []
    for name, label, style in wanted:
        metric = metrics.get(name)
        if metric is not None:
            rendered = f"{metric.value * 100:.2f}%" if style == "percent" else f"{metric.value:.4f}"
            lines.append(f"- {label}：{rendered}")
    if not lines:
        lines.append("- 关键指标：已生成；完整机器指标见审计附录的 machine_report.json。")
    hidden = max(0, len(candidate.official_metrics) - len(lines))
    if hidden:
        lines.append(f"- 完整指标：另有 {hidden} 项机器指标已写入审计 JSON，本页不逐项铺开。")
    return lines


def _metric_value(candidate: ResearchCandidateView, name: str) -> float | None:
    for metric in candidate.official_metrics:
        if metric.name == name:
            return float(metric.value)
    return None


def _metric_table_value(candidate: ResearchCandidateView, name: str, *, percent: bool = False) -> str:
    value = _metric_value(candidate, name)
    if value is None:
        return "—"
    return f"{value * 100:.2f}%" if percent else f"{value:.3f}"


def _candidate_summary_table(report: ResearchOSV3FormalReport) -> list[str]:
    if not report.candidates:
        return ["本轮没有形成可展示的候选；具体数据或系统原因见上方研究组说明。"]
    rows = [
        "| 候选 | 研究组 | 验证 ICIR | 验证 RankIC | 成本后收益 | 换手 | 机器判断 | 主要原因 |",
        "| --- | --- | ---: | ---: | ---: | ---: | --- | --- |",
    ]
    for candidate in report.candidates:
        rows.append(
            "| "
            + " | ".join(
                (
                    candidate.candidate_id,
                    candidate.group_label,
                    _metric_table_value(candidate, "validation_icir"),
                    _metric_table_value(candidate, "validation_rank_ic"),
                    _metric_table_value(candidate, "validation_net_return", percent=True),
                    _metric_table_value(candidate, "validation_turnover", percent=True),
                    _selection_label(candidate.selection_verdict),
                    _reason_text(_candidate_reason_codes(candidate)),
                )
            )
            + " |"
        )
    return rows


def _key_observation_lines(report: ResearchOSV3FormalReport) -> list[str]:
    evaluated = [item for item in report.candidates if item.machine_evidence_status == "OFFICIAL_VISIBLE_EVALUATED"]
    if not evaluated:
        return []
    lines = ["### 最重要的机器观察（仅用于解释，不是晋级排名）"]
    with_icir = [(item, _metric_value(item, "validation_icir")) for item in evaluated]
    with_icir = [(item, value) for item, value in with_icir if value is not None]
    if with_icir:
        item, value = max(with_icir, key=lambda pair: pair[1])
        net = _metric_value(item, "validation_net_return")
        suffix = f"，但成本后收益为 {net * 100:.2f}%" if net is not None else ""
        lines.append(f"- 验证 ICIR 最高的是 {item.candidate_id}（{value:.3f}）{suffix}；单项领先不足以通过多指标审查。")
    with_net = [(item, _metric_value(item, "validation_net_return")) for item in evaluated]
    with_net = [(item, value) for item, value in with_net if value is not None]
    if with_net:
        item, value = max(with_net, key=lambda pair: pair[1])
        rank_ic = _metric_value(item, "validation_rank_ic")
        suffix = f"，RankIC 为 {rank_ic:.3f}" if rank_ic is not None else ""
        lines.append(f"- 成本后收益最高的是 {item.candidate_id}（{value * 100:.2f}%）{suffix}；收益为正不代表横截面预测稳定。")
    flipped = []
    for item in evaluated:
        train = _metric_value(item, "train_icir")
        validation = _metric_value(item, "validation_icir")
        if train is not None and validation is not None and train * validation < 0:
            flipped.append(item.candidate_id)
    if flipped:
        lines.append("- 训练期与验证期 ICIR 方向反转：" + "、".join(flipped) + "；这是稳定性不足或过拟合风险的直接信号。")
    lines.append("")
    return lines


def _group_diagnostic(group: ResearchGroupView) -> str:
    if group.missing_capabilities:
        return "缺少本组所需的冻结数据，模型未被要求猜测或替代。"
    if not group.typed_failures:
        return f"完成 {group.provider_calls} 次受限模型调用并保留可追溯产物。"
    labels = {
        "PROVIDER_TIMEOUT": "模型响应超时",
        "PROVIDER_SCHEMA_REJECTED": "模型输出未通过结构校验",
        "PROVIDER_BUDGET_EXCEEDED": "本组研究预算耗尽",
        "DATA_GAP": "冻结数据不足",
        "INCOMPLETE_RESULT": "仅形成部分研究结果",
    }
    return "；".join(labels.get(item, item) for item in group.typed_failures)


def _next_research_actions(report: ResearchOSV3FormalReport) -> tuple[str, ...]:
    reasons = {reason for candidate in report.candidates for reason in candidate.selection_reason_codes}
    actions: list[str] = []
    if "NET_ICIR_BELOW_POLICY" in reasons:
        actions.append("对信号方向、持有期和相邻窗口做预注册比较，避免只按样本内方向解释结果。")
    if "STABILITY_BELOW_POLICY" in reasons:
        actions.append("增加滚动子时期与市场状态条件测试，优先保留训练期和验证期方向一致的候选。")
    if "COST_CAPACITY_BELOW_POLICY" in reasons:
        actions.append("尝试信号平滑、降低调仓频率、流动性过滤和成交量约束，再重新计算成本后证据。")
    if "INCREMENTALITY_BELOW_POLICY" in reasons or "NOVELTY_BELOW_POLICY" in reasons:
        actions.append("对已登记因子做残差化或正交化，只保留确有增量的候选。")
    if any(group.missing_capabilities for group in report.groups):
        actions.append("若问题确实需要缺失数据，先冻结相应 PIT 数据；与问题无关的缺口不应阻断其他研究组。")
    if any("PROVIDER_TIMEOUT" in group.typed_failures for group in report.groups):
        actions.append("恢复超时研究组并复用已提交检查点；不得重复已经完成的机器试验。")
    if not actions:
        actions.append("保留本轮结论，并在新的冻结数据版本或明确的新机制下再启动研究。")
    return tuple(actions)


def _json_bytes(payload) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def _sha256(payload: bytes) -> str:
    import hashlib

    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _write_new_or_same(path: Path, payload: bytes) -> None:
    if path.exists():
        if path.read_bytes() != payload:
            fail(FailureCode.ARTIFACT_COLLISION, "report artifact already exists with different content")
        return
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _write_or_replace(path: Path, payload: bytes) -> None:
    if path.exists() and path.read_bytes() == payload:
        return
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _archive_superseded_publication(
    run_dir: Path,
    *,
    incoming_machine: bytes,
    incoming_markdown: bytes,
    referenced_report_hash: str | None,
) -> None:
    machine_path = run_dir / "machine_report.json"
    markdown_path = run_dir / "report.md"
    if not machine_path.exists() and not markdown_path.exists():
        return
    if (
        machine_path.exists()
        and markdown_path.exists()
        and machine_path.read_bytes() == incoming_machine
        and markdown_path.read_bytes() == incoming_markdown
    ):
        return
    old_machine = machine_path.read_bytes() if machine_path.exists() else b""
    archive_hash = referenced_report_hash
    receipt_path = run_dir / "receipt.json"
    if archive_hash is None and receipt_path.exists():
        try:
            receipt_payload = json.loads(receipt_path.read_text(encoding="utf-8"))
            receipt_hash = receipt_payload.get("report_hash") if isinstance(receipt_payload, dict) else None
            if isinstance(receipt_hash, str) and receipt_hash.startswith("sha256:"):
                archive_hash = receipt_hash
        except (UnicodeDecodeError, json.JSONDecodeError):
            archive_hash = None
    if archive_hash is None and old_machine:
        try:
            parsed = json.loads(old_machine.decode("utf-8"))
            candidate_hash = parsed.get("artifact_hash") if isinstance(parsed, dict) else None
            if isinstance(candidate_hash, str) and candidate_hash.startswith("sha256:"):
                archive_hash = candidate_hash
        except (UnicodeDecodeError, json.JSONDecodeError):
            archive_hash = None
    archive_id = (archive_hash or _sha256(old_machine)).split(":", 1)[1]
    archive_dir = run_dir / "history" / archive_id
    archive_dir.mkdir(parents=True, exist_ok=True)
    for name in ("machine_report.json", "report.md", "receipt.json"):
        source = run_dir / name
        if source.exists():
            _write_new_or_same(archive_dir / name, source.read_bytes())


__all__ = [
    "ResearchCandidateView",
    "ResearchMetricView",
    "ResearchGroupView",
    "ResearchOSV3FormalReport",
    "ResearchOSV3ReportBackend",
    "ResearchOSV3ReportReceipt",
    "ResearchReportOutcome",
    "render_chinese_report",
]
