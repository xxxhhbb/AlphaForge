"""Selected-only, failure-isolated proposal collection for research groups."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import ClassVar, Protocol

from ..contracts import CanonicalContract, FailureCode
from ..contracts.base import fail, require_identifier, require_text
from ..research_os import AnswerLevel, ResearchGroupSubmission, ResearchIntent
from .selector import GroupSelectionResult, SelectedGroupContext


SCHEMA_VERSION = "alphaforge.research-group-runtime/v1"


@dataclass(frozen=True)
class GroupCandidateDraft(CanonicalContract):
    candidate_id: str
    expression: str
    variables: tuple[str, ...]
    lookback_sessions: int
    rationale: str
    schema_version: ClassVar[str] = SCHEMA_VERSION

    def __post_init__(self) -> None:
        require_identifier(self.candidate_id, "candidate_id")
        require_text(self.expression, "expression")
        require_text(self.rationale, "rationale")
        if not self.variables or len(self.variables) != len(set(self.variables)):
            fail(FailureCode.INVALID_VALUE, "variables must be unique and non-empty")
        for value in self.variables:
            require_identifier(value, "variables")
        if not isinstance(self.lookback_sessions, int) or self.lookback_sessions <= 0:
            fail(FailureCode.INVALID_VALUE, "lookback_sessions must be positive")


@dataclass(frozen=True)
class GroupProposalPayload(CanonicalContract):
    group_id: str
    hypothesis: str
    economic_mechanism: str
    falsification_tests: tuple[str, ...]
    candidates: tuple[GroupCandidateDraft, ...]
    answer_level: AnswerLevel = AnswerLevel.CONCEPTUAL
    schema_version: ClassVar[str] = SCHEMA_VERSION

    def __post_init__(self) -> None:
        require_identifier(self.group_id, "group_id")
        require_text(self.hypothesis, "hypothesis")
        require_text(self.economic_mechanism, "economic_mechanism")
        if not self.falsification_tests or not 1 <= len(self.candidates) <= 3:
            fail(FailureCode.INVALID_VALUE, "proposal requires falsification and one to three candidates")
        if self.answer_level is AnswerLevel.OFFICIAL_EVALUATED:
            fail(FailureCode.UNAUTHORIZED_CAPABILITY, "group proposal cannot be official")


class GroupBranchStatus(str, Enum):
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


@dataclass(frozen=True)
class GroupRuntimeBranchResult(CanonicalContract):
    group_id: str
    status: GroupBranchStatus
    proposal: GroupProposalPayload | None
    submission: ResearchGroupSubmission | None
    failure_code: str | None = None
    message: str = ""
    schema_version: ClassVar[str] = SCHEMA_VERSION

    def __post_init__(self) -> None:
        require_identifier(self.group_id, "group_id")
        if self.status is GroupBranchStatus.COMPLETED:
            if self.proposal is None or self.submission is None or self.failure_code is not None:
                fail(FailureCode.INVALID_STATE, "completed branch requires proposal and submission")
        elif not self.failure_code:
            fail(FailureCode.MISSING_REQUIRED_FIELD, "failed branch requires failure code")


@dataclass(frozen=True)
class GroupRuntimeResult(CanonicalContract):
    branches: tuple[GroupRuntimeBranchResult, ...]
    schema_version: ClassVar[str] = SCHEMA_VERSION

    def __post_init__(self) -> None:
        ids = tuple(item.group_id for item in self.branches)
        if not ids or len(ids) != len(set(ids)):
            fail(FailureCode.INVALID_VALUE, "runtime branches must be unique and non-empty")


class GroupProposalProvider(Protocol):
    def propose(self, *, context: SelectedGroupContext, intent: ResearchIntent, candidate_budget: int) -> GroupProposalPayload: ...


class DeterministicGroupProposalProvider:
    """Offline baseline that honors the user's requested factor lookback."""

    def propose(self, *, context: SelectedGroupContext, intent: ResearchIntent, candidate_budget: int) -> GroupProposalPayload:
        lookback = intent.factor_lookback_sessions or 20
        group = context.group_id
        expressions = _expressions(group, lookback)
        candidates = tuple(
            GroupCandidateDraft(
                candidate_id=f"{group}_candidate_{index + 1}",
                expression=expression,
                variables=_variables(expression),
                lookback_sessions=lookback,
                rationale=f"{group} 从可见材料提出的 {lookback} 日候选；需机器证据复核。",
            )
            for index, expression in enumerate(expressions[:candidate_budget])
        )
        return GroupProposalPayload(
            group_id=group,
            hypothesis=f"{lookback} 日信息在横截面上可能包含可检验的收益预测差异。",
            economic_mechanism=f"由 {group} 从其专业维度解释形成机制，不构成官方结论。",
            falsification_tests=("成本后净表现为负则反驳", "子时期不稳定则要求修订", "与已登记因子无增量则拒绝"),
            candidates=candidates,
        )


class ResearchGroupProposalRuntime:
    def __init__(self, provider: GroupProposalProvider | None = None) -> None:
        self.provider = provider or DeterministicGroupProposalProvider()

    def run(self, *, selection: GroupSelectionResult, intent: ResearchIntent, campaign_hash: str) -> GroupRuntimeResult:
        budget_by_group = {item.group_id: item.candidate_budget for item in selection.assignments}
        branches: list[GroupRuntimeBranchResult] = []
        for context in selection.isolated_contexts:
            try:
                proposal = self.provider.propose(context=context, intent=intent, candidate_budget=budget_by_group[context.group_id])
                if proposal.group_id != context.group_id:
                    fail(FailureCode.INVALID_FIELD_OWNER, "provider returned another group's proposal")
                submission = ResearchGroupSubmission(
                    campaign_hash=campaign_hash,
                    group_id=context.group_id,
                    hypothesis_hash=proposal.artifact_hash,
                    candidate_draft_hashes=tuple(item.artifact_hash for item in proposal.candidates),
                    answer_level=proposal.answer_level,
                )
                branches.append(GroupRuntimeBranchResult(group_id=context.group_id, status=GroupBranchStatus.COMPLETED, proposal=proposal, submission=submission))
            except Exception as exc:  # branch-local and explicitly surfaced
                branches.append(GroupRuntimeBranchResult(group_id=context.group_id, status=GroupBranchStatus.FAILED, proposal=None, submission=None, failure_code=type(exc).__name__, message=str(exc)[:300]))
        return GroupRuntimeResult(branches=tuple(sorted(branches, key=lambda item: item.group_id)))


def _expressions(group: str, lookback: int) -> tuple[str, ...]:
    by_group = {
        "market_structure": (f"rank_cross_section(pct_change(close, {lookback}))", f"rank_cross_section(divide_safe(pct_change(close, {lookback}), rolling_std(close, {lookback})))"),
        "meta_alpha": (f"zscore_cross_section(pct_change(close, {lookback}))", f"rank_cross_section(add(pct_change(close, {lookback}), pct_change(volume, 5)))"),
        "macro_regime": (f"rank_cross_section(multiply(pct_change(close, {lookback}), macro))",),
        "fundamental": ("zscore_cross_section(divide_safe(book_value, market_cap))",),
        "expectations_events": (f"rank_cross_section(pct_change(close, {lookback}))",),
        "alternative_data": (f"rank_cross_section(rolling_mean(activity_proxy, {lookback}))",),
    }
    return by_group.get(group, (f"rank_cross_section(pct_change(close, {lookback}))",))


def _variables(expression: str) -> tuple[str, ...]:
    known = ("activity_proxy", "book_value", "close", "macro", "market_cap", "volume")
    return tuple(value for value in known if value in expression)
