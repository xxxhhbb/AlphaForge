from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from veriquant.alphaforge_llm import AlphaForgeLLMConfigLoader
from veriquant.contracts import ContractValidationError, FailureCode
from veriquant.research_campaign import ResearchDepth, ResearchMode
from veriquant.research_group_real_llm_v2 import (
    AlphaForgeRealLLMTransport,
    GroupAttemptStatus,
    GroupExecutionPolicy,
    GroupLLMCallResponse,
    GroupProposalProvenance,
    SelectedGroupRealLLMProposalProvider,
)
from veriquant.research_group_real_llm_v2.alphaforge_transport import _messages
from veriquant.llm_desks.safety import assert_no_forbidden_content
from veriquant.research_group_runtime import ResearchGroupProposalRuntime
from veriquant.research_group_runtime.runtime import GroupBranchStatus
from veriquant.research_group_runtime.selector import GroupSelectionResult, SelectedGroupContext
from veriquant.research_os import ResearchGroupAssignment, ResearchIntent


CAMPAIGN_HASH = "sha256:" + "a" * 64


def intent() -> ResearchIntent:
    return ResearchIntent(
        question="挖一下最近50天大动量的因子",
        mode=ResearchMode.NEW_ALPHA_EXPLORATION,
        depth=ResearchDepth.STANDARD_RESEARCH,
        factor_lookback_sessions=50,
        label_horizon_sessions=5,
        requested_outputs=("candidate_report",),
    )


def context(group: str) -> SelectedGroupContext:
    return SelectedGroupContext(
        group_id=group,
        campaign_hash=CAMPAIGN_HASH,
        allowed_context_keys=("visible_market_summary",),
        tool_whitelist=("read_market_data",),
    )


def policy(group: str, **overrides) -> GroupExecutionPolicy:
    values = dict(
        group_id=group,
        allowed_fields=("close", "volume", "market_cap"),
        tool_whitelist=("read_market_data",),
        max_tokens=256,
        max_iterations=2,
        timeout_seconds=0.2,
        candidate_budget=2,
        max_revisions=1,
    )
    values.update(overrides)
    return GroupExecutionPolicy(**values)


def valid_payload(group: str, *, expression: str = "rank_cross_section(pct_change(close, 50))", count: int = 1):
    return {
        "state": "DRAFT",
        "group_id": group,
        "hypothesis": "50日价格强势可能包含可证伪的趋势延续信息。",
        "economic_mechanism": "信息扩散和资金再平衡可能使趋势短期延续。",
        "falsification_tests": ["提高成本后净表现转负", "跨子时期不稳定"],
        "candidates": [
            {
                "name": f"momentum_50d_{index}",
                "expression": expression,
                "variables": ["close"],
                "lookback_sessions": 50,
                "rationale": "仅为待机器验证的DRAFT候选。",
            }
            for index in range(count)
        ],
        "runtime_mode": "REAL_LLM",
    }


class FakeTransport:
    available = True

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def complete_group_proposal(self, request):
        self.calls.append(request)
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return GroupLLMCallResponse(payload=result, input_tokens=10, output_tokens=20)


def build(transport, groups=("market_structure",), policies=None):
    return SelectedGroupRealLLMProposalProvider(
        transport=transport,
        selected_group_ids=groups,
        policies=policies or tuple(policy(group) for group in groups),
    )


def test_provider_accepts_six_isolated_research_groups() -> None:
    groups = (
        "fundamental",
        "expectations_events",
        "market_structure",
        "alternative_data",
        "macro_regime",
        "meta_alpha",
    )
    provider = build(FakeTransport([]), groups=groups)
    assert provider.selected_group_ids == groups


def test_real_transport_prompt_itself_passes_the_outbound_safety_guard() -> None:
    request = SimpleNamespace(
        intent=intent(),
        context=context("market_structure"),
        policy=policy("market_structure"),
        attempt_number=1,
        repair_code=None,
        prior_payload_hash=None,
    )
    messages = _messages(request)
    assert len(messages) == 2
    for message in messages:
        assert_no_forbidden_content(message["content"], where="test.outbound_message")
    material = __import__("json").loads(messages[1]["content"])
    assert "pct_change" in material["allowed_operators"]
    assert "rank_cross_section(pct_change(close, 50))" in material[
        "safe_expression_templates"
    ]
    assert all(
        "industry" not in template and "macro" not in template
        for template in material["safe_expression_templates"]
    )


