"""Minimal read-only toolbox protocol used by the research runtime."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Mapping, Protocol

from ..contracts import FailureCode
from ..contracts.base import fail
from ..llm_desks.safety import canonical_hash
from .contracts import InvestigationReceipt, ResearchAgentTask


_FORBIDDEN_TEXT = ("authorization", "broker", "cookie", "ledger", "order", "promotion", "sealed", "token")


@dataclass(frozen=True)
class ResearchToolResult:
    payload: Mapping[str, Any]
    available_at: datetime


class ResearchToolboxProtocol(Protocol):
    @property
    def names(self) -> tuple[str, ...]: ...

    @property
    def definitions(self) -> tuple[Mapping[str, object], ...]: ...

    @property
    def recorded_receipts(self) -> tuple[InvestigationReceipt, ...]: ...

    def call(self, name: str, arguments: Mapping[str, Any]) -> ResearchToolResult: ...


class CompositeResearchToolbox(ResearchToolboxProtocol):
    """Combine disjoint read-only toolboxes behind one stable AgentLoop view."""

    def __init__(self, *toolboxes: ResearchToolboxProtocol) -> None:
        if not toolboxes:
            fail(FailureCode.MISSING_REQUIRED_FIELD, "at least one research toolbox is required")
        by_name: dict[str, ResearchToolboxProtocol] = {}
        definitions: dict[str, Mapping[str, object]] = {}
        for toolbox in toolboxes:
            toolbox_definitions = {str(item.get("name")): item for item in toolbox.definitions}
            if set(toolbox_definitions) != set(toolbox.names):
                fail(FailureCode.INVALID_VALUE, "toolbox definitions do not match names")
            for name in toolbox.names:
                if name in by_name:
                    fail(FailureCode.INVALID_VALUE, "composite research tool names must be disjoint")
                by_name[name] = toolbox
                definitions[name] = toolbox_definitions[name]
        self._by_name = by_name
        self._definitions = definitions

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._by_name))

    @property
    def definitions(self) -> tuple[Mapping[str, object], ...]:
        return tuple(self._definitions[name] for name in self.names)

    @property
    def recorded_receipts(self) -> tuple[InvestigationReceipt, ...]:
        values = [receipt for toolbox in set(self._by_name.values()) for receipt in toolbox.recorded_receipts]
        return tuple(sorted(values, key=lambda item: (item.tool_name, item.arguments_hash, item.result_hash)))

    def call(self, name: str, arguments: Mapping[str, Any]) -> ResearchToolResult:
        toolbox = self._by_name.get(name)
        if toolbox is None:
            fail(FailureCode.UNAUTHORIZED_CAPABILITY, "undeclared research tool")
        return toolbox.call(name, arguments)


class InMemoryResearchToolbox:
    """Deterministic fake used until the separately owned toolbox is integrated."""

    def __init__(
        self,
        *,
        task: ResearchAgentTask,
        handlers: Mapping[str, Callable[[Mapping[str, Any]], ResearchToolResult]],
    ) -> None:
        if tuple(sorted(handlers)) != task.allowed_tools:
            fail(FailureCode.UNAUTHORIZED_CAPABILITY, "toolbox must exactly match task whitelist")
        self.task = task
        self._handlers = dict(handlers)
        self._receipts: list[InvestigationReceipt] = []

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._handlers))

    @property
    def definitions(self) -> tuple[Mapping[str, object], ...]:
        return tuple(
            {
                "name": name,
                "description": "Read an approved frozen research view.",
                "parameters": {
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                    "required": ["query"],
                    "additionalProperties": False,
                },
                "readonly": True,
            }
            for name in self.names
        )

    @property
    def recorded_receipts(self) -> tuple[InvestigationReceipt, ...]:
        return tuple(sorted(self._receipts, key=lambda item: (item.tool_name, item.arguments_hash)))

    def call(self, name: str, arguments: Mapping[str, Any]) -> ResearchToolResult:
        if name not in self._handlers:
            fail(FailureCode.UNAUTHORIZED_CAPABILITY, "undeclared research tool")
        if set(arguments) != {"query"} or not isinstance(arguments.get("query"), str):
            fail(FailureCode.INVALID_VALUE, "research tool requires exactly one string query")
        lowered = json.dumps(arguments, ensure_ascii=False).casefold()
        if any(item in lowered for item in _FORBIDDEN_TEXT):
            fail(FailureCode.UNAUTHORIZED_CAPABILITY, "research tool request contains a forbidden capability")
        result = self._handlers[name](arguments)
        rendered = json.dumps(result.payload, ensure_ascii=False, sort_keys=True, default=str).casefold()
        if any(item in rendered for item in _FORBIDDEN_TEXT):
            fail(FailureCode.UNAUTHORIZED_CAPABILITY, "research tool result contains privileged material")
        if result.available_at > self.task.decision_time:
            fail(FailureCode.PIT_VIOLATION, "research tool result was unavailable at decision time")
        self._receipts.append(self.receipt(name, arguments, result))
        return result

    def receipt(self, name: str, arguments: Mapping[str, Any], result: ResearchToolResult) -> InvestigationReceipt:
        return InvestigationReceipt(
            tool_name=name,
            arguments_hash=canonical_hash(dict(arguments)),
            result_hash=canonical_hash(dict(result.payload)),
            available_at=result.available_at,
        )


__all__ = [
    "CompositeResearchToolbox", "InMemoryResearchToolbox", "ResearchToolResult",
    "ResearchToolboxProtocol",
]
