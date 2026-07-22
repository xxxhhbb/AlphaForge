from __future__ import annotations

import time
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from src.veriquant.contracts import (
    ContractValidationError,
    FailureCode,
    ResearchQuestion,
    ResearchState,
)
from src.veriquant.research import (
    DeskBudget,
    DeterministicProposalProvider,
    FeedbackEvidenceSummary,
    LLMJsonResponse,
    OptionalLLMProposalProvider,
    ProviderSession,
    ResearchDAGRuntime,
    ResearchTaskDAG,
    ResearchTaskSpec,
    TaskExecution,
    TaskStatus,
    build_default_desks,
    build_default_tool_catalog,
)


NOW = datetime(2024, 1, 31, 16, 0, tzinfo=timezone.utc)
HASH_A = "sha256:" + "a" * 64


def question() -> ResearchQuestion:
    return ResearchQuestion(
        question_id="wave05-provider-question",
        text="Find visible offline factors.",
        universe=("AAA", "BBB"),
        horizon="5d",
        decision_time=NOW,
        availability_assumptions=("PIT fields only",),
        state=ResearchState.DRAFT,
    )


def test_dag_rejects_cycle_unknown_dependency_and_illegal_input_from() -> None:
    with pytest.raises(ContractValidationError) as cycle:
        ResearchTaskDAG(
            (
                ResearchTaskSpec(task_id="a", depends_on=("b",)),
                ResearchTaskSpec(task_id="b", depends_on=("a",)),
            )
        )
    assert cycle.value.code is FailureCode.DAG_VALIDATION_FAILED

    with pytest.raises(ContractValidationError) as unknown:
        ResearchTaskDAG((ResearchTaskSpec(task_id="a", depends_on=("missing",)),))
    assert unknown.value.code is FailureCode.DAG_VALIDATION_FAILED

    with pytest.raises(ContractValidationError) as illegal_input:
        ResearchTaskDAG(
            (
                ResearchTaskSpec(task_id="a"),
                ResearchTaskSpec(task_id="b"),
                ResearchTaskSpec(
                    task_id="c",
                    depends_on=("a",),
                    input_from={"wrong": "b"},
                ),
            )
        )
    assert illegal_input.value.code is FailureCode.DAG_VALIDATION_FAILED


def test_runtime_distinguishes_failed_incomplete_completed_and_blocks_dependents() -> None:
    dag = ResearchTaskDAG(
        (
            ResearchTaskSpec(task_id="root"),
            ResearchTaskSpec(task_id="failed", depends_on=("root",)),
            ResearchTaskSpec(task_id="incomplete", depends_on=("root",)),
            ResearchTaskSpec(task_id="independent", depends_on=("root",)),
            ResearchTaskSpec(task_id="after_failed", depends_on=("failed",)),
            ResearchTaskSpec(task_id="after_incomplete", depends_on=("incomplete",)),
        )
    )
    handlers = {
        "root": lambda inputs: TaskExecution(status=TaskStatus.COMPLETED, output="root"),
        "failed": lambda inputs: TaskExecution(
            status=TaskStatus.FAILED,
            failure_code=FailureCode.ADAPTER_EXECUTION_FAILED,
            error="deliberate failure",
        ),
        "incomplete": lambda inputs: TaskExecution(
            status=TaskStatus.INCOMPLETE,
            failure_code=FailureCode.INCOMPLETE_RESULT,
            error="no substantive result",
        ),
        "independent": lambda inputs: TaskExecution(status=TaskStatus.COMPLETED, output="real"),
        "after_failed": lambda inputs: (_ for _ in ()).throw(AssertionError("must be blocked")),
        "after_incomplete": lambda inputs: (_ for _ in ()).throw(AssertionError("must be blocked")),
    }
    result = ResearchDAGRuntime(max_workers=3).run(dag, handlers=handlers)

    assert result.records["failed"].status is TaskStatus.FAILED
    assert result.records["failed"].output is None
    assert result.records["incomplete"].status is TaskStatus.INCOMPLETE
    assert result.records["incomplete"].output is None
    assert result.records["independent"].status is TaskStatus.COMPLETED
    assert result.records["independent"].output == "real"
    assert result.records["after_failed"].status is TaskStatus.BLOCKED
    assert result.records["after_incomplete"].status is TaskStatus.BLOCKED

    with pytest.raises(ContractValidationError):
        TaskExecution(
            status=TaskStatus.FAILED,
            output="forged success",
            failure_code=FailureCode.ADAPTER_EXECUTION_FAILED,
            error="bad",
        )


