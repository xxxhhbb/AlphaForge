"""Deterministic data requirement planning for AlphaForge.

The planner answers a narrow question: given a research intent and currently
known data availability, what data would be required for official evaluation,
and is the official layer currently allowed to proceed?
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Any, ClassVar, Mapping

from ..contracts import CanonicalContract, FailureCode
from ..contracts.base import fail, require_hash, require_identifier, require_text
from ..research_campaign import ResearchMode
from ..research_os import AnswerLevel, DataNeed, DataRequirementPlan, ResearchIntent


DATA_PLANNING_SCHEMA_VERSION = "alphaforge.data-planning/v1"
DEFAULT_TRAIN_SESSIONS = 120
DEFAULT_VALIDATION_SESSIONS = 60
DEFAULT_BUFFER_SESSIONS = 20
# Two distinct symbols are the mechanical lower bound for a cross-sectional
# comparison. Statistical adequacy is decided later by the versioned Evidence
# policy; the product planner must not impose a 30-symbol smoke-test convention
# on every research question.
DEFAULT_MIN_SYMBOLS = 2


class StrEnum(str, Enum):
    """Python 3.10-compatible string enum."""


class PlannedFieldRole(StrEnum):
    REQUIRED = "REQUIRED"
    OPTIONAL = "OPTIONAL"
    PROXY = "PROXY"


@dataclass(frozen=True, kw_only=True)
class DataAvailabilitySnapshot(CanonicalContract):
    """Research-visible data availability envelope.

    This is a declaration of available scope, not a data snapshot and not
    official evidence.
    """

    SCHEMA_VERSION: ClassVar[str] = DATA_PLANNING_SCHEMA_VERSION

    availability_id: str
    available_fields: tuple[str, ...]
    available_sessions: int
    available_symbols: int
    provider_label: str = "UNKNOWN"
    snapshot_hash: str | None = None

    def __post_init__(self) -> None:
        require_identifier(self.availability_id, "availability_id")
        _text_tuple(self.available_fields, "available_fields", nonempty=False)
        for name in ("available_sessions", "available_symbols"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                fail(FailureCode.INVALID_VALUE, f"{name} must be a non-negative integer")
        require_identifier(self.provider_label, "provider_label")
        if self.snapshot_hash is not None:
            require_hash(self.snapshot_hash, "snapshot_hash")


@dataclass(frozen=True, kw_only=True)
class FieldAvailability(CanonicalContract):
    """One planned field and how it is treated by the official layer."""

    SCHEMA_VERSION: ClassVar[str] = DATA_PLANNING_SCHEMA_VERSION

    field_name: str
    role: PlannedFieldRole
    available: bool
    required_for_official: bool
    proxy_for: tuple[str, ...] = ()
    reason: str = ""

    def __post_init__(self) -> None:
        require_identifier(self.field_name, "field_name")
        if not isinstance(self.role, PlannedFieldRole):
            object.__setattr__(self, "role", PlannedFieldRole(self.role))
        if not isinstance(self.available, bool) or not isinstance(self.required_for_official, bool):
            fail(FailureCode.INVALID_VALUE, "field availability flags must be boolean")
        _text_tuple(self.proxy_for, "proxy_for", nonempty=False)
        if self.reason:
            require_text(self.reason, "reason")
        if self.required_for_official and self.role is PlannedFieldRole.PROXY:
            fail(FailureCode.INVALID_VALUE, "proxy fields cannot be mandatory official fields")


@dataclass(frozen=True, kw_only=True)
class DataPlanningResult(CanonicalContract):
    """Data plan plus availability verdict for one research intent."""

    SCHEMA_VERSION: ClassVar[str] = DATA_PLANNING_SCHEMA_VERSION

    intent_hash: str
    data_plan: DataRequirementPlan
    availability: DataAvailabilitySnapshot
    field_availability: tuple[FieldAvailability, ...]
    official_evaluation_ready: bool
    official_blockers: tuple[str, ...] = ()
    strongest_answer_level: AnswerLevel = AnswerLevel.CONCEPTUAL

    def __post_init__(self) -> None:
        require_hash(self.intent_hash, "intent_hash")
        if not isinstance(self.data_plan, DataRequirementPlan):
            fail(FailureCode.INVALID_VALUE, "data_plan must be DataRequirementPlan")
        if not isinstance(self.availability, DataAvailabilitySnapshot):
            fail(FailureCode.INVALID_VALUE, "availability must be DataAvailabilitySnapshot")
        if not isinstance(self.field_availability, tuple) or not self.field_availability:
            fail(FailureCode.MISSING_REQUIRED_FIELD, "field_availability is required")
        for item in self.field_availability:
            if not isinstance(item, FieldAvailability):
                fail(FailureCode.INVALID_VALUE, "field_availability must contain FieldAvailability")
        if len({(item.field_name, item.proxy_for) for item in self.field_availability}) != len(self.field_availability):
            fail(FailureCode.INVALID_VALUE, "field_availability contains duplicate fields")
        if not isinstance(self.official_evaluation_ready, bool):
            fail(FailureCode.INVALID_VALUE, "official_evaluation_ready must be boolean")
        _text_tuple(self.official_blockers, "official_blockers", nonempty=False)
        if not self.official_evaluation_ready and not self.official_blockers:
            fail(FailureCode.MISSING_REQUIRED_FIELD, "official blockers are required when official evaluation is not ready")
        if not isinstance(self.strongest_answer_level, AnswerLevel):
            object.__setattr__(self, "strongest_answer_level", AnswerLevel(self.strongest_answer_level))
        if self.official_evaluation_ready and self.strongest_answer_level is not AnswerLevel.OFFICIAL_EVALUATED:
            fail(FailureCode.INVALID_STATE, "official-ready data must expose OFFICIAL_EVALUATED as strongest level")


class DeterministicDataPlanner:
    """Plan required fields and sample scope without touching data services."""

    def __init__(
        self,
        *,
        train_sessions: int = DEFAULT_TRAIN_SESSIONS,
        validation_sessions: int = DEFAULT_VALIDATION_SESSIONS,
        buffer_sessions: int = DEFAULT_BUFFER_SESSIONS,
        min_symbol_count: int = DEFAULT_MIN_SYMBOLS,
    ) -> None:
        for value_name, value in {
            "train_sessions": train_sessions,
            "validation_sessions": validation_sessions,
            "buffer_sessions": buffer_sessions,
            "min_symbol_count": min_symbol_count,
        }.items():
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                fail(FailureCode.INVALID_VALUE, f"{value_name} must be a positive integer")
        self.train_sessions = train_sessions
        self.validation_sessions = validation_sessions
        self.buffer_sessions = buffer_sessions
        self.min_symbol_count = min_symbol_count

    def plan(
        self,
        intent: ResearchIntent,
        availability: DataAvailabilitySnapshot | None = None,
    ) -> DataPlanningResult:
        if not isinstance(intent, ResearchIntent):
            fail(FailureCode.INVALID_VALUE, "intent must be ResearchIntent")
        availability = availability or DataAvailabilitySnapshot(
            availability_id="availability.unknown",
            available_fields=(),
            available_sessions=0,
            available_symbols=0,
            provider_label="UNKNOWN",
        )
        lookback = intent.factor_lookback_sessions or 20
        label = intent.label_horizon_sessions
        embargo = max(5, label)
        fields = _field_requirements_for(intent)
        # Warm-up observations are consumed before the first eligible factor
        # score; they cannot double as train/validation observations.
        minimum_sessions = lookback + self.train_sessions + self.validation_sessions + label + embargo + self.buffer_sessions
        data_plan = DataRequirementPlan(
            universe=("CN_A_SHARE_PIT",),
            factor_lookback_sessions=lookback,
            label_horizon_sessions=label,
            train_sessions=self.train_sessions,
            validation_sessions=self.validation_sessions,
            embargo_sessions=embargo,
            buffer_sessions=self.buffer_sessions,
            minimum_total_sessions=minimum_sessions,
            minimum_symbol_count=self.min_symbol_count,
            needs=fields,
        )
        field_view = _field_availability(fields, availability)
        blockers = list(_scope_blockers(data_plan, availability))
        blockers.extend(_field_blockers(field_view))
        quick_or_non_factor = intent.mode in {
            ResearchMode.DIAGNOSE_FACTOR_PORTFOLIO,
            ResearchMode.NEW_DATA_SOURCE_EVALUATION,
            ResearchMode.PAPER_STRATEGY_REPLICATION,
            ResearchMode.MONITOR_DECAY_RETIREMENT,
        }
        official_ready = not blockers and not quick_or_non_factor
        if quick_or_non_factor:
            blockers.append("该研究模式默认先形成诊断或评估结论，不直接启动官方因子评价。")
        strongest = AnswerLevel.OFFICIAL_EVALUATED if official_ready else AnswerLevel.SOURCED_RESEARCH
        return DataPlanningResult(
            intent_hash=intent.artifact_hash,
            data_plan=data_plan,
            availability=availability,
            field_availability=field_view,
            official_evaluation_ready=official_ready,
            official_blockers=tuple(dict.fromkeys(blockers)),
            strongest_answer_level=strongest,
        )


def _field_requirements_for(intent: ResearchIntent) -> tuple[DataNeed, ...]:
    text = intent.question.lower()
    needs: list[DataNeed] = [
        DataNeed("close", True, reason="price feature, returns, and labels need PIT close."),
        DataNeed("volume", True, reason="visible cost, liquidity, and turnover checks need PIT volume."),
    ]
    if intent.mode is ResearchMode.PORTFOLIO_GAP_SEARCH:
        needs.append(DataNeed("registered_factor_summary", False, acceptable_proxies=("factor_library_summary",), reason="incremental research benefits from registered factor summaries."))
    if intent.mode is ResearchMode.DIAGNOSE_FACTOR_PORTFOLIO:
        needs.append(DataNeed("registered_factor_summary", True, reason="diagnosis needs the target factor or portfolio public evidence summary."))
        needs.append(DataNeed("macro_regime", False, acceptable_proxies=("index_return", "market_volatility"), reason="regime splits can use approved market-state proxies."))
    if intent.mode is ResearchMode.EVENT_MARKET_TOPIC or any(token in text for token in ("财报", "公告", "event", "earnings")):
        needs.append(DataNeed("event_time", True, reason="event studies require event_time."))
        needs.append(DataNeed("available_at", True, reason="event studies require market-available timestamps."))
    if any(token in text for token in ("上调", "预期修正", "盈利预测", "analyst", "estimate")):
        needs.append(DataNeed("earnings_revision", True, acceptable_proxies=("net_profit_yoy", "eps_forecast_change", "announcement_surprise"), reason="earnings revision hypotheses need an approved PIT estimate or proxy."))
    if any(token in text for token in ("北向", "northbound")):
        needs.append(DataNeed("northbound_flow", True, acceptable_proxies=("turnover", "volume"), reason="northbound-flow questions need an approved PIT capital-flow field; volume is only a proxy."))
    if any(token in text for token in ("市值", "大盘", "小盘", "规模", "size", "market cap")):
        needs.append(DataNeed("market_cap", True, reason="size-aware research needs PIT market capitalization."))
    else:
        needs.append(DataNeed("market_cap", False, acceptable_proxies=("amount",), reason="size control is useful but not mandatory for the first official pass."))
    if any(token in text for token in ("行业", "中性", "申万", "industry")):
        needs.append(DataNeed("industry", True, reason="industry-neutral research needs historical industry membership."))
    else:
        needs.append(DataNeed("industry", False, reason="industry grouping improves diagnostics and neutralization."))
    unique: dict[str, DataNeed] = {}
    for need in needs:
        current = unique.get(need.field_name)
        if current is None or (need.required and not current.required):
            unique[need.field_name] = need
    return tuple(unique[field] for field in sorted(unique))


def _field_availability(
    needs: tuple[DataNeed, ...],
    availability: DataAvailabilitySnapshot,
) -> tuple[FieldAvailability, ...]:
    available = set(availability.available_fields)
    rows: list[FieldAvailability] = []
    for need in needs:
        rows.append(
            FieldAvailability(
                field_name=need.field_name,
                role=PlannedFieldRole.REQUIRED if need.required else PlannedFieldRole.OPTIONAL,
                available=need.field_name in available,
                required_for_official=need.required,
                reason=need.reason,
            )
        )
        for proxy in need.acceptable_proxies:
            rows.append(
                FieldAvailability(
                    field_name=proxy,
                    role=PlannedFieldRole.PROXY,
                    available=proxy in available,
                    required_for_official=False,
                    proxy_for=(need.field_name,),
                    reason=f"Proxy candidate for {need.field_name}; never substitutes field semantics silently.",
                )
            )
    by_key: dict[tuple[str, tuple[str, ...]], FieldAvailability] = {}
    for row in rows:
        by_key[(row.field_name, row.proxy_for)] = row
    return tuple(by_key[key] for key in sorted(by_key))


def _scope_blockers(
    data_plan: DataRequirementPlan,
    availability: DataAvailabilitySnapshot,
) -> tuple[str, ...]:
    blockers: list[str] = []
    if availability.available_sessions < data_plan.minimum_total_sessions:
        blockers.append(
            f"历史交易日不足：需要至少 {data_plan.minimum_total_sessions} 个 session，当前 {availability.available_sessions}。"
        )
    if availability.available_symbols < data_plan.minimum_symbol_count:
        blockers.append(
            f"股票数量不足：需要至少 {data_plan.minimum_symbol_count} 只，当前 {availability.available_symbols}。"
        )
    return tuple(blockers)


def _field_blockers(fields: tuple[FieldAvailability, ...]) -> tuple[str, ...]:
    missing = [
        item.field_name
        for item in fields
        if item.required_for_official and not item.available
    ]
    if not missing:
        return ()
    return (f"缺少官方评价必需 PIT 字段：{', '.join(sorted(missing))}。",)


def _text_tuple(value: tuple[str, ...], name: str, *, nonempty: bool = True) -> None:
    if not isinstance(value, tuple) or (nonempty and not value):
        fail(FailureCode.INVALID_VALUE, f"{name} must be a tuple")
    for item in value:
        require_text(item, name)
    if len(value) != len(set(value)):
        fail(FailureCode.INVALID_VALUE, f"{name} contains duplicates")


__all__ = [
    "DataAvailabilitySnapshot",
    "DataPlanningResult",
    "DeterministicDataPlanner",
    "FieldAvailability",
    "PlannedFieldRole",
]