def test_selected_group_returns_one_to_three_drafts_and_preserves_50_days() -> None:
    transport = FakeTransport([valid_payload("market_structure", count=2)])
    provider = build(transport)
    proposal = provider.propose(context=context("market_structure"), intent=intent(), candidate_budget=2)

    assert len(proposal.candidates) == 2
    assert all(item.lookback_sessions == 50 for item in proposal.candidates)
    assert proposal.answer_level.value == "CONCEPTUAL"
    assert provider.attempts[0].provenance is GroupProposalProvenance.REAL_LLM
    assert provider.attempts[0].tokens_used == 30


def test_unselected_group_and_undeclared_tools_fail_closed() -> None:
    provider = build(FakeTransport([valid_payload("market_structure")]))
    with pytest.raises(ContractValidationError) as exc:
        provider.propose(context=context("fundamental"), intent=intent(), candidate_budget=1)
    assert exc.value.code is FailureCode.UNAUTHORIZED_CAPABILITY

    bad_context = SelectedGroupContext(
        group_id="market_structure",
        campaign_hash=CAMPAIGN_HASH,
        allowed_context_keys=("visible_market_summary",),
        tool_whitelist=("query_documents",),
    )
    with pytest.raises(ContractValidationError) as tool_exc:
        provider.propose(context=bad_context, intent=intent(), candidate_budget=1)
    assert tool_exc.value.code is FailureCode.UNAUTHORIZED_CAPABILITY


@pytest.mark.parametrize(
    "field,value",
    [
        ("official_metrics", {"rank_ic": 0.9}),
        ("promotion", "CHALLENGER"),
        ("sealed_metrics", {"score": 1}),
        ("positions", {"000001.SZ": 0.1}),
        ("orders", [{"symbol": "000001.SZ"}]),
    ],
)
def test_llm_cannot_claim_authority(field, value) -> None:
    payload = valid_payload("market_structure")
    payload[field] = value
    provider = build(FakeTransport([payload]))
    with pytest.raises(ContractValidationError) as exc:
        provider.propose(context=context("market_structure"), intent=intent(), candidate_budget=1)
    assert exc.value.code is FailureCode.UNAUTHORIZED_CAPABILITY


def test_unknown_expression_gets_exactly_one_bounded_revision() -> None:
    bad = valid_payload("market_structure", expression="made_up_operator(close, 50)")
    good = valid_payload("market_structure")
    transport = FakeTransport([bad, good])
    provider = build(transport)

    proposal = provider.propose(context=context("market_structure"), intent=intent(), candidate_budget=1)

    assert proposal.candidates[0].expression == "rank_cross_section(pct_change(close, 50))"
    assert len(transport.calls) == 2
    assert transport.calls[1].repair_code == FailureCode.UNKNOWN_FIELD.value
    assert transport.calls[1].prior_payload_hash.startswith("sha256:")
    assert provider.attempts[0].revisions_used == 1


def test_second_invalid_expression_fails_without_third_call() -> None:
    transport = FakeTransport([
        valid_payload("market_structure", expression="made_up_operator(close, 50)"),
        valid_payload("market_structure", expression="still_unknown(close, 50)"),
    ])
    provider = build(transport)
    with pytest.raises(ContractValidationError):
        provider.propose(context=context("market_structure"), intent=intent(), candidate_budget=1)
    assert len(transport.calls) == 2
    assert provider.attempts[0].status is GroupAttemptStatus.FAILED


class UnavailableTransport:
    available = False

    def complete_group_proposal(self, request):  # pragma: no cover - must not be called
        raise AssertionError("unavailable provider must not be called")


