from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from src.agent.loop import AgentLoop
from src.agent.memory import WorkspaceMemory
from src.agent.tools import ToolRegistry
from veriquant.research_agent_runtime.context import research_context_factory


class _Response:
    content = '{"authority":"DRAFT_ONLY","plan":"done","code_drafts":[],"status":"PARTIAL"}'
    tool_calls: list[Any] = []
    reasoning_content = None
    has_tool_calls = False
    usage_metadata: dict[str, int] = {}


class _CapturingLLM:
    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []

    def stream_chat(
        self,
        messages: list[dict[str, Any]],
        tools=None,
        on_text_chunk: Callable[[str], None] | None = None,
        on_reasoning_chunk: Callable[[str], None] | None = None,
        should_cancel: Callable[[], bool] | None = None,
    ) -> _Response:
        del tools, on_text_chunk, on_reasoning_chunk, should_cancel
        self.messages = messages
        return _Response()

    def chat(self, messages, **kwargs) -> _Response:
        del kwargs
        self.messages = messages
        return _Response()


def test_agent_loop_uses_research_context_factory_without_global_vibe_prompt(tmp_path: Path) -> None:
    llm = _CapturingLLM()
    memory = WorkspaceMemory()
    memory.run_dir = str(tmp_path / "run")
    Path(memory.run_dir).mkdir(parents=True)
    safe_system = "AlphaForge controlled research. DRAFT_ONLY. No privileged tools."
    loop = AgentLoop(
        registry=ToolRegistry(),
        llm=llm,
        memory=memory,
        max_iterations=1,
        context_builder_factory=research_context_factory(safe_system),
    )

    result = loop.run("plan a bounded experiment")

    assert result["status"] == "success"
    assert llm.messages[0] == {"role": "system", "content": safe_system}
    rendered = str(llm.messages)
    assert "write_file" not in rendered
    assert "run_swarm" not in rendered
    assert "broker" not in rendered.casefold()
