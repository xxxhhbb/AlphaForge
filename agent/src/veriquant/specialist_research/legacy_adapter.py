"""Compatibility projection into the existing one-to-three candidate payload."""

from __future__ import annotations

from ..contracts import FailureCode
from ..contracts.base import fail
from ..research_group_runtime import GroupCandidateDraft, GroupProposalPayload
from ..research_os import AnswerLevel
from .models import PreflightStatus, SpecialistAgentOutcome, SpecialistStage


class LegacyGroupProposalAdapter:
    """Project accepted DRAFTs only; this adapter never evaluates candidates."""

    def project(self, outcome: SpecialistAgentOutcome, *, limit: int = 3) -> GroupProposalPayload:
        if outcome.status is not SpecialistStage.COMPLETED:
            fail(FailureCode.INCOMPLETE_RESULT, "only a completed specialist DRAFT can be projected")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 3:
            fail(FailureCode.INVALID_VALUE, "legacy projection limit must be 1..3")
        accepted_ids = {
            item.candidate_id
            for item in outcome.preflight_results
            if item.status is PreflightStatus.ACCEPTED
        }
        selected = tuple(item for item in outcome.candidates if item.candidate_id in accepted_ids)[:limit]
        if not selected:
            fail(FailureCode.INCOMPLETE_RESULT, "specialist outcome has no accepted FactorDAG draft")
        falsification = tuple(
            dict.fromkeys(test for candidate in selected for test in candidate.falsification_tests)
        )
        mechanism = "；".join(dict.fromkeys(item.economic_mechanism for item in selected))
        hypothesis = outcome.plan.scope_summary if outcome.plan is not None else selected[0].hypothesis
        return GroupProposalPayload(
            group_id=outcome.specialist_id,
            hypothesis=hypothesis,
            economic_mechanism=mechanism,
            falsification_tests=falsification,
            candidates=tuple(
                GroupCandidateDraft(
                    candidate_id=item.candidate_id,
                    expression=item.expression,
                    variables=item.required_fields,
                    lookback_sessions=item.lookback_sessions,
                    rationale=item.novelty_explanation,
                )
                for item in selected
            ),
            answer_level=AnswerLevel.CONCEPTUAL,
        )

