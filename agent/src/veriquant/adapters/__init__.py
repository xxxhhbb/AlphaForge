"""Fail-closed research proposal, lint, and offline Qlib adapters."""

from .proposal import (
    DraftProposal,
    ProposalContext,
    QuantaAlphaProposalAdapter,
    compile_proposal_expression,
)
from .qlib import (
    AdapterFailure,
    FakeQlibAdapter,
    LocalQlibVisibleRunner,
    OptionalPyQlibAdapter,
    QlibCostConfig,
    QlibDateSegment,
    QlibManifest,
    QlibResearchAdapter,
    QlibVisibleResult,
    QlibVisibleRow,
    VisibleQlibManifest,
    default_qlib_adapter,
)
from .qlib_subprocess import SubprocessLocalQlibVisibleRunner
from .regulator import FactorLintPolicy, FactorLintReport, FactorRegulatorAdapter

__all__ = [
    "AdapterFailure",
    "DraftProposal",
    "FactorLintPolicy",
    "FactorLintReport",
    "FactorRegulatorAdapter",
    "FakeQlibAdapter",
    "LocalQlibVisibleRunner",
    "OptionalPyQlibAdapter",
    "ProposalContext",
    "QlibCostConfig",
    "QlibDateSegment",
    "QlibManifest",
    "QlibResearchAdapter",
    "QlibVisibleResult",
    "QlibVisibleRow",
    "QuantaAlphaProposalAdapter",
    "SubprocessLocalQlibVisibleRunner",
    "VisibleQlibManifest",
    "compile_proposal_expression",
    "default_qlib_adapter",
]