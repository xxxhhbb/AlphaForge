from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from src.providers.chat import LLMResponse, ToolCallRequest
from veriquant.research_agent_runtime import (
    InMemoryResearchToolbox,
    ResearchAgentStatus,
    ResearchAgentTask,
    ResearchCancellationToken,
    ResearchToolResult,
    SafeVibeAgentLoopFactory,
    VibeAgentLoopAdapter,
)
from veriquant.research_toolbox.handles import ContentAddressedHandleStore


NOW = datetime(2026, 7, 21, 8, 0, tzinfo=timezone.utc)


def _hash(character: str) -> str:
    return "sha256:" + character * 64


def _task(*, max_iterations: int = 3) -> ResearchAgentTask:
    return ResearchAgentTask(
        task_id="task_safe_loop",
        run_id="run_safe_loop",
        specialist_id="market_structure",
        question="研究成交量确认能否增强动量",
        campaign_hash=_hash("a"),
        data_snapshot_hash=_hash("b"),
        decision_time=NOW,
        allowed_tools=("describe_visible_data",),
        max_iterations=max_iterations,
    )


def _toolbox(task: ResearchAgentTask, *, raises: bool = False):
    def handler(arguments):
        if raises:
            raise RuntimeError("SECRET_TOKEN_MUST_NOT_LEAK")
        return ResearchToolResult(
            payload={"coverage": "visible", "query": arguments["query"]},
            available_at=NOW,
        )

    return InMemoryResearchToolbox(
        task=task,
        handlers={"describe_visible_data": handler},
    )


class _TwoTurnLLM:
    model_name = "fake-safe-model"

    def __init__(self) -> None:
        self.calls: list[tuple[list[dict[str, Any]], Any]] = []

    def stream_chat(self, messages, tools=None, on_text_chunk=None, on_reasoning_chunk=None, should_cancel=None):
        del on_reasoning_chunk, should_cancel
        self.calls.append((list(messages), tools))
        if len(self.calls) == 1:
            return LLMResponse(
                content="",
                tool_calls=[
                    ToolCallRequest(
                        id="call_visible_data",
                        name="describe_visible_data",
                        arguments={"query": "approved coverage"},
                    )
                ],
                finish_reason="tool_calls",
            )
        if on_text_chunk:
            on_text_chunk("draft-only")
        return LLMResponse(
            content=json.dumps(
                {
                    "authority": "DRAFT_ONLY",
                    "plan": "先读取可见覆盖，再提交研究草案。",
                    "code_drafts": [],
                    "status": "COMPLETED",
                },
                ensure_ascii=False,
            )
        )


def test_factory_runs_real_vibe_loop_with_only_approved_readonly_tool(tmp_path: Path) -> None:
    task = _task()
    toolbox = _toolbox(task)
    llm = _TwoTurnLLM()
    events: list[tuple[str, dict[str, Any]]] = []
    factory = SafeVibeAgentLoopFactory(llm_factory=lambda _: llm, run_root=tmp_path)

    outcome = VibeAgentLoopAdapter(
        loop_factory=factory,
        event_callback=lambda kind, data: events.append((kind, dict(data))),
        draft_store=ContentAddressedHandleStore(tmp_path / "drafts"),
    ).run(
        task=task,
        toolbox=toolbox,
        upstream_context={},
        cancellation=ResearchCancellationToken(),
    )

    assert outcome.status is ResearchAgentStatus.COMPLETED
    assert outcome.tool_calls == 1
    assert len(llm.calls) == 2
    first_messages, definitions = llm.calls[0]
    assert first_messages[0]["role"] == "system"
    assert "DRAFT_ONLY" in first_messages[0]["content"]
    assert "Generated code is text only" in first_messages[0]["content"]
    assert "<workspace_memory>" not in json.dumps(first_messages)
    assert [item["function"]["name"] for item in definitions] == ["describe_visible_data"]
    assert all(item["function"]["name"] in task.allowed_tools for item in definitions)
    assert (tmp_path / task.run_id / "specialists" / task.specialist_id).is_dir()
    # Streaming/model text and tool arguments are never forwarded to callers.
    assert "draft-only" not in json.dumps(events)
    assert "approved coverage" not in json.dumps(events)


def test_factory_drops_session_goal_injection(monkeypatch, tmp_path: Path) -> None:
    task = _task()
    llm = _TwoTurnLLM()
    monkeypatch.setattr(
        "src.agent.loop.get_current_goal_context",
        lambda _session: ("MALICIOUS_GLOBAL_GOAL", "goal-1"),
    )
    handle = SafeVibeAgentLoopFactory(llm_factory=lambda _: llm, run_root=tmp_path)(
        task=task,
        toolbox=_toolbox(task),
        event_callback=None,
    )

    result = handle.run("bounded draft request", session_id="attacker-controlled-session")

    assert result["status"] == "success"
    assert "MALICIOUS_GLOBAL_GOAL" not in json.dumps(llm.calls)


def test_factory_rejects_tool_definition_without_explicit_readonly(tmp_path: Path) -> None:
    task = _task()

    class UnsafeDefinitions:
        names = task.allowed_tools
        definitions = (
            {
                "name": "describe_visible_data",
                "description": "unsafe declaration",
                "parameters": {"type": "object", "properties": {}},
                "readonly": False,
            },
        )
        recorded_receipts = ()

        def call(self, name, arguments):  # pragma: no cover - must not run
            raise AssertionError((name, arguments))

    factory = SafeVibeAgentLoopFactory(llm_factory=lambda _: _TwoTurnLLM(), run_root=tmp_path)
    with pytest.raises(Exception):
        factory(task=task, toolbox=UnsafeDefinitions(), event_callback=None)


def test_tool_failure_is_generic_and_does_not_leak_exception(tmp_path: Path) -> None:
    task = _task()
    llm = _TwoTurnLLM()
    handle = SafeVibeAgentLoopFactory(llm_factory=lambda _: llm, run_root=tmp_path)(
        task=task,
        toolbox=_toolbox(task, raises=True),
        event_callback=None,
    )

    result = handle.run("bounded draft request")

    assert result["status"] == "success"
    second_messages = json.dumps(llm.calls[1][0], ensure_ascii=False)
    assert "RESEARCH_TOOL_REJECTED" in second_messages
    assert "SECRET_TOKEN_MUST_NOT_LEAK" not in second_messages


def test_factory_fails_closed_when_provider_is_unavailable(tmp_path: Path) -> None:
    task = _task()
    factory = SafeVibeAgentLoopFactory(llm_factory=lambda _: None, run_root=tmp_path)
    with pytest.raises(Exception):
        factory(task=task, toolbox=_toolbox(task), event_callback=None)
