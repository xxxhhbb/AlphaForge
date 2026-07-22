"""Desk-visible, read-only research tool contracts for AlphaForge.

This package is deliberately isolated from official Evidence, Promotion,
Portfolio, Risk, Execution, and Ledger services.  It defines the envelope that
Campaign-routed desks may receive from frozen or fake providers.
"""

from .models import (
    CitationBundle,
    DataAvailabilitySummary,
    DeskToolPolicy,
    ExperimentDesignPlan,
    FinancialAuditFinding,
    FrozenResearchArtifact,
    MarketEventSummary,
    ResearchToolCapability,
    ResearchToolContractError,
    ResearchToolFailureCode,
    ResearchToolRequest,
    ResearchToolResult,
    ResearchToolVisibility,
    SourceProvenance,
    VisibleEvidenceSummary,
    canonical_json,
    content_hash,
)

__all__ = [
    "CitationBundle",
    "DataAvailabilitySummary",
    "DeskToolPolicy",
    "ExperimentDesignPlan",
    "FinancialAuditFinding",
    "FrozenResearchArtifact",
    "MarketEventSummary",
    "ResearchToolCapability",
    "ResearchToolContractError",
    "ResearchToolFailureCode",
    "ResearchToolRequest",
    "ResearchToolResult",
    "ResearchToolVisibility",
    "SourceProvenance",
    "VisibleEvidenceSummary",
    "canonical_json",
    "content_hash",
]
