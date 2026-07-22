"""Research-visible frozen-material tools for Real LLM Desk proposals."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import Any, Mapping

from ..contracts import CanonicalContract, FailureCode
from ..contracts.base import require_aware_datetime, require_hash, require_identifier, require_text
from ..llm_desks.api import (
    BoundedReActExplorer,
    DeskId,
    LLMDeskStage,
    ProviderSession,
    ReactAction,
    ReactLimits,
    ReadOnlyTool,
    ReadOnlyToolRegistry,
    ToolCallSummary,
)
from ..llm_desks.models import coerce_desk_id
from ..llm_desks.react import ReactPlanner, ReactExplorationResult
from ..llm_desks.safety import assert_no_forbidden_content, canonical_hash, fail


REAL_LLM_RESEARCH_TOOLS_SCHEMA_VERSION = "veriquant.real-llm-research-tools/v1"
_VISIBLE_STAGES = (LLMDeskStage.HYPOTHESIS, LLMDeskStage.FACTOR_DRAFT)


def _reject_pathlike(value: Any, *, where: str) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if "path" in str(key).lower() or "file" in str(key).lower():
                fail(FailureCode.UNAUTHORIZED_CAPABILITY, "path-like fields are not research-visible", where=where)
            _reject_pathlike(item, where=f"{where}.{key}")
    elif isinstance(value, (tuple, list)):
        for index, item in enumerate(value):
            _reject_pathlike(item, where=f"{where}[{index}]")
    elif isinstance(value, str):
        lowered = value.lower()
        if ".." in lowered or "\\" in lowered or "://" in lowered:
            fail(FailureCode.UNAUTHORIZED_CAPABILITY, "path-like or URL material is not research-visible", where=where)
        if any(token in lowered for token in ("future_return", "future return", "label_", "target_return")):
            fail(FailureCode.UNAUTHORIZED_CAPABILITY, "future labels are not research-visible", where=where)


@dataclass(frozen=True, kw_only=True)
class CitationSummary(CanonicalContract):
    SCHEMA_VERSION = REAL_LLM_RESEARCH_TOOLS_SCHEMA_VERSION

    citation_id: str
    source_kind: str
    title: str
    published_at: datetime
    source_hash: str
    excerpt: str

    def __post_init__(self) -> None:
        require_identifier(self.citation_id, "citation_id")
        require_identifier(self.source_kind, "source_kind")
        require_text(self.title, "title")
        require_aware_datetime(self.published_at, "published_at")
        require_hash(self.source_hash, "source_hash")
        require_text(self.excerpt, "excerpt")
        assert_no_forbidden_content(self.to_safe_mapping(), where="citation")
        _reject_pathlike(self.to_safe_mapping(), where="citation")

    def to_safe_mapping(self) -> Mapping[str, Any]:
        return {
            "citation_id": self.citation_id,
            "source_kind": self.source_kind,
            "title": self.title,
            "published_at": self.published_at.isoformat(),
            "source_hash": self.source_hash,
            "excerpt": self.excerpt[:240],
        }


@dataclass(frozen=True, kw_only=True)
class ResearchVisibleContext(CanonicalContract):
    SCHEMA_VERSION = REAL_LLM_RESEARCH_TOOLS_SCHEMA_VERSION

    desk_id: DeskId
    dataset_manifest_hash: str
    dataset_card_summary: str
    pit_field_coverage: Mapping[str, float]
    availability_summary: str
    historical_universe_summary: str
    allowed_fields: tuple[str, ...]
    citations: tuple[CitationSummary, ...] = ()
    lineage_summary: str = "visible lineage unavailable"
    validation_summary: str = "visible validation unavailable"

    def __post_init__(self) -> None:
        object.__setattr__(self, "desk_id", coerce_desk_id(self.desk_id))
        require_hash(self.dataset_manifest_hash, "dataset_manifest_hash")
        for field in ("dataset_card_summary", "availability_summary", "historical_universe_summary", "lineage_summary", "validation_summary"):
            require_text(getattr(self, field), field)
        coverage: dict[str, float] = {}
        for key, value in self.pit_field_coverage.items():
            require_identifier(key, "coverage_field")
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0.0 <= float(value) <= 1.0:
                fail(FailureCode.INVALID_VALUE, "coverage values must be in [0, 1]")
            coverage[key] = float(value)
        object.__setattr__(self, "pit_field_coverage", MappingProxyType(coverage))
        if not self.allowed_fields:
            fail(FailureCode.MISSING_REQUIRED_FIELD, "allowed_fields is required")
        for field in self.allowed_fields:
            require_identifier(field, "allowed_field")
            if any(token in field.lower() for token in ("future", "label", "target_return")):
                fail(FailureCode.UNAUTHORIZED_CAPABILITY, "future label field is not research-visible")
        safe = self.to_tool_material()
        assert_no_forbidden_content(safe, where="research_visible_context")
        _reject_pathlike(safe, where="research_visible_context")

    def to_tool_material(self) -> Mapping[str, Any]:
        return {
            "desk_id": self.desk_id.value,
            "dataset_manifest_hash": self.dataset_manifest_hash,
            "dataset_card_summary": self.dataset_card_summary,
            "pit_field_coverage": dict(self.pit_field_coverage),
            "availability_summary": self.availability_summary,
            "historical_universe_summary": self.historical_universe_summary,
            "allowed_fields": self.allowed_fields,
            "citations": tuple(item.to_safe_mapping() for item in self.citations),
            "lineage_summary": self.lineage_summary,
            "validation_summary": self.validation_summary,
        }


@dataclass(frozen=True, kw_only=True)
class DeskToolAuthorityManifest(CanonicalContract):
    SCHEMA_VERSION = REAL_LLM_RESEARCH_TOOLS_SCHEMA_VERSION

    desk_id: DeskId
    tool_names: tuple[str, ...]
    allowed_stages: tuple[LLMDeskStage, ...] = _VISIBLE_STAGES
    read_scopes: tuple[str, ...] = ("dataset_summary", "field_coverage", "universe_history", "citation_summary", "lineage_visible", "validation_visible")
    no_mutation_surface_hash: str = canonical_hash({"no_mutation": True, "capabilities": ("tool", "finish")})

    def __post_init__(self) -> None:
        object.__setattr__(self, "desk_id", coerce_desk_id(self.desk_id))
        if not self.tool_names:
            fail(FailureCode.MISSING_REQUIRED_FIELD, "manifest needs tool names")
        for name in self.tool_names:
            require_identifier(name, "tool_name")
            if not name.startswith(f"{self.desk_id.value}."):
                fail(FailureCode.UNAUTHORIZED_CAPABILITY, "manifest tool is outside desk namespace")
        for stage in self.allowed_stages:
            if stage not in _VISIBLE_STAGES:
                fail(FailureCode.UNAUTHORIZED_CAPABILITY, "research tools are proposal-stage only")
        for scope in self.read_scopes:
            require_identifier(scope, "read_scope")
        require_hash(self.no_mutation_surface_hash, "no_mutation_surface_hash")
        assert_no_forbidden_content(self.to_dict(), where="tool_manifest")
        _reject_pathlike(self.to_dict(), where="tool_manifest")


class FrozenResearchToolFactory:
    """Build desk-scoped read-only registries from frozen, redacted context."""

    def __init__(self, *, contexts: Mapping[DeskId | str, ResearchVisibleContext]) -> None:
        normalized = {coerce_desk_id(key): value for key, value in contexts.items()}
        for desk_id, context in normalized.items():
            if context.desk_id is not desk_id:
                fail(FailureCode.INVALID_VALUE, "context desk_id does not match map key")
        self._contexts = MappingProxyType(normalized)

    def context_for_desk(self, desk_id: DeskId | str) -> ResearchVisibleContext:
        desk = coerce_desk_id(desk_id)
        try:
            return self._contexts[desk]
        except KeyError:
            fail(FailureCode.MISSING_REQUIRED_FIELD, "missing research-visible context for desk", desk_id=desk.value)

    def manifest_for_desk(self, desk_id: DeskId | str) -> DeskToolAuthorityManifest:
        desk = coerce_desk_id(desk_id)
        return DeskToolAuthorityManifest(desk_id=desk, tool_names=self._tool_names(desk))

    def registry_for_desk(self, desk_id: DeskId | str) -> ReadOnlyToolRegistry:
        desk = coerce_desk_id(desk_id)
        context = self.context_for_desk(desk)
        schema = {"type": "object", "properties": {}, "required": (), "additionalProperties": False}

        def material() -> Mapping[str, Any]:
            return context.to_tool_material()

        def one(key: str, *extra: str) -> Mapping[str, Any]:
            payload = material()
            chosen = {item: payload[item] for item in (key, *extra)}
            return {"summary": repr(chosen)[:400], "content_hash": canonical_hash(chosen), "coverage": min(context.pit_field_coverage.values(), default=0.0)}

        tools = (
            ReadOnlyTool(
                name=f"{desk.value}.dataset_summary",
                description="Read frozen dataset and manifest summary for this desk.",
                input_schema=schema,
                result_allowlist=("summary", "content_hash", "coverage"),
                handler=lambda _: one("dataset_card_summary", "dataset_manifest_hash"),
            ),
            ReadOnlyTool(
                name=f"{desk.value}.field_coverage",
                description="Read visible field coverage and approved field names.",
                input_schema=schema,
                result_allowlist=("summary", "content_hash", "coverage"),
                handler=lambda _: one("pit_field_coverage", "allowed_fields"),
            ),
            ReadOnlyTool(
                name=f"{desk.value}.universe_history",
                description="Read historical universe membership summary.",
                input_schema=schema,
                result_allowlist=("summary", "content_hash", "coverage"),
                handler=lambda _: one("historical_universe_summary", "availability_summary"),
            ),
            ReadOnlyTool(
                name=f"{desk.value}.citation_summary",
                description="Read frozen cited source summaries for this desk.",
                input_schema=schema,
                result_allowlist=("summary", "content_hash", "coverage"),
                handler=lambda _: one("citations"),
            ),
            ReadOnlyTool(
                name=f"{desk.value}.lineage_visible",
                description="Read research-visible lineage and validation summaries.",
                input_schema=schema,
                result_allowlist=("summary", "content_hash", "coverage"),
                handler=lambda _: one("lineage_summary", "validation_summary"),
            ),
        )
        return ReadOnlyToolRegistry(desk_id=desk, tools=tools)

    @staticmethod
    def _tool_names(desk: DeskId) -> tuple[str, ...]:
        return (
            f"{desk.value}.dataset_summary",
            f"{desk.value}.field_coverage",
            f"{desk.value}.universe_history",
            f"{desk.value}.citation_summary",
            f"{desk.value}.lineage_visible",
        )


class _ScriptedPlanner:
    def __init__(self, actions: tuple[ReactAction, ...]) -> None:
        self._actions = list(actions)

    def next_action(
        self,
        *,
        stage: LLMDeskStage,
        prompt_hash: str,
        observations: tuple[ToolCallSummary, ...],
        allowed_tools: tuple[str, ...],
    ) -> ReactAction:
        if self._actions:
            return self._actions.pop(0)
        return ReactAction(kind="finish")


class BoundedLLMReactPlanner:
    """Bounded tool/finish loop for hypothesis and factor-draft research only."""

    def __init__(self, *, limits: ReactLimits | None = None, planner: ReactPlanner | None = None) -> None:
        self.limits = limits or ReactLimits(max_iterations=4, max_tool_calls=3, max_tokens=768)
        self._planner = planner

    @classmethod
    def scripted(cls, actions: tuple[ReactAction, ...], *, limits: ReactLimits | None = None) -> "BoundedLLMReactPlanner":
        return cls(limits=limits, planner=_ScriptedPlanner(actions))

    def run(
        self,
        *,
        stage: LLMDeskStage,
        prompt_material: Mapping[str, Any],
        tools: ReadOnlyToolRegistry,
        session: ProviderSession,
    ) -> ReactExplorationResult:
        if stage not in _VISIBLE_STAGES:
            fail(FailureCode.UNAUTHORIZED_CAPABILITY, "research-visible tools are proposal-stage only")
        assert_no_forbidden_content(dict(prompt_material), where="research_visible_prompt")
        _reject_pathlike(prompt_material, where="research_visible_prompt")
        explorer = BoundedReActExplorer(limits=self.limits, planner=self._planner)
        return explorer.explore(stage=stage, prompt_material=prompt_material, tools=tools, session=session)
