"""Real LLM provider boundary for AlphaForge/VeriQuant.

This package exposes only OpenAI-compatible JSON client wiring. It does not
grant Evidence, Promotion, Portfolio, Risk, Execution, broker, or ledger
authority to a model.
"""

from .client import (
    ApprovedEndpointPolicy,
    HTTPTransport,
    HTTPTransportResult,
    OpenAICompatibleChatResponse,
    OpenAICompatibleHTTPClient,
    ProviderToolCall,
    ProviderAvailabilityProbe,
    ProviderFailure,
    ProviderProbeResult,
    ProviderStatus,
    RealLLMProviderSettings,
    UrllibHTTPTransport,
)
from .env import RealLLMEnvBinding, load_real_llm_from_env

__all__ = [
    "ApprovedEndpointPolicy",
    "HTTPTransport",
    "HTTPTransportResult",
    "OpenAICompatibleChatResponse",
    "OpenAICompatibleHTTPClient",
    "ProviderAvailabilityProbe",
    "ProviderFailure",
    "ProviderProbeResult",
    "ProviderStatus",
    "ProviderToolCall",
    "RealLLMEnvBinding",
    "RealLLMProviderSettings",
    "UrllibHTTPTransport",
    "load_real_llm_from_env",
]
