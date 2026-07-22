"""Governed data coverage and controlled read-only research tools."""

from .adapter import ControlledResearchToolbox, FrozenResearchArtifact
from .contracts import CoverageFailureCode, CoverageStatus, DataCoverageDecision, ExternalApprovalNeed, ReadOnlyArtifactHandle, RegisteredContextReceipt, ResearchAuthority
from .coverage import DataCoverageResolver
from .handles import ContentAddressedHandleStore
from .registry import BROAD_READONLY_RESEARCH_TOOLS, ResearchToolRegistry, SPECIALIST_TOOL_ALLOWLISTS

__all__ = [
    "ContentAddressedHandleStore", "ControlledResearchToolbox", "CoverageFailureCode",
    "CoverageStatus", "DataCoverageDecision", "DataCoverageResolver", "ExternalApprovalNeed",
    "FrozenResearchArtifact", "ReadOnlyArtifactHandle", "RegisteredContextReceipt",
    "BROAD_READONLY_RESEARCH_TOOLS", "ResearchAuthority", "ResearchToolRegistry", "SPECIALIST_TOOL_ALLOWLISTS",
    "VIBE_READONLY_RESEARCH_TOOLS", "VibeResearchToolbox", "available_vibe_research_registry",
]


def __getattr__(name: str):
    # Keep the core frozen-toolbox package lightweight.  The Vibe adapter
    # intentionally imports Vibe's full Agent/tool stack only when selected by
    # the product assembly path.
    if name in {"VIBE_READONLY_RESEARCH_TOOLS", "VibeResearchToolbox", "available_vibe_research_registry"}:
        from .vibe import VIBE_READONLY_RESEARCH_TOOLS, VibeResearchToolbox, available_vibe_research_registry

        return {
            "VIBE_READONLY_RESEARCH_TOOLS": VIBE_READONLY_RESEARCH_TOOLS,
            "VibeResearchToolbox": VibeResearchToolbox,
            "available_vibe_research_registry": available_vibe_research_registry,
        }[name]
    raise AttributeError(name)
