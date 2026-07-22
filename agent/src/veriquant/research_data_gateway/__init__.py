"""Governed, full-scope A-share research data gateway."""

from .auxiliary import AUXILIARY_TOOL_IDS, AuxiliaryArtifactFreezer, default_vibe_tools
from .gateway import ResearchDataGateway, discover_historical_symbols
from .harness import GatewayAcceptanceVerdict, ResearchDataGatewayHarness
from .market_spine import FEATURE_FIELDS, build_market_spine
from .models import (
    DataAuthority,
    DataSourceReceipt,
    GatewayFailureCode,
    GatewayStatus,
    ResearchDataGatewayManifest,
    ResearchDataGatewayRequest,
    ResearchDataGatewayResult,
)
from .plans import (
    OFFICIAL_API_NAMES,
    build_comprehensive_plan,
    build_financial_event_canary_plan,
    build_reference_plan,
)

__all__ = [
    "AUXILIARY_TOOL_IDS", "AuxiliaryArtifactFreezer", "DataAuthority",
    "DataSourceReceipt", "GatewayAcceptanceVerdict", "GatewayFailureCode",
    "GatewayStatus", "OFFICIAL_API_NAMES", "ResearchDataGateway",
    "ResearchDataGatewayHarness", "ResearchDataGatewayManifest",
    "ResearchDataGatewayRequest", "ResearchDataGatewayResult",
    "FEATURE_FIELDS", "build_comprehensive_plan", "build_market_spine",
    "build_financial_event_canary_plan", "build_reference_plan", "default_vibe_tools",
    "discover_historical_symbols",
]
