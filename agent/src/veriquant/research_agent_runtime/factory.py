"""Fail-closed construction of a Vibe ``AgentLoop`` for research-only use.

This module deliberately reuses Vibe's loop mechanics while replacing its
global context and unrestricted tool registry.  The resulting loop has DRAFT
authority only; it cannot calculate official evidence, promote a candidate, or
execute generated research code.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from src.agent.context import ContextBuilder
from src.agent.loop import AgentLoop
from src.agent.memory import WorkspaceMemory
from src.agent.tools import BaseTool, ToolRegistry

from ..contracts import FailureCode
from ..contracts.base import fail
from .context import AgentLoopResearchContext, research_context_factory
from .contracts import ResearchAgentTask
from .toolbox import ResearchToolboxProtocol


_EVENT_FIELDS = frozenset({"chars", "elapsed_ms", "iter", "status", "tool"})


class _VibeCompatibleResearchContext:
    """Complete the small safe context with Vibe's message formatting only."""

    def __init__(self, context: AgentLoopResearchContext) -> None:
        self._context = context

    def build_messages(self, user_message: str, history=None):
        return self._context.build_messages(user_message, history)

    @staticmethod
    def format_tool_result(tool_call_id: str, tool_name: str, result: str):
        return ContextBuilder.format_tool_result(tool_call_id, tool_name, result)

    @staticmethod
    def format_assistant_tool_calls(tool_calls, content=None, reasoning_content=None):
        return ContextBuilder.format_assistant_tool_calls(
            tool_calls,
            content=content,
            reasoning_content=reasoning_content,
        )


