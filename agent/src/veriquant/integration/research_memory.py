"""Integration-owned bridge from Agent D source-backed DRAFTs to VeriQuant proposals."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping

from ..adapters.proposal import DraftProposal, ProposalContext, QuantaAlphaProposalAdapter
from ..contracts import ContractValidationError, FailureCode, ResearchState
from ..research_memory.api import EvidenceGroundedHypothesis, FactorDraftMapping, HypothesisState
from ..research_memory.canonical import artifact_hash


_SIGN_MAP = {
    "positive": "POSITIVE",
    "negative": "NEGATIVE",
    "mixed": "NON_MONOTONIC",
    "unknown": "NON_MONOTONIC",
}
_LOOKBACK_RE = re.compile(r"\b(?:lag|pct_change|rolling_mean|rolling_std)\s*\([^,]+,\s*(\d+)\s*\)")


def _fail(code: FailureCode, message: str, **details: Any) -> None:
    raise ContractValidationError(code, message, details=details)


def _infer_lookback(expression: str) -> int:
    windows = [int(match.group(1)) for match in _LOOKBACK_RE.finditer(expression)]
    return max(windows, default=1)


def source_backed_hypothesis_to_proposal_payload(
    hypothesis: EvidenceGroundedHypothesis,
    *,
    mapping_index: int = 0,
    lookback_bars: int | None = None,
) -> dict[str, Any]:
    """Convert a cited source-backed hypothesis into a QuantaAlpha-style DRAFT payload.

    Citations remain in the research-memory artifact; this payload intentionally
    contains only inert hypothesis/factor-task fields accepted by the existing
    `QuantaAlphaProposalAdapter`.
    """

    if not isinstance(hypothesis, EvidenceGroundedHypothesis):
        _fail(FailureCode.INVALID_VALUE, "source-backed input must be EvidenceGroundedHypothesis")
    if hypothesis.state is not HypothesisState.DRAFT:
        _fail(FailureCode.INVALID_STATE, "source-backed research must remain DRAFT")
    if not hypothesis.factor_mapping:
        _fail(FailureCode.MISSING_REQUIRED_FIELD, "source-backed hypothesis has no factor mapping")
    try:
        mapping: FactorDraftMapping = hypothesis.factor_mapping[mapping_index]
    except IndexError as exc:
        raise ContractValidationError(
            FailureCode.INVALID_VALUE,
            "factor mapping index is out of range",
            details={"mapping_index": mapping_index},
        ) from exc
    sign = _SIGN_MAP.get(hypothesis.expected_sign)
    if sign is None:
        _fail(FailureCode.INVALID_VALUE, "source-backed expected_sign is not supported")
    lookback = lookback_bars if lookback_bars is not None else _infer_lookback(mapping.draft_expression)
    if isinstance(lookback, bool) or not isinstance(lookback, int) or lookback < 1:
        _fail(FailureCode.INVALID_VALUE, "lookback_bars must be a positive integer")
    return {
        "hypothesis": {
            "hypothesis_id": "doc-hyp-" + hypothesis.hypothesis_hash.removeprefix("sha256:")[:20],
            "statement": hypothesis.hypothesis,
            "economic_mechanism": hypothesis.economic_mechanism,
            "expected_sign": sign,
            "falsification_tests": list(hypothesis.falsification_tests),
        },
        "factor_task": {
            "draft_id": "doc-draft-" + mapping.factor_draft_hash.removeprefix("sha256:")[:20],
            "factor_name": mapping.factor_name,
            "factor_description": mapping.rationale,
            "expression": mapping.draft_expression,
            "required_fields": list(mapping.feature_names),
            "lookback_bars": lookback,
            "neutralization": [],
        },
    }


@dataclass(frozen=True, kw_only=True)
class SourceBackedDraftBridgeResult:
    proposal: DraftProposal
    citation_hashes: tuple[str, ...]
    status: str = "DRAFT_ONLY"
    direct_downstream_authority: bool = False

    def __post_init__(self) -> None:
        if self.proposal.state is not ResearchState.DRAFT:
            _fail(FailureCode.INVALID_STATE, "source-backed adapter may only emit DRAFT proposals")
        if self.status != "DRAFT_ONLY" or self.direct_downstream_authority:
            _fail(FailureCode.UNAUTHORIZED_CAPABILITY, "source-backed DRAFT has no downstream authority")


def adapt_source_backed_draft(
    hypothesis: EvidenceGroundedHypothesis,
    *,
    context: ProposalContext,
    adapter: QuantaAlphaProposalAdapter | None = None,
) -> SourceBackedDraftBridgeResult:
    """Route Agent D output through the existing proposal adapter into FactorDAG/PIT."""

    payload = source_backed_hypothesis_to_proposal_payload(hypothesis)
    citation_hashes = tuple(artifact_hash(citation) for citation in hypothesis.citations)
    proposal = (adapter or QuantaAlphaProposalAdapter()).adapt(payload, context=context)
    return SourceBackedDraftBridgeResult(proposal=proposal, citation_hashes=citation_hashes)


__all__ = [
    "SourceBackedDraftBridgeResult",
    "adapt_source_backed_draft",
    "source_backed_hypothesis_to_proposal_payload",
]
