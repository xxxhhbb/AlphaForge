from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any, Mapping

import pytest

from src.veriquant.contracts import ContractValidationError, FailureCode
from src.veriquant.llm_desks.api import (
    BoundedReActExplorer,
    DeskId,
    DeskLLMConfig,
    DeterministicProposalProvider,
    OpenAICompatibleConfig,
    OpenAICompatibleProvider,
    OpenAICompatibleResponse,
    ProviderBudget,
    ProviderSession,
    ReactLimits,
    ResearchQuestionView,
    build_default_desk_configs,
    build_default_tools_for_desk,
)


def question() -> ResearchQuestionView:
    return ResearchQuestionView(
        question_id="q.parallel.llm",
        text="Find visible research drafts only.",
        universe=("AAA", "BBB"),
        horizon="5d",
        decision_time=datetime(2026, 1, 5, tzinfo=timezone.utc),
        availability_assumptions=("available_at <= decision_time",),
    )


def desk() -> DeskLLMConfig:
    return build_default_desk_configs()[0]


class SequenceClient:
    def __init__(self, responses: list[OpenAICompatibleResponse], *, delay: float = 0.0) -> None:
        self.responses = list(responses)
        self.delay = delay
        self.calls: list[str] = []

    def complete_json(
        self,
        *,
        messages: tuple[Mapping[str, str], ...],
        model: str,
        timeout_seconds: float,
        max_tokens: int,
        temperature: float,
        top_p: float,
        response_format: str,
        extra: Mapping[str, Any],
    ) -> OpenAICompatibleResponse:
        self.calls.append(messages[0]["content"])
        if self.delay:
            time.sleep(self.delay)
        if not self.responses:
            raise RuntimeError("no response")
        return self.responses.pop(0)


def valid_hypothesis() -> OpenAICompatibleResponse:
    return OpenAICompatibleResponse(
        payload={
            "statement": "Visible valuation spread may forecast returns.",
            "economic_mechanism": "Released accounting signals can diffuse slowly.",
            "expected_sign": "POSITIVE",
            "falsification_tests": ["Reject if validation net evidence is weak."],
        },
        input_tokens=2,
        output_tokens=3,
    )


def provider_with(responses: list[OpenAICompatibleResponse], *, budget: ProviderBudget | None = None) -> tuple[OpenAICompatibleProvider, ProviderSession]:
    client = SequenceClient(responses)
    config = OpenAICompatibleConfig(model="offline-fake")
    local_desk = DeskLLMConfig(
        desk_id=desk().desk_id,
        role=desk().role,
        context=desk().context,
        model=config.model,
        budget=budget or ProviderBudget(model=config.model, max_calls=4, max_retries=1),
        tool_names=desk().tool_names,
        artifact_namespace=desk().artifact_namespace,
    )
    return OpenAICompatibleProvider(client=client, config=config), ProviderSession(desk_id=local_desk.desk_id, budget=local_desk.budget)


def test_deterministic_provider_returns_draft_and_trace() -> None:
    local_desk = desk()
    session = ProviderSession(desk_id=local_desk.desk_id, budget=local_desk.budget)
    provider = DeterministicProposalProvider()
    tools = build_default_tools_for_desk(local_desk.desk_id)
    hypothesis = provider.propose_hypothesis(
        desk=local_desk,
        question=question(),
        tools=tools,
        session=session,
        explorer=BoundedReActExplorer(limits=ReactLimits(max_tool_calls=1)),
    )

    assert hypothesis.state.value == "DRAFT"
    assert hypothesis.desk_id is DeskId.FUNDAMENTAL
    trace = session.snapshot()
    assert len(trace.events) == 1
    assert trace.events[0].prompt_hash.startswith("sha256:")
    assert "api" not in trace.canonical_json().lower()


