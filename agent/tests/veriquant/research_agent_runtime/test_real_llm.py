from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

from veriquant.alphaforge_llm import AlphaForgeLLMConfiguration
from veriquant.llm_desks.providers import OpenAICompatibleResponse
from veriquant.real_llm_provider import RealLLMProviderSettings
from veriquant.real_llm_provider import OpenAICompatibleChatResponse, ProviderFailure, ProviderToolCall
from veriquant.contracts import FailureCode
from veriquant.research_agent_runtime import (
    ConfiguredResearchLLMFactory,
    InMemoryResearchToolbox,
    ResearchAgentTask,
    ResearchCancellationToken,
    ResearchToolResult,
    SafeOpenAIResearchChatLLM,
    SafeVibeAgentLoopFactory,
    VibeAgentLoopAdapter,
)
from veriquant.research_toolbox import ContentAddressedHandleStore
from veriquant.research_agent_runtime.real_smoke import execute_real_agent_smoke
from veriquant.research_agent_runtime.real_llm import _safe_messages


NOW = datetime(2026, 7, 21, 8, 0, tzinfo=timezone.utc)


def _hash(character: str) -> str:
    return "sha256:" + character * 64


def _task() -> ResearchAgentTask:
    return ResearchAgentTask(
        task_id="task_real_adapter",
        run_id="run_real_adapter",
        specialist_id="market_structure",
        question="研究量价确认信号",
        campaign_hash=_hash("a"),
        data_snapshot_hash=_hash("b"),
        decision_time=NOW,
        allowed_tools=("describe_visible_data",),
        max_iterations=3,
    )


def test_semantic_comparison_task_requests_multiple_distinct_experiments() -> None:
    task = replace(
        _task(),
        answer_requirement_ids=("R10_PRIMARY_CONCLUSION", "R20_CANDIDATE_COMPARISON"),
        required_answer_semantics=("candidate_comparison", "controlled_relation", "prediction"),
        comparison_required=True,
        minimum_experiments=2,
        max_experiments=3,
    )
    messages = _safe_messages(task)
    system = messages[0]["content"]
    material = __import__("json").loads(messages[1]["content"])
    assert "Submit 2 to 3 genuinely distinct experiment drafts" in system
    assert material["output_contract"]["comparison_required"] is True
    assert material["output_contract"]["required_answer_semantics"] == [
        "candidate_comparison", "controlled_relation", "prediction",
    ]


def _configuration() -> AlphaForgeLLMConfiguration:
    return AlphaForgeLLMConfiguration(
        enabled=True,
        settings=RealLLMProviderSettings(
            endpoint_url="https://llm.example.test/v1/chat/completions",
            model="research-model",
            approved_hosts=("llm.example.test",),
            timeout_seconds=30.0,
        ),
        _runtime_credential="not-persisted-credential",
    )


class _Client:
    def __init__(self) -> None:
        self.calls = []

    def complete_json(self, **kwargs):
        self.calls.append(kwargs)
        return OpenAICompatibleResponse(
            payload={
                "authority": "DRAFT_ONLY",
                "plan": "先检查可见数据，再形成确定性横截面分数草案。",
                "code_drafts": [
                    "def run_research(train_rows, prediction_rows, seed):\n"
                    "    frame = prediction_rows.copy()\n"
                    "    frame['score'] = frame['close'].pct_change(20)\n"
                    "    return {'score_table': frame}"
                ],
                "status": "COMPLETED",
            },
            input_tokens=31,
            output_tokens=47,
        )


class _DowngradedClient(_Client):
    def complete_json(self, **kwargs):
        response = super().complete_json(**kwargs)
        payload = dict(response.payload)
        payload["authority"] = "MODEL_ASSERTED_AUTHORITY_IS_IGNORED"
        payload["status"] = "MODEL_ASSERTED_STATUS_IS_IGNORED"
        return OpenAICompatibleResponse(
            payload=payload,
            input_tokens=response.input_tokens,
            output_tokens=response.output_tokens,
        )


class _ExtraFieldClient(_Client):
    def complete_json(self, **kwargs):
        response = super().complete_json(**kwargs)
        payload = dict(response.payload)
        payload["method_note"] = "discarded before persistence"
        return OpenAICompatibleResponse(payload=payload, input_tokens=1, output_tokens=1)


class _PrivilegedFieldClient(_Client):
    def complete_json(self, **kwargs):
        response = super().complete_json(**kwargs)
        payload = dict(response.payload)
        payload["promotion"] = "APPROVE"
        return OpenAICompatibleResponse(payload=payload, input_tokens=1, output_tokens=1)


