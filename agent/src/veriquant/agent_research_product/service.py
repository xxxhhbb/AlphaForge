"""Unique, synchronous orchestration service for Agent-generated research.

This service coordinates authority-separated components.  It does not calculate
financial metrics and never treats a source hash as executable source code.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from dataclasses import replace
from pathlib import Path
from typing import Any

from ..agent_research_sandbox.process_runner import canonical_hash
from ..agent_research_sandbox.report import BoundMetric, ChineseResearchReportComposer, ResearchConclusion
from ..agent_research_sandbox.review import ExperimentReviewGate
from ..research_agent_runtime import (
    ResearchAgentStatus, ResearchCancellationToken, ResearchDraftKind, ResearchSwarmSchedulerAdapter,
)
from ..research_toolbox import ContentAddressedHandleStore
from ..research_run_controller import ResearchRunIdentityStore, ResearchRunLifecycle
from .contracts import (
    DraftSourceRepository,
    ExperimentBindingProtocol,
    IsolatedResearchRunnerProtocol,
    ProductExperimentResult,
    ProductExperimentStatus,
    ProductFailureCode,
    ProductReportBuilderProtocol,
    ResearchResultBundle,
    ProductRunRequest,
    ProductRunResult,
)
from .review_gateway import (
    ResearchReviewAdvisorProtocol,
    ResearchReviewGateway,
    evidence_dict as _evidence_dict,
    receipt_dict as _receipt_dict,
)
from ..research_results import ResearchResultKind


class InMemoryDraftSourceRepository:
    """Test/development source repository that verifies every source hash."""

    def __init__(self, sources: dict[str, str] | None = None) -> None:
        self._sources = dict(sources or {})

    def get(self, content_hash: str) -> str | None:
        value = self._sources.get(content_hash)
        if value is None:
            return None
        return value if _source_hash(value) == content_hash else None


class ContentAddressedDraftSourceRepository:
    """Read adapter over the same store used by ``VibeAgentLoopAdapter``."""

    def __init__(self, store: ContentAddressedHandleStore) -> None:
        self.store = store

    def get(self, content_hash: str) -> str | None:
        try:
            return self.store.read_bytes(content_hash).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return None


class MinimalChineseReportBuilder:
    """Generate an honest terminal report without deriving or inventing metrics."""

    def build(
        self, *, request, swarm, experiments, failures, artifact_hashes,
        result_bundle, artifact_repository=None,
    ):
        from ..agent_research_sandbox.report import CustomerResearchReport

        productive = tuple(item for item in experiments if item.status is not ProductExperimentStatus.BLOCKED)
        court_outcomes = {item.court_outcome for item in experiments if item.court_outcome}
        research_verdicts = {item.research_verdict for item in experiments if item.research_verdict}
        routes = tuple(
            f"{_SPECIALIST_NAMES.get(task.specialist_id, task.specialist_id)}："
            "规划、只读调查、代码草案、机械审查、隔离运行"
            for task in request.tasks
        )
        executed = tuple(
            f"{item.experiment_id}：{item.status.value}"
            + (
                f"；机器结论 {item.court_outcome}"
                if item.court_outcome else (
                    f"；辅助评价 {item.research_verdict}"
                    if item.research_verdict else ""
                )
            )
            for item in experiments
        ) or ("未执行实验；具体阻断原因已记录。",)
        review = tuple(
            f"{item.experiment_id}："
            + (
                "、".join(item.court_reason_codes)
                if item.court_reason_codes else item.failure_code or "机械审查及身份绑定已记录"
            )
            for item in experiments
        ) or ("没有可供机械审查的源码内容。",)
        outcome_limitations = []
        if swarm is not None:
            for outcome in swarm.outcomes:
                outcome_limitations.extend(
                    f"{outcome.specialist_id} 数据缺口：{item.data_id}（{item.reason_code}）"
                    for item in outcome.data_gaps
                )
                outcome_limitations.extend(
                    f"{outcome.specialist_id} 使用代理变量：{item.proxy_name} 代替 {item.target_concept}"
                    for item in outcome.proxy_specs
                )
        limitations = tuple(failures) + tuple(outcome_limitations)
        if not limitations:
            limitations = ("未进入独立封闭测试；本报告只陈述可见验证 Evidence。",)
        if "REJECT" in court_outcomes:
            conclusion = ResearchConclusion.REJECTED
        elif "REVISE" in court_outcomes:
            conclusion = ResearchConclusion.REVISE
        elif "VALIDATED_RESEARCH" in court_outcomes:
            conclusion = ResearchConclusion.SUPPORTED
        elif (
            result_bundle.result_kind is ResearchResultKind.EVALUATED_RESEARCH_RESULT
            and "REJECTED" in research_verdicts
        ):
            conclusion = ResearchConclusion.REJECTED
        elif (
            result_bundle.result_kind is ResearchResultKind.EVALUATED_RESEARCH_RESULT
            and research_verdicts & {"SUPPORTED", "CONDITIONALLY_SUPPORTED"}
        ):
            conclusion = ResearchConclusion.SUPPORTED
        elif "DATA_UNAVAILABLE" in research_verdicts:
            conclusion = ResearchConclusion.DATA_GAP
        else:
            conclusion = {
            ResearchResultKind.NEGATIVE_RESEARCH_RESULT: ResearchConclusion.NEGATIVE_RESEARCH_RESULT,
            ResearchResultKind.DATA_OR_CAPABILITY_GAP_REPORT: ResearchConclusion.DATA_GAP,
            ResearchResultKind.PARTIAL_RESEARCH_RESULT: ResearchConclusion.PARTIAL_RESEARCH_RESULT,
            ResearchResultKind.SYSTEM_FAILED: ResearchConclusion.PARTIAL_RESEARCH_RESULT,
            }.get(result_bundle.result_kind, ResearchConclusion.INSUFFICIENT_EVIDENCE)
        next_step = {
            ResearchResultKind.NEGATIVE_RESEARCH_RESULT: "本轮候选未通过统一审查或机器评价；查看反对证据后设计下一轮候选。",
            ResearchResultKind.DATA_OR_CAPABILITY_GAP_REPORT: "补充报告中列明的字段、时间语义或已登记证据后恢复研究。",
            ResearchResultKind.PARTIAL_RESEARCH_RESULT: "保留已完成产物，从首个未完成阶段继续，不重复已提交试验。",
            ResearchResultKind.SYSTEM_FAILED: "修复报告中的基础设施失败码后从 canonical manifest 恢复。",
        }.get(result_bundle.result_kind, "查看机器 Evidence 与稳健性结果，再决定是否继续验证。")
        metrics = tuple(
            BoundMetric(
                name=_metric_name(name, item.experiment_id, multiple=len(experiments) > 1),
                display_value=value,
                evidence_hash=item.official_evidence_hash,
            )
            for item in experiments if item.official_evidence_hash
            for name, value in item.official_metrics
        ) + tuple(
            BoundMetric(
                name="辅助研究 · " + _metric_name(
                    name, item.experiment_id, multiple=len(experiments) > 1
                ),
                display_value=value,
                evidence_hash=item.research_evidence_hash,
            )
            for item in experiments if item.research_evidence_hash
            for name, value in item.research_metrics
        )
        if any(item.research_evidence_hash for item in experiments):
            limitations = limitations + (
                "标注为“辅助研究”的数字来自 Agent 生成的汇总表，已做确定性一致性检查，但不是独立官方指标。",
            )
        if any(
            "UNFROZEN_DYNAMIC_TOOL_INPUT" in item.review_feedback_codes
            for item in experiments
        ):
            limitations = limitations + (
                "动态调查结果尚未冻结进本次数据身份；相关实验和确定性指标仅作为辅助研究，不进入官方 Evidence Court。",
            )
        supporting = tuple(
            item.official_evidence_hash for item in productive
            if item.official_evidence_hash and (
                item.court_outcome == "VALIDATED_RESEARCH"
                or item.research_verdict in {"SUPPORTED", "CONDITIONALLY_SUPPORTED"}
            )
        ) + tuple(
            "辅助研究（不等同官方 Evidence）：" + item.research_evidence_hash
            for item in productive
            if item.research_evidence_hash
            and item.research_verdict in {"SUPPORTED", "CONDITIONALLY_SUPPORTED"}
        )
        opposing = tuple(
            item.official_evidence_hash for item in productive
            if item.official_evidence_hash and (
                item.court_outcome in {"REJECT", "REVISE"}
                or item.research_verdict == "REJECTED"
            )
        ) + tuple(
            "辅助研究反对证据：" + item.research_evidence_hash
            for item in productive
            if item.research_evidence_hash and item.research_verdict == "REJECTED"
        )
        data_receipts = sum(
            len(outcome.investigation_receipts) for outcome in (() if swarm is None else swarm.outcomes)
        )
        research_process = _research_process_narratives(
            swarm=swarm,
            experiments=experiments,
            artifact_repository=artifact_repository,
        )
        return CustomerResearchReport(
            question_id=request.question_id,
            question_hash=canonical_hash({"question": request.question}),
            question=request.question,
            conclusion=conclusion,
            specialists=tuple(
                _SPECIALIST_NAMES.get(task.specialist_id, task.specialist_id)
                for task in request.tasks
            ),
            research_routes=routes,
            data_description=(
                f"冻结数据标识：{request.manifest.data_snapshot_hash}",
                f"决策时间：{request.tasks[0].decision_time.isoformat()}",
                f"只读调查收据：{data_receipts} 份",
            ),
            experiments=executed,
            review_summary=review,
            metrics=metrics,
            robustness_checks=tuple(sorted({
                code for item in experiments for code in item.court_reason_codes
            })) or ("机械安全审查已完成；尚无官方稳健性或反证结论。",),
            supporting_evidence=supporting,
            opposing_evidence=opposing,
            limitations=limitations,
            next_steps=(next_step,),
            artifact_hashes=artifact_hashes or (request.manifest.artifact_hash,),
            research_process=research_process,
        )


_METRIC_NAMES = {
    "pearson_ic": "Pearson IC", "rank_ic": "RankIC", "ic_mean": "IC 均值",
    "ic_std": "IC 标准差", "icir": "ICIR", "quantile_spread": "分位数组合价差",
    "turnover": "换手率", "gross_return": "成本前收益", "net_return": "成本后收益",
    "max_drawdown": "最大回撤", "observation_count": "有效观测数", "period_count": "评价期数",
    "subperiod_min_ic": "最弱子时期 IC", "subperiod_positive_fraction": "子时期正向比例",
    "regime_min_ic": "最弱市场状态 IC", "regime_positive_fraction": "状态正向比例",
    "max_registered_abs_correlation": "与已登记因子最高相关性",
    "incremental_ic_mean": "增量 IC 均值", "spread_cost": "价差成本",
    "linear_impact_cost": "线性冲击成本",
}


_SPECIALIST_NAMES = {
    "alternative_data": "另类数据研究组",
    "expectations_events": "预期与事件研究组",
    "fundamental": "基本面研究组",
    "macro_regime": "宏观状态研究组",
    "market_structure": "市场行为与交易可行性研究组",
    "meta_alpha": "组合增量研究组",
}


def _metric_name(name: str, experiment_id: str, *, multiple: bool) -> str:
    label = _METRIC_NAMES.get(name, name)
    return f"{experiment_id} · {label}" if multiple else label


def _research_process_narratives(*, swarm, experiments, artifact_repository):
    from ..agent_research_sandbox.report import ResearchProcessNarrative

    if swarm is None:
        return ()
    narratives = []
    for outcome in swarm.outcomes:
        plan_hash = outcome.plan_hash
        plan = None
        if artifact_repository is not None and plan_hash:
            raw = artifact_repository.get(plan_hash)
            if raw:
                try:
                    payload = json.loads(raw)
                    plan = payload.get("plan") if isinstance(payload, dict) else None
                except (TypeError, ValueError):
                    plan = None
        if not isinstance(plan, str) or not plan.strip():
            plan = "本次运行只保留了计划身份，计划正文未随恢复产物迁移；报告不根据源码反向编造研究动机。"

        group_experiments = tuple(
            item for item in experiments if item.specialist_id == outcome.specialist_id
        )
        unique_receipts = {
            (receipt.tool_name, receipt.result_hash): receipt
            for receipt in outcome.investigation_receipts
        }
        investigations = tuple(
            f"调用只读工具 {receipt.tool_name}；返回结果已绑定为 {receipt.result_hash}。"
            for receipt in unique_receipts.values()
        ) or ("本组没有产生可验证的调查收据。",)
        attempts = tuple(
            f"提交实验 {item.experiment_id}：{item.status.value}"
            + (
                f"，评价为 {item.court_outcome or item.research_verdict}。"
                if item.court_outcome or item.research_verdict
                else "。"
            )
            for item in group_experiments
        ) or ("本组没有进入实验执行阶段。",)
        review_findings = tuple(
            f"{item.experiment_id}："
            + (
                "通过机械审查并完成代码、数据和运行身份绑定。"
                if not item.review_feedback_codes and not item.failure_code
                else "审查反馈 "
                + "、".join(item.review_feedback_codes or (item.failure_code or "UNKNOWN",))
            )
            for item in group_experiments
        ) or ("没有可审查的实验源码。",)
        evidence_summary = tuple(
            f"{item.experiment_id} 形成"
            + (
                "正式 Evidence。"
                if item.official_evidence_hash
                else "机器复核研究证据。"
                if item.research_evidence_hash
                else "负向或不完整结果。"
            )
            for item in group_experiments
        ) or ("本组没有形成标准证据表。",)
        hashes = tuple(sorted({
            value
            for value in (
                plan_hash,
                *(item.content_hash for item in outcome.draft_artifacts),
                *(item.review_hash for item in group_experiments),
                *(item.runner_receipt_hash for item in group_experiments),
                *(item.official_evidence_hash for item in group_experiments),
                *(item.research_evidence_hash for item in group_experiments),
            )
            if value
        }))
        narratives.append(ResearchProcessNarrative(
            specialist=_SPECIALIST_NAMES.get(outcome.specialist_id, outcome.specialist_id),
            status=outcome.status.value,
            plan=plan,
            investigations=investigations,
            attempts=attempts,
            review_findings=review_findings,
            evidence_summary=evidence_summary,
            artifact_hashes=hashes or (outcome.task_hash,),
        ))
    return tuple(narratives)


class AgentResearchProductService:
    """One manifest, one artifact root, and one trusted orchestration path."""

    def __init__(
        self, *, root: str | Path, scheduler: ResearchSwarmSchedulerAdapter,
        source_repository: DraftSourceRepository, review_gate: ExperimentReviewGate,
        runner: IsolatedResearchRunnerProtocol, binding: ExperimentBindingProtocol,
        evidence_evaluator: Any, report_composer: ChineseResearchReportComposer | None = None,
        report_builder: ProductReportBuilderProtocol | None = None,
        review_advisor: ResearchReviewAdvisorProtocol | None = None,
    ) -> None:
        self.root = Path(root).resolve()
        if scheduler.checkpoint_store.root != self.root:
            raise ValueError("scheduler checkpoint root must be the canonical product root")
        self.identity_store = ResearchRunIdentityStore(self.root)
        self.scheduler = scheduler
        self.sources = source_repository
        self.review_gate = review_gate
        self.runner = runner
        self.binding = binding
        self.evaluator = evidence_evaluator
        self.composer = report_composer or ChineseResearchReportComposer()
        self.report_builder = report_builder or MinimalChineseReportBuilder()
        self.review_gateway = ResearchReviewGateway(
            source_repository=source_repository,
            review_gate=review_gate,
            runner=runner,
            binding=binding,
            evidence_evaluator=evidence_evaluator,
            artifact_writer=self._write_artifact,
            review_advisor=review_advisor,
        )
        self._tokens: dict[str, ResearchCancellationToken] = {}
        self._guard = threading.RLock()

    def run(self, request: ProductRunRequest, *, resume: bool = False) -> ProductRunResult:
        existing_result = self._load_result(request.manifest.run_id)
        existing = self.identity_store.load_manifest(request.manifest.run_id)
        if existing_result is not None and existing is not None and existing.current_stage in _TERMINAL and not resume:
            return existing_result
        if existing is None:
            manifest = self.identity_store.ensure_manifest(request.manifest)
        elif resume and existing.current_stage is ResearchRunLifecycle.CANCELLED:
            manifest = self.identity_store.ensure_manifest(
                replace(existing, current_stage=ResearchRunLifecycle.PLANNING, report_hash=None),
                expected_artifact_hash=existing.artifact_hash,
                allow_cancelled_resume=True,
            )
        elif existing.identity_hash != request.manifest.identity_hash:
            # Let the canonical store emit the repository's collision type.
            manifest = self.identity_store.ensure_manifest(request.manifest)
        else:
            manifest = existing

        self._write_request(request)
        token = ResearchCancellationToken()
        with self._guard:
            self._tokens[manifest.run_id] = token
        failures: list[str] = []
        experiments: list[ProductExperimentResult] = []
        swarm = None
        artifact_hashes: list[str] = [manifest.artifact_hash]
        try:
            manifest = self._advance(manifest, ResearchRunLifecycle.RESEARCHING)
            swarm = self.scheduler.run(
                manifest=manifest, tasks=request.tasks, runtimes=request.runtimes,
                toolboxes=request.toolboxes, cancellation=token, resume=resume,
            )
            artifact_hashes.append(swarm.artifact_hash)
            for outcome in swarm.outcomes:
                if outcome.status not in {ResearchAgentStatus.COMPLETED, ResearchAgentStatus.PARTIAL}:
                    failures.append(f"{ProductFailureCode.BRANCH_FAILED.value}:{outcome.task_id}:{outcome.failure_code.value if outcome.failure_code else 'UNKNOWN'}")
                    continue
                for draft in outcome.draft_artifacts:
                    if draft.kind is not ResearchDraftKind.CODE:
                        continue
                    result = self.review_gateway.evaluate_draft(
                        manifest=manifest, outcome=outcome, source_hash=draft.content_hash
                    )
                    experiments.append(result)
                    artifact_hashes.extend(
                        value for value in (
                            result.review_hash, result.runner_receipt_hash,
                            result.evidence_result_hash, result.official_evidence_hash,
                        ) if value is not None
                    )
                    if result.failure_code:
                        failures.append(f"{result.failure_code}:{result.experiment_id}")
                    revised = self._revise_once(
                        request=request,
                        manifest=manifest,
                        outcome=outcome,
                        initial=result,
                        cancellation=token,
                    )
                    if revised is not None:
                        experiments.extend(revised)
                        for item in revised:
                            artifact_hashes.extend(
                                value for value in (
                                    item.review_hash, item.runner_receipt_hash,
                                    item.evidence_result_hash, item.official_evidence_hash,
                                ) if value is not None
                            )
                            if item.failure_code:
                                failures.append(f"{item.failure_code}:{item.experiment_id}")
            if token.cancelled:
                failures.append(ProductFailureCode.CANCELLED.value)
                lifecycle = ResearchRunLifecycle.CANCELLED
            elif experiments and any(item.status is not ProductExperimentStatus.BLOCKED for item in experiments):
                lifecycle = ResearchRunLifecycle.PARTIAL if any(
                    item.status in {ProductExperimentStatus.DATA_GAP, ProductExperimentStatus.PARTIAL}
                    for item in experiments
                ) or any(item.startswith(ProductFailureCode.BRANCH_FAILED.value) for item in failures) else ResearchRunLifecycle.COMPLETED
            elif failures and _contains_data_gap(failures):
                lifecycle = ResearchRunLifecycle.PARTIAL
            elif failures:
                lifecycle = ResearchRunLifecycle.BLOCKED
            else:
                # A completed bounded search with no submitted candidate is a
                # legitimate negative result, not missing infrastructure.
                lifecycle = ResearchRunLifecycle.COMPLETED
        except Exception:
            failures.append(ProductFailureCode.INFRASTRUCTURE_FAILED.value)
            lifecycle = ResearchRunLifecycle.FAILED

        branch_statuses = tuple(sorted(
            item.status.value for item in (() if swarm is None else swarm.outcomes)
        ))
        result_bundle = _build_result_bundle(
            run_id=manifest.run_id,
            experiments=tuple(experiments),
            failures=tuple(sorted(set(failures))),
            branch_statuses=branch_statuses,
        )
        bundle_hash = self._write_artifact(
            manifest.run_id, "research_result_bundle.json", result_bundle.to_dict(include_hash=False)
        )
        if bundle_hash != result_bundle.artifact_hash:
            raise ValueError("result bundle content identity changed during persistence")
        artifact_hashes.append(bundle_hash)
        evidence_hashes = sorted({item.official_evidence_hash for item in experiments if item.official_evidence_hash})
        evidence_index_hash = None
        if evidence_hashes:
            evidence_index_hash = self._write_artifact(manifest.run_id, "official_evidence_index.json", {"hashes": evidence_hashes})
            artifact_hashes.append(evidence_index_hash)
        report_arguments = {
            "request": request, "swarm": swarm, "experiments": tuple(experiments),
            "failures": tuple(sorted(set(failures))),
            "artifact_hashes": tuple(sorted(set(artifact_hashes))),
            "result_bundle": result_bundle,
            "artifact_repository": self.sources,
        }
        try:
            report = self.report_builder.build(**report_arguments)
            markdown = self.composer.render(report)
        except Exception:
            # A presentation provider is never allowed to erase the terminal
            # research record.  Fall back to the deterministic, metric-free
            # report and mark the run as an infrastructure failure.
            failures.append(ProductFailureCode.INFRASTRUCTURE_FAILED.value)
            lifecycle = ResearchRunLifecycle.FAILED
            report_arguments["failures"] = tuple(sorted(set(failures)))
            failed_bundle = _build_result_bundle(
                run_id=manifest.run_id,
                experiments=tuple(experiments),
                failures=tuple(sorted(set(failures))),
                branch_statuses=branch_statuses,
            )
            result_bundle = failed_bundle
            bundle_hash = self._write_artifact(
                manifest.run_id, "research_result_bundle.json", result_bundle.to_dict(include_hash=False)
            )
            report_arguments["result_bundle"] = failed_bundle
            report = MinimalChineseReportBuilder().build(**report_arguments)
            markdown = ChineseResearchReportComposer().render(report)
        self._write_report(manifest.run_id, report.to_dict(), markdown)
        final = replace(
            manifest,
            current_stage=lifecycle,
            candidate_count=max(manifest.candidate_count, len(experiments)),
            trial_count=max(manifest.trial_count, len(experiments)),
            remaining_candidate_slots=max(0, manifest.remaining_candidate_slots - len(experiments)),
            typed_failures=tuple(sorted(set(manifest.typed_failures) | {_safe_failure(item) for item in failures})),
            official_evidence_hash=evidence_index_hash,
            report_hash=report.artifact_hash,
        )
        final = self.identity_store.ensure_manifest(final, expected_artifact_hash=manifest.artifact_hash)
        result = ProductRunResult(
            run_id=final.run_id,
            manifest_hash=final.artifact_hash,
            lifecycle=final.current_stage.value,
            swarm_hash=None if swarm is None else swarm.artifact_hash,
            experiments=tuple(experiments),
            failures=tuple(sorted(set(failures))),
            report_hash=report.artifact_hash,
            report_path=str(self.identity_store.run_dir(final.run_id) / "report.md"),
            result_kind=result_bundle.result_kind,
            result_bundle_hash=result_bundle.artifact_hash,
        )
        self._write_result(result)
        with self._guard:
            self._tokens.pop(final.run_id, None)
        return result

    def _revise_once(self, *, request, manifest, outcome, initial, cancellation):
        """Return at most one mechanically prompted revision for one rejected draft.

        Only stable mechanical finding codes and the researcher's own prior
        source are returned. Evidence metrics, sealed values and promotion
        decisions never enter this feedback channel.
        """

        revisable_failure = initial.failure_code in {
            ProductFailureCode.REVIEW_REJECTED.value,
            ProductFailureCode.RUNNER_BLOCKED.value,
        }
        if not revisable_failure or not initial.review_hash or not initial.review_feedback_codes:
            return None
        runtime = request.runtimes.get(outcome.specialist_id)
        if runtime is None or getattr(runtime, "supports_review_revision", False) is not True:
            return None
        task = next((item for item in request.tasks if item.task_id == outcome.task_id), None)
        toolbox = request.toolboxes.get(outcome.task_id)
        prior_source = self.sources.get(initial.source_hash)
        if task is None or toolbox is None or prior_source is None:
            return None
        feedback = ",".join(initial.review_feedback_codes)
        if initial.failure_code == ProductFailureCode.RUNNER_BLOCKED.value:
            revision_instruction = (
                "The mechanically accepted program failed inside the isolated runner. "
                "Revise it once using only the typed, redacted runtime feedback. "
                "Check variable types and name shadowing; keep the question, output contract, "
                "data fields and data identity unchanged."
            )
        else:
            revision_instruction = (
                "Revise the prior source once to resolve only these mechanical findings; "
                "keep the research question and data identity unchanged."
            )
        revised_outcome = runtime.run(
            task=task,
            toolbox=toolbox,
            upstream_context={
                "prior_review_hash": initial.review_hash,
                "prior_source_hash": initial.source_hash,
                "prior_source": prior_source,
                "review_feedback_codes": feedback,
                "revision_instruction": revision_instruction,
            },
            cancellation=cancellation,
        )
        self._write_artifact(
            manifest.run_id,
            f"revisions/{outcome.task_id}/{initial.source_hash[-12:]}.json",
            revised_outcome.to_dict(),
        )
        if revised_outcome.status not in {ResearchAgentStatus.COMPLETED, ResearchAgentStatus.PARTIAL}:
            return ()
        results = []
        seen = {initial.source_hash}
        for draft in revised_outcome.draft_artifacts:
            if draft.kind is not ResearchDraftKind.CODE or draft.content_hash in seen:
                continue
            seen.add(draft.content_hash)
            results.append(self.review_gateway.evaluate_draft(
                manifest=manifest,
                outcome=revised_outcome,
                source_hash=draft.content_hash,
                prior_review_hash=initial.review_hash,
            ))
        return tuple(results)

    def cancel(self, run_id: str) -> bool:
        """Request cooperative cancellation; the active run writes its report."""
        with self._guard:
            token = self._tokens.get(run_id)
        if token is None:
            return False
        token.cancel()
        return True

    def _advance(self, manifest, lifecycle):
        proposed = replace(manifest, current_stage=lifecycle)
        return self.identity_store.ensure_manifest(proposed, expected_artifact_hash=manifest.artifact_hash)

    def _run_dir(self, run_id: str) -> Path:
        return self.identity_store.run_dir(run_id)

    def _write_request(self, request: ProductRunRequest) -> None:
        self._atomic_json(self._run_dir(request.manifest.run_id) / "product_request.json", {
            "run_id": request.manifest.run_id,
            "question_id": request.question_id,
            "question_hash": canonical_hash({"question": request.question}),
            "task_hashes": [task.artifact_hash for task in request.tasks],
        })

    def _write_report(self, run_id: str, payload: dict[str, Any], markdown: str) -> None:
        directory = self._run_dir(run_id)
        self._atomic_json(directory / "report.json", payload)
        _atomic_text(directory / "report.md", markdown)

    def _write_result(self, result: ProductRunResult) -> None:
        self._atomic_json(self._run_dir(result.run_id) / "product_result.json", result.to_dict())

    def _load_result(self, run_id: str) -> ProductRunResult | None:
        path = self._run_dir(run_id) / "product_result.json"
        if not path.exists():
            return None
        return ProductRunResult.from_dict(json.loads(path.read_text(encoding="utf-8")))

    def _write_artifact(self, run_id: str, relative: str, payload: dict[str, Any]) -> str:
        target = (self._run_dir(run_id) / relative).resolve()
        target.relative_to(self._run_dir(run_id))
        self._atomic_json(target, payload)
        return canonical_hash(payload)

    @staticmethod
    def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
        _atomic_text(path, json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n")


def _atomic_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _safe_failure(value: str) -> str:
    return value.split(":", 1)[0]


def _contains_data_gap(values) -> bool:
    return any(
        token in value
        for value in values
        for token in (
            "DATA_GAP", "DATA_UNAVAILABLE", "TOOL_UNAVAILABLE",
            "LABEL_MISSING", "CAPACITY_FIELD_INVALID",
        )
    )


def _build_result_bundle(*, run_id, experiments, failures, branch_statuses) -> ResearchResultBundle:
    statuses = {item.status for item in experiments}
    if ProductExperimentStatus.OFFICIAL_EVIDENCE in statuses:
        kind = ResearchResultKind.EVALUATED_RESEARCH_RESULT
    elif ProductExperimentStatus.AUXILIARY_ONLY in statuses or ProductExperimentStatus.PARTIAL in statuses:
        kind = ResearchResultKind.PARTIAL_RESEARCH_RESULT
    elif ProductExperimentStatus.DATA_GAP in statuses or (not experiments and _contains_data_gap(failures)):
        kind = ResearchResultKind.DATA_OR_CAPABILITY_GAP_REPORT
    elif statuses and statuses.issubset({ProductExperimentStatus.NEGATIVE}):
        kind = ResearchResultKind.NEGATIVE_RESEARCH_RESULT
    elif not experiments and not failures and all(
        value in {"COMPLETED", "PARTIAL"} for value in branch_statuses
    ):
        kind = ResearchResultKind.NEGATIVE_RESEARCH_RESULT
    elif ProductFailureCode.INFRASTRUCTURE_FAILED.value in failures:
        kind = ResearchResultKind.SYSTEM_FAILED
    else:
        kind = ResearchResultKind.PARTIAL_RESEARCH_RESULT
    return ResearchResultBundle(
        run_id=run_id,
        result_kind=kind,
        experiments=experiments,
        failure_codes=tuple(sorted(set(failures))),
        branch_statuses=tuple(sorted(branch_statuses)),
        official_evidence_hashes=tuple(sorted({
            item.official_evidence_hash for item in experiments if item.official_evidence_hash
        })),
    )


_TERMINAL = {
    ResearchRunLifecycle.COMPLETED, ResearchRunLifecycle.PARTIAL, ResearchRunLifecycle.BLOCKED,
    ResearchRunLifecycle.FAILED, ResearchRunLifecycle.CANCELLED,
}


__all__ = [
    "AgentResearchProductService", "ContentAddressedDraftSourceRepository",
    "InMemoryDraftSourceRepository", "MinimalChineseReportBuilder",
]