def test_openai_compatible_provider_retries_schema_violation_then_succeeds() -> None:
    bad = OpenAICompatibleResponse(
        payload={"statement": "ok", "official_metrics": {"rank_ic": 1.0}},
        input_tokens=1,
        output_tokens=1,
    )
    local_desk = DeskLLMConfig(
        desk_id=desk().desk_id,
        role=desk().role,
        context=desk().context,
        model="offline-fake",
        budget=ProviderBudget(model="offline-fake", max_calls=4, max_retries=1),
        tool_names=desk().tool_names,
        artifact_namespace=desk().artifact_namespace,
    )
    client = SequenceClient([bad, valid_hypothesis()])
    provider = OpenAICompatibleProvider(client=client, config=OpenAICompatibleConfig(model="offline-fake"))
    session = ProviderSession(desk_id=local_desk.desk_id, budget=local_desk.budget)

    result = provider.propose_hypothesis(
        desk=local_desk,
        question=question(),
        tools=build_default_tools_for_desk(local_desk.desk_id),
        session=session,
        explorer=BoundedReActExplorer(limits=ReactLimits(max_tool_calls=1)),
    )

    assert result.state.value == "DRAFT"
    assert [event.failure_code for event in session.snapshot().events if event.failure_code] == [
        FailureCode.UNAUTHORIZED_CAPABILITY.value
    ]


def test_openai_provider_normalizes_safe_text_array_shape_drift() -> None:
    hypothesis_response = OpenAICompatibleResponse(
        payload={
            "statement": "Visible valuation spread may forecast returns.",
            "economic_mechanism": "Released accounting signals can diffuse slowly.",
            "expected_sign": "positive",
            "falsification_tests": "Reject if validation net evidence is weak.",
        },
        input_tokens=2,
        output_tokens=3,
    )
    factor_response = OpenAICompatibleResponse(
        payload={
            "name": "valuation_pressure",
            "description": "Draft-only valuation pressure proxy.",
            "expression": "rank_cross_section(divide_safe(book_value, market_cap))",
            "variables": "book_value, market_cap",
            "lookback_bars": "20",
            "neutralization": "none",
        },
        input_tokens=2,
        output_tokens=3,
    )
    client = SequenceClient([hypothesis_response, factor_response])
    local_desk = DeskLLMConfig(
        desk_id=desk().desk_id,
        role=desk().role,
        context=desk().context,
        model="offline-fake",
        budget=ProviderBudget(model="offline-fake", max_calls=4, max_retries=0),
        tool_names=desk().tool_names,
        artifact_namespace=desk().artifact_namespace,
    )
    provider = OpenAICompatibleProvider(client=client, config=OpenAICompatibleConfig(model="offline-fake"))
    session = ProviderSession(desk_id=local_desk.desk_id, budget=local_desk.budget)
    tools = build_default_tools_for_desk(local_desk.desk_id)

    hypothesis = provider.propose_hypothesis(
        desk=local_desk,
        question=question(),
        tools=tools,
        session=session,
        explorer=BoundedReActExplorer(limits=ReactLimits(max_tool_calls=1)),
    )
    factor = provider.draft_factor(
        desk=local_desk,
        question=question(),
        hypothesis=hypothesis,
        tools=tools,
        session=session,
        explorer=BoundedReActExplorer(limits=ReactLimits(max_tool_calls=1)),
    )

    assert hypothesis.expected_sign.value == "POSITIVE"
    assert hypothesis.falsification_tests == ("Reject if validation net evidence is weak.",)
    assert factor.variables == ("book_value", "market_cap")
    assert factor.neutralization == ()
    assert factor.lookback_bars == 20