def test_six_desks_have_isolated_context_tools_budgets_and_namespaces() -> None:
    desks = build_default_desks()
    catalog = build_default_tool_catalog()
    registries = tuple(catalog.for_desk(desk) for desk in desks)

    assert len(desks) == 6
    assert len({desk.role for desk in desks}) == 6
    assert len({desk.context for desk in desks}) == 6
    assert len({id(desk.budget) for desk in desks}) == 6
    assert len({desk.artifact_namespace for desk in desks}) == 6
    assert len({id(registry) for registry in registries}) == 6
    assert all(registry.names == tuple(sorted(desk.tool_whitelist)) for desk, registry in zip(desks, registries))


def test_undeclared_tool_fails_closed() -> None:
    desk = build_default_desks()[0]
    registry = build_default_tool_catalog().for_desk(desk)
    assert "read_macro" not in registry.names
    with pytest.raises(ContractValidationError) as caught:
        registry.call("read_macro")
    assert caught.value.code is FailureCode.UNAUTHORIZED_CAPABILITY


def test_optional_llm_is_disabled_by_default_without_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ENABLE_LLM_RESEARCH", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    desk = build_default_desks()[0]
    session = ProviderSession(desk)
    with pytest.raises(ContractValidationError) as caught:
        OptionalLLMProposalProvider().propose_hypothesis(
            desk=desk,
            question=question(),
            tools=build_default_tool_catalog().for_desk(desk),
            session=session,
        )
    assert caught.value.code is FailureCode.ADAPTER_UNAVAILABLE


class _FlakyClient:
    def __init__(self) -> None:
        self.calls = 0

    def complete_json(self, **kwargs) -> LLMJsonResponse:
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("transient")
        return LLMJsonResponse(
            payload={
                "statement": "Visible information predicts returns.",
                "economic_mechanism": "Slow diffusion.",
                "expected_sign": "POSITIVE",
                "falsification_tests": ["Reject weak visible evidence."],
            },
            tokens_used=10,
        )


def test_optional_llm_retry_and_budget_are_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENABLE_LLM_RESEARCH", "1")
    desk = build_default_desks()[0]
    client = _FlakyClient()
    session = ProviderSession(desk)
    result = OptionalLLMProposalProvider(client=client).propose_hypothesis(
        desk=desk,
        question=question(),
        tools=build_default_tool_catalog().for_desk(desk),
        session=session,
    )
    assert result.expected_sign == "POSITIVE"
    assert client.calls == 2
    assert session.iterations_used == 2
    assert session.tokens_used == 10

    class TooManyTokens:
        def complete_json(self, **kwargs) -> LLMJsonResponse:
            return LLMJsonResponse(payload={}, tokens_used=10_000)

    with pytest.raises(ContractValidationError) as budget:
        OptionalLLMProposalProvider(client=TooManyTokens()).propose_hypothesis(
            desk=desk,
            question=question(),
            tools=build_default_tool_catalog().for_desk(desk),
            session=ProviderSession(desk),
        )
    assert budget.value.code is FailureCode.PROVIDER_BUDGET_EXCEEDED


def test_optional_llm_timeout_is_typed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENABLE_LLM_RESEARCH", "1")
    original = build_default_desks()[0]
    desk = replace(
        original,
        budget=DeskBudget(
            family_units=1,
            max_tokens=100,
            max_iterations=3,
            max_retries=0,
            timeout_seconds=0.01,
        ),
    )

    class SlowClient:
        def complete_json(self, **kwargs) -> LLMJsonResponse:
            time.sleep(0.05)
            return LLMJsonResponse(payload={}, tokens_used=0)

    with pytest.raises(ContractValidationError) as caught:
        OptionalLLMProposalProvider(client=SlowClient()).propose_hypothesis(
            desk=desk,
            question=question(),
            tools=build_default_tool_catalog().for_desk(desk),
            session=ProviderSession(desk),
        )
    assert caught.value.code is FailureCode.PROVIDER_TIMEOUT


def test_feedback_summary_rejects_sealed_fields() -> None:
    with pytest.raises(ContractValidationError) as caught:
        FeedbackEvidenceSummary(identity_hash=HASH_A, visible_metrics={"sealed_rank_ic": 1.0})
    assert caught.value.code is FailureCode.UNAUTHORIZED_CAPABILITY
