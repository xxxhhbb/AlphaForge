"""Explicit research-state transition policy."""

from __future__ import annotations

from types import MappingProxyType
from typing import Mapping

from .enums import ContractValidationError, FailureCode, ResearchState


TRANSITION_TABLE: Mapping[ResearchState, frozenset[ResearchState]] = MappingProxyType(
    {
        ResearchState.DRAFT: frozenset(
            {
                ResearchState.DATA_VALIDATED,
                ResearchState.REVISE,
                ResearchState.REJECTED,
                ResearchState.BLOCKED,
            }
        ),
        ResearchState.DATA_VALIDATED: frozenset(
            {
                ResearchState.PIT_VALIDATED,
                ResearchState.REVISE,
                ResearchState.REJECTED,
                ResearchState.BLOCKED,
            }
        ),
        ResearchState.PIT_VALIDATED: frozenset(
            {
                ResearchState.EVALUATED,
                ResearchState.REVISE,
                ResearchState.REJECTED,
                ResearchState.BLOCKED,
            }
        ),
        ResearchState.EVALUATED: frozenset(
            {
                ResearchState.VALIDATED_RESEARCH,
                ResearchState.REVISE,
                ResearchState.REJECTED,
                ResearchState.BLOCKED,
            }
        ),
        ResearchState.REVISE: frozenset(
            {ResearchState.DRAFT, ResearchState.REJECTED, ResearchState.BLOCKED}
        ),
        ResearchState.VALIDATED_RESEARCH: frozenset(
            {ResearchState.CHALLENGER, ResearchState.RETIRED, ResearchState.BLOCKED}
        ),
        ResearchState.CHALLENGER: frozenset(
            {ResearchState.RETIRED, ResearchState.BLOCKED}
        ),
        ResearchState.BLOCKED: frozenset(
            {ResearchState.DRAFT, ResearchState.REVISE, ResearchState.REJECTED}
        ),
        ResearchState.REJECTED: frozenset(),
        ResearchState.RETIRED: frozenset(),
    }
)


def validate_transition(from_state: ResearchState, to_state: ResearchState) -> None:
    allowed = TRANSITION_TABLE.get(from_state, frozenset())
    if to_state not in allowed:
        raise ContractValidationError(
            FailureCode.INVALID_STATE_TRANSITION,
            f"illegal research transition {from_state.value} -> {to_state.value}",
            details={
                "from_state": from_state.value,
                "to_state": to_state.value,
                "allowed": tuple(sorted(state.value for state in allowed)),
            },
        )