class _CommonAliasClient(_Client):
    def complete_json(self, **kwargs):
        response = super().complete_json(**kwargs)
        return OpenAICompatibleResponse(
            payload={
                "authority": "DRAFT_ONLY",
                "research_plan": response.payload["plan"],
                "python_code": response.payload["code_drafts"][0],
                "status": "COMPLETED",
            },
            input_tokens=1,
            output_tokens=1,
        )


class _ToolAwareClient:
    def __init__(self) -> None:
        self.calls = []

    def complete_chat(self, **kwargs):
        self.calls.append(kwargs)
        if len(self.calls) == 1:
            return OpenAICompatibleChatResponse(
                content="",
                payload=None,
                tool_calls=(ProviderToolCall(
                    call_id="call_visible_data",
                    name="describe_visible_data",
                    arguments={"query": "approved coverage"},
                ),),
                finish_reason="tool_calls",
                input_tokens=10,
                output_tokens=3,
            )
        return OpenAICompatibleChatResponse(
            content=None,
            payload={
                "authority": "DRAFT_ONLY",
                "plan": "读取实际工具结果后形成研究代码草案。",
                "code_drafts": [
                    "def run_research(train_rows, prediction_rows, seed):\n"
                    "    return {'rows': []}\n"
                ],
                "status": "COMPLETED",
            },
            tool_calls=(),
            finish_reason="stop",
            input_tokens=20,
            output_tokens=30,
        )


class _ToolSchemaFallbackClient(_Client):
    def __init__(self):
        super().__init__()
        self.chat_calls = 0

    def complete_chat(self, **kwargs):
        self.chat_calls += 1
        raise ProviderFailure(FailureCode.PROVIDER_SCHEMA_REJECTED, "invalid tool-aware JSON")


def _tools():
    return [
        {
            "type": "function",
            "function": {
                "name": "describe_visible_data",
                "description": "read visible data",
                "parameters": {"type": "object", "properties": {}},
            },
        }
    ]


def test_safe_adapter_returns_vibe_response_without_forwarding_ambient_messages():
    client = _Client()
    llm = SafeOpenAIResearchChatLLM(task=_task(), configuration=_configuration(), client=client)
    response = llm.stream_chat(
        [{"role": "user", "content": "AMBIENT_CONTENT_MUST_NOT_BE_FORWARDED"}],
        tools=_tools(),
    )
    assert "def run_research(" in (response.content or "")
    assert response.usage_metadata == {"input_tokens": 31, "output_tokens": 47, "total_tokens": 78}
    rendered = str(client.calls[0]["messages"])
    assert "AMBIENT_CONTENT_MUST_NOT_BE_FORWARDED" not in rendered
    assert "not-persisted-credential" not in rendered
    assert "run_research" in rendered
    assert "numeric fields can be null" in rendered
    assert "train row has no feature names at its top level" in rendered
    assert "customer-facing candidate definition" in rendered
    assert "what a higher score means" in rendered
    assert "cannot change same-day stock ranks" in rendered
    assert client.calls[0]["response_safety_profile"] == "RESEARCH_SOURCE_DRAFT"


def test_final_agent_loop_turn_may_remove_all_tools_but_not_use_a_partial_set():
    client = _Client()
    llm = SafeOpenAIResearchChatLLM(task=_task(), configuration=_configuration(), client=client)

    assert "def run_research(" in (llm.stream_chat([], tools=None).content or "")
    unauthorized = [{
        "type": "function",
        "function": {
            "name": "undeclared_tool",
            "description": "not allowed",
            "parameters": {"type": "object", "properties": {}},
        },
    }]
    with pytest.raises(Exception):
        llm.stream_chat([], tools=unauthorized)


def test_tool_aware_schema_failure_retries_once_on_json_only_draft_surface():
    client = _ToolSchemaFallbackClient()
    llm = SafeOpenAIResearchChatLLM(task=_task(), configuration=_configuration(), client=client)
    response = llm.stream_chat([], tools=_tools())
    payload = __import__("json").loads(response.content)
    assert client.chat_calls == 1
    assert len(client.calls) == 1
    assert payload["authority"] == "DRAFT_ONLY"
    assert payload["code_drafts"]


def test_regression_contract_requires_non_empty_deterministic_singular_fallback():
    messages = _safe_messages(replace(_task(), output_kind="RegressionTable"))
    rendered = str(messages)
    assert "rows array must never be empty" in rendered
    assert "exactly collinear" in rendered
    assert "deterministic ridge fallback" in rendered
    assert "p_value 1" in rendered
    assert "math.isfinite" in rendered
    assert "inclusive [0, 1]" in rendered
    assert "Do not let NaN" in rendered
    assert "distinct descriptive names" in rendered
    assert "Never reuse or rebind" in rendered


