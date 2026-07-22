"""Bounded concurrent, selected-only proposal runtime with deterministic resume."""

from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor, wait
from typing import Mapping

from ..contracts import FailureCode
from ..contracts.base import fail, require_hash
from ..research_group_runtime.runtime import GroupProposalPayload, GroupProposalProvider
from ..research_group_runtime.selector import GroupSelectionResult, SelectedGroupContext
from ..research_os import AnswerLevel, ResearchGroupAssignment, ResearchGroupSubmission, ResearchIntent
from .contracts import (
    CommittedGroupBranch,
    ConcurrentBranchFailureCode,
    ConcurrentBranchStatus,
    ConcurrentGroupBranchResult,
    ConcurrentGroupRuntimeResult,
)


_FORBIDDEN_TOKENS = (
    "sealed",
    "holdout_metric",
    "test_metric",
    "promotion_verdict",
    "official_metrics",
    "target_weights",
    "risk_approval",
    "orders",
)


class ConcurrentResearchGroupRuntimeV2:
    """Run one to six selected proposal groups concurrently and fail locally."""

    def __init__(
        self,
        *,
        provider: GroupProposalProvider,
        max_workers: int = 6,
        branch_timeout_seconds: float = 30.0,
    ) -> None:
        if not 1 <= max_workers <= 6:
            fail(FailureCode.INVALID_VALUE, "max_workers must be 1..6")
        if not isinstance(branch_timeout_seconds, (int, float)) or branch_timeout_seconds <= 0:
            fail(FailureCode.INVALID_VALUE, "branch_timeout_seconds must be positive")
        self._provider = provider
        self._max_workers = max_workers
        self._branch_timeout_seconds = float(branch_timeout_seconds)

    def run(
        self,
        *,
        selection: GroupSelectionResult,
        intent: ResearchIntent,
        campaign_hash: str,
        committed_branch_map: Mapping[str, CommittedGroupBranch] | None = None,
    ) -> ConcurrentGroupRuntimeResult:
        if not isinstance(selection, GroupSelectionResult):
            fail(FailureCode.INVALID_VALUE, "selection must be GroupSelectionResult")
        if not isinstance(intent, ResearchIntent):
            fail(FailureCode.INVALID_VALUE, "intent must be ResearchIntent")
        require_hash(campaign_hash, "campaign_hash")
        if any(context.campaign_hash != campaign_hash for context in selection.isolated_contexts):
            fail(FailureCode.INVALID_HASH, "selected context campaign hash mismatch")
        self._validate_isolation(selection)

        assignments = {item.group_id: item for item in selection.assignments}
        contexts = {item.group_id: item for item in selection.isolated_contexts}
        selected_ids = tuple(sorted(assignments))
        prior = dict(committed_branch_map or {})
        if set(prior) - set(selected_ids):
            fail(FailureCode.INVALID_STATE, "committed map contains an unselected group")

        results: dict[str, ConcurrentGroupBranchResult] = {}
        committed: dict[str, CommittedGroupBranch] = {}
        for group_id, record in prior.items():
            self._validate_committed(
                record=record,
                campaign_hash=campaign_hash,
                intent=intent,
                context=contexts[group_id],
                assignment=assignments[group_id],
            )
            results[group_id] = record.branch
            committed[group_id] = record

        pending_ids = tuple(group_id for group_id in selected_ids if group_id not in prior)
        if pending_ids:
            executor = ThreadPoolExecutor(
                max_workers=min(self._max_workers, len(pending_ids)),
                thread_name_prefix="alphaforge-research-group",
            )
            futures: dict[Future[GroupProposalPayload], str] = {
                executor.submit(
                    self._provider.propose,
                    context=contexts[group_id],
                    intent=intent,
                    candidate_budget=assignments[group_id].candidate_budget,
                ): group_id
                for group_id in pending_ids
            }
            done, not_done = wait(tuple(futures), timeout=self._branch_timeout_seconds)
            for future in done:
                group_id = futures[future]
                results[group_id] = self._resolve_future(
                    future=future,
                    group_id=group_id,
                    context=contexts[group_id],
                    assignment=assignments[group_id],
                    campaign_hash=campaign_hash,
                )
            for future in not_done:
                group_id = futures[future]
                future.cancel()
                results[group_id] = ConcurrentGroupBranchResult(
                    group_id=group_id,
                    status=ConcurrentBranchStatus.TIMED_OUT,
                    proposal=None,
                    submission=None,
                    failure_code=ConcurrentBranchFailureCode.PROVIDER_TIMEOUT,
                    failure_type="TimeoutError",
                )
            executor.shutdown(wait=False, cancel_futures=True)

        for group_id in selected_ids:
            if group_id not in committed:
                committed[group_id] = self._commit(
                    branch=results[group_id],
                    campaign_hash=campaign_hash,
                    intent=intent,
                    context=contexts[group_id],
                    assignment=assignments[group_id],
                )
        ordered_branches = tuple(results[group_id] for group_id in selected_ids)
        ordered_committed = tuple(committed[group_id] for group_id in selected_ids)
        return ConcurrentGroupRuntimeResult(
            campaign_hash=campaign_hash,
            branches=ordered_branches,
            committed_branches=ordered_committed,
        )

    @staticmethod
    def _validate_isolation(selection: GroupSelectionResult) -> None:
        assignment_by_group = {item.group_id: item for item in selection.assignments}
        for context in selection.isolated_contexts:
            assignment = assignment_by_group[context.group_id]
            if context.tool_whitelist != assignment.tool_whitelist:
                fail(FailureCode.UNAUTHORIZED_CAPABILITY, "context tool whitelist differs from assignment")
            exposed = tuple(context.allowed_context_keys) + tuple(context.tool_whitelist)
            for value in exposed:
                lowered = value.lower()
                if any(token in lowered for token in _FORBIDDEN_TOKENS):
                    fail(FailureCode.UNAUTHORIZED_CAPABILITY, "sealed or privileged context/tool is forbidden")

    @staticmethod
    def _resolve_future(
        *,
        future: Future[GroupProposalPayload],
        group_id: str,
        context: SelectedGroupContext,
        assignment: ResearchGroupAssignment,
        campaign_hash: str,
    ) -> ConcurrentGroupBranchResult:
        try:
            proposal = future.result()
            if not isinstance(proposal, GroupProposalPayload):
                return _failed(group_id, ConcurrentBranchFailureCode.PROVIDER_FAILED, "InvalidProviderResult")
            if proposal.group_id != group_id:
                return _failed(group_id, ConcurrentBranchFailureCode.GROUP_MISMATCH, "GroupMismatch")
            if len(proposal.candidates) > assignment.candidate_budget:
                return _failed(group_id, ConcurrentBranchFailureCode.BUDGET_EXCEEDED, "CandidateBudgetExceeded")
            serialized = proposal.canonical_json().lower()
            if any(token in serialized for token in _FORBIDDEN_TOKENS):
                return _failed(group_id, ConcurrentBranchFailureCode.FORBIDDEN_PAYLOAD, "ForbiddenPayload")
            if proposal.answer_level is AnswerLevel.OFFICIAL_EVALUATED:
                return _failed(group_id, ConcurrentBranchFailureCode.FORBIDDEN_PAYLOAD, "OfficialEvidenceForbidden")
            submission = ResearchGroupSubmission(
                campaign_hash=campaign_hash,
                group_id=context.group_id,
                hypothesis_hash=proposal.artifact_hash,
                candidate_draft_hashes=tuple(item.artifact_hash for item in proposal.candidates),
                answer_level=proposal.answer_level,
            )
            return ConcurrentGroupBranchResult(
                group_id=group_id,
                status=ConcurrentBranchStatus.COMPLETED,
                proposal=proposal,
                submission=submission,
            )
        except Exception as exc:
            return _failed(group_id, ConcurrentBranchFailureCode.PROVIDER_FAILED, type(exc).__name__)

    @staticmethod
    def _commit(
        *,
        branch: ConcurrentGroupBranchResult,
        campaign_hash: str,
        intent: ResearchIntent,
        context: SelectedGroupContext,
        assignment: ResearchGroupAssignment,
    ) -> CommittedGroupBranch:
        return CommittedGroupBranch(
            group_id=branch.group_id,
            campaign_hash=campaign_hash,
            intent_hash=intent.artifact_hash,
            context_hash=context.artifact_hash,
            assignment_hash=assignment.artifact_hash,
            branch=branch,
        )

    @staticmethod
    def _validate_committed(
        *,
        record: CommittedGroupBranch,
        campaign_hash: str,
        intent: ResearchIntent,
        context: SelectedGroupContext,
        assignment: ResearchGroupAssignment,
    ) -> None:
        if not isinstance(record, CommittedGroupBranch):
            fail(FailureCode.INVALID_STATE, "committed map values must be CommittedGroupBranch")
        expected = (
            record.group_id == context.group_id,
            record.campaign_hash == campaign_hash,
            record.intent_hash == intent.artifact_hash,
            record.context_hash == context.artifact_hash,
            record.assignment_hash == assignment.artifact_hash,
        )
        if not all(expected):
            fail(FailureCode.INVALID_HASH, "committed branch does not match current run identity")


def _failed(
    group_id: str,
    code: ConcurrentBranchFailureCode,
    failure_type: str,
) -> ConcurrentGroupBranchResult:
    return ConcurrentGroupBranchResult(
        group_id=group_id,
        status=ConcurrentBranchStatus.FAILED,
        proposal=None,
        submission=None,
        failure_code=code,
        failure_type=failure_type[:80],
    )