def test_unavailable_provider_uses_explicit_deterministic_fallback() -> None:
    provider = build(UnavailableTransport())
    proposal = provider.propose(context=context("market_structure"), intent=intent(), candidate_budget=1)
    attempt = provider.attempts[0]
    assert proposal.candidates[0].lookback_sessions == 50
    assert attempt.status is GroupAttemptStatus.FALLBACK_COMPLETED
    assert attempt.provenance is GroupProposalProvenance.DETERMINISTIC_FALLBACK
    assert attempt.provider_calls == 0


class TimeoutOneGroupTransport:
    available = True

    def complete_group_proposal(self, request):
        if request.context.group_id == "fundamental":
            time.sleep(0.04)
        return GroupLLMCallResponse(payload=valid_payload(request.context.group_id), input_tokens=1, output_tokens=1)


def assignment(group: str) -> ResearchGroupAssignment:
    return ResearchGroupAssignment(
        group_id=group,
        role="selected",
        score=0.8,
        reasons=("fit",),
        tool_whitelist=("read_market_data",),
        candidate_budget=1,
    )


def test_group_timeout_is_branch_local() -> None:
    groups = ("fundamental", "market_structure")
    provider = build(
        TimeoutOneGroupTransport(),
        groups=groups,
        policies=(policy("fundamental", timeout_seconds=0.01), policy("market_structure", timeout_seconds=0.2)),
    )
    selection = GroupSelectionResult(
        assignments=tuple(assignment(group) for group in groups),
        isolated_contexts=tuple(context(group) for group in groups),
        excluded_group_ids=("meta_alpha",),
    )
    result = ResearchGroupProposalRuntime(provider=provider).run(
        selection=selection,
        intent=intent(),
        campaign_hash=CAMPAIGN_HASH,
    )
    by_group = {item.group_id: item for item in result.branches}
    assert by_group["fundamental"].status is GroupBranchStatus.FAILED
    assert by_group["market_structure"].status is GroupBranchStatus.COMPLETED


def test_token_and_candidate_budgets_fail_closed() -> None:
    provider = build(FakeTransport([valid_payload("market_structure")]), policies=(policy("market_structure", max_tokens=5),))
    with pytest.raises(ContractValidationError) as token_exc:
        provider.propose(context=context("market_structure"), intent=intent(), candidate_budget=1)
    assert token_exc.value.code is FailureCode.PROVIDER_BUDGET_EXCEEDED

    provider = build(FakeTransport([valid_payload("market_structure")]), policies=(policy("market_structure", candidate_budget=1),))
    with pytest.raises(ContractValidationError) as candidate_exc:
        provider.propose(context=context("market_structure"), intent=intent(), candidate_budget=2)
    assert candidate_exc.value.code is FailureCode.PROVIDER_BUDGET_EXCEEDED


def test_dotenv_smoke_hook_reuses_narrow_loader_without_secret_in_public_view(tmp_path: Path) -> None:
    dotenv = tmp_path / ".env"
    dotenv.write_text(
        "ENABLE_LLM_RESEARCH=1\n"
        "ALPHAFORGE_LLM_ENDPOINT_URL=https://llm.example.test/v1\n"
        "ALPHAFORGE_LLM_API_KEY=do-not-serialize\n"
        "ALPHAFORGE_LLM_MODEL=fake-model\n"
        "ALPHAFORGE_LLM_APPROVED_HOSTS=llm.example.test\n",
        encoding="utf-8",
    )

    configuration = AlphaForgeLLMConfigLoader(environ={}, dotenv_path=dotenv).load()
    transport = AlphaForgeRealLLMTransport(configuration=configuration, client=SimpleNamespace())

    assert transport.available is True
    assert transport.public_view["model"] == "fake-model"
    assert "do-not-serialize" not in repr(transport)
    assert "do-not-serialize" not in str(dict(transport.public_view))


def test_same_inputs_produce_same_proposal_and_attempt_hashes() -> None:
    first = build(FakeTransport([valid_payload("market_structure")]))
    second = build(FakeTransport([valid_payload("market_structure")]))
    p1 = first.propose(context=context("market_structure"), intent=intent(), candidate_budget=1)
    p2 = second.propose(context=context("market_structure"), intent=intent(), candidate_budget=1)
    assert p1.artifact_hash == p2.artifact_hash
    assert first.attempts[0].artifact_hash == second.attempts[0].artifact_hash
