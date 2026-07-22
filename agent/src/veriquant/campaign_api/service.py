"""Campaign API service facade for AlphaForge product flows."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any, Mapping

from ..contracts import Component, ContractValidationError, FailureCode
from ..contracts.base import canonicalize, fail, require_hash, require_identifier
from ..research_campaign import (
    CampaignBudget,
    CampaignClarificationQuestion,
    CampaignRoute,
    CampaignState,
    DataAvailability,
    DeterministicCampaignRouter,
    DeterministicMethodProtocolCompiler,
    InteractionStyle,
    MethodModel,
    RegisteredResearchContext,
    ResearchCampaign,
    ResearchCampaignDraft,
    ResearchDepth,
    ResearchMode,
    ResearchSubject,
    StopCondition,
    UserCheckpoint,
)
from .models import (
    CampaignApiFailureCode,
    CampaignApiResponse,
    CampaignApiStatus,
    CampaignCatalogView,
    CampaignConfirmationRequest,
    CampaignDraftRequest,
    CampaignRunIntent,
    labels_for,
)


CAMPAIGN_API_RULE_VERSION = "alphaforge.campaign-api-service/v1"
DEFAULT_CONFIRMED_AT = datetime(2026, 7, 19, 10, 0, tzinfo=timezone.utc)


_ALLOWED_DESKS = (
    "fundamental",
    "expectations_events",
    "market_structure",
    "alternative_data",
    "macro_regime",
    "meta_alpha",
)

_INJECTION_PATTERNS = (
    "ignore previous",
    "ignore all",
    "system prompt",
    "developer message",
    "tool_call",
    "function_call",
    "authorization",
    "api_key",
    "secret",
    "official_metrics",
    "promotion",
    "risk_approval",
    "target_weights",
    "orders",
    "broker",
    "ledger",
    "忽略规则",
    "忽略以上",
    "系统提示",
    "开发者消息",
    "调用工具",
    "修改权限",
    "官方指标",
    "晋级",
    "风险审批",
    "目标仓位",
    "直接下单",
    "真实下单",
)


class CampaignApiService:
    """Small product-layer facade around Campaign compiler, confirmation, and routing."""

    def __init__(
        self,
        *,
        router: DeterministicCampaignRouter | None = None,
        protocol_compiler: DeterministicMethodProtocolCompiler | None = None,
    ) -> None:
        self.router = router or DeterministicCampaignRouter()
        self.protocol_compiler = protocol_compiler or DeterministicMethodProtocolCompiler()

    def catalog(self) -> CampaignCatalogView:
        return CampaignCatalogView()

    def create_draft(self, request: CampaignDraftRequest) -> tuple[CampaignApiResponse, ResearchCampaignDraft | None]:
        if not isinstance(request, CampaignDraftRequest):
            fail(FailureCode.INVALID_VALUE, "request must be CampaignDraftRequest")
        sanitized, injection_removed = _sanitize_user_text(request.user_text)
        mode = _detect_mode(sanitized)
        depth = _depth_for(request, sanitized)
        interaction = _interaction_for(request, sanitized)
        methods, desks, explanation = _recommendation_for(mode)
        clarifications = _clarifications_for(mode=mode, text=sanitized, request=request)
        if _is_unsure_request(sanitized):
            questions = (
                "你希望优先解决：找新 Alpha、验证想法、改进因子、补组合缺口、诊断失效，还是事件专题？",
                "请确认研究对象，例如股票池、父因子、组合、事件类型或数据源。",
            )
            return (
                CampaignApiResponse(
                    request_hash=request.artifact_hash,
                    status=CampaignApiStatus.NEEDS_CLARIFICATION,
                    message="需要用户确认研究模式和研究对象后才能生成可确认 Campaign。",
                    display_labels=labels_for(mode=mode, depth=depth, interaction=interaction, methods=methods),
                    clarification_questions=questions,
                    prompt_injection_removed=injection_removed,
                    failure_code=CampaignApiFailureCode.MISSING_CONFIRMATION_INPUT,
                    can_execute=False,
                ),
                None,
            )

        draft = _build_draft(
            request=request,
            sanitized_text=sanitized,
            mode=mode,
            depth=depth,
            interaction=interaction,
            method_models=methods,
            candidate_agents=desks,
            clarifications=clarifications,
            explanation=explanation,
        )
        return (
            CampaignApiResponse(
                request_hash=request.artifact_hash,
                status=CampaignApiStatus.DRAFT_CREATED,
                message="已生成待确认 Campaign Draft；确认前不会启动 FactorDAG、PIT、Evidence 或 Promotion。",
                display_labels=labels_for(mode=mode, depth=depth, interaction=interaction, methods=methods),
                draft_hash=draft.artifact_hash,
                payload_hashes={"draft": draft.artifact_hash},
                clarification_questions=tuple(item.question for item in clarifications),
                prompt_injection_removed=injection_removed,
                can_execute=False,
            ),
            draft,
        )

    def confirm(
        self,
        request: CampaignConfirmationRequest,
    ) -> tuple[CampaignApiResponse, ResearchCampaign]:
        if not isinstance(request, CampaignConfirmationRequest):
            fail(FailureCode.INVALID_VALUE, "request must be CampaignConfirmationRequest")
        draft = request.draft
        if request.answers:
            draft = draft.resolve_clarifications(request.answers, resolved_by=request.confirmed_by)
        _validate_draft_can_confirm(draft)
        campaign = ResearchCampaign.confirm(
            draft.ready_for_confirmation() if draft.state is CampaignState.DRAFT else draft,
            confirmed_at=request.confirmed_at,
            confirmed_by=request.confirmed_by,
        )
        return (
            CampaignApiResponse(
                request_hash=request.artifact_hash,
                status=CampaignApiStatus.CONFIRMED,
                message="Campaign 已确认；后续只能按预算、检查点和停止条件路由执行。",
                display_labels=labels_for(
                    mode=campaign.mode,
                    depth=campaign.research_depth,
                    interaction=campaign.interaction_style,
                    methods=campaign.method_models,
                ),
                draft_hash=campaign.draft_hash,
                campaign_hash=campaign.artifact_hash,
                payload_hashes={"campaign": campaign.artifact_hash, "draft": campaign.draft_hash},
                can_execute=True,
            ),
            campaign,
        )

    def plan_run(
        self,
        campaign: ResearchCampaign,
        *,
        registered_context: RegisteredResearchContext | None = None,
        data_availability: DataAvailability | None = None,
    ) -> tuple[CampaignApiResponse, CampaignRunIntent | None, CampaignRoute | None]:
        if not isinstance(campaign, ResearchCampaign) or campaign.state is not CampaignState.CONFIRMED:
            raise ContractValidationError(
                FailureCode.INVALID_STATE,
                "only a confirmed Campaign can produce a run intent",
                details={"campaign_api_failure_code": CampaignApiFailureCode.UNCONFIRMED_CAMPAIGN.value},
            )
        availability = data_availability or _default_data_availability(campaign)
        context = registered_context or _default_registered_context(campaign)
        routing = self.router.route(
            campaign,
            data_availability=availability,
            registered_context=context,
        )
        if routing.state is CampaignState.BLOCKED:
            return (
                CampaignApiResponse(
                    request_hash=campaign.artifact_hash,
                    status=CampaignApiStatus.BLOCKED,
                    message="Campaign 路由被阻断；不会静默替换数据或启动未授权 Desk。",
                    display_labels=labels_for(
                        mode=campaign.mode,
                        depth=campaign.research_depth,
                        interaction=campaign.interaction_style,
                        methods=campaign.method_models,
                    ),
                    failure_code=CampaignApiFailureCode.ROUTE_BLOCKED,
                    upstream_failure_code=routing.failure_code,
                    can_execute=False,
                ),
                None,
                None,
            )
        route = routing.route
        if route is None:
            fail(FailureCode.INVALID_STATE, "routed result must include a route")
        protocol_plan = self.protocol_compiler.compile(campaign=campaign, route=route)
        task_protocols = tuple(protocol_plan.task_protocols)
        allows_official = campaign.research_depth is not ResearchDepth.QUICK_EXPLORATION and any(
            item.requires_official_evidence for item in task_protocols
        )
        intent = CampaignRunIntent(
            campaign_hash=campaign.artifact_hash,
            route_hash=route.artifact_hash,
            protocol_plan_hash=protocol_plan.artifact_hash,
            selected_desks=route.selected_desks,
            method_models=route.method_models,
            protocol_tasks=tuple(_protocol_label(item) for item in task_protocols),
            allows_official_evidence=allows_official,
            allows_promotion=False,
            requires_checkpoint=bool(route.checkpoint_plan),
        )
        return (
            CampaignApiResponse(
                request_hash=campaign.artifact_hash,
                status=CampaignApiStatus.ROUTED,
                message="已生成 Campaign run intent；只允许 Integration Agent 接入实际运行入口。",
                display_labels=labels_for(
                    mode=campaign.mode,
                    depth=campaign.research_depth,
                    interaction=campaign.interaction_style,
                    methods=route.method_models,
                ),
                campaign_hash=campaign.artifact_hash,
                route_hash=route.artifact_hash,
                run_intent_hash=intent.artifact_hash,
                payload_hashes={
                    "route": route.artifact_hash,
                    "protocol_plan": protocol_plan.artifact_hash,
                    "run_intent": intent.artifact_hash,
                },
                can_execute=True,
            ),
            intent,
            route,
        )


def _build_draft(
    *,
    request: CampaignDraftRequest,
    sanitized_text: str,
    mode: ResearchMode,
    depth: ResearchDepth,
    interaction: InteractionStyle,
    method_models: tuple[MethodModel, ...],
    candidate_agents: tuple[str, ...],
    clarifications: tuple[CampaignClarificationQuestion, ...],
    explanation: str,
) -> ResearchCampaignDraft:
    subject = ResearchSubject(
        subject_id=_stable_id("subject", request.artifact_hash, mode.value, sanitized_text),
        description=_subject_description(mode, sanitized_text),
        universe=request.universe,
        horizon=_extract_horizon(sanitized_text) or request.horizon,
        decision_time=request.decision_time,
        allowed_data=_allowed_data_for(mode, request.allowed_data),
        existing_factor_ids=_existing_factor_ids_for(mode=mode, text=sanitized_text, request=request),
        portfolio_id=request.portfolio_id,
        tags=_subject_tags(sanitized_text),
    )
    assumptions = (
        "compiler output is DRAFT and requires operator confirmation",
        "no FactorDAG, PIT, Evidence, Promotion, Portfolio, Risk, Execution, or Ledger action is started",
        f"recommendation:{explanation}",
        *tuple(f"clarification_required:{item.field_name or item.question_id}" for item in clarifications if item.required),
    )
    return ResearchCampaignDraft(
        campaign_id=_stable_id("campaign", request.artifact_hash, subject.artifact_hash, mode.value),
        question=sanitized_text,
        mode=mode,
        research_depth=depth,
        interaction_style=interaction,
        subject=subject,
        method_models=method_models,
        candidate_agents=candidate_agents,
        budget=_budget_for(depth),
        user_checkpoints=_checkpoints_for(depth, interaction),
        stop_conditions=_stop_conditions_for(mode),
        data_manifest_hash=request.data_manifest_hash,
        policy_hash=request.policy_hash,
        code_hash=request.code_hash,
        created_at=request.decision_time,
        proposed_by=Component.LLM_RESEARCHER,
        assumptions=assumptions,
        state=CampaignState.NEEDS_CONFIRMATION,
        clarification_questions=clarifications,
        unresolved_fields=tuple(
            sorted(item.field_name or item.question_id for item in clarifications if item.required)
        ),
    )


def _validate_draft_can_confirm(draft: ResearchCampaignDraft) -> None:
    if draft.state is not CampaignState.NEEDS_CONFIRMATION:
        raise ContractValidationError(
            FailureCode.INVALID_STATE,
            "draft must be NEEDS_CONFIRMATION before confirmation",
            details={"campaign_api_failure_code": CampaignApiFailureCode.MISSING_CONFIRMATION_INPUT.value},
        )
    missing = []
    if not draft.subject.universe:
        missing.append("universe")
    if not draft.subject.horizon:
        missing.append("horizon")
    if draft.budget.max_trials < 1 or draft.budget.max_factor_families < 1:
        missing.append("budget")
    if not draft.stop_conditions:
        missing.append("stop_conditions")
    if missing or draft.unresolved_fields:
        raise ContractValidationError(
            FailureCode.MISSING_REQUIRED_FIELD,
            "Campaign confirmation requires frozen universe, horizon, budget, stop conditions, and resolved clarifications",
            details={
                "missing": tuple(missing),
                "clarifications": draft.unresolved_fields,
                "campaign_api_failure_code": CampaignApiFailureCode.MISSING_CONFIRMATION_INPUT.value,
            },
        )


def _sanitize_user_text(text: str) -> tuple[str, bool]:
    cleaned_lines: list[str] = []
    removed = False
    for raw_line in text.replace("\r\n", "\n").split("\n"):
        line = raw_line.strip()
        if not line:
            continue
        lowered = line.lower()
        if any(pattern in lowered or pattern in line for pattern in _INJECTION_PATTERNS):
            removed = True
            continue
        cleaned_lines.append(line)
    cleaned = " ".join(cleaned_lines).strip()
    if not cleaned:
        cleaned = "我不确定，帮我选择"
    return cleaned[:1000], removed


def _is_unsure_request(text: str) -> bool:
    return any(token in text for token in ("我不确定", "帮我选择", "不知怎么选", "不确定选"))


def _detect_mode(text: str) -> ResearchMode:
    lowered = text.lower()
    if _is_unsure_request(text):
        return ResearchMode.NEW_ALPHA_EXPLORATION
    if any(token in text for token in ("为什么", "失效", "回撤", "衰减", "拥挤", "诊断")):
        return ResearchMode.DIAGNOSE_FACTOR_PORTFOLIO
    if any(token in text for token in ("财报后漂移", "财报", "公告", "事件窗口", "高波动期", "专题")):
        return ResearchMode.EVENT_MARKET_TOPIC
    if any(token in text for token in ("降低换手", "改进", "优化", "父因子", "parent_factor_id")):
        return ResearchMode.IMPROVE_EXISTING_FACTOR
    if any(token in text for token in ("低相关", "组合缺口", "补充组合", "增量 Alpha", "增量alpha")):
        return ResearchMode.PORTFOLIO_GAP_SEARCH
    if any(token in text for token in ("能否预测", "是否预测", "能不能预测", "验证", "我的想法")):
        return ResearchMode.VALIDATE_IDEA
    if "alpha" in lowered or any(token in text for token in ("找新的", "新因子", "找新", "新的20日")):
        return ResearchMode.NEW_ALPHA_EXPLORATION
    return ResearchMode.NEW_ALPHA_EXPLORATION


def _depth_for(request: CampaignDraftRequest, text: str) -> ResearchDepth:
    if request.preferred_depth is not None:
        return request.preferred_depth
    if "严格" in text or "上线前" in text:
        return ResearchDepth.STRICT_VALIDATION
    if "快速" in text:
        return ResearchDepth.QUICK_EXPLORATION
    return ResearchDepth.STANDARD_RESEARCH


def _interaction_for(request: CampaignDraftRequest, text: str) -> InteractionStyle:
    if request.interaction_style is not None:
        return request.interaction_style
    if any(token in text for token in ("一步步", "引导", "帮我选择")):
        return InteractionStyle.GUIDED
    if "批处理" in text or "批量" in text:
        return InteractionStyle.BATCH
    return InteractionStyle.STANDARD


def _recommendation_for(mode: ResearchMode) -> tuple[tuple[MethodModel, ...], tuple[str, ...], str]:
    mapping: Mapping[ResearchMode, tuple[tuple[MethodModel, ...], tuple[str, ...], str]] = {
        ResearchMode.NEW_ALPHA_EXPLORATION: (
            (MethodModel.DATA_ANOMALY, MethodModel.HYPOTHESIS_DRIVEN, MethodModel.ADVERSARIAL_FALSIFICATION),
            ("market_structure", "fundamental", "meta_alpha"),
            "新 Alpha 探索",
        ),
        ResearchMode.VALIDATE_IDEA: (
            (MethodModel.HYPOTHESIS_DRIVEN, MethodModel.ADVERSARIAL_FALSIFICATION),
            ("fundamental", "market_structure"),
            "验证用户想法",
        ),
        ResearchMode.IMPROVE_EXISTING_FACTOR: (
            (MethodModel.BOUNDED_EVOLUTION, MethodModel.DIAGNOSTIC_ATTRIBUTION, MethodModel.ADVERSARIAL_FALSIFICATION),
            ("market_structure", "meta_alpha"),
            "改进现有因子",
        ),
        ResearchMode.PORTFOLIO_GAP_SEARCH: (
            (MethodModel.RESIDUAL_INCREMENTAL, MethodModel.HYPOTHESIS_DRIVEN, MethodModel.ADVERSARIAL_FALSIFICATION),
            ("meta_alpha", "market_structure", "fundamental"),
            "补充组合缺口",
        ),
        ResearchMode.DIAGNOSE_FACTOR_PORTFOLIO: (
            (MethodModel.DIAGNOSTIC_ATTRIBUTION, MethodModel.REGIME_CONDITIONING, MethodModel.ADVERSARIAL_FALSIFICATION),
            ("meta_alpha", "market_structure", "macro_regime"),
            "诊断因子或组合",
        ),
        ResearchMode.EVENT_MARKET_TOPIC: (
            (MethodModel.EVENT_STUDY, MethodModel.REGIME_CONDITIONING, MethodModel.HYPOTHESIS_DRIVEN),
            ("expectations_events", "macro_regime", "market_structure"),
            "事件与市场专题",
        ),
    }
    return mapping.get(
        mode,
        (
            (MethodModel.HYPOTHESIS_DRIVEN, MethodModel.ADVERSARIAL_FALSIFICATION),
            ("market_structure", "fundamental"),
            mode.value,
        ),
    )


def _clarifications_for(
    *,
    mode: ResearchMode,
    text: str,
    request: CampaignDraftRequest,
) -> tuple[CampaignClarificationQuestion, ...]:
    questions: list[CampaignClarificationQuestion] = []
    if mode is ResearchMode.IMPROVE_EXISTING_FACTOR and not _has_parent_factor(text, request):
        questions.append(
            CampaignClarificationQuestion(
                question_id="clarify.parent_factor_id",
                field_name="parent_factor_id",
                question="请提供要改进的 parent_factor_id 或已登记父因子版本。",
            )
        )
    if mode is ResearchMode.PORTFOLIO_GAP_SEARCH and not (
        request.portfolio_id or request.existing_factor_ids or _named_factor_tags(text)
    ):
        questions.append(
            CampaignClarificationQuestion(
                question_id="clarify.portfolio_or_factor_scope",
                field_name="portfolio_or_existing_factor_ids",
                question="请提供 portfolio_id，或现有动量、价值等因子的具体 factor_id 范围。",
            )
        )
    if mode is ResearchMode.DIAGNOSE_FACTOR_PORTFOLIO and not (
        request.portfolio_id or request.existing_factor_ids or _explicit_factor_or_portfolio_id(text)
    ):
        questions.append(
            CampaignClarificationQuestion(
                question_id="clarify.diagnostic_scope",
                field_name="portfolio_or_existing_factor_ids",
                question="请提供要诊断的已登记 factor_id 或 portfolio_id。",
            )
        )
    return tuple(questions[:3])


def _explicit_factor_or_portfolio_id(text: str) -> bool:
    return re.search(r"(?:factor_id|portfolio_id)\s*[:=：]\s*[A-Za-z0-9_.:/-]+", text) is not None


def _has_parent_factor(text: str, request: CampaignDraftRequest) -> bool:
    if request.existing_factor_ids:
        return True
    return re.search(r"(parent_factor_id|父因子|factor_id)\s*[:=：]\s*[A-Za-z0-9_.:/-]+", text) is not None


def _existing_factor_ids_for(
    *,
    mode: ResearchMode,
    text: str,
    request: CampaignDraftRequest,
) -> tuple[str, ...]:
    if request.existing_factor_ids:
        return request.existing_factor_ids
    match = re.search(r"(?:parent_factor_id|factor_id)\s*[:=：]\s*([A-Za-z0-9_.:/-]+)", text)
    if match:
        return (match.group(1),)
    if mode is ResearchMode.PORTFOLIO_GAP_SEARCH:
        return _named_factor_tags(text)
    return ()


def _named_factor_tags(text: str) -> tuple[str, ...]:
    tags: list[str] = []
    for token, tag in (("动量", "momentum"), ("价值", "value"), ("反转", "reversal"), ("质量", "quality")):
        if token in text:
            tags.append(tag)
    return tuple(dict.fromkeys(tags))


def _subject_tags(text: str) -> tuple[str, ...]:
    tags = list(_named_factor_tags(text))
    for token, tag in (("财报", "earnings"), ("盈利上调", "earnings_revision"), ("高波动", "high_volatility")):
        if token in text:
            tags.append(tag)
    return tuple(dict.fromkeys(tags))


def _subject_description(mode: ResearchMode, text: str) -> str:
    if mode is ResearchMode.DIAGNOSE_FACTOR_PORTFOLIO:
        return f"诊断研究对象：{text}"
    if mode is ResearchMode.EVENT_MARKET_TOPIC:
        return f"事件与市场专题：{text}"
    if mode is ResearchMode.PORTFOLIO_GAP_SEARCH:
        return f"组合缺口或已有因子增量研究：{text}"
    return f"研究对象：{text}"


def _allowed_data_for(mode: ResearchMode, base: tuple[str, ...]) -> tuple[str, ...]:
    extras: tuple[str, ...] = ()
    if mode is ResearchMode.EVENT_MARKET_TOPIC:
        extras = ("events", "macro_regime")
    elif mode is ResearchMode.DIAGNOSE_FACTOR_PORTFOLIO:
        extras = ("registered_factor_summary", "macro_regime")
    elif mode is ResearchMode.PORTFOLIO_GAP_SEARCH:
        extras = ("registered_factor_summary",)
    return tuple(dict.fromkeys((*base, *extras)))


def _budget_for(depth: ResearchDepth) -> CampaignBudget:
    if depth is ResearchDepth.QUICK_EXPLORATION:
        return CampaignBudget(max_factor_families=1, max_trials=1, max_llm_calls=4, max_runtime_seconds=60)
    if depth is ResearchDepth.STRICT_VALIDATION:
        return CampaignBudget(max_factor_families=4, max_trials=24, max_llm_calls=24, max_runtime_seconds=600)
    return CampaignBudget(max_factor_families=3, max_trials=12, max_llm_calls=12, max_runtime_seconds=300)


def _checkpoints_for(depth: ResearchDepth, style: InteractionStyle) -> tuple[UserCheckpoint, ...]:
    checkpoints = [
        UserCheckpoint(checkpoint_id="checkpoint.after_research_brief", stage="after_research_brief", description="确认 Research Brief")
    ]
    if depth is not ResearchDepth.QUICK_EXPLORATION:
        checkpoints.append(
            UserCheckpoint(checkpoint_id="checkpoint.after_candidate_shortlist", stage="after_candidate_shortlist", description="确认候选短名单")
        )
    if depth is ResearchDepth.STRICT_VALIDATION:
        checkpoints.extend(
            [
                UserCheckpoint(checkpoint_id="checkpoint.before_sealed_validation", stage="before_sealed_validation", description="确认封闭测试前置条件"),
                UserCheckpoint(checkpoint_id="checkpoint.before_promotion", stage="before_promotion", description="确认晋级审查"),
            ]
        )
    if style is InteractionStyle.GUIDED:
        checkpoints.append(
            UserCheckpoint(checkpoint_id="checkpoint.before_any_evaluation", stage="before_any_evaluation", description="引导式运行前确认")
        )
    return tuple(checkpoints)


def _stop_conditions_for(mode: ResearchMode) -> tuple[StopCondition, ...]:
    conditions = [StopCondition.BUDGET_EXHAUSTED, StopCondition.USER_STOPPED]
    if mode is ResearchMode.PORTFOLIO_GAP_SEARCH:
        conditions.append(StopCondition.NO_INCREMENTAL_CANDIDATE)
    conditions.append(StopCondition.VALIDATION_COMPLETED)
    return tuple(conditions)


def _extract_horizon(text: str) -> str | None:
    match = re.search(r"(\d{1,3})\s*(日|天|d|D)", text)
    if not match:
        return None
    return f"{int(match.group(1))}d"


def _default_data_availability(campaign: ResearchCampaign) -> DataAvailability:
    return DataAvailability(
        available_data=campaign.subject.allowed_data,
        universe_ids=campaign.subject.universe,
        data_manifest_hash=campaign.data_manifest_hash,
        calendar_ids=("cn_ashare_sse_szse",),
    )


def _default_registered_context(campaign: ResearchCampaign) -> RegisteredResearchContext:
    portfolio_ids = (campaign.subject.portfolio_id,) if campaign.subject.portfolio_id else ()
    return RegisteredResearchContext(
        factor_ids=campaign.subject.existing_factor_ids,
        portfolio_ids=portfolio_ids,
    )


def _protocol_label(protocol: CampaignTaskProtocolPlan) -> str:
    require_identifier(protocol.route_task_id, "route_task_id")
    return f"{protocol.route_task_id}.{protocol.protocol_kind.value.lower()}"


def _stable_id(prefix: str, *parts: Any) -> str:
    rendered = json.dumps(canonicalize(parts), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return f"{prefix}.{hashlib.sha256(rendered.encode('utf-8')).hexdigest()[:16]}"