def test_openai_provider_retries_empty_factor_variables_inside_provider_boundary() -> None:
    invalid_factor = OpenAICompatibleResponse(
        payload={
            "name": "valuation_pressure",
            "description": "Draft-only valuation pressure proxy.",
            "expression": "rank_cross_section(divide_safe(book_value, market_cap))",
            "variables": [],
            "lookback_bars": 20,
            "neutralization": [],
        },
        input_tokens=2,
        output_tokens=3,
    )
    valid_factor = OpenAICompatibleResponse(
        payload={
            "name": "valuation_pressure",
            "description": "Draft-only valuation pressure proxy.",
            "expression": "rank_cross_section(divide_safe(book_value, market_cap))",
            "variables": ["book_value", "market_cap"],
            "lookback_bars": 20,
            "neutralization": [],
        },
        input_tokens=2,
        output_tokens=3,
    )
    client = SequenceClient([valid_hypothesis(), invalid_factor, valid_factor])
    local_desk = DeskLLMConfig(
        desk_id=desk().desk_id,
        role=desk().role,
        context=desk().context,
        model="offline-fake",
        budget=ProviderBudget(model="offline-fake", max_calls=5, max_retries=1),
        tool_names=desk().tool_names,
        artifact_namespace=desk().artifact_namespace,
    )
    provider = OpenAICompatibleProvider(client=client, config=OpenAICompatibleConfig(model="offline-fake"))
    session = ProviderSession(desk_id=local_desk.desk_id, budget=local_desk.budget)
    tools = build_default_tools_for_desk(local_desk.desk_id)

    hypothesis = provider.propose_hypothesis(
        desk=local_desk,
        question=question(),
        tools=tools,
        session=session,
        explorer=BoundedReActExplorer(limits=ReactLimits(max_tool_calls=1)),
    )
    factor = provider.draft_factor(
        desk=local_desk,
        question=question(),
        hypothesis=hypothesis,
        tools=tools,
        session=session,
        explorer=BoundedReActExplorer(limits=ReactLimits(max_tool_calls=1)),
    )

    assert factor.variables == ("book_value", "market_cap")
    failures = [event.failure_code for event in session.snapshot().events if event.failure_code]
    assert failures == [FailureCode.PROVIDER_SCHEMA_REJECTED.value]


def test_openai_provider_timeout_is_typed() -> None:
    local_desk = DeskLLMConfig(
        desk_id=desk().desk_id,
        role=desk().role,
        context=desk().context,
        model="offline-fake",
        budget=ProviderBudget(model="offline-fake", timeout_seconds=0.01, max_calls=1, max_retries=0),
        tool_names=desk().tool_names,
        artifact_namespace=desk().artifact_namespace,
    )
    provider = OpenAICompatibleProvider(
        client=SequenceClient([valid_hypothesis()], delay=0.05),
        config=OpenAICompatibleConfig(model="offline-fake"),
    )

    with pytest.raises(ContractValidationError) as exc:
        provider.propose_hypothesis(
            desk=local_desk,
            question=question(),
            tools=build_default_tools_for_desk(local_desk.desk_id),
            session=ProviderSession(desk_id=local_desk.desk_id, budget=local_desk.budget),
            explorer=BoundedReActExplorer(limits=ReactLimits(max_tool_calls=1)),
        )

    assert exc.value.code is FailureCode.PROVIDER_TIMEOUT


def test_openai_provider_token_budget_exceeded() -> None:
    local_desk = DeskLLMConfig(
        desk_id=desk().desk_id,
        role=desk().role,
        context=desk().context,
        model="offline-fake",
        budget=ProviderBudget(model="offline-fake", max_tokens=2, max_calls=1, max_retries=0),
        tool_names=desk().tool_names,
        artifact_namespace=desk().artifact_namespace,
    )
    provider = OpenAICompatibleProvider(
        client=SequenceClient([valid_hypothesis()]),
        config=OpenAICompatibleConfig(model="offline-fake"),
    )

    with pytest.raises(ContractValidationError) as exc:
        provider.propose_hypothesis(
            desk=local_desk,
            question=question(),
            tools=build_default_tools_for_desk(local_desk.desk_id),
            session=ProviderSession(desk_id=local_desk.desk_id, budget=local_desk.budget),
            explorer=BoundedReActExplorer(limits=ReactLimits(max_tool_calls=1)),
        )

    assert exc.value.code is FailureCode.PROVIDER_BUDGET_EXCEEDED


def test_provider_config_rejects_sensitive_params() -> None:
    with pytest.raises(ContractValidationError) as exc:
        OpenAICompatibleConfig(model="offline-fake", non_sensitive_params={"Authorization": "Bearer nope"})

    assert exc.value.code is FailureCode.UNAUTHORIZED_CAPABILITY
