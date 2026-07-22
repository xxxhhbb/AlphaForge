from __future__ import annotations

from datetime import datetime, timezone

import pytest

from veriquant.contracts import ContractValidationError, FailureCode
from veriquant.llm_desks.models import (
    DeskId,
    ExpectedSign,
    FactorDraftFill,
    HypothesisFill,
    LLMDeskStatus,
)
from veriquant.research_campaign import ResearchDepth, ResearchMode
from veriquant.research_group_llm_adapter import (
    DeterministicResearchGroupFallbackProvider,
    ProviderMode,
    ResearchGroupLLMAdapter,
    ResearchGroupProviderRequest,
)
from veriquant.research_group_runtime import ResearchGroupProposalRuntime
from veriquant.research_group_runtime.runtime import GroupBranchStatus
from veriquant.research_group_runtime.selector import GroupSelectionResult, SelectedGroupContext
from veriquant.research_os import ResearchGroupAssignment, ResearchIntent


CAMPAIGN_HASH = "sha256:" + "a" * 64


def _intent(lookback: int = 50) -> ResearchIntent:
    return ResearchIntent(
        question="挖一下最近50天大动量的因子",
        mode=ResearchMode.NEW_ALPHA_EXPLORATION,
        depth=ResearchDepth.STANDARD_RESEARCH,
        factor_lookback_sessions=lookback,
        label_horizon_sessions=5,
        requested_outputs=("candidate_report",),
    )


def _context(group_id: str = "market_structure") -> SelectedGroupContext:
    return SelectedGroupContext(
        group_id=group_id,
        campaign_hash=CAMPAIGN_HASH,
        allowed_context_keys=("visible_market_summary",),
        tool_whitelist=("read_market_data",),
    )


def _assignment(group_id: str, budget: int = 1) -> ResearchGroupAssignment:
    return ResearchGroupAssignment(
        group_id=group_id,
        role="selected",
        score=0.9,
        reasons=("fits question",),
        tool_whitelist=("read_market_data",),
        candidate_budget=budget,
    )


class GoodMappingProvider:
    provider_kind = ProviderMode.REAL_LLM

    def complete_proposal(self, request: ResearchGroupProviderRequest):
        return {
            "state": "DRAFT",
            "group_id": request.context.group_id,
            "hypothesis": "50日价格强势可能代表机构趋势跟随，但必须经机器证据复核。",
            "economic_mechanism": "趋势延续可能来自资金再平衡和信息扩散延迟。",
            "falsification_tests": "成本后收益转负；子时期不稳定",
            "candidates": [
                {
                    "name": "momentum_50d_rank",
                    "expression": "rank_cross_section(pct_change(close, 50))",
                    "variables": "close, volume",
                    "lookback_sessions": 50,
                    "rationale": "保持用户指定的50日回看，仅作为DRAFT候选。",
                }
            ],
            "runtime_mode": "REAL_LLM",
        }


def test_mapping_provider_repairs_safe_arrays_and_preserves_50d_lookback() -> None:
    adapter = ResearchGroupLLMAdapter(
        GoodMappingProvider(),
        selected_group_ids=("market_structure",),
        provider_mode=ProviderMode.REAL_LLM,
    )

    proposal = adapter.propose(context=_context(), intent=_intent(50), candidate_budget=1)

    assert proposal.group_id == "market_structure"
    assert proposal.answer_level.value == "CONCEPTUAL"
    assert proposal.falsification_tests == ("成本后收益转负", "子时期不稳定")
    assert proposal.candidates[0].lookback_sessions == 50
    assert "pct_change(close, 50)" in proposal.candidates[0].expression
    assert proposal.candidates[0].variables == ("close", "volume")


class ForbiddenProvider:
    provider_kind = ProviderMode.REAL_LLM

    def __init__(self, extra):
        self.extra = extra

    def complete_proposal(self, request: ResearchGroupProviderRequest):
        payload = GoodMappingProvider().complete_proposal(request)
        payload.update(self.extra)
        return payload


