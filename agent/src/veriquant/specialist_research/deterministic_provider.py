"""Production-safe deterministic fallback for bounded specialist research.

This provider is an offline DRAFT generator.  It never wraps a configured
provider, so an enabled real provider that fails cannot be silently replaced.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from .models import (
    CandidateCritique,
    CandidateFamilyPlan,
    CombinationAxis,
    CritiqueAction,
    ResearchDirectionIdea,
    ResearchExplorationPlan,
    ResearchMechanismIdea,
    SpecialistCandidateDraft,
    SpecialistStage,
    SpecialistTurn,
    TurnKind,
)
from .provider import SpecialistProviderContext, SpecialistResearchProvider, build_configured_specialist_provider


class DeterministicSpecialistProvider:
    """Create four bounded FactorDAG drafts across two specialist families."""

    provider_id = "deterministic_specialist"
    model_id = "deterministic-offline-v1"

    def __init__(self, specialist_id: str) -> None:
        self.specialist_id = specialist_id
        self.calls = 0

    def next_turn(self, context: SpecialistProviderContext) -> SpecialistTurn:
        self.calls += 1
        if context.profile.specialist_id != self.specialist_id:
            raise ValueError("deterministic provider belongs to another specialist")
        if context.stage is SpecialistStage.PLANNING:
            return SpecialistTurn(kind=TurnKind.PLAN, plan=_plan(context))
        if context.stage in {SpecialistStage.INVESTIGATING, SpecialistStage.GENERATING}:
            return SpecialistTurn(kind=TurnKind.CANDIDATES, candidates=_candidates(context))
        if context.stage is SpecialistStage.SELF_CRITIQUING:
            return SpecialistTurn(
                kind=TurnKind.SELF_CRITIQUE,
                critiques=tuple(
                    CandidateCritique(
                        candidate_id=item.candidate_id,
                        action=CritiqueAction.KEEP,
                        weaknesses=("需要由时点检查、成本模型和机器证据继续证伪",),
                        rationale="候选机制与同组其他候选有差异，保留进入安全预检。",
                    )
                    for item in context.candidates
                ),
            )
        if context.stage is SpecialistStage.REVISING:
            revised = tuple(
                replace(
                    item,
                    candidate_id=f"{item.candidate_id}_revision_1",
                    parent_candidate_id=item.candidate_id,
                    revision_number=item.revision_number + 1,
                    novelty_explanation=item.novelty_explanation + " 本次修订保留受限表达式并明确机器复核边界。",
                )
                for item in context.candidates[: context.request.budget.max_submitted_candidates]
            )
            return SpecialistTurn(kind=TurnKind.REVISED_CANDIDATES, candidates=revised)
        raise ValueError(f"unsupported deterministic specialist stage: {context.stage.value}")


def build_preferred_specialist_provider(
    specialist_id: str,
    dotenv_path: Path = Path("agent/.env"),
) -> SpecialistResearchProvider:
    """Prefer a fully configured real provider, otherwise use offline DRAFTs.

    Selection happens before research begins.  A real provider is returned
    directly and is never wrapped with a runtime fallback; call/schema/timeout
    failures therefore remain visible and fail closed.
    """

    from ..alphaforge_llm import AlphaForgeLLMConfigLoader

    configuration = AlphaForgeLLMConfigLoader(dotenv_path=dotenv_path).load()
    if configuration.enabled and configuration.settings is not None and configuration.runtime_credential:
        return build_configured_specialist_provider(dotenv_path)
    return DeterministicSpecialistProvider(specialist_id)


def _plan(context: SpecialistProviderContext) -> ResearchExplorationPlan:
    profile = context.profile
    fields = _research_fields(context)
    families = profile.candidate_families[:2]
    mechanisms = tuple(
        ResearchMechanismIdea(
            mechanism_id=f"{profile.specialist_id}_{family}_mechanism",
            family_id=family,
            mechanism=f"从{profile.research_lenses[index]}视角构造可证伪的横截面信号。",
            observable_proxy=f"批准字段 {fields[index % len(fields)]} 的时点安全变换",
            predicted_direction="POSITIVE" if index == 0 else "NON_MONOTONIC",
            required_fields=(fields[index % len(fields)],),
            falsification="成本后效果为负、跨时期不稳定或缺少增量时拒绝。",
        )
        for index, family in enumerate(families)
    )
    directions = tuple(
        ResearchDirectionIdea(
            direction_id=f"{profile.specialist_id}_direction_{index + 1}",
            mechanism=item.mechanism,
            differentiation=f"区别于同组其他路线的 {item.family_id} 机制家族。",
            required_fields=item.required_fields,
            falsification=item.falsification,
        )
        for index, item in enumerate(mechanisms)
    )
    family_plans = tuple(
        CandidateFamilyPlan(
            family_id=item.family_id,
            mechanism_ids=(item.mechanism_id,),
            combination_axes=(CombinationAxis(
                axis_id="normalization_style",
                values=("rank", "risk_adjusted"),
                rationale="比较原始横截面排序与波动调整表达。",
            ),),
            max_combinations=2,
        )
        for item in mechanisms
    )
    return ResearchExplorationPlan(
        specialist_id=profile.specialist_id,
        scope_summary=f"围绕用户问题，从{profile.display_name}的两类独立机制形成有界候选。",
        directions=directions,
        investigation_questions=("批准字段和时间覆盖是否足够？", "候选是否可由现有受限因子语言表达？"),
        stop_conditions=("候选预算耗尽", "没有可安全表达的差异化机制", "机器证据拒绝全部候选"),
        mechanism_ideas=mechanisms,
        candidate_family_plans=family_plans,
    )


def _candidates(context: SpecialistProviderContext) -> tuple[SpecialistCandidateDraft, ...]:
    request = context.request
    profile = context.profile
    fields = _research_fields(context)
    primary = fields[0]
    secondary = fields[1] if len(fields) > 1 else primary
    lookback = request.requested_lookback_sessions or 20
    short = max(2, min(20, lookback // 2 or 2))
    # The offline fallback uses the same certified Qlib Expression language as
    # the real-model product path.  Cross-sectional normalization belongs to
    # the deterministic evaluation policy, not to a legacy formula dialect.
    expressions = (
        (f"(${primary}/Ref(${primary},{lookback}))-1", (primary,)),
        (f"Mean(${primary},{short})", (primary,)),
        (f"Div(((${primary}/Ref(${primary},{lookback}))-1),Std(${primary},{short}))", (primary,)),
        (f"Sub(((${primary}/Ref(${primary},{lookback}))-1),((${secondary}/Ref(${secondary},{short}))-1))", tuple(dict.fromkeys((primary, secondary)))),
    )
    maximum = min(12, request.budget.max_submitted_candidates)
    target = min(maximum, 4)
    families = profile.candidate_families[:2]
    return tuple(
        SpecialistCandidateDraft(
            candidate_id=f"{profile.specialist_id}_{families[index % 2]}_{index + 1}",
            family_id=families[index % 2],
            hypothesis=f"{profile.research_lenses[index % len(profile.research_lenses)]}形成的候选可能解释未来 {request.label_horizon_sessions} 个交易日横截面收益。",
            economic_mechanism=_mechanism(profile.specialist_id, profile.research_lenses[index % len(profile.research_lenses)]),
            expected_direction="POSITIVE" if index != 1 else "NON_MONOTONIC",
            forecast_horizon_sessions=request.label_horizon_sessions,
            lookback_sessions=lookback if index != 1 else short,
            required_fields=expressions[index][1],
            expression=expressions[index][0],
            availability_assumptions=_availability(profile.specialist_id),
            neutralization=("industry", "market_cap"),
            falsification_tests=("成本后净收益为负", "跨时期或市场状态符号不稳定", "控制已有因子后没有增量"),
            cost_risks=("换手和市场冲击可能吞噬毛收益",),
            exposure_risks=("行业、市值和流动性暴露需要机器复核",),
            novelty_explanation=f"属于 {families[index % 2]} 家族，使用第 {index + 1} 种受限组合；不声明任何官方指标或晋级结果。",
        )
        for index in range(target)
    )


def _research_fields(context: SpecialistProviderContext) -> tuple[str, ...]:
    available = context.request.available_fields
    preferences = {
        "fundamental": ("roe", "book_value", "market_cap", "close"),
        "expectations_events": ("earnings_surprise", "earnings_forecast", "close", "volume"),
        "market_structure": ("close", "volume", "market_cap"),
        "alternative_data": ("activity_proxy", "volume", "close"),
        "macro_regime": ("shibor_overnight", "benchmark_return", "close"),
        "meta_alpha": ("close", "volume", "market_cap"),
    }[context.profile.specialist_id]
    selected = tuple(item for item in preferences if item in available)
    return selected or (available[0],)


def _availability(specialist_id: str) -> tuple[str, ...]:
    if specialist_id == "expectations_events":
        return ("event_time、published_at、available_at 和 first_tradable_time 均不晚于 decision_time，首次可交易后才形成信号",)
    if specialist_id == "fundamental":
        return ("财务记录 available_at 不晚于 decision_time；仅使用决策时点已公开版本",)
    if specialist_id == "macro_regime":
        return ("状态字段在 decision_time 已可知，不使用事后市场状态标签",)
    return ("输入记录 available_at 不晚于 decision_time，历史股票池按决策时点冻结",)


def _mechanism(specialist_id: str, lens: str) -> str:
    prefix = {
        "fundamental": "基本面信息采用跨域价格代理或已批准财务字段",
        "expectations_events": "事件信息在首次可交易时点后逐步进入价格",
        "market_structure": "市场参与者反应、流动性与交易行为",
        "alternative_data": "另类信息只作为已批准时点安全代理",
        "macro_regime": "决策时点可知的宏观与市场状态",
        "meta_alpha": "可见因子覆盖缺口与成本后增量",
    }[specialist_id]
    return f"{prefix}；从{lens}角度提出草案，最终有效性完全由机器证据决定。"


__all__ = ["DeterministicSpecialistProvider", "build_preferred_specialist_provider"]
