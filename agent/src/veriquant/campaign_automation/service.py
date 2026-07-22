"""Automation service for one confirmed AlphaForge Campaign run."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Mapping

from ..campaign_data_binding import (
    CampaignDataBindingRequest,
    CampaignDataBindingResult,
    CampaignDataBindingService,
    CampaignDataBindingStatus,
    DataVendorClass as BindingDataVendorClass,
)
from ..campaign_llm_runtime import CampaignDeskRunPlan, CampaignLLMRuntime
from ..campaign_run_store import (
    CampaignRunEventType,
    CampaignRunRecord,
    CampaignRunStoreStatus,
    FileCampaignRunStore,
)
from ..campaign_stage_execution import (
    CampaignStageRequest as BoundaryStageRequest,
    CampaignStageStatus as BoundaryStageStatus,
    DeterministicStageFixtureExecutor,
    ProviderMode,
    StageExecutionPolicy,
)
from ..contracts import Component, ContractValidationError, FailureCode
from ..contracts.base import canonicalize
from ..campaign_real_llm import build_campaign_real_llm_stage_executor
from ..real_llm_provider import load_real_llm_from_env
from ..research_campaign import (
    BranchRuntimeStatus,
    CampaignBudget,
    CampaignCommittedStage,
    CampaignStageExecutionContext,
    CampaignStageResult,
    CampaignRuntimeStatus,
    CampaignState,
    DataAvailability,
    DeterministicCampaignRouter,
    DeterministicCampaignStageExecutor,
    DeterministicMethodProtocolCompiler,
    InteractionStyle,
    MethodModel,
    RegisteredResearchContext,
    ResearchCampaign,
    ResearchCampaignDraft,
    ResearchDepth,
    ResearchMode,
    ResearchSubject,
    StageRuntimeStatus,
    StopCondition,
    UserCheckpoint,
    stage_key_for,
)
from ..research_campaign.runtime import CampaignStageExecutor
from .models import (
    AutomationDataManifest,
    AutomationReport,
    AutomationRequest,
    AutomationResult,
    AutomationRuntimeMode,
    AutomationStatus,
    DataVendorClass,
    automation_status_from_runtime,
)


_ALL_DESKS = (
    "fundamental",
    "expectations_events",
    "market_structure",
    "alternative_data",
    "macro_regime",
    "meta_alpha",
)
_HASH_ZERO = "sha256:" + "0" * 64


class CampaignAutomationService:
    """Compile, confirm, route, run, and report one bounded Campaign."""

    def __init__(
        self,
        *,
        router: DeterministicCampaignRouter | None = None,
        protocol_compiler: DeterministicMethodProtocolCompiler | None = None,
        stage_executor: CampaignStageExecutor | None = None,
        data_binder: CampaignDataBindingService | None = None,
    ) -> None:
        self.router = router or DeterministicCampaignRouter()
        self.protocol_compiler = protocol_compiler or DeterministicMethodProtocolCompiler()
        self.stage_executor = stage_executor
        self.data_binder = data_binder or CampaignDataBindingService()

    def run(self, request: AutomationRequest) -> AutomationResult:
        if not isinstance(request, AutomationRequest):
            raise ContractValidationError(FailureCode.INVALID_VALUE, "request must be AutomationRequest")
        output_dir = _safe_output_dir(request)
        try:
            data_manifest = _build_data_manifest(request)
            campaign = _build_confirmed_campaign(request=request, data_manifest=data_manifest)
            data_binding = self._bind_campaign_data(
                request=request,
                campaign=campaign,
                data_manifest=data_manifest,
            )
            routing = self.router.route(
                campaign,
                data_availability=DataAvailability(
                    available_data=campaign.subject.allowed_data,
                    universe_ids=campaign.subject.universe,
                    data_manifest_hash=data_manifest.artifact_hash,
                    calendar_ids=(data_manifest.calendar_id,),
                ),
                registered_context=_registered_context_for(campaign),
            )
            if routing.state is CampaignState.BLOCKED or routing.route is None:
                return AutomationResult(
                    request_hash=request.artifact_hash,
                    status=AutomationStatus.BLOCKED,
                    run_id=request.run_id,
                    campaign_hash=campaign.artifact_hash,
                    route_hash=None,
                    protocol_plan_hash=None,
                    runtime_manifest_hash=None,
                    data_manifest_hash=data_manifest.artifact_hash,
                    report_hash=None,
                    output_dir=str(output_dir),
                    failure_code=routing.failure_code or FailureCode.UPSTREAM_BLOCKED,
                    message="Campaign routing blocked before Desk execution",
                )
            protocol_plan = self.protocol_compiler.compile(campaign=campaign, route=routing.route)
            run_store = FileCampaignRunStore(output_dir / "run_store")
            run_store.create_run(
                CampaignRunRecord(
                    run_id=request.run_id,
                    request_hash=request.artifact_hash,
                    campaign_hash=campaign.artifact_hash,
                    route_hash=routing.route.artifact_hash,
                    protocol_plan_hash=protocol_plan.artifact_hash,
                    status=CampaignRunStoreStatus.CREATED,
                    data_binding_hash=data_binding.bundle.artifact_hash if data_binding.bundle else data_manifest.artifact_hash,
                )
            )
            run_store.append_event(
                request.run_id,
                CampaignRunEventType.DATA_BOUND,
                {
                    "data_manifest_hash": data_manifest.artifact_hash,
                    "dataset_bundle_hash": data_binding.bundle.artifact_hash if data_binding.bundle else None,
                    "research_visible_summary_hash": (
                        data_binding.bundle.research_visible_summary_hash if data_binding.bundle else None
                    ),
                },
                event_time=request.decision_time,
            )
            stage_executor = self._stage_executor_for(
                request,
                campaign=campaign,
                route=routing.route,
                data_binding=data_binding,
            )
            runtime = CampaignLLMRuntime(
                executor=stage_executor,
                runtime_mode="REAL_LLM" if request.runtime_mode is AutomationRuntimeMode.REAL_LLM else "DETERMINISTIC_OFFLINE_WRAPPER",
                provider_available=request.provider_available,
            )
            plan = CampaignDeskRunPlan(
                run_id=request.run_id,
                campaign=campaign,
                route=routing.route,
                protocol_plan=protocol_plan,
                runtime_mode="REAL_LLM"
                if request.runtime_mode is AutomationRuntimeMode.REAL_LLM
                else "DETERMINISTIC_OFFLINE_WRAPPER",
                desk_context_hashes={
                    desk: data_binding.bundle.research_visible_summary_hash
                    if data_binding.bundle is not None
                    else data_manifest.artifact_hash
                    for desk in routing.route.selected_desks
                },
                desk_tool_whitelists={desk: ("read_campaign_brief",) for desk in routing.route.selected_desks},
                desk_budgets={
                    budget.desk_id: {
                        "max_llm_calls": budget.max_llm_calls,
                        "max_trials": budget.max_trials,
                    }
                    for budget in routing.route.desk_budgets
                },
            )
            runtime_manifest = runtime.run(plan)
            desk_outputs = _desk_outputs_from_executor(stage_executor)
            for branch in runtime_manifest.branch_results:
                for stage in branch.stage_results:
                    if stage.status in {
                        StageRuntimeStatus.BLOCKED,
                        StageRuntimeStatus.CANCELLED,
                        StageRuntimeStatus.SKIPPED,
                    }:
                        continue
                    run_store.commit_stage(
                        request.run_id,
                        CampaignCommittedStage(
                            stage_key=stage_key_for(stage.route_task_id, stage.stage_id),
                            result=stage,
                        ),
                    )
            run_store.commit_runtime_manifest(request.run_id, runtime_manifest)
            report = _build_report(
                request=request,
                campaign=campaign,
                data_manifest=data_manifest,
                data_binding=data_binding,
                route_hash=routing.route.artifact_hash,
                protocol_plan_hash=protocol_plan.artifact_hash,
                runtime_manifest=runtime_manifest,
            )
            json_path, markdown_path = _write_reports(
                output_dir=output_dir,
                request=request,
                data_manifest=data_manifest,
                data_binding=data_binding,
                campaign=campaign,
                route=routing.route,
                protocol_plan=protocol_plan,
                runtime_manifest=runtime_manifest,
                report=report,
                desk_outputs=desk_outputs,
            )
            run_store.append_event(
                request.run_id,
                CampaignRunEventType.REPORT_PUBLISHED,
                {
                    "report_hash": report.artifact_hash,
                    "report_json_hash": _file_hash(json_path),
                    "report_markdown_hash": _file_hash(markdown_path),
                },
                event_time=request.decision_time,
            )
            return AutomationResult(
                request_hash=request.artifact_hash,
                status=report.status,
                run_id=request.run_id,
                campaign_hash=campaign.artifact_hash,
                route_hash=routing.route.artifact_hash,
                protocol_plan_hash=protocol_plan.artifact_hash,
                runtime_manifest_hash=runtime_manifest.artifact_hash,
                data_manifest_hash=data_manifest.artifact_hash,
                report_hash=report.artifact_hash,
                output_dir=str(output_dir),
                json_report_path=str(json_path),
                markdown_report_path=str(markdown_path),
                message="Campaign automation completed",
            )
        except ContractValidationError as exc:
            return AutomationResult(
                request_hash=request.artifact_hash,
                status=AutomationStatus.FAILED,
                run_id=request.run_id,
                campaign_hash=None,
                route_hash=None,
                protocol_plan_hash=None,
                runtime_manifest_hash=None,
                data_manifest_hash=None,
                report_hash=None,
                output_dir=str(output_dir),
                failure_code=exc.code,
                message=f"Campaign automation failed closed: {exc.code.value}",
            )

    def _stage_executor_for(
        self,
        request: AutomationRequest,
        *,
        campaign: ResearchCampaign,
        route: Any,
        data_binding: CampaignDataBindingResult,
    ) -> CampaignStageExecutor:
        if request.runtime_mode is AutomationRuntimeMode.DETERMINISTIC_OFFLINE:
            return self.stage_executor or _BoundaryStageExecutorAdapter()
        if self.stage_executor is None and request.real_llm_from_env and request.provider_available:
            binding = load_real_llm_from_env(env_file=_default_env_file(), require_enabled=True)
            return build_campaign_real_llm_stage_executor(
                campaign=campaign,
                route=route,
                data_binding=data_binding,
                client=binding.client,
                provider_config=binding.config,
            )
        if (
            not request.provider_available
            or self.stage_executor is None
            or isinstance(self.stage_executor, DeterministicCampaignStageExecutor)
            or isinstance(self.stage_executor, _BoundaryStageExecutorAdapter)
        ):
            raise ContractValidationError(
                FailureCode.ADAPTER_UNAVAILABLE,
                "REAL_LLM automation requires an explicitly injected non-deterministic stage executor",
            )
        return self.stage_executor

    def _bind_campaign_data(
        self,
        *,
        request: AutomationRequest,
        campaign: ResearchCampaign,
        data_manifest: AutomationDataManifest,
    ) -> CampaignDataBindingResult:
        binding_request = CampaignDataBindingRequest(
            campaign=campaign,
            vendor_class=BindingDataVendorClass(request.data_vendor_class.value),
            provider_source_label=data_manifest.source_label,
            calendar_id=data_manifest.calendar_id,
            provider_available=True,
            content_refs={"automation_manifest": data_manifest.artifact_hash},
        )
        result = self.data_binder.bind(binding_request)
        if result.status is not CampaignDataBindingStatus.BOUND or result.bundle is None:
            raise ContractValidationError(
                result.failure_code or FailureCode.ADAPTER_UNAVAILABLE,
                "Campaign data binding did not produce a frozen dataset bundle",
            )
        return result


class _BoundaryStageExecutorAdapter:
    """Adapt the one-stage boundary package to the existing runtime protocol."""

    def __init__(self) -> None:
        self.executor = DeterministicStageFixtureExecutor(provider_available=False)
        self.policy = StageExecutionPolicy(
            provider_mode=ProviderMode.DETERMINISTIC,
            allow_real_provider=False,
            allow_deterministic_fallback=True,
        )

    def run_stage(self, context: CampaignStageExecutionContext) -> CampaignStageResult:
        request = BoundaryStageRequest(
            run_id=context.route.route_id,
            campaign_hash=context.campaign.artifact_hash,
            route_hash=context.route.artifact_hash,
            protocol_plan_hash=context.task_protocol.protocol_template_hash,
            campaign_state=context.campaign.state,
            route_state=context.route.state,
            selected_desks=context.route.selected_desks,
            route_task_id=context.route_task.task_id,
            desk_id=context.route_task.desk_id,
            stage_id=context.stage_id,
            stage_index=context.stage_index,
            stage_sequence=context.task_protocol.stage_ids,
            input_hash=_stable_hash(
                {
                    "campaign": context.campaign.artifact_hash,
                    "route_task": context.route_task.artifact_hash,
                    "stage": context.stage_id,
                }
            ),
            artifact_namespace=context.artifact_namespace,
        )
        boundary = self.executor.run_stage(request, self.policy)
        status = _runtime_status_from_boundary(boundary.status)
        return CampaignStageResult(
            route_task_id=context.route_task.task_id,
            desk_id=context.route_task.desk_id,
            stage_id=context.stage_id,
            stage_index=context.stage_index,
            status=status,
            produced_by=boundary.produced_by,
            output_hash=boundary.output_hash,
            prompt_hash=boundary.prompt_hash,
            trace_hash=boundary.trace_hash,
            artifact_namespace=context.artifact_namespace,
            failure_code=boundary.failure_detail_code,
            message=boundary.message,
        )


def _runtime_status_from_boundary(status: BoundaryStageStatus) -> StageRuntimeStatus:
    if status is BoundaryStageStatus.DRAFT:
        return StageRuntimeStatus.DRAFT
    if status is BoundaryStageStatus.EXPLANATION:
        return StageRuntimeStatus.EXPLANATION
    if status is BoundaryStageStatus.CONTROL_REFERENCE:
        return StageRuntimeStatus.CONTROL_REFERENCE
    if status is BoundaryStageStatus.CANCELLED:
        return StageRuntimeStatus.CANCELLED
    if status is BoundaryStageStatus.SKIPPED_UPSTREAM:
        return StageRuntimeStatus.SKIPPED
    return StageRuntimeStatus.BLOCKED


def build_automation_report_markdown(payload: Mapping[str, Any]) -> str:
    report = payload["report"]
    runtime = payload["runtime_manifest"]
    lines = [
        "# AlphaForge Campaign 研究报告",
        "",
        f"- 运行 ID：`{report['run_id']}`",
        f"- 状态：`{report['status']}`",
        f"- 研究模式：`{report['mode']}`",
        f"- 运行类型：`{report['runtime_mode']}`",
        f"- 本次选中 Desk：`{', '.join(report['selected_desks'])}`",
        f"- 已完成 Desk：`{', '.join(report['completed_desks']) or '无'}`",
        f"- 阻断 Desk：`{', '.join(report['blocked_desks']) or '无'}`",
        f"- 数据 manifest hash：`{report['data_manifest_hash']}`",
        f"- Runtime manifest hash：`{report['runtime_manifest_hash']}`",
        "",
        "## 自动流程",
        "",
        "用户问题 -> 已确认 Campaign -> Router 选择专业 Desk -> 固定方法协议 -> 脱敏报告。",
        "",
        "## Desk 阶段",
        "",
    ]
    for branch in runtime["branch_results"]:
        lines.append(f"### {branch['desk_id']} / {branch['status']}")
        for stage in branch["stage_results"]:
            lines.append(
                f"- `{stage['stage_id']}`: `{stage['status']}` by `{stage['produced_by']}` "
                f"hash `{stage.get('output_hash') or 'none'}`"
            )
        if branch.get("message"):
            lines.append(f"- 消息：{branch['message']}")
        lines.append("")
    lines.extend(
        [
            "## 权限边界",
            "",
            "- 模型和 Desk 输出只允许是 DRAFT 或脱敏解释。",
            "- 本自动化层不声明官方指标、晋级、风险审批、订单、成交或账本真值。",
            "- 封闭测试明文、密钥、真实券商路径和生成 Python 执行不属于本次运行。",
        ]
    )
    return "\n".join(lines)


def build_automation_report_markdown(payload: Mapping[str, Any]) -> str:
    """Build the public Markdown report from redacted payload material."""

    report = payload["report"]
    runtime = payload["runtime_manifest"]
    desk_outputs = payload.get("desk_outputs", {})
    lines = [
        "# AlphaForge Campaign 研究报告",
        "",
        f"- 运行 ID：`{report['run_id']}`",
        f"- 状态：`{report['status']}`",
        f"- 研究模式：`{report['mode']}`",
        f"- 运行类型：`{report['runtime_mode']}`",
        f"- 本次选中 Desk：`{', '.join(report['selected_desks'])}`",
        f"- 已完成 Desk：`{', '.join(report['completed_desks']) or '无'}`",
        f"- 阻断 Desk：`{', '.join(report['blocked_desks']) or '无'}`",
        f"- 数据 manifest hash：`{report['data_manifest_hash']}`",
        f"- Runtime manifest hash：`{report['runtime_manifest_hash']}`",
        "",
        "## 自动流程",
        "",
        "用户问题 -> 已确认 Campaign -> Router 选择专业 Desk -> 固定方法协议 -> 脱敏报告。",
        "",
        "## Desk 草案输出",
        "",
    ]
    if isinstance(desk_outputs, Mapping) and desk_outputs:
        for desk_id, output in sorted(desk_outputs.items()):
            if not isinstance(output, Mapping):
                continue
            lines.append(f"### {desk_id}")
            lines.append(f"- 状态：`{output.get('status', 'UNKNOWN')}`")
            hypothesis = output.get("hypothesis")
            factor = output.get("factor_draft")
            feedback = output.get("feedback")
            if isinstance(hypothesis, Mapping):
                lines.append(f"- 假设：{hypothesis.get('statement', '')}")
                lines.append(f"- 机制：{hypothesis.get('economic_mechanism', '')}")
                lines.append(f"- 预期方向：`{hypothesis.get('expected_sign', '')}`")
            if isinstance(factor, Mapping):
                variables = factor.get("variables", ())
                rendered_variables = ", ".join(variables) if isinstance(variables, list) else str(variables)
                lines.append(f"- 因子草案：`{factor.get('name', '')}`")
                lines.append(f"- 表达式：`{factor.get('expression', '')}`")
                lines.append(f"- 字段：`{rendered_variables}`")
            if isinstance(feedback, Mapping):
                lines.append(f"- 脱敏反馈：{feedback.get('explanation', '')}")
            if output.get("failure_code"):
                lines.append(f"- 失败代码：`{output.get('failure_code')}`")
            lines.append("")
    else:
        lines.append("本次运行没有可公开的 Desk 草案明文；请查看阶段哈希和 run_store。")
        lines.append("")
    lines.extend(["## Desk 阶段留痕", ""])
    for branch in runtime["branch_results"]:
        lines.append(f"### {branch['desk_id']} / {branch['status']}")
        for stage in branch["stage_results"]:
            lines.append(
                f"- `{stage['stage_id']}`: `{stage['status']}` by `{stage['produced_by']}` "
                f"hash `{stage.get('output_hash') or 'none'}`"
            )
        if branch.get("message"):
            lines.append(f"- 消息：{branch['message']}")
        lines.append("")
    lines.extend(
        [
            "## 权限边界",
            "",
            "- 模型和 Desk 输出只能是 DRAFT 或脱敏解释。",
            "- 本自动化层不声明 official metrics、Promotion、风险审批、订单、成交或账本真值。",
            "- 封闭测试明文、密钥、真实券商路径和任意代码执行不属于本次运行。",
        ]
    )
    return "\n".join(lines)


def _desk_outputs_from_executor(executor: CampaignStageExecutor) -> Mapping[str, Any]:
    results = getattr(executor, "desk_results", None)
    if not isinstance(results, Mapping):
        return {}
    outputs: dict[str, Any] = {}
    for desk_id, result in sorted(results.items()):
        result_desk_id = getattr(getattr(result, "desk_id", None), "value", None) or str(desk_id)
        output: dict[str, Any] = {
            "status": getattr(getattr(result, "status", None), "value", str(getattr(result, "status", ""))),
            "failure_code": getattr(result, "failure_code", None),
            "message": getattr(result, "message", None),
        }
        hypothesis = getattr(result, "hypothesis", None)
        if hypothesis is not None:
            output["hypothesis"] = {
                "statement": hypothesis.statement,
                "economic_mechanism": hypothesis.economic_mechanism,
                "expected_sign": hypothesis.expected_sign.value,
                "falsification_tests": hypothesis.falsification_tests,
                "artifact_hash": hypothesis.artifact_hash,
            }
        factor = getattr(result, "factor_draft", None)
        if factor is not None:
            output["factor_draft"] = {
                "name": factor.name,
                "description": factor.description,
                "expression": factor.expression,
                "variables": factor.variables,
                "lookback_bars": factor.lookback_bars,
                "neutralization": factor.neutralization,
                "artifact_hash": factor.artifact_hash,
            }
        feedback = getattr(result, "feedback", None)
        if feedback is not None:
            output["feedback"] = {
                "explanation": feedback.explanation,
                "artifact_hash": feedback.artifact_hash,
            }
        outputs[result_desk_id] = output
    return outputs


def _build_data_manifest(request: AutomationRequest) -> AutomationDataManifest:
    source_label = (
        "APPROVED_PROXY_DATA_VENDOR"
        if request.data_vendor_class is DataVendorClass.APPROVED_PROXY_DATA_VENDOR
        else "DETERMINISTIC_OFFLINE_FIXTURE"
    )
    return AutomationDataManifest(
        manifest_id=f"data.{request.run_id}",
        vendor_class=request.data_vendor_class,
        source_label=source_label,
        official_tushare_source=False,
        universe=request.universe,
        horizon=_effective_horizon(request),
        decision_time=request.decision_time,
        allowed_data=request.allowed_data,
        calendar_id="cn_ashare_sse_szse",
        content_refs={
            "request": request.artifact_hash,
            "frozen_policy": _stable_hash({"calendar": "cn_ashare_sse_szse", "seed": request.seed}),
        },
    )


def _build_confirmed_campaign(
    *,
    request: AutomationRequest,
    data_manifest: AutomationDataManifest,
) -> ResearchCampaign:
    mode = _detect_mode(request.question)
    depth = _depth_for(request.question)
    subject = ResearchSubject(
        subject_id=_stable_id("subject", request.artifact_hash, mode.value),
        description=_subject_description(mode, request.question),
        universe=request.universe,
        horizon=_effective_horizon(request),
        decision_time=request.decision_time,
        allowed_data=_allowed_data_for(mode, request.allowed_data),
        existing_factor_ids=_existing_factors_for(mode, request.question),
        portfolio_id=_portfolio_for(mode, request.question),
        tags=_tags_for(request.question),
    )
    methods, desks = _recommendation_for(mode)
    draft = ResearchCampaignDraft(
        campaign_id=_stable_id("campaign", request.artifact_hash, subject.artifact_hash, data_manifest.artifact_hash),
        question=request.question.strip(),
        mode=mode,
        research_depth=depth,
        interaction_style=InteractionStyle.BATCH,
        subject=subject,
        method_models=methods,
        candidate_agents=desks,
        budget=CampaignBudget(
            max_factor_families=request.max_factor_families,
            max_trials=request.max_trials,
            max_llm_calls=request.max_llm_calls,
            max_runtime_seconds=300,
        ),
        user_checkpoints=(
            UserCheckpoint(checkpoint_id="checkpoint.after_research_brief", stage="after_research_brief"),
            UserCheckpoint(checkpoint_id="checkpoint.after_candidate_shortlist", stage="after_candidate_shortlist"),
        ),
        stop_conditions=(
            StopCondition.BUDGET_EXHAUSTED,
            StopCondition.USER_STOPPED,
            StopCondition.VALIDATION_COMPLETED,
        ),
        data_manifest_hash=data_manifest.artifact_hash,
        policy_hash=_stable_hash({"policy": "campaign_automation", "version": "v1"}),
        code_hash=_stable_hash({"code": "campaign_automation", "version": "v1"}),
        created_at=request.decision_time,
        proposed_by=Component.LLM_RESEARCHER,
        assumptions=(
            "Campaign is operator-confirmed by automation request flag",
            "Desk stages may produce DRAFT/explanation only",
            "machine validation and approval remain outside campaign automation",
        ),
        state=CampaignState.NEEDS_CONFIRMATION,
    )
    return ResearchCampaign.confirm(draft, confirmed_at=request.decision_time)


def _build_report(
    *,
    request: AutomationRequest,
    campaign: ResearchCampaign,
    data_manifest: AutomationDataManifest,
    data_binding: CampaignDataBindingResult,
    route_hash: str,
    protocol_plan_hash: str,
    runtime_manifest: Any,
) -> AutomationReport:
    completed = tuple(
        branch.desk_id for branch in runtime_manifest.branch_results if branch.status is BranchRuntimeStatus.COMPLETED
    )
    blocked = tuple(
        branch.desk_id for branch in runtime_manifest.branch_results if branch.status is not BranchRuntimeStatus.COMPLETED
    )
    stage_count = sum(len(branch.stage_results) for branch in runtime_manifest.branch_results)
    status = automation_status_from_runtime(runtime_manifest.status)
    return AutomationReport(
        report_id=_stable_id("report", request.artifact_hash, runtime_manifest.artifact_hash),
        run_id=request.run_id,
        campaign_hash=campaign.artifact_hash,
        route_hash=route_hash,
        protocol_plan_hash=protocol_plan_hash,
        runtime_manifest_hash=runtime_manifest.artifact_hash,
        data_manifest_hash=data_manifest.artifact_hash,
        mode=campaign.mode,
        runtime_mode=request.runtime_mode,
        status=status,
        selected_desks=tuple(runtime_manifest.selected_desks),
        completed_desks=completed,
        blocked_desks=blocked,
        not_selected_desks=tuple(runtime_manifest.not_selected_desks),
        stage_count=stage_count,
        official_evidence_claimed=False,
        promotion_claimed=False,
        generated_at=request.decision_time,
        summary=(
            "Completed a Campaign-routed research automation run with selected Desk stages "
            "and no downstream authority claims."
        ),
    )


def _write_reports(
    *,
    output_dir: Path,
    request: AutomationRequest,
    data_manifest: AutomationDataManifest,
    data_binding: CampaignDataBindingResult,
    campaign: ResearchCampaign,
    route: Any,
    protocol_plan: Any,
    runtime_manifest: Any,
    report: AutomationReport,
    desk_outputs: Mapping[str, Any] | None = None,
) -> tuple[Path, Path]:
    payload = {
        "schema_version": "alphaforge.campaign-automation-report/v1",
        "request": request.to_dict(),
        "data_manifest": data_manifest.to_dict(),
        "data_binding": data_binding.to_dict(),
        "campaign": campaign.to_dict(),
        "route": route.to_dict(),
        "protocol_plan": protocol_plan.to_dict(),
        "runtime_manifest": runtime_manifest.to_dict(),
        "desk_outputs": dict(desk_outputs or {}),
        "report": report.to_dict(),
    }
    json_path = output_dir / "report.json"
    md_path = output_dir / "report.md"
    _atomic_json(json_path, payload)
    _atomic_text(md_path, build_automation_report_markdown(payload))
    _atomic_json(
        output_dir / "artifact_manifest.json",
        {
            "schema_version": "alphaforge.campaign-automation-artifacts/v1",
            "run_id": request.run_id,
            "report_json_hash": _file_hash(json_path),
            "report_markdown_hash": _file_hash(md_path),
            "automation_report_hash": report.artifact_hash,
            "data_manifest_hash": data_manifest.artifact_hash,
            "dataset_bundle_hash": data_binding.bundle.artifact_hash if data_binding.bundle else None,
        },
    )
    return json_path, md_path


def _safe_output_dir(request: AutomationRequest) -> Path:
    root = Path(request.output_root)
    if not root.is_absolute():
        root = (Path.cwd() / root).resolve()
    else:
        root = root.resolve()
    target = (root / request.run_id).resolve()
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise ContractValidationError(FailureCode.UNAUTHORIZED_CAPABILITY, "output path traversal rejected") from exc
    return target


def _default_env_file() -> Path:
    agent_env = Path(__file__).resolve().parents[3] / ".env"
    return agent_env


def _atomic_json(path: Path, value: Any) -> None:
    payload = json.dumps(
        canonicalize(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ) + "\n"
    _atomic_text(path, payload.rstrip("\n"))


def _atomic_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile("w", encoding="utf-8", newline="\n", delete=False, dir=str(path.parent))
    name = handle.name
    try:
        handle.write(text.rstrip() + "\n")
        handle.flush()
        os.fsync(handle.fileno())
        handle.close()
        os.replace(name, path)
    finally:
        if not handle.closed:
            handle.close()
        if os.path.exists(name):
            os.unlink(name)


def _file_hash(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _stable_hash(value: Any) -> str:
    rendered = json.dumps(canonicalize(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(rendered.encode("utf-8")).hexdigest()


def _stable_id(prefix: str, *parts: Any) -> str:
    return f"{prefix}.{_stable_hash(parts).removeprefix('sha256:')[:16]}"


def _detect_mode(text: str) -> ResearchMode:
    lowered = text.lower()
    if any(token in text for token in ("为什么", "失效", "回撤", "衰减", "拥挤", "诊断")):
        return ResearchMode.DIAGNOSE_FACTOR_PORTFOLIO
    if any(token in text for token in ("财报", "公告", "事件", "高波动期", "专题")):
        return ResearchMode.EVENT_MARKET_TOPIC
    if any(token in text for token in ("改进", "优化", "降低换手", "parent_factor_id")):
        return ResearchMode.IMPROVE_EXISTING_FACTOR
    if any(token in text for token in ("低相关", "组合缺口", "补充组合", "增量")):
        return ResearchMode.PORTFOLIO_GAP_SEARCH
    if any(token in text for token in ("能否预测", "是否预测", "验证", "我的想法")):
        return ResearchMode.VALIDATE_IDEA
    if "alpha" in lowered or any(token in text for token in ("挖", "找", "新因子", "动量", "反转", "价值")):
        return ResearchMode.NEW_ALPHA_EXPLORATION
    return ResearchMode.NEW_ALPHA_EXPLORATION


def _depth_for(text: str) -> ResearchDepth:
    if "快速" in text:
        return ResearchDepth.QUICK_EXPLORATION
    if "严格" in text or "上线前" in text:
        return ResearchDepth.STRICT_VALIDATION
    return ResearchDepth.STANDARD_RESEARCH


def _recommendation_for(mode: ResearchMode) -> tuple[tuple[MethodModel, ...], tuple[str, ...]]:
    if mode is ResearchMode.VALIDATE_IDEA:
        return (
            (MethodModel.HYPOTHESIS_DRIVEN, MethodModel.ADVERSARIAL_FALSIFICATION),
            ("fundamental", "market_structure"),
        )
    if mode is ResearchMode.PORTFOLIO_GAP_SEARCH:
        return (
            (MethodModel.RESIDUAL_INCREMENTAL, MethodModel.HYPOTHESIS_DRIVEN, MethodModel.ADVERSARIAL_FALSIFICATION),
            ("meta_alpha", "market_structure", "fundamental"),
        )
    if mode is ResearchMode.DIAGNOSE_FACTOR_PORTFOLIO:
        return (
            (MethodModel.DIAGNOSTIC_ATTRIBUTION, MethodModel.REGIME_CONDITIONING, MethodModel.ADVERSARIAL_FALSIFICATION),
            ("meta_alpha", "market_structure", "macro_regime"),
        )
    if mode is ResearchMode.EVENT_MARKET_TOPIC:
        return (
            (MethodModel.EVENT_STUDY, MethodModel.REGIME_CONDITIONING, MethodModel.HYPOTHESIS_DRIVEN),
            ("expectations_events", "macro_regime", "market_structure"),
        )
    if mode is ResearchMode.IMPROVE_EXISTING_FACTOR:
        return (
            (MethodModel.BOUNDED_EVOLUTION, MethodModel.DIAGNOSTIC_ATTRIBUTION, MethodModel.ADVERSARIAL_FALSIFICATION),
            ("market_structure", "meta_alpha"),
        )
    return (
        (MethodModel.DATA_ANOMALY, MethodModel.HYPOTHESIS_DRIVEN, MethodModel.ADVERSARIAL_FALSIFICATION),
        ("market_structure", "fundamental", "meta_alpha"),
    )


def _subject_description(mode: ResearchMode, text: str) -> str:
    if mode is ResearchMode.DIAGNOSE_FACTOR_PORTFOLIO:
        return f"Diagnose factor or portfolio behavior: {text}"
    if mode is ResearchMode.PORTFOLIO_GAP_SEARCH:
        return f"Search for incremental alpha around registered factor/portfolio gaps: {text}"
    return f"Research subject: {text}"


def _allowed_data_for(mode: ResearchMode, base: tuple[str, ...]) -> tuple[str, ...]:
    extras: tuple[str, ...] = ()
    if mode is ResearchMode.EVENT_MARKET_TOPIC:
        extras = ("events", "macro_regime")
    elif mode is ResearchMode.DIAGNOSE_FACTOR_PORTFOLIO:
        extras = ("macro_regime", "registered_factor_summary")
    elif mode is ResearchMode.PORTFOLIO_GAP_SEARCH:
        extras = ("registered_factor_summary",)
    return tuple(dict.fromkeys((*base, *extras)))


def _effective_horizon(request: AutomationRequest) -> str:
    match = re.search(r"(\d{1,3})\s*(天|日|d|D)", request.question)
    if match:
        return f"{int(match.group(1))}d"
    return _extract_horizon(request.question) or request.horizon


def _extract_horizon(text: str) -> str | None:
    match = re.search(r"(\d{1,3})\s*(日|天|d|D)", text)
    if not match:
        return None
    return f"{int(match.group(1))}d"


def _tags_for(text: str) -> tuple[str, ...]:
    tags: list[str] = []
    for token, tag in (
        ("动量", "momentum"),
        ("反转", "reversal"),
        ("价值", "value"),
        ("大动量", "large_momentum"),
        ("财报", "earnings"),
        ("高波动", "high_volatility"),
    ):
        if token in text:
            tags.append(tag)
    return tuple(dict.fromkeys(tags))


def _existing_factors_for(mode: ResearchMode, text: str) -> tuple[str, ...]:
    match = re.search(r"(?:parent_factor_id|factor_id)\s*[:=：]\s*([A-Za-z0-9_.:/-]+)", text)
    if match:
        return (match.group(1),)
    if mode is ResearchMode.PORTFOLIO_GAP_SEARCH:
        factors: list[str] = []
        if "动量" in text:
            factors.append("momentum")
        if "价值" in text:
            factors.append("value")
        return tuple(factors)
    return ()


def _portfolio_for(mode: ResearchMode, text: str) -> str | None:
    match = re.search(r"portfolio_id\s*[:=：]\s*([A-Za-z0-9_.:/-]+)", text)
    if match:
        return match.group(1)
    if mode is ResearchMode.PORTFOLIO_GAP_SEARCH and "组合" in text:
        return "registered.portfolio.scope"
    return None


def _registered_context_for(campaign: ResearchCampaign) -> RegisteredResearchContext:
    portfolio_ids = (campaign.subject.portfolio_id,) if campaign.subject.portfolio_id else ()
    factor_domains = {factor_id: "market_structure" for factor_id in campaign.subject.existing_factor_ids}
    return RegisteredResearchContext(
        factor_ids=campaign.subject.existing_factor_ids,
        portfolio_ids=portfolio_ids,
        factor_domains=factor_domains,
    )
