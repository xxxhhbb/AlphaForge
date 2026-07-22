"""Desk-specific read-only tool wrappers for bounded ReAct exploration."""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Callable, Mapping

from ..contracts import CanonicalContract, FailureCode
from ..contracts.base import require_identifier, require_text
from .models import DeskId, coerce_desk_id
from .safety import (
    assert_no_forbidden_content,
    assert_safe_tool_name,
    canonical_hash,
    fail,
    summarize_mapping,
)


ToolHandler = Callable[[Mapping[str, Any]], Mapping[str, Any]]


@dataclass(frozen=True, kw_only=True)
class ToolCallSummary(CanonicalContract):
    tool_name: str
    arguments_hash: str
    result_hash: str
    result_summary: Mapping[str, Any]

    def __post_init__(self) -> None:
        require_identifier(self.tool_name, "tool_name")
        assert_safe_tool_name(self.tool_name)
        require_identifier(self.arguments_hash, "arguments_hash")
        require_identifier(self.result_hash, "result_hash")
        assert_no_forbidden_content(dict(self.result_summary), where="tool_summary")
        object.__setattr__(self, "result_summary", MappingProxyType(dict(self.result_summary)))


class ReadOnlyTool:
    """A small schema-checked tool object; handlers are caller-supplied fixtures."""

    def __init__(
        self,
        *,
        name: str,
        description: str,
        input_schema: Mapping[str, Any],
        result_allowlist: tuple[str, ...],
        handler: ToolHandler,
    ) -> None:
        require_identifier(name, "tool.name")
        assert_safe_tool_name(name)
        require_text(description, "tool.description")
        assert_no_forbidden_content(description, where=f"tool.{name}.description")
        self.name = name
        self.description = description
        self.input_schema = self._validate_schema(input_schema)
        self.result_allowlist = tuple(result_allowlist)
        if not self.result_allowlist:
            fail(FailureCode.INVALID_VALUE, "tool result allowlist cannot be empty")
        for key in self.result_allowlist:
            require_identifier(key, "result_allowlist")
        self._handler = handler

    @staticmethod
    def _validate_schema(schema: Mapping[str, Any]) -> Mapping[str, Any]:
        if schema.get("type") != "object":
            fail(FailureCode.PROVIDER_SCHEMA_REJECTED, "tool input schema must be an object")
        if schema.get("additionalProperties") is not False:
            fail(FailureCode.PROVIDER_SCHEMA_REJECTED, "tool input schema must deny extra properties")
        properties = schema.get("properties")
        if not isinstance(properties, Mapping):
            fail(FailureCode.PROVIDER_SCHEMA_REJECTED, "tool input schema needs properties")
        required = tuple(schema.get("required", ()))
        for key, definition in properties.items():
            require_identifier(key, "tool_schema_property")
            assert_no_forbidden_content(key, where="tool_schema")
            if not isinstance(definition, Mapping) or definition.get("type") not in {"string", "integer", "number", "boolean"}:
                fail(FailureCode.PROVIDER_SCHEMA_REJECTED, "tool schema uses unsupported property type")
        for key in required:
            if key not in properties:
                fail(FailureCode.PROVIDER_SCHEMA_REJECTED, "tool schema requires an unknown property")
        return MappingProxyType({"type": "object", "properties": dict(properties), "required": required, "additionalProperties": False})

    def call(self, arguments: Mapping[str, Any]) -> ToolCallSummary:
        self._validate_arguments(arguments)
        assert_no_forbidden_content(arguments, where=f"tool.{self.name}.arguments")
        result = self._handler(MappingProxyType(dict(arguments)))
        if not isinstance(result, Mapping):
            fail(FailureCode.PROVIDER_SCHEMA_REJECTED, "tool returned a non-mapping result")
        summary = summarize_mapping(
            result,
            allowed_keys=self.result_allowlist,
            where=f"tool.{self.name}.result",
        )
        return ToolCallSummary(
            tool_name=self.name,
            arguments_hash=canonical_hash(dict(arguments)),
            result_hash=canonical_hash(summary),
            result_summary=summary,
        )

    def _validate_arguments(self, arguments: Mapping[str, Any]) -> None:
        if not isinstance(arguments, Mapping):
            fail(FailureCode.PROVIDER_SCHEMA_REJECTED, "tool arguments must be a mapping")
        properties = self.input_schema["properties"]
        required = set(self.input_schema["required"])
        unknown = set(arguments) - set(properties)
        missing = required - set(arguments)
        if unknown:
            fail(FailureCode.PROVIDER_SCHEMA_REJECTED, "tool arguments contain unknown fields")
        if missing:
            fail(FailureCode.MISSING_REQUIRED_FIELD, "tool arguments miss required fields")
        type_map = {"string": str, "integer": int, "number": (int, float), "boolean": bool}
        for key, value in arguments.items():
            expected = type_map[properties[key]["type"]]
            if isinstance(value, bool) and properties[key]["type"] in {"integer", "number"}:
                fail(FailureCode.PROVIDER_SCHEMA_REJECTED, "boolean is not numeric")
            if not isinstance(value, expected):
                fail(FailureCode.PROVIDER_SCHEMA_REJECTED, "tool argument has wrong type", field=key)