def test_adapter_mechanically_downgrades_model_claims_to_draft_only():
    llm = SafeOpenAIResearchChatLLM(
        task=_task(),
        configuration=_configuration(),
        client=_DowngradedClient(),
    )
    payload = __import__("json").loads(llm.stream_chat([], tools=_tools()).content)
    assert payload["authority"] == "DRAFT_ONLY"
    assert payload["status"] == "COMPLETED"


def test_adapter_discards_harmless_unknown_fields_but_rejects_privileged_fields():
    allowed = SafeOpenAIResearchChatLLM(task=_task(), configuration=_configuration(), client=_ExtraFieldClient())
    payload = __import__("json").loads(allowed.stream_chat([], tools=_tools()).content)
    assert "method_note" not in payload
    denied = SafeOpenAIResearchChatLLM(task=_task(), configuration=_configuration(), client=_PrivilegedFieldClient())
    with pytest.raises(Exception):
        denied.stream_chat([], tools=_tools())


def test_adapter_normalizes_common_non_privileged_plan_and_source_aliases():
    llm = SafeOpenAIResearchChatLLM(task=_task(), configuration=_configuration(), client=_CommonAliasClient())
    payload = __import__("json").loads(llm.stream_chat([], tools=_tools()).content)
    assert payload["plan"].startswith("先检查")
    assert len(payload["code_drafts"]) == 1
    assert "def run_research(" in payload["code_drafts"][0]


def test_adapter_cancellation_fails_before_provider_call():
    client = _Client()
    llm = SafeOpenAIResearchChatLLM(task=_task(), configuration=_configuration(), client=client)
    with pytest.raises(Exception):
        llm.stream_chat([], tools=_tools(), should_cancel=lambda: True)
    assert client.calls == []


def test_real_adapter_preserves_vibe_tool_cycle_and_tool_result_context(tmp_path: Path):
    task = _task()
    client = _ToolAwareClient()
    llm = SafeOpenAIResearchChatLLM(task=task, configuration=_configuration(), client=client)
    toolbox = InMemoryResearchToolbox(
        task=task,
        handlers={
            "describe_visible_data": lambda arguments: ResearchToolResult(
                payload={"coverage": "visible", "query": arguments["query"]},
                available_at=NOW,
            )
        },
    )
    runtime = VibeAgentLoopAdapter(
        loop_factory=SafeVibeAgentLoopFactory(llm_factory=lambda _: llm, run_root=tmp_path / "runs"),
        draft_store=ContentAddressedHandleStore(tmp_path / "drafts"),
    )

    outcome = runtime.run(
        task=task,
        toolbox=toolbox,
        upstream_context={},
        cancellation=ResearchCancellationToken(),
    )

    assert outcome.tool_calls == 1
    assert outcome.provider_calls == 2
    assert len(client.calls) == 2
    second_messages = client.calls[1]["messages"]
    assert any(item.get("role") == "tool" for item in second_messages)
    assert "approved coverage" in str(second_messages)


def test_factory_loads_only_narrow_dotenv_and_public_view_excludes_credential(tmp_path: Path):
    dotenv = tmp_path / ".env"
    dotenv.write_text(
        "ENABLE_LLM_RESEARCH=1\n"
        "OPENAI_BASE_URL=https://llm.example.test/v1\n"
        "OPENAI_API_KEY=not-persisted-credential\n"
        "LANGCHAIN_MODEL_NAME=research-model\n",
        encoding="utf-8",
    )
    client = _Client()
    factory = ConfiguredResearchLLMFactory(dotenv_path=dotenv, client_factory=lambda _: client)
    assert factory.public_view["available_config"] is True
    assert "credential" not in str(factory.public_view).casefold()
    assert "not-persisted-credential" not in repr(factory)
    assert isinstance(factory(_task()), SafeOpenAIResearchChatLLM)


def test_real_smoke_pipeline_stores_source_by_hash_without_running_it(tmp_path: Path):
    client = _Client()
    factory = ConfiguredResearchLLMFactory.__new__(ConfiguredResearchLLMFactory)
    factory._configuration = _configuration()
    factory._client_factory = lambda _: client
    result = execute_real_agent_smoke(
        question="研究过去20日量价信号",
        artifact_root=tmp_path,
        llm_factory=factory,
    )
    assert result["smoke_passed"] is True
    assert result["source_has_run_research"] is True
    assert result["source_executed"] is False
    assert len(result["code_draft_handles"]) == 1
    assert result["code_draft_handles"][0].startswith("sha256:")
    assert result["official_evidence_hash"] is None
    assert result["promotion_hash"] is None
    persisted = (tmp_path / "result.json").read_text(encoding="utf-8")
    assert "not-persisted-credential" not in persisted
    assert "def run_research" not in persisted
