"""Typed Research Desk identities, budgets, stages, statuses, and read-only tools."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping

from ..contracts import CanonicalContract, ContractValidationError, FailureCode
from ..contracts.base import require_identifier, require_nonempty_tuple, require_text


class DeskId(str, Enum):
    FUNDAMENTAL = "fundamental"
    EXPECTATIONS_EVENTS = "expectations_events"
    MARKET_STRUCTURE = "market_structure"
    ALTERNATIVE_DATA = "alternative_data"
    MACRO_REGIME = "macro_regime"
    META_ALPHA = "meta_alpha"


class BranchStage(str, Enum):
    HYPOTHESIS_PROPOSE = "hypothesis_propose"
    FACTOR_CONSTRUCT = "factor_construct"
    FACTOR_COMPILE_CALCULATE = "factor_compile_calculate"
    FACTOR_EVALUATE = "factor_evaluate"
    FEEDBACK = "feedback"


FIVE_STAGE_SEQUENCE: tuple[BranchStage, ...] = (
    BranchStage.HYPOTHESIS_PROPOSE,
    BranchStage.FACTOR_CONSTRUCT,
    BranchStage.FACTOR_COMPILE_CALCULATE,
    BranchStage.FACTOR_EVALUATE,
    BranchStage.FEEDBACK,
)


class TaskStatus(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    INCOMPLETE = "INCOMPLETE"
    BLOCKED = "BLOCKED"


def _fail(code: FailureCode, message: str, **details: Any) -> None:
    raise ContractValidationError(code, message, details=details)


@dataclass(frozen=True, kw_only=True)
class DeskBudget(CanonicalContract):
    family_units: int
    max_tokens: int
    max_iterations: int
    max_retries: int
    timeout_seconds: float

    def __post_init__(self) -> None:
        for name in ("family_units", "max_tokens", "max_iterations"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                _fail(FailureCode.INVALID_VALUE, f"{name} must be a positive integer")
        if isinstance(self.max_retries, bool) or not isinstance(self.max_retries, int) or self.max_retries < 0:
            _fail(FailureCode.INVALID_VALUE, "max_retries must be a non-negative integer")
        if isinstance(self.timeout_seconds, bool) or not isinstance(self.timeout_seconds, (int, float)):
            _fail(FailureCode.INVALID_VALUE, "timeout_seconds must be numeric")
        if not 0.0 < float(self.timeout_seconds) <= 300.0:
            _fail(FailureCode.INVALID_VALUE, "timeout_seconds must be in (0, 300]")
        if self.max_iterations < 3:
            _fail(FailureCode.INVALID_VALUE, "provider budget must cover three fixed LLM-fill slots")


@dataclass(frozen=True, kw_only=True)
class DeskSpec(CanonicalContract):
    desk_id: DeskId
    role: str
    context: tuple[str, ...]
    tool_whitelist: tuple[str, ...]
    budget: DeskBudget
    artifact_namespace: str

    def __post_init__(self) -> None:
        if not isinstance(self.desk_id, DeskId):
            _fail(FailureCode.INVALID_VALUE, "desk_id must be DeskId")
        require_text(self.role, "role")
        require_nonempty_tuple(self.context, "context")
        require_nonempty_tuple(self.tool_whitelist, "tool_whitelist")
        if any(not isinstance(item, str) or not item for item in (*self.context, *self.tool_whitelist)):
            _fail(FailureCode.INVALID_VALUE, "desk context and tools must be non-empty strings")
        if len(set(self.tool_whitelist)) != len(self.tool_whitelist):
            _fail(FailureCode.INVALID_VALUE, "tool whitelist contains duplicates")
        if not isinstance(self.budget, DeskBudget):
            _fail(FailureCode.INVALID_VALUE, "budget must be DeskBudget")
        require_identifier(self.artifact_namespace, "artifact_namespace")
        if self.artifact_namespace != self.desk_id.value:
            _fail(FailureCode.INVALID_VALUE, "artifact namespace must equal the isolated desk id")


def build_default_desks() -> tuple[DeskSpec, ...]:
    """Return six independent immutable desks; no desk inherits another's tools."""

    definitions = (
        (
            DeskId.FUNDAMENTAL,
            "Fundamental accounting and valuation researcher",
            ("Use only point-in-time financial records.", "Treat revisions as later knowledge."),
            ("read_fundamentals", "read_prices"),
        ),
        (
            DeskId.EXPECTATIONS_EVENTS,
            "Expectations and corporate-events researcher",
            ("Use only timestamped visible event proxies.", "Do not infer sealed outcomes."),
            ("read_events", "read_prices"),
        ),
        (
            DeskId.MARKET_STRUCTURE,
            "Market-structure and liquidity researcher",
            ("Use volume and price microstructure proxies.", "No broker or order access."),
            ("read_volume", "read_prices"),
        ),
        (
            DeskId.ALTERNATIVE_DATA,
            "Alternative-data proxy researcher",
            ("Use only sanitized offline fixtures.", "Document proxy coverage limitations."),
            ("read_alternative_fixture", "read_prices"),
        ),
        (
            DeskId.MACRO_REGIME,
            "Macro-regime researcher",
            ("Use only released macro fields.", "Regime labels are research-visible only."),
            ("read_macro", "read_prices"),
        ),
        (
            DeskId.META_ALPHA,
            "Meta-alpha and registered-factor researcher",
            ("Use redacted registered-factor summaries.", "Never read sealed scores."),
            ("read_registered_factor_summary", "read_prices"),
        ),
    )
    return tuple(
        DeskSpec(
            desk_id=desk_id,
            role=role,
            context=context,
            tool_whitelist=tools,
            budget=DeskBudget(
                family_units=1,
                max_tokens=2048,
                max_iterations=6,
                max_retries=1,
                timeout_seconds=5.0,
            ),
            artifact_namespace=desk_id.value,
        )
        for desk_id, role, context, tools in definitions
    )


