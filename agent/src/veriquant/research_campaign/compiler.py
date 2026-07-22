"""Research Copilot compilers for confirmed-later Campaign Drafts."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, ClassVar, Mapping

from ..contracts import CanonicalContract, Component, ContractValidationError, FailureCode
from ..contracts.base import canonicalize, fail, require_aware_datetime, require_hash, require_identifier, require_text
from ..llm_desks.providers import OpenAICompatibleClient, OpenAICompatibleConfig, OpenAICompatibleResponse
if TYPE_CHECKING:
    from ..research_planner.models_v2 import ResearchIntentV2
from .models import (
    RESEARCH_CAMPAIGN_SCHEMA_VERSION,
    CampaignBudget,
    CampaignClarificationQuestion,
    CampaignState,
    InteractionStyle,
    MethodModel,
    ResearchCampaignDraft,
    ResearchDepth,
    ResearchMode,
    ResearchSubject,
    StopCondition,
    UserCheckpoint,
)


CAMPAIGN_COMPILER_RULE_VERSION = "veriquant.campaign-compiler/v2"
DEFAULT_DECISION_TIME = datetime(2026, 7, 19, 9, 30, tzinfo=timezone.utc)
DEFAULT_HASH = "sha256:" + "0" * 64


_MODE_ALIASES: Mapping[ResearchMode, str] = {
    ResearchMode.NEW_ALPHA_EXPLORATION: "新 Alpha 探索",
    ResearchMode.VALIDATE_IDEA: "验证我的想法",
    ResearchMode.IMPROVE_EXISTING_FACTOR: "改进现有因子",
    ResearchMode.PORTFOLIO_GAP_SEARCH: "补充组合缺口",
    ResearchMode.DIAGNOSE_FACTOR_PORTFOLIO: "因子/组合诊断",
    ResearchMode.EVENT_MARKET_TOPIC: "事件与市场专题",
}

_ALLOWED_DESKS = frozenset(
    {
        "fundamental",
        "expectations_events",
        "market_structure",
        "alternative_data",
        "macro_regime",
        "meta_alpha",
    }
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
    "risk_approval",
    "target_weights",
    "place_order",
    "broker",
    "ledger_entries",
    "忽略规则",
    "忽略以上",
    "系统提示",
    "开发者消息",
    "调用工具",
    "修改权限",
    "官方指标",
    "风险审批",
    "直接下单",
    "真实下单",
)


@dataclass(frozen=True, kw_only=True)
class CampaignCompileRequest(CanonicalContract):
    SCHEMA_VERSION: ClassVar[str] = RESEARCH_CAMPAIGN_SCHEMA_VERSION

    user_text: str
    decision_time: datetime = DEFAULT_DECISION_TIME
    universe: tuple[str, ...] = ("CN_ASHARE_APPROVED_PIT_SAMPLE",)
    default_horizon: str = "20d"
    allowed_data: tuple[str, ...] = ("price_volume", "fundamentals")
    data_manifest_hash: str = DEFAULT_HASH
    policy_hash: str = DEFAULT_HASH
    code_hash: str = DEFAULT_HASH
    existing_factor_ids: tuple[str, ...] = ()
    portfolio_id: str | None = None
    rule_version: str = CAMPAIGN_COMPILER_RULE_VERSION

    def __post_init__(self) -> None:
        require_text(self.user_text, "user_text")
        require_aware_datetime(self.decision_time, "decision_time")
        _require_text_tuple(self.universe, "universe")
        require_text(self.default_horizon, "default_horizon")
        _require_text_tuple(self.allowed_data, "allowed_data")
        for name in ("data_manifest_hash", "policy_hash", "code_hash"):
            require_hash(getattr(self, name), name)
        _require_text_tuple(self.existing_factor_ids, "existing_factor_ids", nonempty=False)
        if self.portfolio_id is not None:
            require_identifier(self.portfolio_id, "portfolio_id")
        require_identifier(self.rule_version, "rule_version")


@dataclass(frozen=True, kw_only=True)
class CampaignRecommendation(CanonicalContract):
    SCHEMA_VERSION: ClassVar[str] = RESEARCH_CAMPAIGN_SCHEMA_VERSION

    mode: ResearchMode
    research_depth: ResearchDepth
    interaction_style: InteractionStyle
    method_models: tuple[MethodModel, ...]
    candidate_agents: tuple[str, ...]
    explanation: str

    def __post_init__(self) -> None:
        if not isinstance(self.mode, ResearchMode):
            object.__setattr__(self, "mode", ResearchMode(self.mode))
        if not isinstance(self.research_depth, ResearchDepth):
            object.__setattr__(self, "research_depth", ResearchDepth(self.research_depth))
        if not isinstance(self.interaction_style, InteractionStyle):
            object.__setattr__(self, "interaction_style", InteractionStyle(self.interaction_style))
        _require_enum_tuple(self.method_models, MethodModel, "method_models")
        _require_text_tuple(self.candidate_agents, "candidate_agents")
        unknown_desks = set(self.candidate_agents) - _ALLOWED_DESKS
        if unknown_desks:
            fail(
                FailureCode.UNAUTHORIZED_CAPABILITY,
                "Campaign compiler may only route to approved Research Desks",
                desks=tuple(sorted(unknown_desks)),
            )
        require_text(self.explanation, "explanation")


@dataclass(frozen=True, kw_only=True)
class CampaignCompileResult(CanonicalContract):
    SCHEMA_VERSION: ClassVar[str] = RESEARCH_CAMPAIGN_SCHEMA_VERSION

    request_hash: str
    provider: str
    rule_version: str
    sanitized_user_text: str
    intent: Any
    recommendation: CampaignRecommendation
    clarification_questions: tuple[CampaignClarificationQuestion, ...]
    draft: ResearchCampaignDraft | None
    can_execute: bool = False
    prompt_injection_removed: bool = False

    def __post_init__(self) -> None:
        require_hash(self.request_hash, "request_hash")
        require_identifier(self.provider, "provider")
        require_identifier(self.rule_version, "rule_version")
        require_text(self.sanitized_user_text, "sanitized_user_text")
        from ..research_planner.models_v2 import ResearchIntentV2

        if not isinstance(self.intent, ResearchIntentV2):
            fail(FailureCode.INVALID_VALUE, "intent must be ResearchIntentV2")
        if not isinstance(self.recommendation, CampaignRecommendation):
            fail(FailureCode.INVALID_VALUE, "recommendation must be CampaignRecommendation")
        if not isinstance(self.clarification_questions, tuple):
            fail(FailureCode.INVALID_VALUE, "clarification_questions must be a tuple")
        if len(self.clarification_questions) > 3:
            fail(FailureCode.INVALID_VALUE, "at most three clarification questions are allowed")
        if not all(isinstance(item, CampaignClarificationQuestion) for item in self.clarification_questions):
            fail(FailureCode.INVALID_VALUE, "clarification_questions must contain CampaignClarificationQuestion")
        if self.draft is not None and not isinstance(self.draft, ResearchCampaignDraft):
            fail(FailureCode.INVALID_VALUE, "draft must be ResearchCampaignDraft")
        if self.can_execute is not False:
            fail(FailureCode.UNAUTHORIZED_CAPABILITY, "compiler results are never executable")
        if not isinstance(self.prompt_injection_removed, bool):
            fail(FailureCode.INVALID_VALUE, "prompt_injection_removed must be boolean")


class DeterministicCampaignCompiler:
    """Default offline natural-language to Campaign Draft compiler."""

    provider_id = "deterministic_campaign_compiler"

    def __init__(self, *, rule_version: str = CAMPAIGN_COMPILER_RULE_VERSION) -> None:
        require_identifier(rule_version, "rule_version")
        self.rule_version = rule_version
        from ..research_planner.semantic import DeterministicResearchIntentCompiler

        self.intent_compiler = DeterministicResearchIntentCompiler()

    def compile_text(self, user_text: str, **overrides: Any) -> CampaignCompileResult:
        return self.compile(CampaignCompileRequest(user_text=user_text, rule_version=self.rule_version, **overrides))

    def compile(self, request: CampaignCompileRequest) -> CampaignCompileResult:
        if not isinstance(request, CampaignCompileRequest):
            fail(FailureCode.INVALID_VALUE, "request must be CampaignCompileRequest")
        intent = self.intent_compiler.compile(
            request.user_text,
            decision_time=request.decision_time,
            universe=request.universe,
            existing_factor_ids=request.existing_factor_ids,
            portfolio_id=request.portfolio_id,
        )
        sanitized = intent.question
        injection_removed = "privileged or prompt-injection instructions were removed" in intent.assumptions
        mode = intent.mode
        recommendation = _complete_recommendation_coverage(
            _recommend_for_mode(mode, sanitized), tags=_subject_tags(sanitized)
        )
        clarifications = tuple(
            CampaignClarificationQuestion(
                question_id=f"clarify.intent.{index + 1}",
                field_name=_clarification_field(question),
                question=question,
            )
            for index, question in enumerate(intent.clarification_questions)
        )
        draft: ResearchCampaignDraft | None = None
        from ..research_planner.models_v2 import IntentResolution

        if intent.resolution is not IntentResolution.UNSUPPORTED_SAFELY and not _is_unsure_request(sanitized):
            draft = _build_draft(
                request=request,
                sanitized_text=sanitized,
                recommendation=recommendation,
                clarifications=clarifications,
                intent=intent,
            )
        else:
            clarifications = (
                CampaignClarificationQuestion(
                    question_id="clarify.research_mode",
                    field_name="mode",
                    question="你希望优先解决：寻找新 Alpha、验证想法、改进因子、补组合缺口、诊断失效，还是事件专题？",
                ),
                CampaignClarificationQuestion(
                    question_id="clarify.research_subject",
                    field_name="subject",
                    question="请给出研究对象，例如股票池、父因子、组合、事件类型或数据源。",
                ),
            )
        return CampaignCompileResult(
            request_hash=request.artifact_hash,
            provider=self.provider_id,
            rule_version=self.rule_version,
            sanitized_user_text=sanitized,
            intent=intent,
            recommendation=recommendation,
            clarification_questions=clarifications[:3],
            draft=draft,
            can_execute=False,
            prompt_injection_removed=injection_removed,
        )


class OptionalLLMCampaignCompiler:
    """Optional strict-JSON LLM compiler, disabled unless explicitly enabled."""

    provider_id = "optional_llm_campaign_compiler"

    def __init__(
        self,
        *,
        client: OpenAICompatibleClient | None = None,
        config: OpenAICompatibleConfig | None = None,
        enabled: bool = False,
        fallback: DeterministicCampaignCompiler | None = None,
    ) -> None:
        self.client = client
        self.config = config
        self.enabled = enabled
        self.fallback = fallback or DeterministicCampaignCompiler()

    def compile_text(self, user_text: str, **overrides: Any) -> CampaignCompileResult:
        return self.compile(CampaignCompileRequest(user_text=user_text, **overrides))

    def compile(self, request: CampaignCompileRequest) -> CampaignCompileResult:
        if not self.enabled or self.client is None or self.config is None:
            raise ContractValidationError(
                FailureCode.ADAPTER_UNAVAILABLE,
                "optional LLM Campaign compiler is not explicitly enabled",
                details={"provider": self.provider_id},
            )
        sanitized, injection_removed = _sanitize_user_text(request.user_text)
        from ..research_planner.models_v2 import IntentResolution
        from ..research_planner.semantic import DeterministicResearchIntentCompiler

        intent = DeterministicResearchIntentCompiler().compile(
            sanitized,
            decision_time=request.decision_time,
            universe=request.universe,
            existing_factor_ids=request.existing_factor_ids,
            portfolio_id=request.portfolio_id,
        )
        response = self.client.complete_json(
            messages=_llm_messages(request=request, sanitized=sanitized),
            model=self.config.model,
            timeout_seconds=10.0,
            max_tokens=1024,
            temperature=0.0,
            top_p=1.0,
            response_format="json_object",
            extra={},
        )
        if not isinstance(response, OpenAICompatibleResponse):
            fail(FailureCode.PROVIDER_SCHEMA_REJECTED, "LLM compiler returned an invalid response envelope")
        payload = dict(response.payload)
        expected = {
            "mode",
            "research_depth",
            "interaction_style",
            "method_models",
            "candidate_agents",
            "subject_description",
            "clarification_questions",
            "explanation",
        }
        if set(payload) != expected:
            fail(FailureCode.PROVIDER_SCHEMA_REJECTED, "LLM compiler returned an inexact Campaign schema")
        mode = ResearchMode(payload["mode"])
        if mode is not intent.mode:
            fail(FailureCode.PROVIDER_SCHEMA_REJECTED, "LLM mode conflicts with deterministic intent semantics")
        recommendation = CampaignRecommendation(
            mode=mode,
            research_depth=ResearchDepth(payload["research_depth"]),
            interaction_style=InteractionStyle(payload["interaction_style"]),
            method_models=tuple(MethodModel(item) for item in payload["method_models"]),
            candidate_agents=tuple(str(item) for item in payload["candidate_agents"]),
            explanation=str(payload["explanation"]),
        )
        recommendation = _complete_recommendation_coverage(
            recommendation, tags=_subject_tags(sanitized)
        )
        llm_questions = tuple(
            CampaignClarificationQuestion(
                question_id=f"clarify.llm.{index + 1}",
                question=str(question),
                field_name=None,
                required=False,
            )
            for index, question in enumerate(tuple(payload["clarification_questions"])[:3])
        )
        deterministic_questions = tuple(
            CampaignClarificationQuestion(
                question_id=f"clarify.intent.{index + 1}",
                question=question,
                field_name=_clarification_field(question),
            )
            for index, question in enumerate(intent.clarification_questions)
        )
        questions = (*deterministic_questions, *llm_questions)[:3]
        draft = None
        if intent.resolution is not IntentResolution.UNSUPPORTED_SAFELY and not _is_unsure_request(sanitized):
            draft = _build_draft(
                request=request,
                sanitized_text=sanitized,
                recommendation=recommendation,
                clarifications=questions,
                subject_description=str(payload["subject_description"]),
                intent=intent,
            )
        return CampaignCompileResult(
            request_hash=request.artifact_hash,
            provider=self.provider_id,
            rule_version=request.rule_version,
            sanitized_user_text=sanitized,
            intent=intent,
            recommendation=recommendation,
            clarification_questions=questions,
            draft=draft,
            can_execute=False,
            prompt_injection_removed=injection_removed,
        )


def _llm_messages(*, request: CampaignCompileRequest, sanitized: str) -> tuple[Mapping[str, str], ...]:
    system = {
        "role": "system",
        "content": json.dumps(
            {
                "task": "compile_research_campaign_draft_only",
                "allowed_output": "strict_json_only",
                "no_tools": True,
                "no_evidence": True,
                "no_promotion": True,
                "rule_version": request.rule_version,
            },
            sort_keys=True,
            ensure_ascii=False,
        ),
    }
    user = {
        "role": "user",
        "content": json.dumps(
            {
                "sanitized_user_text": sanitized,
                "universe": request.universe,
                "default_horizon": request.default_horizon,
                "allowed_data": request.allowed_data,
                "existing_factor_ids": request.existing_factor_ids,
                "portfolio_id": request.portfolio_id,
            },
            sort_keys=True,
            ensure_ascii=False,
        ),
    }
    return (system, user)


def _build_draft(
    *,
    request: CampaignCompileRequest,
    sanitized_text: str,
    recommendation: CampaignRecommendation,
    clarifications: tuple[CampaignClarificationQuestion, ...],
    subject_description: str | None = None,
    intent: "ResearchIntentV2",
) -> ResearchCampaignDraft:
    from ..research_planner.models_v2 import IntentResolution

    # A legacy ResearchSubject can store only one horizon.  Preserve the
    # configured default while V2 retains both suggested alternatives and the
    # required clarification; never reinterpret a lookback number as horizon.
    horizon = (
        f"{intent.forecast_horizons[0]}d"
        if intent.resolution is IntentResolution.COMPLETE_DRAFT and len(intent.forecast_horizons) == 1
        else request.default_horizon
    )
    existing_factor_ids = _existing_factors_for(text=sanitized_text, request=request, mode=recommendation.mode)
    subject = ResearchSubject(
        subject_id=_stable_id("subject", request, recommendation.mode.value, sanitized_text),
        description=subject_description or _subject_description(recommendation.mode, sanitized_text),
        universe=request.universe,
        horizon=horizon,
        decision_time=request.decision_time,
        allowed_data=_allowed_data_for(
            recommendation.mode, request.allowed_data, tags=_subject_tags(sanitized_text)
        ),
        existing_factor_ids=existing_factor_ids,
        portfolio_id=request.portfolio_id,
        tags=_subject_tags(sanitized_text),
    )
    return ResearchCampaignDraft(
        campaign_id=_stable_id("campaign", request, recommendation.mode.value, subject.artifact_hash),
        question=sanitized_text,
        mode=recommendation.mode,
        research_depth=recommendation.research_depth,
        interaction_style=recommendation.interaction_style,
        subject=subject,
        method_models=recommendation.method_models,
        candidate_agents=recommendation.candidate_agents,
        budget=_budget_for(recommendation.research_depth),
        user_checkpoints=_checkpoints_for(recommendation.research_depth, recommendation.interaction_style),
        stop_conditions=_stop_conditions_for(recommendation.mode),
        data_manifest_hash=request.data_manifest_hash,
        policy_hash=request.policy_hash,
        code_hash=request.code_hash,
        created_at=request.decision_time,
        proposed_by=Component.LLM_RESEARCHER,
        assumptions=(
            "compiler output is a draft requiring operator confirmation",
            "no FactorDAG, PIT, Evidence, Promotion, Portfolio, Risk, Execution, or Ledger action is started",
            *tuple(f"clarification_required:{item.field_name or item.question_id}" for item in clarifications if item.required),
        ),
        state=CampaignState.NEEDS_CONFIRMATION,
        clarification_questions=clarifications,
        unresolved_fields=tuple(
            sorted(
                item.field_name or item.question_id
                for item in clarifications
                if item.required
            )
        ),
    )


def _detect_mode(text: str) -> ResearchMode:
    lowered = text.lower()
    if _is_unsure_request(text):
        return ResearchMode.NEW_ALPHA_EXPLORATION
    if any(token in text for token in ("为什么失效", "失效", "回撤", "衰减", "拥挤", "诊断")):
        return ResearchMode.DIAGNOSE_FACTOR_PORTFOLIO
    if any(token in text for token in ("财报后漂移", "财报", "事件窗口", "高波动期", "公告后")):
        return ResearchMode.EVENT_MARKET_TOPIC
    if any(token in text for token in ("降低换手", "改进", "优化", "父因子")):
        return ResearchMode.IMPROVE_EXISTING_FACTOR
    if any(token in text for token in ("低相关", "组合缺口", "补充组合", "增量 alpha", "增量Alpha")):
        return ResearchMode.PORTFOLIO_GAP_SEARCH
    if any(token in text for token in ("能否预测", "是否能预测", "能不能预测", "验证", "是否预测")):
        return ResearchMode.VALIDATE_IDEA
    if "new alpha" in lowered or "alpha" in lowered or any(token in text for token in ("新的", "新因子", "找新", "找新的")):
        return ResearchMode.NEW_ALPHA_EXPLORATION
    return ResearchMode.NEW_ALPHA_EXPLORATION


def _recommend_for_mode(mode: ResearchMode, text: str) -> CampaignRecommendation:
    depth = ResearchDepth.STRICT_VALIDATION if "严格" in text else ResearchDepth.STANDARD_RESEARCH
    if "快速" in text:
        depth = ResearchDepth.QUICK_EXPLORATION
    style = InteractionStyle.GUIDED if any(token in text for token in ("一步步", "引导", "帮我选择")) else InteractionStyle.STANDARD
    if "批量" in text or "批处理" in text:
        style = InteractionStyle.BATCH
    mapping: Mapping[ResearchMode, tuple[tuple[MethodModel, ...], tuple[str, ...], str]] = {
        ResearchMode.NEW_ALPHA_EXPLORATION: (
            (MethodModel.DATA_ANOMALY, MethodModel.HYPOTHESIS_DRIVEN, MethodModel.ADVERSARIAL_FALSIFICATION),
            ("market_structure", "fundamental", "meta_alpha"),
            "目标是提出新的可证伪 Alpha 候选，并在确认后交给确定性验证。",
        ),
        ResearchMode.VALIDATE_IDEA: (
            (MethodModel.HYPOTHESIS_DRIVEN, MethodModel.ADVERSARIAL_FALSIFICATION),
            ("fundamental", "expectations_events"),
            "目标是把用户想法预注册成可证伪假设。",
        ),
        ResearchMode.IMPROVE_EXISTING_FACTOR: (
            (MethodModel.BOUNDED_EVOLUTION, MethodModel.DIAGNOSTIC_ATTRIBUTION, MethodModel.ADVERSARIAL_FALSIFICATION),
            ("market_structure", "meta_alpha"),
            "目标是在父因子约束下提出低换手或更稳健的 DRAFT 版本。",
        ),
        ResearchMode.PORTFOLIO_GAP_SEARCH: (
            (MethodModel.RESIDUAL_INCREMENTAL, MethodModel.HYPOTHESIS_DRIVEN, MethodModel.ADVERSARIAL_FALSIFICATION),
            ("meta_alpha", "market_structure", "fundamental"),
            "目标是寻找相对现有组合或因子库的增量 Alpha。",
        ),
        ResearchMode.DIAGNOSE_FACTOR_PORTFOLIO: (
            (MethodModel.DIAGNOSTIC_ATTRIBUTION, MethodModel.REGIME_CONDITIONING, MethodModel.ADVERSARIAL_FALSIFICATION),
            ("market_structure", "macro_regime", "meta_alpha"),
            "目标是解释失效、回撤、拥挤或状态变化，而不是默认产生新因子。",
        ),
        ResearchMode.EVENT_MARKET_TOPIC: (
            (MethodModel.EVENT_STUDY, MethodModel.REGIME_CONDITIONING, MethodModel.HYPOTHESIS_DRIVEN),
            ("expectations_events", "macro_regime", "market_structure"),
            "目标是研究事件可得时间、事件窗口和市场状态条件。",
        ),
        ResearchMode.PAPER_STRATEGY_REPLICATION: (
            (MethodModel.REPLICATION, MethodModel.ADVERSARIAL_FALSIFICATION),
            ("fundamental", "market_structure"),
            "目标是映射外部定义、数据与时间语义并形成可审计复现差异。",
        ),
        ResearchMode.NEW_DATA_SOURCE_EVALUATION: (
            (MethodModel.DATA_ANOMALY, MethodModel.ADVERSARIAL_FALSIFICATION),
            ("alternative_data", "market_structure"),
            "目标是评价数据来源、覆盖、PIT 与增量潜力，不直接晋级因子。",
        ),
        ResearchMode.PRELAUNCH_REVIEW: (
            (MethodModel.DIAGNOSTIC_ATTRIBUTION, MethodModel.ADVERSARIAL_FALSIFICATION),
            ("market_structure", "meta_alpha"),
            "目标是检查成本、容量、稳定性和压力风险。",
        ),
        ResearchMode.MONITOR_DECAY_RETIREMENT: (
            (MethodModel.ONLINE_MONITORING, MethodModel.DIAGNOSTIC_ATTRIBUTION),
            ("meta_alpha", "market_structure", "macro_regime"),
            "目标是形成保持、缩减、暂停或退役建议，不直接改变持仓。",
        ),
    }
    methods, agents, explanation = mapping[mode]
    return CampaignRecommendation(
        mode=mode,
        research_depth=depth,
        interaction_style=style,
        method_models=methods,
        candidate_agents=agents,
        explanation=f"{_MODE_ALIASES.get(mode, mode.value)}：{explanation}",
    )


def _complete_recommendation_coverage(
    recommendation: CampaignRecommendation, *, tags: tuple[str, ...]
) -> CampaignRecommendation:
    """Complete specialist coverage from typed subject dimensions.

    This is deliberately based on the compiler's structured subject tags, not
    question-specific patches in the product runtime.  The original lead and
    supporting order is preserved; missing domain specialists are appended in
    a stable capability order.
    """

    tag_set = set(tags)
    required: set[str] = set()
    if tag_set & {"value", "quality", "growth", "cashflow"}:
        required.add("fundamental")
    if tag_set & {"momentum", "reversal", "volatility", "volume", "liquidity", "moneyflow"}:
        required.add("market_structure")
    if tag_set & {"earnings", "earnings_revision", "event"}:
        required.add("expectations_events")
    if "alternative_data" in tag_set:
        required.add("alternative_data")
    if tag_set & {"macro", "high_volatility"}:
        required.add("macro_regime")
    if (
        recommendation.mode is ResearchMode.PORTFOLIO_GAP_SEARCH
        or MethodModel.RESIDUAL_INCREMENTAL in recommendation.method_models
    ):
        required.add("meta_alpha")
    capability_order = (
        "fundamental", "expectations_events", "market_structure",
        "alternative_data", "macro_regime", "meta_alpha",
    )
    agents = tuple(dict.fromkeys((
        *recommendation.candidate_agents,
        *(item for item in capability_order if item in required),
    )))
    if agents == recommendation.candidate_agents:
        return recommendation
    added = tuple(item for item in agents if item not in recommendation.candidate_agents)
    return CampaignRecommendation(
        mode=recommendation.mode,
        research_depth=recommendation.research_depth,
        interaction_style=recommendation.interaction_style,
        method_models=recommendation.method_models,
        candidate_agents=agents,
        explanation=recommendation.explanation + " 覆盖复核补充：" + "、".join(added) + "。",
    )


def _clarifications_for(
    *,
    mode: ResearchMode,
    text: str,
    request: CampaignCompileRequest,
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
    if mode is ResearchMode.PORTFOLIO_GAP_SEARCH and not (request.portfolio_id or request.existing_factor_ids or _named_factor_tags(text)):
        questions.append(
            CampaignClarificationQuestion(
                question_id="clarify.portfolio_or_existing_factors",
                field_name="portfolio_or_existing_factor_ids",
                question="请提供 portfolio_id，或现有动量、价值等因子的具体 factor_id 范围。",
            )
        )
    if not request.universe:
        questions.append(
            CampaignClarificationQuestion(
                question_id="clarify.universe",
                field_name="universe",
                question="请确认本次研究使用的冻结股票池。",
            )
        )
    return tuple(questions[:3])


def _sanitize_user_text(text: str) -> tuple[str, bool]:
    cleaned_lines: list[str] = []
    removed = False
    for raw_line in text.replace("\r\n", "\n").split("\n"):
        line = raw_line.strip()
        lowered = line.lower()
        if not line:
            continue
        if any(pattern in lowered or pattern in line for pattern in _INJECTION_PATTERNS):
            removed = True
            continue
        cleaned_lines.append(line)
    cleaned = " ".join(cleaned_lines).strip()
    if not cleaned:
        cleaned = "我不确定，帮我选择"
    return cleaned[:1000], removed


def _clarification_field(question: str) -> str | None:
    if "parent_factor_id" in question:
        return "parent_factor_id"
    if "要评审或监控" in question:
        return "factor_id"
    if "要诊断" in question or ("factor_id" in question and "portfolio_id" in question):
        return "portfolio_or_existing_factor_ids"
    if "预测未来" in question or "收益周期" in question:
        return "forecast_horizons"
    if "论文、研报或策略定义" in question:
        return "source_document"
    if "待评估数据源" in question:
        return "data_source_scope_time_semantics"
    return None


def _is_unsure_request(text: str) -> bool:
    return any(token in text for token in ("我不确定", "帮我选择", "不知道选", "不确定选"))


def _extract_horizon(text: str) -> str | None:
    match = re.search(r"(\d{1,3})\s*(日|天|d|D)", text)
    if not match:
        return None
    return f"{int(match.group(1))}d"


def _has_parent_factor(text: str, request: CampaignCompileRequest) -> bool:
    if request.existing_factor_ids:
        return True
    return re.search(r"(parent_factor_id|父因子|factor_id)\s*[:=：]\s*[A-Za-z0-9_.:/-]+", text) is not None


def _named_factor_tags(text: str) -> tuple[str, ...]:
    tags: list[str] = []
    lowered = text.lower()
    for token, tag in (
        ("动量", "momentum"), ("momentum", "momentum"),
        ("价值", "value"), ("估值", "value"), ("valuation", "value"),
        ("反转", "reversal"), ("反差", "reversal"), ("reversal", "reversal"),
        ("波动", "volatility"), ("volatility", "volatility"),
        ("成交量", "volume"), ("量价", "volume"),
        ("流动性", "liquidity"), ("liquidity", "liquidity"),
        ("质量", "quality"), ("quality", "quality"),
        ("成长", "growth"), ("growth", "growth"),
        ("现金流", "cashflow"), ("cashflow", "cashflow"),
        ("资金流", "moneyflow"), ("money flow", "moneyflow"),
    ):
        if token.lower() in lowered:
            tags.append(tag)
    return tuple(dict.fromkeys(tags))


def _existing_factors_for(
    *,
    text: str,
    request: CampaignCompileRequest,
    mode: ResearchMode,
) -> tuple[str, ...]:
    if request.existing_factor_ids:
        return request.existing_factor_ids
    if mode is ResearchMode.PORTFOLIO_GAP_SEARCH:
        return _named_factor_tags(text)
    match = re.search(r"(?:parent_factor_id|factor_id)\s*[:=：]\s*([A-Za-z0-9_.:/-]+)", text)
    if match:
        return (match.group(1),)
    return ()


def _subject_tags(text: str) -> tuple[str, ...]:
    tags = list(_named_factor_tags(text))
    lowered = text.lower()
    for token, tag in (
        ("财报", "earnings"), ("业绩", "earnings"), ("earnings", "earnings"),
        ("盈利上调", "earnings_revision"), ("盈利修正", "earnings_revision"),
        ("高波动", "high_volatility"),
        ("宏观", "macro"), ("利率", "macro"), ("shibor", "macro"),
        ("新闻", "alternative_data"), ("情绪", "alternative_data"), ("舆情", "alternative_data"),
        ("事件", "event"), ("公告", "event"),
    ):
        if token.lower() in lowered:
            tags.append(tag)
    return tuple(dict.fromkeys(tags))


def _subject_description(mode: ResearchMode, text: str) -> str:
    if mode is ResearchMode.DIAGNOSE_FACTOR_PORTFOLIO:
        return f"诊断研究对象：{text}"
    if mode is ResearchMode.EVENT_MARKET_TOPIC:
        return f"事件与状态专题：{text}"
    if mode is ResearchMode.PORTFOLIO_GAP_SEARCH:
        return f"组合缺口或已有因子增量研究：{text}"
    return f"研究对象：{text}"


def _allowed_data_for(
    mode: ResearchMode, base: tuple[str, ...], *, tags: tuple[str, ...] = ()
) -> tuple[str, ...]:
    extras: tuple[str, ...] = ()
    if mode is ResearchMode.EVENT_MARKET_TOPIC:
        extras = ("events", "macro_regime")
    elif mode is ResearchMode.DIAGNOSE_FACTOR_PORTFOLIO:
        extras = ("registered_factor_summary", "macro_regime")
    elif mode is ResearchMode.PORTFOLIO_GAP_SEARCH:
        extras = ("registered_factor_summary",)
    tag_set = set(tags)
    tag_extras: list[str] = []
    if tag_set & {"macro", "high_volatility"}:
        tag_extras.append("macro_regime")
    if tag_set & {"earnings", "earnings_revision", "event"}:
        tag_extras.append("events")
    if "alternative_data" in tag_set:
        tag_extras.append("alternative_data")
    if "moneyflow" in tag_set:
        tag_extras.append("moneyflow")
    merged = tuple(dict.fromkeys((*base, *extras, *tag_extras)))
    return merged


def _budget_for(depth: ResearchDepth) -> CampaignBudget:
    if depth is ResearchDepth.QUICK_EXPLORATION:
        return CampaignBudget(max_factor_families=1, max_trials=1, max_llm_calls=4, max_runtime_seconds=60)
    if depth is ResearchDepth.STRICT_VALIDATION:
        return CampaignBudget(max_factor_families=4, max_trials=24, max_llm_calls=24, max_runtime_seconds=600)
    return CampaignBudget(max_factor_families=3, max_trials=12, max_llm_calls=12, max_runtime_seconds=300)


def _checkpoints_for(depth: ResearchDepth, style: InteractionStyle) -> tuple[UserCheckpoint, ...]:
    checkpoints = [
        UserCheckpoint(checkpoint_id="checkpoint.after_research_brief", stage="after_research_brief", description="确认 Research Brief"),
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


def _stable_id(prefix: str, *parts: Any) -> str:
    rendered = json.dumps(canonicalize(parts), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    import hashlib

    return f"{prefix}.{hashlib.sha256(rendered.encode('utf-8')).hexdigest()[:16]}"


def _require_text_tuple(value: tuple[str, ...], field_name: str, *, nonempty: bool = True) -> None:
    if not isinstance(value, tuple) or (nonempty and not value):
        fail(FailureCode.INVALID_VALUE, f"{field_name} must be a tuple", field=field_name)
    for item in value:
        require_text(item, field_name)


def _require_enum_tuple(value: tuple[Any, ...], enum_type: type, field_name: str) -> None:
    if not isinstance(value, tuple) or not value:
        fail(FailureCode.INVALID_VALUE, f"{field_name} must be a non-empty tuple", field=field_name)
    for item in value:
        if not isinstance(item, enum_type):
            enum_type(item)
