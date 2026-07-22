"""Bounded ReAct explorer for hypothesis and factor-draft slots only."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Mapping, Protocol

from ..contracts import CanonicalContract, FailureCode
from ..contracts.base import require_identifier
from .models import LLMDeskStage, ProviderSession
from .safety import assert_no_forbidden_content, canonical_hash, fail
from .tools import ReadOnlyToolRegistry, ToolCallSummary


REACT_ALLOWED_STAGES = {LLMDeskStage.HYPOTHESIS, LLMDeskStage.FACTOR_DRAFT}


@dataclass(frozen=True, kw_only=True)
class ReactLimits(CanonicalContract):
    max_iterations: int = 3
    max_tool_calls: int = 2
    max_summaries: int = 4
    max_documents: int = 0
    max_tokens: int = 512
    timeout_seconds: float = 2.0

    def __post_init__(self) -> None:
        for name in ("max_iterations", "max_tool_calls", "max_summaries", "max_tokens"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                fail(FailureCode.INVALID_VALUE, f"{name} must be a non-negative integer")
        if self.max_iterations < 1:
            fail(FailureCode.INVALID_VALUE, "max_iterations must be positive")
        if isinstance(self.max_documents, bool) or not isinstance(self.max_documents, int) or self.max_documents < 0:
            fail(FailureCode.INVALID_VALUE, "max_documents must be a non-negative integer")
        if not isinstance(self.timeout_seconds, (int, float)) or not 0.0 < float(self.timeout_seconds) <= 60.0:
            fail(FailureCode.INVALID_VALUE, "timeout_seconds must be in (0, 60]")


@dataclass(frozen=True, kw_only=True)
class ReactAction(CanonicalContract):
    kind: str
    tool_name: str | None = None
    arguments: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        if self.kind not in {"tool", "finish"}:
            fail(FailureCode.PROVIDER_SCHEMA_REJECTED, "ReAct action kind must be tool or finish")
        if self.kind == "tool":
            if self.tool_name is None or self.arguments is None:
                fail(FailureCode.PROVIDER_SCHEMA_REJECTED, "tool action needs a tool name and arguments")
            require_identifier(self.tool_name, "tool_name")
            assert_no_forbidden_content(dict(self.arguments), where="react.arguments")
        elif self.tool_name is not None or self.arguments is not None:
            fail(FailureCode.PROVIDER_SCHEMA_REJECTED, "finish action cannot include a tool call")


class ReactPlanner(Protocol):
    def next_action(
        self,
        *,
        stage: LLMDeskStage,
        prompt_hash: str,
        observations: tuple[ToolCallSummary, ...],
        allowed_tools: tuple[str, ...],
    ) -> ReactAction: ...


class DeterministicReactPlanner:
    """Use the first available desk tool once, then stop."""

    def next_action(
        self,
        *,
        stage: LLMDeskStage,
        prompt_hash: str,
        observations: tuple[ToolCallSummary, ...],
        allowed_tools: tuple[str, ...],
    ) -> ReactAction:
        if not observations and allowed_tools:
            return ReactAction(kind="tool", tool_name=allowed_tools[0], arguments={})
        return ReactAction(kind="finish")


@dataclass(frozen=True, kw_only=True)
class ReactExplorationResult(CanonicalContract):
    stage: LLMDeskStage
    context_hash: str
    tool_summaries: tuple[ToolCallSummary, ...]
    tokens_used: int

    def __post_init__(self) -> None:
        if not isinstance(self.stage, LLMDeskStage):
            object.__setattr__(self, "stage", LLMDeskStage(self.stage))
        if self.stage not in REACT_ALLOWED_STAGES:
            fail(FailureCode.UNAUTHORIZED_CAPABILITY, "ReAct is allowed only in proposal slots")
        require_identifier(self.context_hash, "context_hash")
        for summary in self.tool_summaries:
            assert_no_forbidden_content(summary.to_dict(), where="react.summary")
        if isinstance(self.tokens_used, bool) or not isinstance(self.tokens_used, int) or self.tokens_used < 0:
            fail(FailureCode.INVALID_VALUE, "tokens_used must be non-negative")


class BoundedReActExplorer:
    """A bounded read-only exploration loop; it never executes code or finance tools."""

    def __init__(self, *, limits: ReactLimits | None = None, planner: ReactPlanner | None = None) -> None:
        self.limits = limits or ReactLimits()
        self.planner = planner or DeterministicReactPlanner()

    def explore(
        self,
        *,
        stage: LLMDeskStage,
        prompt_material: Mapping[str, Any],
        tools: ReadOnlyToolRegistry,
        session: ProviderSession,
    ) -> ReactExplorationResult:
        if stage not in REACT_ALLOWED_STAGES:
            fail(FailureCode.UNAUTHORIZED_CAPABILITY, "ReAct is restricted to hypothesis and factor draft")
        assert_no_forbidden_content(dict(prompt_material), where="react.prompt")
        started = time.monotonic()
        prompt_hash = canonical_hash(prompt_material)
        observations: list[ToolCallSummary] = []
        tool_calls = 0
        tokens_used = self._estimate_tokens(prompt_material)
        for _ in range(self.limits.max_iterations):
            if time.monotonic() - started > self.limits.timeout_seconds:
                fail(FailureCode.PROVIDER_TIMEOUT, "ReAct explorer timed out")
            if tokens_used > self.limits.max_tokens:
                fail(FailureCode.PROVIDER_BUDGET_EXCEEDED, "ReAct token budget exhausted")
            action = self.planner.next_action(
                stage=stage,
                prompt_hash=prompt_hash,
                observations=tuple(observations),
                allowed_tools=tools.names,
            )
            if not isinstance(action, ReactAction):
                fail(FailureCode.PROVIDER_SCHEMA_REJECTED, "ReAct planner returned an invalid action")
            if action.kind == "finish":
                return ReactExplorationResult(
                    stage=stage,
                    context_hash=canonical_hash(tuple(item.to_dict() for item in observations)),
                    tool_summaries=tuple(observations),
                    tokens_used=tokens_used,
                )
            tool_calls += 1
            if tool_calls > self.limits.max_tool_calls:
                fail(FailureCode.PROVIDER_BUDGET_EXCEEDED, "ReAct tool-call budget exhausted")
            assert action.tool_name is not None and action.arguments is not None
            summary = tools.call(action.tool_name, action.arguments)
            session.note_tool_call(action.tool_name)
            observations.append(summary)
            if len(observations) > self.limits.max_summaries:
                fail(FailureCode.PROVIDER_BUDGET_EXCEEDED, "ReAct summary budget exhausted")
            tokens_used += self._estimate_tokens(summary.to_dict())
        fail(FailureCode.INCOMPLETE_RESULT, "ReAct explorer did not finish within iteration bound")

    @staticmethod
    def _estimate_tokens(value: Any) -> int:
        rendered = str(value)
        return max(1, (len(rendered) + 3) // 4)
