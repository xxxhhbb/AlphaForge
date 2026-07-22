"""Question assessment for AlphaForge.

The planner is intentionally deterministic: same question, clock, and data
availability produce the same intent, data plan, and artifact hash.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import ClassVar

from ..contracts import CanonicalContract, FailureCode
from ..contracts.base import fail, require_aware_datetime, require_text
from ..data_planning import DataAvailabilitySnapshot, DataPlanningResult, DeterministicDataPlanner
from ..research_campaign.models import ResearchDepth, ResearchMode
from ..research_os.models import AnswerLevel, ResearchQuestionAssessment
from .semantic import DeterministicResearchIntentCompiler


RESEARCH_PLANNER_SCHEMA_VERSION = "alphaforge.research-planner/v1"
DEFAULT_DECISION_TIME = datetime(2026, 7, 19, 9, 30, tzinfo=timezone.utc)

_INJECTION_PATTERNS = (
    "ignore previous",
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
    "系统提示",
    "开发者消息",
    "调用工具",
    "修改权限",
    "官方指标",
    "晋级",
    "风险审批",
    "目标仓位",
    "真实下单",
    "密钥",
)


@dataclass(frozen=True, kw_only=True)
class QuestionAssessmentRequest(CanonicalContract):
    SCHEMA_VERSION: ClassVar[str] = RESEARCH_PLANNER_SCHEMA_VERSION

    question: str
    decision_time: datetime = DEFAULT_DECISION_TIME
    preferred_depth: ResearchDepth | None = None
    availability: DataAvailabilitySnapshot | None = None

    def __post_init__(self) -> None:
        require_text(self.question, "question")
        require_aware_datetime(self.decision_time, "decision_time")
        if self.preferred_depth is not None and not isinstance(self.preferred_depth, ResearchDepth):
            object.__setattr__(self, "preferred_depth", ResearchDepth(self.preferred_depth))
        if self.availability is not None and not isinstance(self.availability, DataAvailabilitySnapshot):
            fail(FailureCode.INVALID_VALUE, "availability must be DataAvailabilitySnapshot")


@dataclass(frozen=True, kw_only=True)
class QuestionAssessmentResult(CanonicalContract):
    SCHEMA_VERSION: ClassVar[str] = RESEARCH_PLANNER_SCHEMA_VERSION

    sanitized_question: str
    prompt_injection_removed: bool
    assessment: ResearchQuestionAssessment
    data_planning: DataPlanningResult

    def __post_init__(self) -> None:
        require_text(self.sanitized_question, "sanitized_question")
        if not isinstance(self.prompt_injection_removed, bool):
            fail(FailureCode.INVALID_VALUE, "prompt_injection_removed must be boolean")
        if not isinstance(self.assessment, ResearchQuestionAssessment):
            fail(FailureCode.INVALID_VALUE, "assessment must be ResearchQuestionAssessment")
        if not isinstance(self.data_planning, DataPlanningResult):
            fail(FailureCode.INVALID_VALUE, "data_planning must be DataPlanningResult")
        rendered = self.sanitized_question.lower()
        if any(term in rendered for term in ("official_metrics", "promotion", "target_weights", "orders", "api_key")):
            fail(FailureCode.UNAUTHORIZED_CAPABILITY, "sanitized question still contains privileged instruction terms")


class DeterministicResearchQuestionPlanner:
    """Classify question intent and derive data scope before execution."""

    def __init__(self, *, data_planner: DeterministicDataPlanner | None = None) -> None:
        self.data_planner = data_planner or DeterministicDataPlanner()
        self.intent_compiler = DeterministicResearchIntentCompiler()

    def assess(self, request: QuestionAssessmentRequest | str) -> QuestionAssessmentResult:
        if isinstance(request, str):
            request = QuestionAssessmentRequest(question=request)
        if not isinstance(request, QuestionAssessmentRequest):
            fail(FailureCode.INVALID_VALUE, "request must be QuestionAssessmentRequest or text")
        intent_v2 = self.intent_compiler.compile(
            request.question,
            decision_time=request.decision_time,
            depth=request.preferred_depth,
        )
        sanitized = intent_v2.question
        removed = "privileged or prompt-injection instructions were removed" in intent_v2.assumptions
        mode = intent_v2.mode
        intent = intent_v2.to_legacy()
        data_planning = self.data_planner.plan(intent, request.availability)
        levels = _answerable_levels(data_planning)
        assessment = ResearchQuestionAssessment(
            intent=intent,
            answerable_levels=levels,
            unavailable_official_reasons=tuple(data_planning.official_blockers)
            if AnswerLevel.OFFICIAL_EVALUATED not in levels
            else (),
            clarification_questions=intent_v2.clarification_questions,
        )
        return QuestionAssessmentResult(
            sanitized_question=sanitized,
            prompt_injection_removed=removed,
            assessment=assessment,
            data_planning=data_planning,
        )


def _sanitize(text: str) -> tuple[str, bool]:
    removed = False
    kept: list[str] = []
    for raw in text.replace("\r\n", "\n").split("\n"):
        line = raw.strip()
        if not line:
            continue
        lowered = line.lower()
        if any(pattern in lowered or pattern in line for pattern in _INJECTION_PATTERNS):
            removed = True
            continue
        kept.append(line)
    cleaned = " ".join(kept).strip()
    if not cleaned:
        cleaned = "我不确定，帮我选择研究问题"
    return cleaned[:1000], removed


def _detect_mode(text: str) -> ResearchMode:
    lowered = text.lower()
    if any(token in text for token in ("为什么", "失效", "回撤", "拥挤", "衰减", "诊断")):
        return ResearchMode.DIAGNOSE_FACTOR_PORTFOLIO
    if any(token in text for token in ("财报", "公告", "事件", "漂移", "高波动期", "专题")):
        return ResearchMode.EVENT_MARKET_TOPIC
    if any(token in text for token in ("改进", "优化", "降低换手", "父因子", "parent_factor_id")):
        return ResearchMode.IMPROVE_EXISTING_FACTOR
    if any(token in text for token in ("低相关", "组合缺口", "补充组合", "增量")):
        return ResearchMode.PORTFOLIO_GAP_SEARCH
    if any(token in text for token in ("能否预测", "是否预测", "能不能预测", "验证", "我的想法")):
        return ResearchMode.VALIDATE_IDEA
    if "alpha" in lowered or any(token in text for token in ("找", "挖", "因子", "动量", "反转", "价值")):
        return ResearchMode.NEW_ALPHA_EXPLORATION
    return ResearchMode.NEW_ALPHA_EXPLORATION


def _detect_depth(text: str) -> ResearchDepth:
    if any(token in text for token in ("快速", "先看看", "草案")):
        return ResearchDepth.QUICK_EXPLORATION
    if any(token in text for token in ("严格", "上线前", "封闭测试", "严谨")):
        return ResearchDepth.STRICT_VALIDATION
    return ResearchDepth.STANDARD_RESEARCH


def _extract_label_horizon(text: str) -> int:
    patterns = (
        r"(?:预测|预判|未来|forward|forecast|horizon)\s*[:=]?\s*(\d{1,6})\s*(?:日|天|d|D)",
        r"(?:收益|return)\s*[:=]?\s*(\d{1,6})\s*(?:日|天|d|D)",
    )
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return _bounded_sessions(match.group(1), default=5)
    return 5


def _extract_factor_lookback(text: str, mode: ResearchMode) -> int | None:
    lookback_patterns = (
        r"lookback\s*[:=]\s*(\d{1,6})",
        r"最近\s*(\d{1,6})\s*(?:日|天)",
        r"过去\s*(\d{1,6})\s*(?:日|天)",
        r"近\s*(\d{1,6})\s*(?:日|天)",
        r"(\d{1,6})\s*(?:日|天)\s*(?:动量|反转|波动|均线|大动量|因子|alpha|Alpha)",
    )
    for pattern in lookback_patterns:
        match = re.search(pattern, text)
        if match:
            return _bounded_sessions(match.group(1), default=20)
    if "大动量" in text:
        return 50
    if mode in {ResearchMode.NEW_ALPHA_EXPLORATION, ResearchMode.IMPROVE_EXISTING_FACTOR, ResearchMode.PORTFOLIO_GAP_SEARCH}:
        return 20
    return None


def _bounded_sessions(value: str, *, default: int) -> int:
    try:
        number = int(value)
    except ValueError:
        return default
    if number < 1:
        return default
    # Never silently rewrite the user's horizon. Operational acquisition
    # budgets may stop explicitly, but they do not change research semantics.
    return number


def _requested_outputs(mode: ResearchMode) -> tuple[str, ...]:
    mapping = {
        ResearchMode.NEW_ALPHA_EXPLORATION: ("candidate_set", "formal_report"),
        ResearchMode.VALIDATE_IDEA: ("validation_conclusion", "formal_report"),
        ResearchMode.IMPROVE_EXISTING_FACTOR: ("factor_comparison_report", "formal_report"),
        ResearchMode.PORTFOLIO_GAP_SEARCH: ("portfolio_gap_report", "formal_report"),
        ResearchMode.DIAGNOSE_FACTOR_PORTFOLIO: ("diagnostic_report", "formal_report"),
        ResearchMode.EVENT_MARKET_TOPIC: ("event_study_report", "formal_report"),
    }
    return mapping.get(mode, ("formal_report",))


def _answerable_levels(data_planning: DataPlanningResult) -> tuple[AnswerLevel, ...]:
    levels: list[AnswerLevel] = [AnswerLevel.CONCEPTUAL]
    if data_planning.availability.available_fields or data_planning.availability.provider_label != "UNKNOWN":
        levels.append(AnswerLevel.SOURCED_RESEARCH)
    if data_planning.official_evaluation_ready:
        levels.append(AnswerLevel.OFFICIAL_EVALUATED)
    return tuple(levels)


def _clarifications(text: str, mode: ResearchMode) -> tuple[str, ...]:
    questions: list[str] = []
    if mode is ResearchMode.IMPROVE_EXISTING_FACTOR and "parent_factor_id" not in text and "父因子" not in text:
        questions.append("请补充要改进的父因子 ID。")
    if mode is ResearchMode.PORTFOLIO_GAP_SEARCH and not any(token in text for token in ("factor_id", "portfolio_id", "动量", "价值", "反转")):
        questions.append("请补充现有因子或组合范围。")
    return tuple(questions[:3])


__all__ = [
    "DeterministicResearchQuestionPlanner",
    "QuestionAssessmentRequest",
    "QuestionAssessmentResult",
]
