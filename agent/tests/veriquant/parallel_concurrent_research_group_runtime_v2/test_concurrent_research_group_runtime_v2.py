from __future__ import annotations

import hashlib
import threading
import time
from dataclasses import replace

import pytest

from veriquant.concurrent_research_group_runtime_v2 import (
    ConcurrentBranchFailureCode,
    ConcurrentBranchStatus,
    ConcurrentResearchGroupRuntimeV2,
)
from veriquant.contracts import ContractValidationError
from veriquant.research_campaign import ResearchDepth, ResearchMode
from veriquant.research_group_runtime import (
    GroupCandidateDraft,
    GroupProposalPayload,
    GroupSelectionResult,
    SelectedGroupContext,
)
from veriquant.research_os import AnswerLevel, ResearchGroupAssignment, ResearchIntent


def h(label: str) -> str:
    return "sha256:" + hashlib.sha256(label.encode("utf-8")).hexdigest()


CAMPAIGN_HASH = h("campaign")


def intent() -> ResearchIntent:
    return ResearchIntent(
        question="研究50日动量候选",
        mode=ResearchMode.NEW_ALPHA_EXPLORATION,
        depth=ResearchDepth.STANDARD_RESEARCH,
        factor_lookback_sessions=50,
        label_horizon_sessions=5,
        requested_outputs=("候选因子", "机器验证"),
    )


def selection(*group_ids: str, context_keys: tuple[str, ...] = ("question", "data_fields")) -> GroupSelectionResult:
    assignments = tuple(
        ResearchGroupAssignment(
            group_id=group_id,
            role="specialist",
            score=0.8,
            reasons=("relevant",),
            tool_whitelist=(f"read_{group_id}",),
            candidate_budget=min(index + 1, 3),
        )
        for index, group_id in enumerate(group_ids)
    )
    contexts = tuple(
        SelectedGroupContext(
            group_id=item.group_id,
            campaign_hash=CAMPAIGN_HASH,
            allowed_context_keys=context_keys,
            tool_whitelist=item.tool_whitelist,
        )
        for item in assignments
    )
    return GroupSelectionResult(
        assignments=assignments,
        isolated_contexts=contexts,
        excluded_group_ids=tuple(sorted({"fundamental", "market_structure", "meta_alpha", "macro_regime"} - set(group_ids))),
    )


def proposal(group_id: str, budget: int, *, hypothesis: str = "可证伪动量假设") -> GroupProposalPayload:
    candidates = tuple(
        GroupCandidateDraft(
            candidate_id=f"{group_id}.candidate.{index + 1}",
            expression="rank_cross_section(pct_change(close, 50))",
            variables=("close",),
            lookback_sessions=50,
            rationale="等待确定性机器验证",
        )
        for index in range(budget)
    )
    return GroupProposalPayload(
        group_id=group_id,
        hypothesis=hypothesis,
        economic_mechanism="价格信息扩散可能存在持续性",
        falsification_tests=("成本后表现为负则拒绝",),
        candidates=candidates,
        answer_level=AnswerLevel.CONCEPTUAL,
    )


class BarrierProvider:
    def __init__(self, count: int) -> None:
        self.barrier = threading.Barrier(count)
        self.calls: list[tuple[str, int, tuple[str, ...]]] = []
        self.lock = threading.Lock()

    def propose(self, *, context, intent, candidate_budget):
        with self.lock:
            self.calls.append((context.group_id, candidate_budget, context.tool_whitelist))
        self.barrier.wait(timeout=1.0)
        return proposal(context.group_id, candidate_budget)


def test_three_selected_groups_really_run_concurrently_with_isolated_budget_and_tools() -> None:
    selected = selection("meta_alpha", "market_structure", "fundamental")
    provider = BarrierProvider(3)
    result = ConcurrentResearchGroupRuntimeV2(provider=provider).run(
        selection=selected,
        intent=intent(),
        campaign_hash=CAMPAIGN_HASH,
    )
    assert tuple(branch.group_id for branch in result.branches) == (
        "fundamental",
        "market_structure",
        "meta_alpha",
    )
    assert all(branch.status is ConcurrentBranchStatus.COMPLETED for branch in result.branches)
    assert {call[0] for call in provider.calls} == {"fundamental", "market_structure", "meta_alpha"}
    assert {call[1] for call in provider.calls} == {1, 2, 3}
    assert all(branch.artifact_state == "DRAFT" for branch in result.branches)


def test_all_six_selected_groups_can_run_concurrently() -> None:
    group_ids = (
        "fundamental",
        "expectations_events",
        "market_structure",
        "alternative_data",
        "macro_regime",
        "meta_alpha",
    )
    provider = BarrierProvider(6)
    result = ConcurrentResearchGroupRuntimeV2(provider=provider, max_workers=6).run(
        selection=selection(*group_ids),
        intent=intent(),
        campaign_hash=CAMPAIGN_HASH,
    )
    assert tuple(branch.group_id for branch in result.branches) == tuple(sorted(group_ids))
    assert all(branch.status is ConcurrentBranchStatus.COMPLETED for branch in result.branches)
    assert {call[0] for call in provider.calls} == set(group_ids)


class DelayProvider:
    def __init__(self, failures: set[str] | None = None) -> None:
        self.failures = failures or set()
        self.calls: list[str] = []

    def propose(self, *, context, intent, candidate_budget):
        self.calls.append(context.group_id)
        time.sleep({"fundamental": 0.03, "market_structure": 0.01, "meta_alpha": 0.02}.get(context.group_id, 0.0))
        if context.group_id in self.failures:
            raise RuntimeError("provider branch failed")
        return proposal(context.group_id, candidate_budget)


