"""Read-only visible evidence and experiment design tools."""

from .tools import (
    ExplorerFailureCode,
    ExplorerToolResult,
    InMemoryVisibleEvidenceReader,
    ResearchExplorerError,
    VisibleEvidenceReader,
    VisibleEvidenceToolbox,
    compare_candidates,
    design_control_tests,
    design_placebo_tests,
    design_regime_splits,
    design_walk_forward,
    explain_rejection,
    inspect_robustness,
    inspect_trial_budget,
    preregister_hypothesis,
    query_visible_evidence,
)

__all__ = [
    "ExplorerFailureCode",
    "ExplorerToolResult",
    "InMemoryVisibleEvidenceReader",
    "ResearchExplorerError",
    "VisibleEvidenceReader",
    "VisibleEvidenceToolbox",
    "compare_candidates",
    "design_control_tests",
    "design_placebo_tests",
    "design_regime_splits",
    "design_walk_forward",
    "explain_rejection",
    "inspect_robustness",
    "inspect_trial_budget",
    "preregister_hypothesis",
    "query_visible_evidence",
]
