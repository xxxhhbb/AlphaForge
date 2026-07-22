"""Formal Chinese research report v2 for AlphaForge.

This package composes stable machine JSON and useful Markdown from already
produced plans, candidate receipts, and official evidence references. It owns
no Evidence, Promotion, Portfolio, Risk, Execution, broker, or ledger truth.
"""

from .models import (
    CandidateReportV2,
    DataCoverageV2,
    FormalResearchReportV2,
    FormalReportV2Composer,
    ResearchGroupReportV2,
    ResearchabilitySectionV2,
    render_markdown_v2,
)

__all__ = [
    "CandidateReportV2",
    "DataCoverageV2",
    "FormalResearchReportV2",
    "FormalReportV2Composer",
    "ResearchGroupReportV2",
    "ResearchabilitySectionV2",
    "render_markdown_v2",
]
