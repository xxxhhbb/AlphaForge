"""Machine selection of one to six complementary research groups.

Selection uses only Campaign-visible context and historical *visible* quality.
Sealed metrics and LLM preference are deliberately absent from this API.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from ..contracts import CanonicalContract, FailureCode
from ..contracts.base import fail, require_identifier, require_text
from ..research_campaign import MethodModel, ResearchMode
from ..research_os import ResearchGroupAssignment


SCHEMA_VERSION = "alphaforge.research-group-selection/v1"
MAX_RESEARCH_GROUPS = 6
FORBIDDEN_CONTEXT_KEYS = frozenset({
    "sealed", "sealed_metrics", "sealed_rank", "test_metrics", "promotion_verdict",
    "official_metrics", "orders", "target_weights", "risk_approval",
})


@dataclass(frozen=True)
class GroupCapabilityManifest(CanonicalContract):
    group_id: str
    role_name: str
    topic_tags: tuple[str, ...]
    method_models: tuple[MethodModel, ...]
    data_fields: tuple[str, ...]
    tool_whitelist: tuple[str, ...]
    visible_quality_score: float = 0.5
    budget_efficiency_score: float = 0.5
    enabled: bool = True
    schema_version: ClassVar[str] = SCHEMA_VERSION

    def __post_init__(self) -> None:
        require_identifier(self.group_id, "group_id")
        require_text(self.role_name, "role_name")
        for name in ("topic_tags", "data_fields", "tool_whitelist"):
            value = getattr(self, name)
            if not isinstance(value, tuple) or not value:
                fail(FailureCode.MISSING_REQUIRED_FIELD, f"{name} is required", field=name)
            if len(value) != len(set(value)):
                fail(FailureCode.INVALID_VALUE, f"{name} contains duplicates", field=name)
        if not self.method_models:
            fail(FailureCode.MISSING_REQUIRED_FIELD, "method_models are required")
        for name in ("visible_quality_score", "budget_efficiency_score"):
            if not 0.0 <= float(getattr(self, name)) <= 1.0:
                fail(FailureCode.INVALID_VALUE, f"{name} must be in [0,1]", field=name)
        lowered = {tool.lower() for tool in self.tool_whitelist}
        if lowered & {"shell", "bash", "write_file", "execute_python", "promotion", "orders"}:
            fail(FailureCode.UNAUTHORIZED_CAPABILITY, "group tools must be read-only", field="tool_whitelist")


@dataclass(frozen=True)
class GroupSelectionContext(CanonicalContract):
    campaign_hash: str
    mode: ResearchMode
    method_models: tuple[MethodModel, ...]
    question_tags: tuple[str, ...]
    available_fields: tuple[str, ...]
    max_groups: int = MAX_RESEARCH_GROUPS
    max_candidates_per_group: int = 2
    context_keys: tuple[str, ...] = ()
    schema_version: ClassVar[str] = SCHEMA_VERSION

    def __post_init__(self) -> None:
        from ..contracts.base import require_hash

        require_hash(self.campaign_hash, "campaign_hash")
        if not isinstance(self.mode, ResearchMode):
            fail(FailureCode.INVALID_VALUE, "mode must be ResearchMode")
        if not self.method_models or not self.question_tags:
            fail(FailureCode.MISSING_REQUIRED_FIELD, "methods and question tags are required")
        if not 1 <= self.max_groups <= MAX_RESEARCH_GROUPS:
            fail(FailureCode.INVALID_VALUE, "max_groups must be 1..6")
        if not 1 <= self.max_candidates_per_group <= 3:
            fail(FailureCode.INVALID_VALUE, "max_candidates_per_group must be 1..3")
        forbidden = {key.lower() for key in self.context_keys} & FORBIDDEN_CONTEXT_KEYS
        if forbidden:
            fail(FailureCode.UNAUTHORIZED_CAPABILITY, "forbidden context supplied to selector", fields=tuple(sorted(forbidden)))


@dataclass(frozen=True)
class SelectedGroupContext(CanonicalContract):
    group_id: str
    campaign_hash: str
    allowed_context_keys: tuple[str, ...]
    tool_whitelist: tuple[str, ...]
    schema_version: ClassVar[str] = SCHEMA_VERSION

    def __post_init__(self) -> None:
        from ..contracts.base import require_hash

        require_identifier(self.group_id, "group_id")
        require_hash(self.campaign_hash, "campaign_hash")
        forbidden = {key.lower() for key in self.allowed_context_keys} & FORBIDDEN_CONTEXT_KEYS
        if forbidden:
            fail(FailureCode.UNAUTHORIZED_CAPABILITY, "selected context contains forbidden keys")


@dataclass(frozen=True)
class GroupSelectionResult(CanonicalContract):
    assignments: tuple[ResearchGroupAssignment, ...]
    isolated_contexts: tuple[SelectedGroupContext, ...]
    excluded_group_ids: tuple[str, ...]
    schema_version: ClassVar[str] = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if not 1 <= len(self.assignments) <= MAX_RESEARCH_GROUPS:
            fail(FailureCode.INVALID_VALUE, "selection must contain one to six groups")
        selected = tuple(item.group_id for item in self.assignments)
        if selected != tuple(item.group_id for item in self.isolated_contexts):
            fail(FailureCode.INVALID_STATE, "context order must match selected groups")
        if set(selected) & set(self.excluded_group_ids):
            fail(FailureCode.INVALID_STATE, "excluded group received context")


class DeterministicResearchGroupSelector:
    """Select primary, complementary, and adversarial groups by fixed policy."""

    def __init__(self, manifests: tuple[GroupCapabilityManifest, ...] | None = None) -> None:
        self.manifests = manifests or DEFAULT_GROUP_MANIFESTS
        if len({item.group_id for item in self.manifests}) != len(self.manifests):
            fail(FailureCode.INVALID_VALUE, "duplicate group manifests")

    def select(self, context: GroupSelectionContext) -> GroupSelectionResult:
        scored: list[tuple[float, GroupCapabilityManifest, tuple[str, ...]]] = []
        question = set(context.question_tags)
        methods = set(context.method_models)
        fields = set(context.available_fields)
        for manifest in self.manifests:
            if not manifest.enabled:
                continue
            needed = set(manifest.data_fields)
            if not needed.issubset(fields):
                # ``data_fields`` are executable minimums, not soft topic hints.
                # A group without its minimum PIT-visible inputs must not receive
                # campaign context and then improvise unavailable data.
                continue
            topic_overlap = len(question & set(manifest.topic_tags)) / max(1, len(question))
            method_overlap = len(methods & set(manifest.method_models)) / max(1, len(methods))
            data_readiness = len(fields & needed) / max(1, len(needed))
            complementarity = _complementarity(context.mode, manifest.group_id)
            if topic_overlap == 0.0 and complementarity < 0.8:
                # A generic method overlap is not enough to route an unrelated
                # desk. Keep only question-relevant or explicitly designated
                # complementary/adversarial groups.
                continue
            score = (
                0.35 * topic_overlap
                + 0.20 * data_readiness
                + 0.15 * method_overlap
                + 0.15 * complementarity
                + 0.10 * manifest.visible_quality_score
                + 0.05 * manifest.budget_efficiency_score
            )
            reasons = (
                f"question_relevance={topic_overlap:.3f}",
                f"data_readiness={data_readiness:.3f}",
                f"method_fit={method_overlap:.3f}",
                f"complementarity={complementarity:.3f}",
            )
            scored.append((score, manifest, reasons))
        if not scored:
            fail(FailureCode.ADAPTER_UNAVAILABLE, "no enabled research group")
        scored.sort(key=lambda item: (-item[0], item[1].group_id))
        chosen: list[tuple[float, GroupCapabilityManifest, tuple[str, ...]]] = []
        chosen_topics: set[str] = set()
        for raw_score, manifest, reasons in scored:
            redundancy = 0.10 if chosen_topics and set(manifest.topic_tags) <= chosen_topics else 0.0
            final_score = max(0.0, raw_score - redundancy)
            if final_score <= 0.0 and chosen:
                continue
            chosen.append((final_score, manifest, reasons + (f"redundancy_penalty={redundancy:.3f}",)))
            chosen_topics.update(manifest.topic_tags)
            if len(chosen) >= context.max_groups:
                break
        roles = ("primary", "complementary", "adversarial") + ("specialist",) * 3
        assignments = tuple(
            ResearchGroupAssignment(
                group_id=manifest.group_id,
                role=roles[index],
                score=round(score, 6),
                reasons=reasons,
                tool_whitelist=manifest.tool_whitelist,
                candidate_budget=context.max_candidates_per_group,
            )
            for index, (score, manifest, reasons) in enumerate(chosen)
        )
        isolated = tuple(
            SelectedGroupContext(
                group_id=item.group_id,
                campaign_hash=context.campaign_hash,
                allowed_context_keys=context.context_keys,
                tool_whitelist=item.tool_whitelist,
            )
            for item in assignments
        )
        selected_ids = {item.group_id for item in assignments}
        excluded = tuple(sorted(item.group_id for item in self.manifests if item.group_id not in selected_ids))
        return GroupSelectionResult(assignments=assignments, isolated_contexts=isolated, excluded_group_ids=excluded)


def _complementarity(mode: ResearchMode, group_id: str) -> float:
    preferred = {
        ResearchMode.NEW_ALPHA_EXPLORATION: {"market_structure": 1.0, "fundamental": 0.8, "meta_alpha": 0.9},
        ResearchMode.VALIDATE_IDEA: {"market_structure": 1.0, "fundamental": 0.9, "expectations_events": 0.9},
        ResearchMode.IMPROVE_EXISTING_FACTOR: {"meta_alpha": 1.0, "market_structure": 0.9},
        ResearchMode.PORTFOLIO_GAP_SEARCH: {"meta_alpha": 1.0, "market_structure": 0.9},
        ResearchMode.DIAGNOSE_FACTOR_PORTFOLIO: {"meta_alpha": 1.0, "market_structure": 1.0, "macro_regime": 1.0},
        ResearchMode.EVENT_MARKET_TOPIC: {"expectations_events": 1.0, "macro_regime": 0.9, "market_structure": 0.9},
    }
    return preferred.get(mode, {}).get(group_id, 0.4)


def _manifest(group_id: str, role: str, topics: tuple[str, ...], methods: tuple[MethodModel, ...], fields: tuple[str, ...], tools: tuple[str, ...]) -> GroupCapabilityManifest:
    return GroupCapabilityManifest(
        group_id=group_id, role_name=role, topic_tags=topics, method_models=methods,
        data_fields=fields, tool_whitelist=tools,
    )


DEFAULT_GROUP_MANIFESTS = (
    _manifest("fundamental", "基本面研究", ("fundamental", "quality", "value", "earnings"), (MethodModel.HYPOTHESIS_DRIVEN, MethodModel.RESIDUAL_INCREMENTAL), ("roe", "market_cap"), ("query_fundamentals", "query_factor_library")),
    _manifest("expectations_events", "预期与事件研究", ("event", "earnings", "expectations", "announcement"), (MethodModel.EVENT_STUDY, MethodModel.HYPOTHESIS_DRIVEN), ("forecast_profit_change_min", "close"), ("query_documents", "read_market_data")),
    _manifest("market_structure", "市场结构研究", ("momentum", "reversal", "liquidity", "volume", "cost"), (MethodModel.HYPOTHESIS_DRIVEN, MethodModel.DATA_ANOMALY, MethodModel.DIAGNOSTIC_ATTRIBUTION), ("close", "volume"), ("read_market_data", "query_factor_library")),
    _manifest("alternative_data", "另类数据研究", ("alternative", "sentiment", "news", "document"), (MethodModel.HYPOTHESIS_DRIVEN, MethodModel.NEW_DATA_SOURCE_EVALUATION if hasattr(MethodModel, "NEW_DATA_SOURCE_EVALUATION") else MethodModel.DATA_ANOMALY), ("document", "available_at"), ("query_documents",)),
    _manifest("macro_regime", "宏观状态研究", ("macro", "regime", "volatility", "momentum"), (MethodModel.REGIME_CONDITIONING, MethodModel.DIAGNOSTIC_ATTRIBUTION), ("shibor_overnight", "close"), ("read_market_data", "query_macro")),
    _manifest("meta_alpha", "元阿尔法研究", ("momentum", "incrementality", "portfolio", "diagnosis", "correlation"), (MethodModel.RESIDUAL_INCREMENTAL, MethodModel.ADVERSARIAL_FALSIFICATION, MethodModel.DIAGNOSTIC_ATTRIBUTION), ("close",), ("query_factor_library", "query_visible_evidence")),
)
