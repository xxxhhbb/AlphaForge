"""Deterministic specialist-group selection and context isolation."""

from .selector import (
    DEFAULT_GROUP_MANIFESTS,
    MAX_RESEARCH_GROUPS,
    DeterministicResearchGroupSelector,
    GroupCapabilityManifest,
    GroupSelectionContext,
    GroupSelectionResult,
    SelectedGroupContext,
)
from .runtime import (
    DeterministicGroupProposalProvider,
    GroupCandidateDraft,
    GroupProposalPayload,
    GroupRuntimeBranchResult,
    GroupRuntimeResult,
    ResearchGroupProposalRuntime,
)

__all__ = [
    "DEFAULT_GROUP_MANIFESTS",
    "MAX_RESEARCH_GROUPS",
    "DeterministicResearchGroupSelector",
    "GroupCapabilityManifest",
    "GroupSelectionContext",
    "GroupSelectionResult",
    "SelectedGroupContext",
    "DeterministicGroupProposalProvider",
    "GroupCandidateDraft",
    "GroupProposalPayload",
    "GroupRuntimeBranchResult",
    "GroupRuntimeResult",
    "ResearchGroupProposalRuntime",
]
