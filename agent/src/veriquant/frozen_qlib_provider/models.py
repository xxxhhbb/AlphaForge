"""Read-only contracts for PIT-safe inputs to a controlled Qlib executor.

These models contain inert data only.  They do not initialize Qlib, touch the
filesystem, download data, compute evidence, or expose labels.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping, Protocol, runtime_checkable

from ..contracts import DatasetCard, FailureCode, PointInTimeRecord
from ..contracts.base import canonicalize, require_aware_datetime, require_hash, require_identifier
from ..data import FieldRole, MarketSnapshot, UniverseMembership, canonical_content_hash


SCHEMA_VERSION = "frozen-qlib-provider/v1"
_FIELD_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,127}$")
_FORBIDDEN_FIELD_FRAGMENTS = ("label", "sealed")


class FrozenQlibProviderError(ValueError):
    """Fail-closed adapter error with a stable VeriQuant failure code."""

    def __init__(
        self,
        code: FailureCode,
        message: str,
        *,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.details = MappingProxyType(dict(details or {}))


def provider_fail(code: FailureCode, message: str, **details: Any) -> None:
    raise FrozenQlibProviderError(code, message, details=details)


def validate_feature_name(value: str, *, field_name: str = "field") -> None:
    if not isinstance(value, str) or _FIELD_PATTERN.fullmatch(value) is None:
        provider_fail(
            FailureCode.UNKNOWN_FIELD,
            "Qlib feature names must be bare stable identifiers",
            field=field_name,
            value=value,
        )
    lowered = value.lower()
    if (
        any(fragment in lowered for fragment in _FORBIDDEN_FIELD_FRAGMENTS)
        or lowered.startswith("future_")
        or lowered in {"target", "future_return"}
        or lowered.endswith("_target")
    ):
        provider_fail(
            FailureCode.UNAUTHORIZED_CAPABILITY,
            "label, sealed, future, and target fields cannot enter Qlib features",
            field=value,
        )


def _freeze_string_tuple_mapping(
    value: Mapping[str, tuple[str, ...]],
    *,
    field_name: str,
) -> Mapping[str, tuple[str, ...]]:
    if not isinstance(value, Mapping) or not value:
        provider_fail(FailureCode.INCOMPLETE_RESULT, f"{field_name} must be non-empty")
    output: dict[str, tuple[str, ...]] = {}
    for key, members in sorted(value.items()):
        if not isinstance(key, str) or not key:
            provider_fail(FailureCode.INVALID_VALUE, f"{field_name} keys must be strings")
        if not isinstance(members, tuple) or not members:
            provider_fail(
                FailureCode.INCOMPLETE_RESULT,
                f"{field_name} sessions must have at least one member",
                session=key,
            )
        if len(set(members)) != len(members):
            provider_fail(FailureCode.INVALID_VALUE, f"{field_name} contains duplicate members")
        for symbol in members:
            require_identifier(symbol, f"{field_name}.symbol")
        output[key] = tuple(sorted(members))
    return MappingProxyType(output)


def _freeze_numeric_mapping(
    value: Mapping[str, int | float | None],
    *,
    field_name: str,
) -> Mapping[str, int | float | None]:
    if not isinstance(value, Mapping):
        provider_fail(FailureCode.INVALID_VALUE, f"{field_name} must be a mapping")
    output: dict[str, int | float | None] = {}
    for key, item in value.items():
        validate_feature_name(key, field_name=f"{field_name}.{key}")
        if item is not None and (isinstance(item, bool) or not isinstance(item, (int, float))):
            provider_fail(
                FailureCode.INVALID_VALUE,
                "Qlib features must be numeric or null",
                field=key,
            )
        if isinstance(item, float) and not math.isfinite(item):
            provider_fail(FailureCode.NON_FINITE_NUMBER, "Qlib features must be finite", field=key)
        output[key] = item
    return MappingProxyType(output)


@dataclass(frozen=True, kw_only=True)
class QlibDataRequirement:
    """Expression-derived data requirements bound into the provider identity."""

    required_fields: tuple[str, ...]
    lookback_bars: int
    warmup_bars: int
    frequency: str = "1D"

    def __post_init__(self) -> None:
        if not isinstance(self.required_fields, tuple) or not self.required_fields:
            provider_fail(FailureCode.MISSING_REQUIRED_FIELD, "required_fields must be non-empty")
        if len(set(self.required_fields)) != len(self.required_fields):
            provider_fail(FailureCode.INVALID_VALUE, "required_fields must be unique")
        for field_name in self.required_fields:
            validate_feature_name(field_name)
        for name in ("lookback_bars", "warmup_bars"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                provider_fail(FailureCode.INVALID_VALUE, f"{name} must be a non-negative integer")
        if self.warmup_bars < self.lookback_bars:
            provider_fail(
                FailureCode.INVALID_VALUE,
                "warmup_bars must cover lookback_bars",
                lookback_bars=self.lookback_bars,
                warmup_bars=self.warmup_bars,
            )
        if self.frequency != "1D":
            provider_fail(FailureCode.INVALID_VALUE, "only deterministic daily Qlib inputs are supported")

    @property
    def artifact_hash(self) -> str:
        return canonical_content_hash(self)


@dataclass(frozen=True, kw_only=True)
class StructuredPITFixture:
    """Structured revision input used when an already-materialized snapshot is unavailable."""

    dataset_id: str
    decision_time: datetime
    calendar: tuple[datetime, ...]
    records: tuple[PointInTimeRecord, ...]
    memberships: tuple[UniverseMembership, ...]
    field_roles: Mapping[str, FieldRole]
    dataset_card: DatasetCard | None = None

    def __post_init__(self) -> None:
        require_identifier(self.dataset_id, "dataset_id")
        require_aware_datetime(self.decision_time, "decision_time")
        if not isinstance(self.calendar, tuple) or not self.calendar:
            provider_fail(FailureCode.INCOMPLETE_RESULT, "calendar must be a non-empty tuple")
        for session in self.calendar:
            require_aware_datetime(session, "calendar")
        if tuple(sorted(set(self.calendar))) != self.calendar:
            provider_fail(FailureCode.INVALID_VALUE, "calendar must be unique and sorted")
        if not isinstance(self.records, tuple) or not self.records:
            provider_fail(FailureCode.INCOMPLETE_RESULT, "records must be a non-empty tuple")
        if not isinstance(self.memberships, tuple) or not self.memberships:
            provider_fail(FailureCode.INCOMPLETE_RESULT, "memberships must be a non-empty tuple")
        if any(not isinstance(item, PointInTimeRecord) for item in self.records):
            provider_fail(FailureCode.INVALID_VALUE, "records must contain PointInTimeRecord values")
        if any(not isinstance(item, UniverseMembership) for item in self.memberships):
            provider_fail(FailureCode.INVALID_VALUE, "memberships must contain UniverseMembership values")
        if any(item.dataset_id != self.dataset_id for item in self.records):
            provider_fail(FailureCode.INVALID_VALUE, "record dataset_id mismatch")
        if any(item.dataset_id != self.dataset_id for item in self.memberships):
            provider_fail(FailureCode.INVALID_VALUE, "membership dataset_id mismatch")
        roles = dict(sorted(self.field_roles.items()))
        if set(roles) != {item.field_name for item in self.records}:
            provider_fail(
                FailureCode.INVALID_VALUE,
                "field_roles must exactly cover structured record fields",
            )
        if any(not isinstance(role, FieldRole) for role in roles.values()):
            provider_fail(FailureCode.INVALID_VALUE, "field_roles must contain FieldRole values")
        object.__setattr__(self, "field_roles", MappingProxyType(roles))
        if self.dataset_card is not None:
            if not isinstance(self.dataset_card, DatasetCard):
                provider_fail(FailureCode.INVALID_VALUE, "dataset_card must be DatasetCard")
            if self.dataset_card.dataset_id != self.dataset_id:
                provider_fail(FailureCode.INVALID_VALUE, "DatasetCard dataset_id mismatch")
            if self.decision_time > self.dataset_card.decision_time:
                provider_fail(FailureCode.INVALID_TIME_ORDER, "fixture decision_time exceeds DatasetCard")

@dataclass(frozen=True, kw_only=True)
class QlibFeatureRow:
    session: datetime
    symbol: str
    values: Mapping[str, int | float | None]
    revision_ids: Mapping[str, str]

    def __post_init__(self) -> None:
        require_aware_datetime(self.session, "session")
        require_identifier(self.symbol, "symbol")
        object.__setattr__(self, "values", _freeze_numeric_mapping(self.values, field_name="values"))
        if not isinstance(self.revision_ids, Mapping):
            provider_fail(FailureCode.INVALID_VALUE, "revision_ids must be a mapping")
        revisions: dict[str, str] = {}
        for key, value in sorted(self.revision_ids.items()):
            validate_feature_name(key)
            require_identifier(value, "revision_id")
            revisions[key] = value
        if revisions and set(revisions) != set(self.values):
            provider_fail(FailureCode.INVALID_VALUE, "revision_ids must cover every materialized field")
        object.__setattr__(self, "revision_ids", MappingProxyType(revisions))


@dataclass(frozen=True, kw_only=True)
class FrozenQlibCalendarManifest:
    sessions: tuple[datetime, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.sessions, tuple) or not self.sessions:
            provider_fail(FailureCode.INCOMPLETE_RESULT, "calendar manifest is empty")
        for session in self.sessions:
            require_aware_datetime(session, "calendar session")
        if tuple(sorted(set(self.sessions))) != self.sessions:
            provider_fail(FailureCode.INVALID_VALUE, "calendar sessions must be unique and sorted")

    @property
    def artifact_hash(self) -> str:
        return canonical_content_hash(self)


@dataclass(frozen=True, kw_only=True)
class FrozenQlibUniverseManifest:
    members_by_session: Mapping[str, tuple[str, ...]]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "members_by_session",
            _freeze_string_tuple_mapping(self.members_by_session, field_name="members_by_session"),
        )

    @property
    def artifact_hash(self) -> str:
        return canonical_content_hash(self)


@dataclass(frozen=True, kw_only=True)
class FrozenQlibFeatureManifest:
    fields: tuple[str, ...]
    rows: tuple[QlibFeatureRow, ...]
    coverage_by_field: Mapping[str, float]

    def __post_init__(self) -> None:
        if not isinstance(self.fields, tuple) or not self.fields:
            provider_fail(FailureCode.INCOMPLETE_RESULT, "feature manifest fields are required")
        for field_name in self.fields:
            validate_feature_name(field_name)
        if len(set(self.fields)) != len(self.fields):
            provider_fail(FailureCode.INVALID_VALUE, "feature manifest fields must be unique")
        if not isinstance(self.rows, tuple) or not self.rows:
            provider_fail(FailureCode.INCOMPLETE_RESULT, "feature manifest rows are required")
        if tuple(sorted(self.rows, key=lambda row: (row.session, row.symbol))) != self.rows:
            provider_fail(FailureCode.INVALID_VALUE, "feature rows must be canonically ordered")
        row_keys = tuple((row.session, row.symbol) for row in self.rows)
        if len(set(row_keys)) != len(row_keys):
            provider_fail(FailureCode.INVALID_VALUE, "feature rows contain duplicate session/symbol pairs")
        for row in self.rows:
            if not isinstance(row, QlibFeatureRow) or tuple(row.values) != self.fields:
                provider_fail(FailureCode.INVALID_VALUE, "feature row schema does not match manifest")
        coverage: dict[str, float] = {}
        for field_name, value in sorted(self.coverage_by_field.items()):
            if field_name not in self.fields or not isinstance(value, (int, float)) or isinstance(value, bool):
                provider_fail(FailureCode.INVALID_VALUE, "invalid feature coverage entry")
            number = float(value)
            if not math.isfinite(number) or not 0.0 <= number <= 1.0:
                provider_fail(FailureCode.INVALID_VALUE, "feature coverage must lie in [0, 1]")
            coverage[field_name] = number
        if set(coverage) != set(self.fields):
            provider_fail(FailureCode.INVALID_VALUE, "coverage must exactly cover feature fields")
        object.__setattr__(self, "coverage_by_field", MappingProxyType(coverage))

    @property
    def artifact_hash(self) -> str:
        return canonical_content_hash(self)


@dataclass(frozen=True, kw_only=True)
class PITAvailabilityProof:
    proof_kind: str
    decision_time: datetime
    maximum_visible_available_at: datetime
    assumptions: tuple[str, ...]
    visible_input_hash: str

    def __post_init__(self) -> None:
        if self.proof_kind not in {"DATASET_CARD_SNAPSHOT", "ROW_LEVEL_BITEMPORAL"}:
            provider_fail(FailureCode.INVALID_VALUE, "unsupported availability proof kind")
        require_aware_datetime(self.decision_time, "decision_time")
        require_aware_datetime(self.maximum_visible_available_at, "maximum_visible_available_at")
        if self.maximum_visible_available_at > self.decision_time:
            provider_fail(FailureCode.INVALID_TIME_ORDER, "availability proof exceeds decision_time")
        if not isinstance(self.assumptions, tuple) or not self.assumptions:
            provider_fail(FailureCode.MISSING_REQUIRED_FIELD, "availability proof assumptions are required")
        if any(not isinstance(item, str) or not item.strip() for item in self.assumptions):
            provider_fail(FailureCode.INVALID_VALUE, "availability proof assumptions must be text")
        require_hash(self.visible_input_hash, "visible_input_hash")

    @property
    def artifact_hash(self) -> str:
        return canonical_content_hash(self)


@dataclass(frozen=True, kw_only=True)
class FrozenQlibProviderManifest:
    """Content-addressed, read-only Qlib provider input manifest."""

    dataset_id: str
    source_snapshot_hash: str
    requirement: QlibDataRequirement
    calendar: FrozenQlibCalendarManifest
    universe: FrozenQlibUniverseManifest
    features: FrozenQlibFeatureManifest
    availability_proof: PITAvailabilityProof
    schema_version: str = SCHEMA_VERSION
    read_only: bool = True
    network_allowed: bool = False
    qlib_home_write_allowed: bool = False
    labels_included: bool = False

    def __post_init__(self) -> None:
        require_identifier(self.dataset_id, "dataset_id")
        require_hash(self.source_snapshot_hash, "source_snapshot_hash")
        if self.schema_version != SCHEMA_VERSION:
            provider_fail(FailureCode.INVALID_SCHEMA_VERSION, "unknown frozen Qlib schema version")
        if not isinstance(self.requirement, QlibDataRequirement):
            provider_fail(FailureCode.INVALID_VALUE, "requirement must be QlibDataRequirement")
        if self.features.fields != self.requirement.required_fields:
            provider_fail(FailureCode.INVALID_VALUE, "feature fields do not bind required_fields")
        session_keys = tuple(_session_key(item) for item in self.calendar.sessions)
        if tuple(self.universe.members_by_session) != session_keys:
            provider_fail(FailureCode.INVALID_VALUE, "calendar and universe sessions differ")
        expected_pairs = {
            (session_key, symbol)
            for session_key, symbols in self.universe.members_by_session.items()
            for symbol in symbols
        }
        actual_pairs = {(_session_key(row.session), row.symbol) for row in self.features.rows}
        if actual_pairs != expected_pairs:
            provider_fail(FailureCode.INVALID_VALUE, "features do not exactly cover historical universe")
        if (
            self.read_only is not True
            or self.network_allowed is not False
            or self.qlib_home_write_allowed is not False
            or self.labels_included is not False
        ):
            provider_fail(FailureCode.UNAUTHORIZED_CAPABILITY, "unsafe Qlib provider capability flags")

    @property
    def calendar_hash(self) -> str:
        return self.calendar.artifact_hash

    @property
    def universe_hash(self) -> str:
        return self.universe.artifact_hash

    @property
    def features_hash(self) -> str:
        return self.features.artifact_hash

    @property
    def manifest_hash(self) -> str:
        return canonical_content_hash(self)

    def to_payload(self) -> dict[str, Any]:
        payload = canonicalize(self)
        assert isinstance(payload, dict)
        return {
            **payload,
            "calendar_hash": self.calendar_hash,
            "universe_hash": self.universe_hash,
            "features_hash": self.features_hash,
            "manifest_hash": self.manifest_hash,
        }


def _session_key(value: datetime) -> str:
    rendered = canonicalize(value)
    assert isinstance(rendered, str)
    return rendered


@runtime_checkable
class FrozenQlibProviderProtocol(Protocol):
    def from_market_snapshot(
        self,
        snapshot: MarketSnapshot,
        *,
        dataset_card: DatasetCard,
        requirement: QlibDataRequirement,
    ) -> FrozenQlibProviderManifest: ...

    def from_structured_fixture(
        self,
        fixture: StructuredPITFixture,
        *,
        requirement: QlibDataRequirement,
    ) -> FrozenQlibProviderManifest: ...


def validate_staging_path(path: str | Path, *, allowed_root: str | Path) -> Path:
    """Validate an integration-owned staging path without creating or writing it."""

    raw = str(path)
    if not raw.strip() or "\x00" in raw:
        provider_fail(FailureCode.INVALID_VALUE, "staging path is empty or contains NUL")
    root = Path(allowed_root).resolve()
    candidate = Path(path)
    resolved = candidate.resolve() if candidate.is_absolute() else (root / candidate).resolve()
    try:
        relative = resolved.relative_to(root)
    except ValueError as exc:
        raise FrozenQlibProviderError(
            FailureCode.UNAUTHORIZED_CAPABILITY,
            "staging path escapes the integration-owned root",
            details={"path": raw, "allowed_root": str(root)},
        ) from exc
    if relative == Path("."):
        provider_fail(FailureCode.UNAUTHORIZED_CAPABILITY, "staging path cannot be the broad allowed root")
    qlib_home = (Path.home() / ".qlib").resolve()
    try:
        resolved.relative_to(qlib_home)
    except ValueError:
        pass
    else:
        provider_fail(FailureCode.UNAUTHORIZED_CAPABILITY, "writing ~/.qlib is forbidden")
    return resolved


__all__ = [
    "FrozenQlibCalendarManifest",
    "FrozenQlibFeatureManifest",
    "FrozenQlibProviderError",
    "FrozenQlibProviderManifest",
    "FrozenQlibProviderProtocol",
    "FrozenQlibUniverseManifest",
    "PITAvailabilityProof",
    "QlibDataRequirement",
    "QlibFeatureRow",
    "SCHEMA_VERSION",
    "StructuredPITFixture",
    "provider_fail",
    "validate_feature_name",
    "validate_staging_path",
]