@dataclass(frozen=True, kw_only=True)
class StaticReadOnlyTool:
    name: str
    response: Mapping[str, str | int | float | bool | None]
    readonly: bool = True

    def __post_init__(self) -> None:
        require_identifier(self.name, "tool.name")
        if self.readonly is not True:
            _fail(FailureCode.UNAUTHORIZED_CAPABILITY, "Research Desk tools must be read-only")
        if not isinstance(self.response, Mapping):
            _fail(FailureCode.INVALID_VALUE, "tool response must be a mapping")
        for key in self.response:
            lowered = str(key).lower()
            if any(token in lowered for token in ("sealed", "order", "risk_approval", "official_metrics")):
                _fail(FailureCode.UNAUTHORIZED_CAPABILITY, "tool response contains a forbidden field")
        object.__setattr__(self, "response", MappingProxyType(dict(self.response)))

    def execute(self) -> Mapping[str, str | int | float | bool | None]:
        return MappingProxyType(dict(self.response))


class DeskToolRegistry:
    """Per-desk filtered view; it has no path back to the full catalog."""

    def __init__(self, desk_id: DeskId, tools: Mapping[str, StaticReadOnlyTool]) -> None:
        self.desk_id = desk_id
        self._tools = MappingProxyType(dict(tools))

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._tools))

    def call(self, name: str) -> Mapping[str, str | int | float | bool | None]:
        tool = self._tools.get(name)
        if tool is None:
            _fail(
                FailureCode.UNAUTHORIZED_CAPABILITY,
                "tool is not declared for this Research Desk",
                desk=self.desk_id.value,
                tool=name,
            )
        return tool.execute()


class ReadOnlyToolCatalog:
    def __init__(self, tools: tuple[StaticReadOnlyTool, ...]) -> None:
        by_name = {tool.name: tool for tool in tools}
        if len(by_name) != len(tools):
            _fail(FailureCode.INVALID_VALUE, "tool catalog contains duplicate names")
        if any(tool.readonly is not True for tool in tools):
            _fail(FailureCode.UNAUTHORIZED_CAPABILITY, "tool catalog contains a mutating tool")
        self._tools = MappingProxyType(by_name)

    def for_desk(self, desk: DeskSpec) -> DeskToolRegistry:
        missing = set(desk.tool_whitelist) - set(self._tools)
        if missing:
            _fail(
                FailureCode.INVALID_IDENTIFIER,
                "desk declares an unavailable tool",
                desk=desk.desk_id.value,
                tools=tuple(sorted(missing)),
            )
        return DeskToolRegistry(
            desk.desk_id,
            {name: self._tools[name] for name in desk.tool_whitelist},
        )


def build_default_tool_catalog() -> ReadOnlyToolCatalog:
    return ReadOnlyToolCatalog(
        (
            StaticReadOnlyTool(name="read_fundamentals", response={"fields": "book_value,market_cap"}),
            StaticReadOnlyTool(name="read_prices", response={"fields": "close", "mode": "PIT"}),
            StaticReadOnlyTool(name="read_events", response={"proxy": "price_reaction", "mode": "visible"}),
            StaticReadOnlyTool(name="read_volume", response={"fields": "volume,market_cap"}),
            StaticReadOnlyTool(name="read_alternative_fixture", response={"proxy": "sanitized_volume_pattern"}),
            StaticReadOnlyTool(name="read_macro", response={"fields": "macro_rate", "mode": "released"}),
            StaticReadOnlyTool(name="read_registered_factor_summary", response={"scope": "redacted_visible"}),
        )
    )
