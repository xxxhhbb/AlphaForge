"""Typed contracts for deterministic, scale-independent data planning.

These contracts deliberately carry no provider credentials and grant no
authority to calculate official evidence or promote research candidates.
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields, is_dataclass
from datetime import date
from enum import Enum
import hashlib
import json
from typing import Any, Mapping


SCHEMA_VERSION = "veriquant.dynamic-data-planning/v1"


class StrEnum(str, Enum):
    """Python 3.10-compatible string enum."""


class ResearchTheme(StrEnum):
    MOMENTUM = "MOMENTUM"
    FUNDAMENTAL = "FUNDAMENTAL"
    EVENT = "EVENT"
    MONEY_FLOW = "MONEY_FLOW"
    MACRO = "MACRO"
    PORTFOLIO_INCREMENTAL = "PORTFOLIO_INCREMENTAL"


class UniverseKind(StrEnum):
    HISTORICAL_ALL_A_SHARE = "HISTORICAL_ALL_A_SHARE"
    HISTORICAL_INDEX_CONSTITUENTS = "HISTORICAL_INDEX_CONSTITUENTS"
    EXPLICIT_SYMBOLS = "EXPLICIT_SYMBOLS"
    REGISTERED_PORTFOLIO_HISTORY = "REGISTERED_PORTFOLIO_HISTORY"


class RequirementLevel(StrEnum):
    REQUIRED = "REQUIRED"
    OPTIONAL = "OPTIONAL"


class DataReadiness(StrEnum):
    READY = "READY"
    PARTIAL = "PARTIAL"
    UNAVAILABLE = "UNAVAILABLE"


@dataclass(frozen=True, kw_only=True)
class StableContract:
    """Canonical serialization and identity shared by all public contracts."""

    @property
    def artifact_hash(self) -> str:
        payload = canonical_json(self)
        return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def canonical_json(self) -> str:
        return canonical_json(self)

    def to_dict(self) -> dict[str, Any]:
        value = _canonicalize(self)
        assert isinstance(value, dict)
        return value


@dataclass(frozen=True, kw_only=True)
class HistorySpec(StableContract):
    """Six non-overlapping session budgets used to derive history."""

    warmup_sessions: int
    train_sessions: int
    validation_sessions: int
    sealed_sessions: int
    label_sessions: int
    buffer_sessions: int

    def __post_init__(self) -> None:
        for field in fields(self):
            value = getattr(self, field.name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{field.name} must be a non-negative integer")
        if self.label_sessions < 1:
            raise ValueError("label_sessions must be positive")

    @property
    def minimum_total_sessions(self) -> int:
        return (
            self.warmup_sessions
            + self.train_sessions
            + self.validation_sessions
            + self.sealed_sessions
            + self.label_sessions
            + self.buffer_sessions
        )


@dataclass(frozen=True, kw_only=True)
class TimeRangeSpec(StableContract):
    start_date: date | None
    end_date: date | None
    derived_minimum_sessions: int

    def __post_init__(self) -> None:
        if self.start_date and self.end_date and self.start_date > self.end_date:
            raise ValueError("start_date must not be after end_date")
        if self.derived_minimum_sessions < 1:
            raise ValueError("derived_minimum_sessions must be positive")


@dataclass(frozen=True, kw_only=True)
class UniverseSpec(StableContract):
    kind: UniverseKind = UniverseKind.HISTORICAL_ALL_A_SHARE
    symbols: tuple[str, ...] = ()
    index_code: str | None = None
    portfolio_id: str | None = None
    filters: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "kind", UniverseKind(self.kind))
        object.__setattr__(self, "symbols", _unique_text(self.symbols, "symbols"))
        object.__setattr__(self, "filters", _unique_text(self.filters, "filters"))
        if self.kind is UniverseKind.EXPLICIT_SYMBOLS and not self.symbols:
            raise ValueError("explicit-symbol universe requires symbols")
        if self.kind is UniverseKind.HISTORICAL_INDEX_CONSTITUENTS and not self.index_code:
            raise ValueError("historical-index universe requires index_code")
        if self.kind is UniverseKind.REGISTERED_PORTFOLIO_HISTORY and not self.portfolio_id:
            raise ValueError("portfolio-history universe requires portfolio_id")


@dataclass(frozen=True, kw_only=True)
class DataRequirement(StableContract):
    dataset_id: str
    api_names: tuple[str, ...]
    required_fields: tuple[str, ...]
    optional_fields: tuple[str, ...] = ()
    level: RequirementLevel = RequirementLevel.REQUIRED
    time_semantics: str
    purpose: str

    def __post_init__(self) -> None:
        _require_text(self.dataset_id, "dataset_id")
        object.__setattr__(self, "api_names", _unique_text(self.api_names, "api_names"))
        object.__setattr__(self, "required_fields", _unique_text(self.required_fields, "required_fields"))
        object.__setattr__(self, "optional_fields", _unique_text(self.optional_fields, "optional_fields"))
        object.__setattr__(self, "level", RequirementLevel(self.level))
        _require_text(self.time_semantics, "time_semantics")
        _require_text(self.purpose, "purpose")
        overlap = set(self.required_fields).intersection(self.optional_fields)
        if overlap:
            raise ValueError(f"fields cannot be both required and optional: {sorted(overlap)}")


@dataclass(frozen=True, kw_only=True)
class DataPlanningRequest(StableContract):
    question: str
    research_mode: str
    factor_lookback_sessions: int | None = None
    label_horizon_sessions: int = 5
    train_sessions: int = 120
    validation_sessions: int = 60
    sealed_sessions: int = 40
    buffer_sessions: int = 20
    start_date: date | None = None
    end_date: date | None = None
    universe: UniverseSpec = field(default_factory=UniverseSpec)

    def __post_init__(self) -> None:
        _require_text(self.question, "question")
        mode = getattr(self.research_mode, "value", self.research_mode)
        _require_text(mode, "research_mode")
        object.__setattr__(self, "research_mode", str(mode))
        for name in (
            "label_horizon_sessions",
            "train_sessions",
            "validation_sessions",
            "sealed_sessions",
            "buffer_sessions",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if self.label_horizon_sessions < 1:
            raise ValueError("label_horizon_sessions must be positive")
        if self.factor_lookback_sessions is not None:
            if (
                isinstance(self.factor_lookback_sessions, bool)
                or not isinstance(self.factor_lookback_sessions, int)
                or self.factor_lookback_sessions < 1
            ):
                raise ValueError("factor_lookback_sessions must be positive when provided")
        if not isinstance(self.universe, UniverseSpec):
            raise ValueError("universe must be UniverseSpec")


@dataclass(frozen=True, kw_only=True)
class DynamicDataPlan(StableContract):
    schema_version: str
    question: str
    research_mode: str
    themes: tuple[ResearchTheme, ...]
    universe: UniverseSpec
    time_range: TimeRangeSpec
    history: HistorySpec
    requirements: tuple[DataRequirement, ...]
    requested_api_names: tuple[str, ...]
    requested_fields: tuple[str, ...]
    authority_restrictions: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError(f"schema_version must be {SCHEMA_VERSION}")
        if not self.themes or not self.requirements:
            raise ValueError("themes and requirements are required")
        object.__setattr__(self, "themes", tuple(ResearchTheme(item) for item in self.themes))
        object.__setattr__(self, "requested_api_names", _unique_text(self.requested_api_names, "requested_api_names"))
        object.__setattr__(self, "requested_fields", _unique_text(self.requested_fields, "requested_fields"))
        object.__setattr__(
            self,
            "authority_restrictions",
            _unique_text(self.authority_restrictions, "authority_restrictions"),
        )

    @property
    def plan_hash(self) -> str:
        return self.artifact_hash


@dataclass(frozen=True, kw_only=True)
class DataCapabilitySnapshot(StableContract):
    """Public, secret-free declaration of frozen/obtainable data."""

    available_datasets: tuple[str, ...] = ()
    available_api_names: tuple[str, ...] = ()
    available_fields: tuple[str, ...] = ()
    available_sessions: int = 0
    provider_label: str = "UNKNOWN"
    frozen_snapshot: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "available_datasets", _unique_text(self.available_datasets, "available_datasets"))
        object.__setattr__(self, "available_api_names", _unique_text(self.available_api_names, "available_api_names"))
        object.__setattr__(self, "available_fields", _unique_text(self.available_fields, "available_fields"))
        if (
            isinstance(self.available_sessions, bool)
            or not isinstance(self.available_sessions, int)
            or self.available_sessions < 0
        ):
            raise ValueError("available_sessions must be a non-negative integer")
        if not isinstance(self.frozen_snapshot, bool):
            raise ValueError("frozen_snapshot must be boolean")
        _require_text(self.provider_label, "provider_label")
        lowered = self.provider_label.lower()
        forbidden = ("token", "secret", "api_key", "authorization", "official_tushare")
        if any(item in lowered for item in forbidden):
            raise ValueError("provider label must be public, secret-free, and non-official")


@dataclass(frozen=True, kw_only=True)
class DatasetReadiness(StableContract):
    dataset_id: str
    readiness: DataReadiness
    missing_api_names: tuple[str, ...] = ()
    missing_fields: tuple[str, ...] = ()
    reason: str = ""

    def __post_init__(self) -> None:
        _require_text(self.dataset_id, "dataset_id")
        object.__setattr__(self, "readiness", DataReadiness(self.readiness))
        object.__setattr__(
            self,
            "missing_api_names",
            _unique_text(self.missing_api_names, "missing_api_names"),
        )
        object.__setattr__(self, "missing_fields", _unique_text(self.missing_fields, "missing_fields"))
        _require_text(self.reason, "reason")


@dataclass(frozen=True, kw_only=True)
class ResearchGroupDecision(StableContract):
    group_id: str
    selected: bool
    score: int
    supported_themes: tuple[ResearchTheme, ...]
    missing_datasets: tuple[str, ...] = ()
    missing_fields: tuple[str, ...] = ()
    reason: str

    def __post_init__(self) -> None:
        _require_text(self.group_id, "group_id")
        if not isinstance(self.selected, bool):
            raise ValueError("selected must be boolean")
        if isinstance(self.score, bool) or not isinstance(self.score, int):
            raise ValueError("score must be an integer")
        object.__setattr__(
            self,
            "supported_themes",
            tuple(sorted({ResearchTheme(item) for item in self.supported_themes}, key=lambda item: item.value)),
        )
        object.__setattr__(
            self,
            "missing_datasets",
            _unique_text(self.missing_datasets, "missing_datasets"),
        )
        object.__setattr__(self, "missing_fields", _unique_text(self.missing_fields, "missing_fields"))
        _require_text(self.reason, "reason")


@dataclass(frozen=True, kw_only=True)
class ResearchRoutingResult(StableContract):
    plan_hash: str
    readiness: DataReadiness
    dataset_readiness: tuple[DatasetReadiness, ...]
    selected_group_ids: tuple[str, ...]
    group_decisions: tuple[ResearchGroupDecision, ...]
    researchable_parts: tuple[str, ...]
    missing_requirements: tuple[str, ...]
    degradation_suggestions: tuple[str, ...]
    authority_restrictions: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "readiness", DataReadiness(self.readiness))
        object.__setattr__(
            self,
            "selected_group_ids",
            _unique_text_preserving_order(self.selected_group_ids, "selected_group_ids"),
        )
        if len(self.selected_group_ids) > 6:
            raise ValueError("at most six research groups may be selected")
        object.__setattr__(
            self,
            "researchable_parts",
            _unique_text(self.researchable_parts, "researchable_parts"),
        )
        object.__setattr__(
            self,
            "missing_requirements",
            _unique_text(self.missing_requirements, "missing_requirements"),
        )
        object.__setattr__(
            self,
            "degradation_suggestions",
            _unique_text(self.degradation_suggestions, "degradation_suggestions"),
        )
        object.__setattr__(
            self,
            "authority_restrictions",
            _unique_text(self.authority_restrictions, "authority_restrictions"),
        )
        if any("official evidence" in item.lower() and "cannot" not in item.lower() for item in self.authority_restrictions):
            raise ValueError("routing cannot grant official-evidence authority")


def canonical_json(value: Any) -> str:
    return json.dumps(_canonicalize(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _canonicalize(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, date):
        return value.isoformat()
    if is_dataclass(value):
        return {field.name: _canonicalize(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, Mapping):
        return {str(key): _canonicalize(value[key]) for key in sorted(value, key=str)}
    if isinstance(value, (tuple, list)):
        return [_canonicalize(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    raise TypeError(f"unsupported canonical value: {type(value).__name__}")


def _unique_text(values: tuple[str, ...], name: str) -> tuple[str, ...]:
    return tuple(sorted(_unique_text_preserving_order(values, name)))


def _unique_text_preserving_order(values: tuple[str, ...], name: str) -> tuple[str, ...]:
    if not isinstance(values, tuple):
        values = tuple(values)
    result: list[str] = []
    for value in values:
        _require_text(value, name)
        if value not in result:
            result.append(value)
    return tuple(result)


def _require_text(value: Any, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be non-empty text")