@pytest.mark.parametrize(
    "extra",
    [
        {"official_metrics": {"rank_ic": 0.9}},
        {"promotion": "PROMOTE_CHALLENGER"},
        {"risk_approval": "APPROVE"},
        {"target_weights": {"000001.SZ": 0.1}},
        {"orders": [{"symbol": "000001.SZ", "qty": 100}]},
        {"sealed_metrics": {"rank": 1}},
        {"tool_whitelist": ("shell",)},
    ],
)
def test_provider_cannot_write_privileged_fields(extra) -> None:
    adapter = ResearchGroupLLMAdapter(
        ForbiddenProvider(extra),
        selected_group_ids=("market_structure",),
        provider_mode=ProviderMode.REAL_LLM,
    )
    with pytest.raises(ContractValidationError) as error:
        adapter.propose(context=_context(), intent=_intent(), candidate_budget=1)
    assert error.value.code is FailureCode.UNAUTHORIZED_CAPABILITY


class BadStateProvider:
    provider_kind = ProviderMode.REAL_LLM

    def complete_proposal(self, request: ResearchGroupProviderRequest):
        payload = GoodMappingProvider().complete_proposal(request)
        payload["state"] = "VALIDATED_RESEARCH"
        return payload


def test_llm_output_must_remain_draft() -> None:
    adapter = ResearchGroupLLMAdapter(
        BadStateProvider(),
        selected_group_ids=("market_structure",),
        provider_mode=ProviderMode.REAL_LLM,
    )
    with pytest.raises(ContractValidationError) as error:
        adapter.propose(context=_context(), intent=_intent(), candidate_budget=1)
    assert error.value.code is FailureCode.INVALID_STATE


class WrongLookbackProvider:
    provider_kind = ProviderMode.REAL_LLM

    def complete_proposal(self, request: ResearchGroupProviderRequest):
        payload = GoodMappingProvider().complete_proposal(request)
        payload["candidates"][0]["lookback_sessions"] = 20
        return payload


def test_requested_lookback_cannot_be_silently_changed() -> None:
    adapter = ResearchGroupLLMAdapter(
        WrongLookbackProvider(),
        selected_group_ids=("market_structure",),
        provider_mode=ProviderMode.REAL_LLM,
    )
    with pytest.raises(ContractValidationError) as error:
        adapter.propose(context=_context(), intent=_intent(50), candidate_budget=1)
    assert error.value.code is FailureCode.PROVIDER_SCHEMA_REJECTED
    assert error.value.details["expected"] == 50


class WrongGroupProvider:
    provider_kind = ProviderMode.REAL_LLM

    def complete_proposal(self, request: ResearchGroupProviderRequest):
        payload = GoodMappingProvider().complete_proposal(request)
        payload["group_id"] = "fundamental"
        return payload


def test_unselected_or_wrong_group_is_rejected() -> None:
    adapter = ResearchGroupLLMAdapter(
        WrongGroupProvider(),
        selected_group_ids=("market_structure",),
        provider_mode=ProviderMode.REAL_LLM,
    )
    with pytest.raises(ContractValidationError) as error:
        adapter.propose(context=_context(), intent=_intent(), candidate_budget=1)
    assert error.value.code is FailureCode.INVALID_FIELD_OWNER

    with pytest.raises(ContractValidationError) as selected_error:
        ResearchGroupLLMAdapter(
            GoodMappingProvider(),
            selected_group_ids=("fundamental",),
            provider_mode=ProviderMode.REAL_LLM,
        ).propose(context=_context(), intent=_intent(), candidate_budget=1)
    assert selected_error.value.code is FailureCode.UNAUTHORIZED_CAPABILITY


