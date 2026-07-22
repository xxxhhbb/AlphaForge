"""Strict read-only tool registry for one specialist research agent."""

from __future__ import annotations

from types import MappingProxyType
from typing import Any, Callable, Mapping

from ..contracts import FailureCode
from ..contracts.base import fail
from ..llm_desks.safety import canonical_hash
from ..llm_desks.tools import ReadOnlyTool, ToolCallSummary
from .profiles import SpecialistAgentProfile


ToolHandler = Callable[[Mapping[str, Any]], Mapping[str, Any]]


class RestrictedResearchToolRegistry:
    """Registry physically containing only one profile's declared read tools."""

    def __init__(self, *, profile: SpecialistAgentProfile, tools: tuple[ReadOnlyTool, ...]) -> None:
        by_name = {tool.name: tool for tool in tools}
        if len(by_name) != len(tools):
            fail(FailureCode.INVALID_IDENTIFIER, "specialist tool names must be unique")
        if set(by_name) != set(profile.required_tool_names):
            fail(
                FailureCode.UNAUTHORIZED_CAPABILITY,
                "specialist registry must exactly match the profile whitelist",
            )
        prefix = f"{profile.specialist_id}."
        if any(not name.startswith(prefix) for name in by_name):
            fail(FailureCode.UNAUTHORIZED_CAPABILITY, "tool belongs to another specialist")
        self.profile = profile
        self._tools = MappingProxyType(by_name)

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._tools))

    @property
    def definitions(self) -> tuple[Mapping[str, Any], ...]:
        return tuple(
            MappingProxyType(
                {
                    "name": tool.name,
                    "description": tool.description,
                    "input_schema": tool.input_schema,
                }
            )
            for tool in sorted(self._tools.values(), key=lambda item: item.name)
        )

    def call(self, name: str, arguments: Mapping[str, Any]) -> ToolCallSummary:
        tool = self._tools.get(name)
        if tool is None:
            fail(FailureCode.UNAUTHORIZED_CAPABILITY, "undeclared specialist tool", tool=name)
        return tool.call(arguments)


def build_fixture_registry(
    profile: SpecialistAgentProfile,
    *,
    available_fields: tuple[str, ...],
    handlers: Mapping[str, ToolHandler] | None = None,
) -> RestrictedResearchToolRegistry:
    """Build deterministic offline tools; production adapters replace handlers."""

    supplied = dict(handlers or {})
    schema = {
        "type": "object",
        "properties": {"query": {"type": "string"}},
        "required": ("query",),
        "additionalProperties": False,
    }

    def default_handler(name: str) -> ToolHandler:
        def handler(arguments: Mapping[str, Any]) -> Mapping[str, Any]:
            material = {
                "specialist": profile.specialist_id,
                "tool": name,
                "query": arguments["query"],
                "fields": available_fields,
            }
            if name.endswith("describe_factor_language"):
                summary = (
                    "field, lag, rolling_mean, rolling_std, pct_change, subtract, add, "
                    "multiply, divide_safe, winsorize, rank_cross_section, "
                    "zscore_cross_section, neutralize_by_group"
                )
            else:
                summary = f"offline visible summary for {name}; fields={','.join(available_fields)}"
            return {
                "summary": summary,
                "coverage": 1.0,
                "source_hash": canonical_hash(material),
                "available_at": "fixture-visible-at-decision-time",
            }

        return handler

    tools: list[ReadOnlyTool] = []
    for name in profile.required_tool_names:
        tools.append(
            ReadOnlyTool(
                name=name,
                description="Return a redacted, research-visible summary from an approved read-only source.",
                input_schema=schema,
                result_allowlist=("summary", "coverage", "source_hash", "available_at"),
                handler=supplied.get(name, default_handler(name)),
            )
        )
    return RestrictedResearchToolRegistry(profile=profile, tools=tuple(tools))

