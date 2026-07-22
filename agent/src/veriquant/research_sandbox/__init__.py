"""Isolated auxiliary research sandbox for AlphaForge.

The sandbox can produce only AUXILIARY_RESEARCH material. It never executes
FactorDAG/PIT/Evidence, broker, order, portfolio, risk, or ledger authority.
"""

from .models import (
    FakeAuxiliaryResearchSandboxRunner,
    ResearchSandboxArtifactRef,
    ResearchSandboxFailureCode,
    ResearchSandboxProtocol,
    ResearchSandboxRunRequest,
    ResearchSandboxRunResult,
    ResearchSandboxRunStatus,
    SandboxResourceBudget,
)

__all__ = [
    "FakeAuxiliaryResearchSandboxRunner",
    "ResearchSandboxArtifactRef",
    "ResearchSandboxFailureCode",
    "ResearchSandboxProtocol",
    "ResearchSandboxRunRequest",
    "ResearchSandboxRunResult",
    "ResearchSandboxRunStatus",
    "SandboxResourceBudget",
]