class ReadOnlyToolRegistry:
    """A filtered registry for one Desk; it has no access to hidden tools."""

    def __init__(self, *, desk_id: DeskId | str, tools: tuple[ReadOnlyTool, ...]) -> None:
        self.desk_id = coerce_desk_id(desk_id)
        by_name = {tool.name: tool for tool in tools}
        if len(by_name) != len(tools):
            fail(FailureCode.INVALID_IDENTIFIER, "tool registry has duplicate names")
        for name in by_name:
            if not name.startswith(f"{self.desk_id.value}."):
                fail(FailureCode.UNAUTHORIZED_CAPABILITY, "tool is not desk-specific", tool=name)
        self._tools = MappingProxyType(by_name)

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._tools))

    def call(self, name: str, arguments: Mapping[str, Any]) -> ToolCallSummary:
        assert_safe_tool_name(name)
        tool = self._tools.get(name)
        if tool is None:
            fail(
                FailureCode.UNAUTHORIZED_CAPABILITY,
                "undeclared or unavailable Research Desk tool",
                desk_id=self.desk_id.value,
                tool=name,
            )
        return tool.call(arguments)


def build_default_tools_for_desk(desk_id: DeskId | str) -> ReadOnlyToolRegistry:
    desk = coerce_desk_id(desk_id)

    def context_handler(_: Mapping[str, Any]) -> Mapping[str, Any]:
        return {
            "summary": f"{desk.value} visible research context",
            "coverage": 1.0,
        }

    def field_handler(_: Mapping[str, Any]) -> Mapping[str, Any]:
        fields_by_desk = {
            DeskId.FUNDAMENTAL: "book_value,market_cap,close",
            DeskId.EXPECTATIONS_EVENTS: "close,event_reaction",
            DeskId.MARKET_STRUCTURE: "close,volume,market_cap",
            DeskId.ALTERNATIVE_DATA: "close,volume,activity_proxy",
            DeskId.MACRO_REGIME: "close,macro_rate",
            DeskId.META_ALPHA: "close,registered_factor_summary",
        }
        return {
            "summary": fields_by_desk[desk],
            "coverage": 1.0,
        }

    schema = {"type": "object", "properties": {}, "required": (), "additionalProperties": False}
    return ReadOnlyToolRegistry(
        desk_id=desk,
        tools=(
            ReadOnlyTool(
                name=f"{desk.value}.read_context",
                description="Read redacted visible research context for this desk.",
                input_schema=schema,
                result_allowlist=("summary", "coverage"),
                handler=context_handler,
            ),
            ReadOnlyTool(
                name=f"{desk.value}.read_field_catalog",
                description="Read redacted visible field names for this desk.",
                input_schema=schema,
                result_allowlist=("summary", "coverage"),
                handler=field_handler,
            ),
        ),
    )
