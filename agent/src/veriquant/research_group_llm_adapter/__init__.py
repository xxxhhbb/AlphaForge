"""DRAFT-only provider adapter for AlphaForge research groups.

This package normalizes real-LLM or deterministic provider output into the
existing selected research-group runtime. It never creates official evidence,
promotion, portfolio, risk, order, execution, or ledger truth.
"""

from .adapter import (
    DeterministicResearchGroupFallbackProvider,
    ProviderMode,
    RawProviderCandidate,
    RawProviderProposal,
    ResearchGroupLLMAdapter,
    ResearchGroupLLMProvider,
    ResearchGroupProviderRequest,
)

__all__ = [
    "DeterministicResearchGroupFallbackProvider",
    "ProviderMode",
    "RawProviderCandidate",
    "RawProviderProposal",
    "ResearchGroupLLMAdapter",
    "ResearchGroupLLMProvider",
    "ResearchGroupProviderRequest",
]
