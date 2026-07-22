"""Real LLM Campaign stage executor factory."""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping

from ..campaign_data_binding import CampaignDataBindingResult
from ..contracts import ContractValidationError, FailureCode
from ..data import FieldRole
from ..integration.llm_desks import FactorDAGCompilePITAdapter, InMemoryOfficialEvaluationAdapter
from ..llm_desks.api import (
    DeskId,
    DeskLLMConfig,
    OpenAICompatibleClient,
    OpenAICompatibleConfig,
    ProviderBudget,
    ResearchQuestionView,
    ResearchStageController,
)
from ..real_llm_research_tools.tools import FrozenResearchToolFactory, ResearchVisibleContext
from ..real_llm_vertical_slice.vertical import RealLLMOneDeskProposalProvider
from ..research_campaign import CampaignRoute, ResearchCampaign
from ..research_campaign.runtime import ResearchStageControllerFactorExecutor


@dataclass(frozen=True, kw_only=True)
class CampaignRealLLMExecutorConfig:
    model: str
    max_tokens_per_desk: int = 12000
    max_calls_per_desk: int = 6
    max_cost_per_desk: float = 1.0
    timeout_seconds: float = 45.0
    pit_snapshot_hash: str | None = None


def build_campaign_real_llm_stage_executor(
    *,
    campaign: ResearchCampaign,
    route: CampaignRoute,
    data_binding: CampaignDataBindingResult,
    client: OpenAICompatibleClient,
    provider_config: OpenAICompatibleConfig,
    config: CampaignRealLLMExecutorConfig | None = None,
    field_roles: Mapping[str, FieldRole] | None = None,
) -> ResearchStageControllerFactorExecutor:
    """Build a CampaignStageExecutor backed by a real OpenAI-compatible model."""

    if data_binding.bundle is None:
        raise ContractValidationError(FailureCode.MISSING_REQUIRED_FIELD, "real LLM Campaign execution requires a data bundle")
    local_config = config or CampaignRealLLMExecutorConfig(model=provider_config.model)
    if local_config.model != provider_config.model:
        raise ContractValidationError(FailureCode.INVALID_VALUE, "executor model must match provider config model")
    question = ResearchQuestionView(
        question_id=f"{campaign.campaign_id}.real_llm_question",
        text=campaign.question,
        universe=campaign.subject.universe,
        horizon=campaign.subject.horizon,
        decision_time=campaign.subject.decision_time,
        availability_assumptions=(
            "feature fields are visible only after the Campaign decision-time cutoff",
            "historical membership is frozen before prompt construction",
            "forward returns are unavailable to proposal workers",
        ),
    )
    selected_desks = tuple(DeskId(desk_id) for desk_id in route.selected_desks)
    contexts = {
        desk_id: _visible_context_for(
            desk_id=desk_id,
            campaign=campaign,
            data_binding=data_binding,
            field_roles=field_roles or default_campaign_field_roles(),
        )
        for desk_id in selected_desks
    }
    tool_factory = FrozenResearchToolFactory(contexts=contexts)
    desks: dict[str, DeskLLMConfig] = {}
    providers: dict[DeskId, RealLLMOneDeskProposalProvider] = {}
    tools = {}
    for desk_id in selected_desks:
        registry = tool_factory.registry_for_desk(desk_id)
        desk = DeskLLMConfig(
            desk_id=desk_id,
            role=f"{desk_id.value} real LLM Campaign DRAFT proposer",
            context=("Use only frozen research-visible summaries.", "Return strict DRAFT JSON only."),
            model=provider_config.model,
            budget=ProviderBudget(
                model=provider_config.model,
                max_tokens=local_config.max_tokens_per_desk,
                max_calls=local_config.max_calls_per_desk,
                max_cost=local_config.max_cost_per_desk,
                timeout_seconds=local_config.timeout_seconds,
            ),
            tool_names=registry.names,
            artifact_namespace=desk_id.value,
        )
        desks[desk_id.value] = desk
        tools[desk_id] = registry
        providers[desk_id] = RealLLMOneDeskProposalProvider(client=client, config=provider_config)
    controller = ResearchStageController(
        compiler=FactorDAGCompilePITAdapter(
            field_roles=field_roles or default_campaign_field_roles(),
            pit_snapshot_hash=local_config.pit_snapshot_hash or data_binding.bundle.manifest_hash,
        ),
        evaluator=InMemoryOfficialEvaluationAdapter(),
    )
    return ResearchStageControllerFactorExecutor(
        controller=controller,
        question=question,
        desks=MappingProxyType(desks),
        providers=MappingProxyType(providers),
        tools=MappingProxyType(tools),
    )


def default_campaign_field_roles() -> Mapping[str, FieldRole]:
    return MappingProxyType(
        {
            "activity_proxy": FieldRole.FEATURE,
            "book_value": FieldRole.FEATURE,
            "close": FieldRole.FEATURE,
            "macro_rate": FieldRole.FEATURE,
            "market_cap": FieldRole.FEATURE,
            "registered_factor_summary": FieldRole.FEATURE,
            "volume": FieldRole.FEATURE,
        }
    )


def _visible_context_for(
    *,
    desk_id: DeskId,
    campaign: ResearchCampaign,
    data_binding: CampaignDataBindingResult,
    field_roles: Mapping[str, FieldRole],
) -> ResearchVisibleContext:
    assert data_binding.bundle is not None
    allowed_fields = _fields_for_desk(desk_id, field_roles)
    coverage = {field: 1.0 for field in allowed_fields}
    return ResearchVisibleContext(
        desk_id=desk_id,
        dataset_manifest_hash=data_binding.bundle.manifest_hash,
        dataset_card_summary=(
            f"{data_binding.bundle.provider_source_label} frozen Campaign data bundle; "
            f"universe_count={len(campaign.subject.universe)}; horizon={campaign.subject.horizon}"
        ),
        pit_field_coverage=coverage,
        availability_summary="available_at cutoff is not after decision_time; proposal workers receive feature summaries only",
        historical_universe_summary="historical universe membership is bound by the frozen Campaign data bundle",
        allowed_fields=allowed_fields,
        citations=(),
        lineage_summary="new Campaign-routed DRAFT candidate family",
        validation_summary="visible validation is unavailable before deterministic calculation",
    )


def _fields_for_desk(desk_id: DeskId, field_roles: Mapping[str, FieldRole]) -> tuple[str, ...]:
    by_desk = {
        DeskId.FUNDAMENTAL: ("book_value", "market_cap", "close"),
        DeskId.EXPECTATIONS_EVENTS: ("close", "volume"),
        DeskId.MARKET_STRUCTURE: ("close", "volume", "market_cap"),
        DeskId.ALTERNATIVE_DATA: ("activity_proxy", "volume", "close"),
        DeskId.MACRO_REGIME: ("macro_rate", "close", "market_cap"),
        DeskId.META_ALPHA: ("close", "volume", "market_cap", "registered_factor_summary"),
    }
    fields = tuple(field for field in by_desk[desk_id] if field_roles.get(field) is FieldRole.FEATURE)
    if not fields:
        raise ContractValidationError(FailureCode.MISSING_REQUIRED_FIELD, "desk has no approved feature fields")
    return fields


__all__ = [
    "CampaignRealLLMExecutorConfig",
    "build_campaign_real_llm_stage_executor",
    "default_campaign_field_roles",
]
