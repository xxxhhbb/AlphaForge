"""Campaign-routed Real LLM Desk execution.

This package wires the existing real LLM provider/prompt/tool assets into the
Campaign runtime. It remains proposal-only: model output is DRAFT or redacted
explanation and has no Evidence, Promotion, Portfolio, Risk, Execution, broker,
order, or ledger authority.
"""

from .executor import (
    CampaignRealLLMExecutorConfig,
    build_campaign_real_llm_stage_executor,
    default_campaign_field_roles,
)

__all__ = [
    "CampaignRealLLMExecutorConfig",
    "build_campaign_real_llm_stage_executor",
    "default_campaign_field_roles",
]