def test_output_hash_and_order_do_not_depend_on_completion_order() -> None:
    selected = selection("meta_alpha", "fundamental", "market_structure")
    first = ConcurrentResearchGroupRuntimeV2(provider=DelayProvider()).run(
        selection=selected, intent=intent(), campaign_hash=CAMPAIGN_HASH
    )
    second = ConcurrentResearchGroupRuntimeV2(provider=DelayProvider()).run(
        selection=selected, intent=intent(), campaign_hash=CAMPAIGN_HASH
    )
    assert first.artifact_hash == second.artifact_hash
    assert tuple(item.group_id for item in first.branches) == tuple(sorted(item.group_id for item in first.branches))


def test_one_branch_failure_does_not_drag_independent_branches() -> None:
    selected = selection("fundamental", "market_structure", "meta_alpha")
    result = ConcurrentResearchGroupRuntimeV2(provider=DelayProvider({"market_structure"})).run(
        selection=selected, intent=intent(), campaign_hash=CAMPAIGN_HASH
    )
    by_group = {item.group_id: item for item in result.branches}
    assert by_group["market_structure"].status is ConcurrentBranchStatus.FAILED
    assert by_group["market_structure"].failure_code is ConcurrentBranchFailureCode.PROVIDER_FAILED
    assert by_group["fundamental"].status is ConcurrentBranchStatus.COMPLETED
    assert by_group["meta_alpha"].status is ConcurrentBranchStatus.COMPLETED


def test_unselected_groups_are_never_called() -> None:
    provider = DelayProvider()
    ConcurrentResearchGroupRuntimeV2(provider=provider).run(
        selection=selection("market_structure"), intent=intent(), campaign_hash=CAMPAIGN_HASH
    )
    assert provider.calls == ["market_structure"]


def test_resume_reuses_all_committed_terminal_branches_without_provider_calls() -> None:
    selected = selection("fundamental", "market_structure")
    provider = DelayProvider({"fundamental"})
    runtime = ConcurrentResearchGroupRuntimeV2(provider=provider)
    first = runtime.run(selection=selected, intent=intent(), campaign_hash=CAMPAIGN_HASH)
    call_count = len(provider.calls)
    committed = {item.group_id: item for item in first.committed_branches}
    second = runtime.run(
        selection=selected,
        intent=intent(),
        campaign_hash=CAMPAIGN_HASH,
        committed_branch_map=committed,
    )
    assert len(provider.calls) == call_count
    assert second.artifact_hash == first.artifact_hash


class SlowProvider:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def propose(self, *, context, intent, candidate_budget):
        self.calls.append(context.group_id)
        if context.group_id == "fundamental":
            time.sleep(0.20)
        else:
            time.sleep(0.005)
        return proposal(context.group_id, candidate_budget)


def test_timeout_is_terminal_and_fast_branch_still_completes() -> None:
    started = time.monotonic()
    result = ConcurrentResearchGroupRuntimeV2(
        provider=SlowProvider(), branch_timeout_seconds=0.05
    ).run(
        selection=selection("fundamental", "market_structure"),
        intent=intent(),
        campaign_hash=CAMPAIGN_HASH,
    )
    elapsed = time.monotonic() - started
    by_group = {item.group_id: item for item in result.branches}
    assert elapsed < 0.15
    assert by_group["fundamental"].status is ConcurrentBranchStatus.TIMED_OUT
    assert by_group["market_structure"].status is ConcurrentBranchStatus.COMPLETED


def test_sealed_or_privileged_context_key_is_rejected_before_provider_call() -> None:
    provider = DelayProvider()
    with pytest.raises(ContractValidationError):
        ConcurrentResearchGroupRuntimeV2(provider=provider).run(
            selection=selection("market_structure", context_keys=("question", "sealed_aggregate_path")),
            intent=intent(),
            campaign_hash=CAMPAIGN_HASH,
        )
    assert provider.calls == []


class ForbiddenPayloadProvider:
    def propose(self, *, context, intent, candidate_budget):
        return proposal(context.group_id, candidate_budget, hypothesis="read sealed_metric before promotion")


def test_sealed_payload_is_branch_failure_and_never_a_submission() -> None:
    result = ConcurrentResearchGroupRuntimeV2(provider=ForbiddenPayloadProvider()).run(
        selection=selection("market_structure"), intent=intent(), campaign_hash=CAMPAIGN_HASH
    )
    branch = result.branches[0]
    assert branch.status is ConcurrentBranchStatus.FAILED
    assert branch.failure_code is ConcurrentBranchFailureCode.FORBIDDEN_PAYLOAD
    assert branch.submission is None


class OverBudgetProvider:
    def propose(self, *, context, intent, candidate_budget):
        return proposal(context.group_id, 3)


def test_provider_cannot_exceed_per_group_candidate_budget() -> None:
    result = ConcurrentResearchGroupRuntimeV2(provider=OverBudgetProvider()).run(
        selection=selection("market_structure"), intent=intent(), campaign_hash=CAMPAIGN_HASH
    )
    assert result.branches[0].failure_code is ConcurrentBranchFailureCode.BUDGET_EXCEEDED


def test_changed_resume_identity_fails_closed_instead_of_reusing_commit() -> None:
    selected = selection("market_structure")
    provider = DelayProvider()
    runtime = ConcurrentResearchGroupRuntimeV2(provider=provider)
    first = runtime.run(selection=selected, intent=intent(), campaign_hash=CAMPAIGN_HASH)
    changed_intent = replace(intent(), factor_lookback_sessions=20)
    with pytest.raises(ContractValidationError):
        runtime.run(
            selection=selected,
            intent=changed_intent,
            campaign_hash=CAMPAIGN_HASH,
            committed_branch_map={item.group_id: item for item in first.committed_branches},
        )