def test_deterministic_fallback_is_explicit_and_not_mislabeled_real_llm() -> None:
    with pytest.raises(ContractValidationError) as error:
        ResearchGroupLLMAdapter(
            DeterministicResearchGroupFallbackProvider(),
            selected_group_ids=("market_structure",),
            provider_mode=ProviderMode.REAL_LLM,
        )
    assert error.value.code is FailureCode.UNAUTHORIZED_CAPABILITY

    adapter = ResearchGroupLLMAdapter(
        DeterministicResearchGroupFallbackProvider(),
        selected_group_ids=("market_structure",),
        provider_mode=ProviderMode.DETERMINISTIC_FALLBACK,
    )
    proposal = adapter.propose(context=_context(), intent=_intent(50), candidate_budget=1)
    assert proposal.candidates[0].lookback_sessions == 50


class TimeoutForFundamentalProvider(GoodMappingProvider):
    def complete_proposal(self, request: ResearchGroupProviderRequest):
        if request.context.group_id == "fundamental":
            raise TimeoutError("provider stalled")
        return super().complete_proposal(request)


def test_provider_timeout_is_branch_local_in_existing_runtime() -> None:
    selection = GroupSelectionResult(
        assignments=(
            _assignment("fundamental"),
            _assignment("market_structure"),
        ),
        isolated_contexts=(
            _context("fundamental"),
            _context("market_structure"),
        ),
        excluded_group_ids=("meta_alpha",),
    )
    runtime = ResearchGroupProposalRuntime(
        provider=ResearchGroupLLMAdapter(
            TimeoutForFundamentalProvider(),
            selected_group_ids=("fundamental", "market_structure"),
            provider_mode=ProviderMode.REAL_LLM,
        )
    )

    result = runtime.run(selection=selection, intent=_intent(), campaign_hash=CAMPAIGN_HASH)

    by_group = {branch.group_id: branch for branch in result.branches}
    assert by_group["fundamental"].status is GroupBranchStatus.FAILED
    assert "provider timed out" in by_group["fundamental"].message
    assert by_group["market_structure"].status is GroupBranchStatus.COMPLETED
    assert by_group["market_structure"].proposal is not None


def test_same_provider_input_produces_stable_payload_hash() -> None:
    adapter = ResearchGroupLLMAdapter(
        GoodMappingProvider(),
        selected_group_ids=("market_structure",),
        provider_mode=ProviderMode.REAL_LLM,
    )

    first = adapter.propose(context=_context(), intent=_intent(50), candidate_budget=1)
    second = adapter.propose(context=_context(), intent=_intent(50), candidate_budget=1)

    assert first.artifact_hash == second.artifact_hash


class FillProvider:
    provider_kind = ProviderMode.REAL_LLM

    def complete_proposal(self, request: ResearchGroupProviderRequest):
        hypothesis = HypothesisFill(
            state=LLMDeskStatus.DRAFT,
            desk_id=DeskId.MARKET_STRUCTURE,
            statement="50日强势股票可能存在短期延续。",
            economic_mechanism="信息扩散和资金趋势跟随形成可证伪候选。",
            expected_sign=ExpectedSign.POSITIVE,
            universe=("cn_ashare_sample",),
            horizon="5d",
            decision_time=datetime(2026, 7, 19, tzinfo=timezone.utc),
            availability_assumptions=("仅使用decision_time前可得价格成交量",),
            falsification_tests=("成本后收益转负则修订",),
        )
        factor = FactorDraftFill(
            state=LLMDeskStatus.DRAFT,
            desk_id=DeskId.MARKET_STRUCTURE,
            name="momentum_50d_rank",
            description="50日收益横截面排序草案。",
            expression="rank_cross_section(pct_change(close, 50))",
            variables=("close",),
            lookback_bars=50,
        )
        return (hypothesis, (factor,))


def test_existing_llm_desk_fills_can_be_adapted_to_group_payload() -> None:
    adapter = ResearchGroupLLMAdapter(
        FillProvider(),
        selected_group_ids=("market_structure",),
        provider_mode=ProviderMode.REAL_LLM,
    )

    proposal = adapter.propose(context=_context(), intent=_intent(50), candidate_budget=1)

    assert proposal.group_id == "market_structure"
    assert proposal.candidates[0].candidate_id == "market_structure_candidate_1"
    assert proposal.candidates[0].lookback_sessions == 50