class _ResearchToolAdapter(BaseTool):
    """Expose one approved toolbox capability as a read-only Vibe tool."""

    repeatable = True
    is_readonly = True

    def __init__(self, *, definition: Mapping[str, object], toolbox: ResearchToolboxProtocol) -> None:
        self.name = str(definition["name"])
        self.description = str(definition["description"])
        self.parameters = dict(definition["parameters"])  # type: ignore[arg-type]
        self._toolbox = toolbox

    def execute(self, **kwargs: Any) -> str:
        # Vibe injects its internal ``run_dir`` into every tool invocation.
        # Research capabilities are pathless, so never forward that ambient
        # filesystem authority into the VeriQuant toolbox.
        allowed = set(self.parameters.get("properties", {}))
        unexpected = set(kwargs).difference(allowed).difference({"run_dir"})
        if unexpected:
            return json.dumps(
                {"status": "error", "code": "RESEARCH_TOOL_REJECTED"},
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        arguments = {key: value for key, value in kwargs.items() if key in allowed}
        try:
            result = self._toolbox.call(self.name, arguments)
        except Exception:
            # Never let ToolRegistry serialize a raw exception.  Provider,
            # path, secret, and policy details are intentionally discarded.
            return json.dumps(
                {"status": "error", "code": "RESEARCH_TOOL_REJECTED"},
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        return json.dumps(
            {
                "authority": "AUXILIARY_RESEARCH_EVIDENCE",
                "available_at": result.available_at.isoformat(),
                "payload": result.payload,
                "status": "ok",
            },
            ensure_ascii=False,
            sort_keys=True,
            default=str,
            separators=(",", ":"),
        )


class _ResearchLoopHandle:
    """Prevent goal/session state from being injected into a research turn."""

    def __init__(self, loop: AgentLoop) -> None:
        self._loop = loop

    def run(self, user_message: str, history=None, session_id: str = ""):
        del session_id
        return self._loop.run(user_message, history=history, session_id="")

    def cancel(self) -> None:
        self._loop.cancel()


class SafeVibeAgentLoopFactory:
    """Build an isolated, read-only Vibe loop for one specialist task.

    ``llm_factory`` is injected so construction never reads provider settings
    or keys by itself. ``run_root`` is the sole filesystem location assigned to
    Vibe's trace/run-state machinery.
    """

    def __init__(
        self,
        *,
        llm_factory: Callable[[ResearchAgentTask], Any],
        run_root: str | Path,
    ) -> None:
        if not callable(llm_factory):
            fail(FailureCode.INVALID_VALUE, "llm_factory must be callable")
        self._llm_factory = llm_factory
        self._run_root = Path(run_root).resolve()

    def __call__(
        self,
        *,
        task: ResearchAgentTask,
        toolbox: ResearchToolboxProtocol,
        event_callback: Callable[[str, Mapping[str, Any]], None] | None,
    ) -> _ResearchLoopHandle:
        registry = _build_registry(task, toolbox)
        run_dir = (self._run_root / task.run_id / "specialists" / task.specialist_id).resolve()
        try:
            run_dir.relative_to(self._run_root)
        except ValueError:
            fail(FailureCode.UNAUTHORIZED_CAPABILITY, "research run directory escaped its root")
        run_dir.mkdir(parents=True, exist_ok=True)

        try:
            llm = self._llm_factory(task)
        except Exception:
            fail(FailureCode.UNAVAILABLE, "research provider construction failed")
        if llm is None or not callable(getattr(llm, "stream_chat", None)):
            fail(FailureCode.UNAVAILABLE, "research provider is unavailable")

        system_prompt = _system_prompt(task)
        base_factory = research_context_factory(system_prompt)

        def safe_context_factory(registry_arg, memory_arg, *, persistent_memory=None):
            base = base_factory(
                registry_arg,
                memory_arg,
                persistent_memory=persistent_memory,
            )
            return _VibeCompatibleResearchContext(base)

        safe_callback = _safe_event_callback(event_callback)
        loop = AgentLoop(
            registry=registry,
            llm=llm,
            memory=WorkspaceMemory(run_dir=str(run_dir)),
            event_callback=safe_callback,
            max_iterations=task.max_iterations,
            persistent_memory=None,
            context_builder_factory=safe_context_factory,
        )
        return _ResearchLoopHandle(loop)


def _build_registry(task: ResearchAgentTask, toolbox: ResearchToolboxProtocol) -> ToolRegistry:
    names = tuple(toolbox.names)
    if names != task.allowed_tools:
        fail(FailureCode.UNAUTHORIZED_CAPABILITY, "toolbox names differ from task whitelist")

    definitions = tuple(toolbox.definitions)
    by_name: dict[str, Mapping[str, object]] = {}
    for raw in definitions:
        if not isinstance(raw, Mapping):
            fail(FailureCode.INVALID_VALUE, "research tool definition must be an object")
        name = raw.get("name")
        if not isinstance(name, str) or name not in task.allowed_tools or name in by_name:
            fail(FailureCode.UNAUTHORIZED_CAPABILITY, "research tool definition is undeclared or duplicated")
        if raw.get("readonly") is not True:
            fail(FailureCode.UNAUTHORIZED_CAPABILITY, "research tools must be explicitly read-only")
        if not isinstance(raw.get("description"), str) or not isinstance(raw.get("parameters"), Mapping):
            fail(FailureCode.INVALID_VALUE, "research tool definition is incomplete")
        properties = raw["parameters"].get("properties", {})  # type: ignore[union-attr]
        if isinstance(properties, Mapping) and "run_dir" in properties:
            fail(FailureCode.UNAUTHORIZED_CAPABILITY, "research tools cannot receive runtime paths")
        by_name[name] = raw
    if tuple(sorted(by_name)) != task.allowed_tools:
        fail(FailureCode.UNAUTHORIZED_CAPABILITY, "tool definitions differ from task whitelist")

    registry = ToolRegistry()
    for name in task.allowed_tools:
        registry.register(_ResearchToolAdapter(definition=by_name[name], toolbox=toolbox))
    if tuple(sorted(registry.tool_names)) != task.allowed_tools:
        fail(FailureCode.UNAUTHORIZED_CAPABILITY, "constructed registry differs from task whitelist")
    return registry


def _system_prompt(task: ResearchAgentTask) -> str:
    tools = ",".join(task.allowed_tools) if task.allowed_tools else "(none)"
    return (
        "AlphaForge controlled specialist research runtime. Authority is DRAFT_ONLY. "
        "Use only the explicitly registered read-only tools: "
        f"{tools}. Never claim official metrics, Evidence approval, Promotion, portfolio, "
        "risk approval, orders, ledger authority, or sealed access. Generated code is text "
        "only and is not executed by this loop. Each source draft must define exactly one "
        "synchronous run_research function. The plan must be a complete customer-facing candidate "
        "definition: exact fields, transformations, score direction, windows, normalization, economic "
        "rationale, and proxy limitations. Return the required JSON draft contract."
        f" This task must answer semantic requirements: {','.join(task.required_answer_semantics) or 'primary_conclusion'}."
        f" Submit {task.minimum_experiments} to {task.max_experiments} distinct experiments;"
        " when comparison is requested they must represent genuinely different definitions, not renamed duplicates."
    )


def _safe_event_callback(callback):
    if callback is None:
        return None

    def emit(event_type: str, data: Mapping[str, Any]) -> None:
        callback(
            event_type,
            {key: data[key] for key in sorted(_EVENT_FIELDS.intersection(data))},
        )

    return emit


__all__ = ["SafeVibeAgentLoopFactory"]
