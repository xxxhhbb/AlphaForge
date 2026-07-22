from __future__ import annotations

from veriquant.research_campaign import MethodModel, ResearchDepth, ResearchMode
from veriquant.research_group_runtime import DeterministicResearchGroupSelector, GroupSelectionContext, ResearchGroupProposalRuntime
from veriquant.research_os import ResearchIntent


H = "sha256:" + "f" * 64


def selection():
    return DeterministicResearchGroupSelector().select(GroupSelectionContext(campaign_hash=H, mode=ResearchMode.NEW_ALPHA_EXPLORATION, method_models=(MethodModel.HYPOTHESIS_DRIVEN,), question_tags=("momentum",), available_fields=("close", "volume", "adv", "registered_factor_summary", "macro"), max_groups=3, max_candidates_per_group=2, context_keys=("question", "data_manifest")))


def intent():
    return ResearchIntent(question="挖掘50日大动量", mode=ResearchMode.NEW_ALPHA_EXPLORATION, depth=ResearchDepth.STANDARD_RESEARCH, factor_lookback_sessions=50, label_horizon_sessions=5, requested_outputs=("formal_report",))


def test_only_selected_groups_run_and_each_returns_candidates() -> None:
    result = ResearchGroupProposalRuntime().run(selection=selection(), intent=intent(), campaign_hash=H)
    assert tuple(item.group_id for item in result.branches) == tuple(sorted(item.group_id for item in selection().assignments))
    assert all(1 <= len(item.proposal.candidates) <= 2 for item in result.branches)


def test_50_day_request_is_present_in_momentum_group_drafts() -> None:
    result = ResearchGroupProposalRuntime().run(selection=selection(), intent=intent(), campaign_hash=H)
    momentum = next(item for item in result.branches if item.group_id == "market_structure")
    assert all("50" in candidate.expression for candidate in momentum.proposal.candidates)


def test_one_provider_failure_does_not_erase_independent_branches() -> None:
    class Provider:
        def propose(self, *, context, intent, candidate_budget):
            if context.group_id == "market_structure":
                raise TimeoutError("timeout")
            from veriquant.research_group_runtime import DeterministicGroupProposalProvider
            return DeterministicGroupProposalProvider().propose(context=context, intent=intent, candidate_budget=candidate_budget)

    result = ResearchGroupProposalRuntime(Provider()).run(selection=selection(), intent=intent(), campaign_hash=H)
    statuses = {item.group_id: item.status.value for item in result.branches}
    assert statuses["market_structure"] == "FAILED"
    assert "COMPLETED" in statuses.values()


def test_group_outputs_are_drafts_not_official() -> None:
    result = ResearchGroupProposalRuntime().run(selection=selection(), intent=intent(), campaign_hash=H)
    assert all(item.proposal.answer_level.value != "OFFICIAL_EVALUATED" for item in result.branches if item.proposal)
