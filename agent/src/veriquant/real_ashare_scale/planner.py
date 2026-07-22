"""Deterministic qualification and replay-vintage planning for frozen inputs."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import ClassVar, Protocol, runtime_checkable

from ..contracts import CanonicalContract, ContractValidationError, FailureCode
from ..contracts.base import fail, require_identifier
from .models import (
    APPROVED_PROVIDER_CLASS,
    APPROVED_SOURCE_CLASS,
    REAL_ASHARE_SCALE_SCHEMA_VERSION,
    DataVintagePlan,
    FieldCoverage,
    FrozenAshareScaleInput,
    FrozenAshareScaleManifest,
    FrozenDataVintage,
    HistoricalUniverseCoverage,
    InputOrigin,
    ScaleQualificationFailureCode,
    ScaleQualificationOutcome,
    ScaleQualificationPolicy,
    ScaleQualificationStatus,
    ScaleQualificationVerdict,
    TimeSemanticsReport,
    stable_content_hash,
    validate_source_labels,
)


@dataclass(frozen=True, kw_only=True)
class BoundedScaleInputRequest(CanonicalContract):
    """Request envelope deliberately lacking URL, token, callback, or code fields."""

    SCHEMA_VERSION: ClassVar[str] = REAL_ASHARE_SCALE_SCHEMA_VERSION

    dataset_id: str
    max_symbols: int = 50
    max_sessions: int = 250
    offline_only: bool = True

    def __post_init__(self) -> None:
        require_identifier(self.dataset_id, "dataset_id")
        if isinstance(self.max_symbols, bool) or not (1 <= self.max_symbols <= 50):
            fail(FailureCode.INVALID_VALUE, "max_symbols must be bounded by 50")
        if isinstance(self.max_sessions, bool) or not (1 <= self.max_sessions <= 250):
            fail(FailureCode.INVALID_VALUE, "max_sessions must be bounded by 250")
        if self.offline_only is not True:
            fail(FailureCode.UNAUTHORIZED_CAPABILITY, "qualification requests are offline-only")


class BoundedAdapterUnavailable(RuntimeError):
    """Typed unavailability from a disk/in-memory bounded input adapter."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.code = ScaleQualificationFailureCode.ADAPTER_UNAVAILABLE


@runtime_checkable
class BoundedAshareScaleInputAdapter(Protocol):
    """Integration boundary for already-frozen inputs; it has no fetch method."""

    adapter_id: str
    network_capable: bool

    def load_frozen(self, request: BoundedScaleInputRequest) -> FrozenAshareScaleInput:
        """Load a bounded immutable input without making a network request."""


class InMemoryBoundedScaleInputAdapter:
    """Deterministic adapter used only by offline tests and integration fakes."""

    network_capable = False

    def __init__(self, frozen_input: FrozenAshareScaleInput | None, *, adapter_id: str = "in-memory-scale-input") -> None:
        require_identifier(adapter_id, "adapter_id")
        self.adapter_id = adapter_id
        self._frozen_input = frozen_input

    def load_frozen(self, request: BoundedScaleInputRequest) -> FrozenAshareScaleInput:
        if not isinstance(request, BoundedScaleInputRequest):
            fail(FailureCode.INVALID_VALUE, "bounded scale request is required")
        if self._frozen_input is None:
            raise BoundedAdapterUnavailable("no expanded frozen A-share snapshot is configured")
        if self._frozen_input.dataset_id != request.dataset_id:
            raise BoundedAdapterUnavailable("requested frozen dataset is unavailable")
        if len(self._frozen_input.calendar_sessions) > request.max_sessions:
            fail(FailureCode.PROVIDER_BUDGET_EXCEEDED, "frozen input exceeds requested session bound")
        union_symbols = {symbol for snapshot in self._frozen_input.historical_universe for symbol in snapshot.symbols}
        if len(union_symbols) > request.max_symbols:
            fail(FailureCode.PROVIDER_BUDGET_EXCEEDED, "frozen input exceeds requested symbol bound")
        return self._frozen_input


class RealAshareScalePlanner:
    """Build a compact identity manifest and a deterministic 20-session plan."""

    def __init__(self, policy: ScaleQualificationPolicy | None = None) -> None:
        self.policy = policy or ScaleQualificationPolicy()

    def run_adapter(
        self,
        adapter: BoundedAshareScaleInputAdapter,
        request: BoundedScaleInputRequest,
    ) -> ScaleQualificationOutcome:
        if not isinstance(request, BoundedScaleInputRequest):
            fail(FailureCode.INVALID_VALUE, "BoundedScaleInputRequest is required")
        if not isinstance(adapter, BoundedAshareScaleInputAdapter):
            fail(FailureCode.INVALID_VALUE, "bounded A-share input adapter is required")
        if adapter.network_capable:
            return self._no_artifact_outcome(
                ScaleQualificationStatus.FAILED,
                ScaleQualificationFailureCode.ADAPTER_UNAVAILABLE,
                "qualification adapter must load an existing frozen input and must not access network",
                "UNAVAILABLE",
            )
        try:
            frozen_input = adapter.load_frozen(request)
        except BoundedAdapterUnavailable as exc:
            return self._no_artifact_outcome(
                ScaleQualificationStatus.UNAVAILABLE,
                exc.code,
                str(exc),
                "UNAVAILABLE",
            )
        except ContractValidationError as exc:
            return self._no_artifact_outcome(
                ScaleQualificationStatus.FAILED,
                _map_contract_failure(exc),
                str(exc),
                "IMPLEMENTED_NOT_RUN",
            )
        return self.qualify(frozen_input)

    def qualify(self, frozen_input: FrozenAshareScaleInput) -> ScaleQualificationOutcome:
        try:
            manifest = self.build_manifest(frozen_input)
            vintage_plan = self.build_vintage_plan(frozen_input, manifest)
        except ContractValidationError as exc:
            return self._no_artifact_outcome(
                ScaleQualificationStatus.FAILED,
                _map_contract_failure(exc),
                str(exc),
                "IMPLEMENTED_NOT_RUN",
            )

        failures, reasons = self._qualification_findings(frozen_input, manifest, vintage_plan)
        if not failures:
            status = ScaleQualificationStatus.QUALIFIED
            eligible = True
            operational = "IMPLEMENTED_AND_TESTED"
        else:
            status = ScaleQualificationStatus.IMPLEMENTED_NOT_RUN
            eligible = False
            operational = "IMPLEMENTED_NOT_RUN"
        verdict = ScaleQualificationVerdict(
            status=status,
            eligible=eligible,
            policy_hash=self.policy.artifact_hash,
            manifest_hash=manifest.artifact_hash,
            vintage_plan_hash=None if vintage_plan is None else vintage_plan.artifact_hash,
            failure_codes=failures,
            reasons=reasons,
            operational_smoke_status=operational,
        )
        return ScaleQualificationOutcome(verdict=verdict, manifest=manifest, vintage_plan=vintage_plan)

    def build_manifest(self, frozen_input: FrozenAshareScaleInput) -> FrozenAshareScaleManifest:
        if not isinstance(frozen_input, FrozenAshareScaleInput):
            fail(FailureCode.INVALID_VALUE, "FrozenAshareScaleInput is required")
        validate_source_labels(frozen_input.provider_class, frozen_input.source_class)
        self._verify_content_hashes(frozen_input)
        universe_coverage = self._universe_coverage(frozen_input)
        field_coverage = self._field_coverage(frozen_input)
        time_semantics = self._time_semantics(frozen_input)
        return FrozenAshareScaleManifest(
            dataset_id=frozen_input.dataset_id,
            provider_class=frozen_input.provider_class,
            source_class=frozen_input.source_class,
            input_origin=frozen_input.input_origin,
            capture_time=frozen_input.capture_time,
            capture_network_used=frozen_input.capture_network_used,
            calendar_id=frozen_input.calendar_id,
            calendar_hash=frozen_input.calendar_hash,
            universe_hash=frozen_input.universe_hash,
            data_hash=frozen_input.data_hash,
            adapter_manifest_hash=frozen_input.adapter_manifest_hash,
            availability_policy_hash=frozen_input.availability_policy_hash,
            symbol_count=len(universe_coverage.union_symbols),
            session_count=len(frozen_input.calendar_sessions),
            row_count=len(frozen_input.rows),
            feature_fields=frozen_input.feature_fields,
            label_fields=frozen_input.label_fields,
            field_coverage=field_coverage,
            universe_coverage=universe_coverage,
            time_semantics=time_semantics,
            vintage_count=len(frozen_input.vintages),
            future_return_feature_present=False,
        )

    def build_vintage_plan(
        self,
        frozen_input: FrozenAshareScaleInput,
        manifest: FrozenAshareScaleManifest,
    ) -> DataVintagePlan | None:
        self._validate_revision_history(frozen_input.vintages)
        ordered = tuple(sorted(frozen_input.vintages, key=lambda item: (item.decision_time, item.vintage_id)))
        if len(ordered) < self.policy.replay_sessions:
            return None
        selected = ordered[-self.policy.replay_sessions :]
        if len({item.session for item in selected}) != self.policy.replay_sessions:
            fail(
                FailureCode.INVALID_VALUE,
                "replay vintages must bind distinct decision sessions",
                scale_failure_code=ScaleQualificationFailureCode.REVISION_HISTORY_INVALID.value,
            )
        calendar = set(frozen_input.calendar_sessions)
        if any(item.session not in calendar for item in selected):
            fail(FailureCode.INVALID_VALUE, "replay vintage uses a non-calendar session")
        return DataVintagePlan(
            plan_id=f"{frozen_input.dataset_id}.replay-{self.policy.replay_sessions}d",
            manifest_hash=manifest.artifact_hash,
            replay_session_count=self.policy.replay_sessions,
            vintages=selected,
            first_decision_time=selected[0].decision_time,
            last_decision_time=selected[-1].decision_time,
        )

    def _verify_content_hashes(self, frozen_input: FrozenAshareScaleInput) -> None:
        expected = {
            "calendar_hash": stable_content_hash(frozen_input.calendar_sessions),
            "universe_hash": stable_content_hash(frozen_input.historical_universe),
            "data_hash": stable_content_hash(frozen_input.rows),
        }
        for field_name, computed in expected.items():
            if getattr(frozen_input, field_name) != computed:
                fail(
                    FailureCode.INVALID_HASH,
                    f"{field_name} does not match frozen content",
                    field=field_name,
                    scale_failure_code=ScaleQualificationFailureCode.HASH_MISMATCH.value,
                )

    def _universe_coverage(self, frozen_input: FrozenAshareScaleInput) -> HistoricalUniverseCoverage:
        sessions = set(frozen_input.calendar_sessions)
        mapping: dict[str, frozenset[str]] = {}
        for snapshot in frozen_input.historical_universe:
            if snapshot.session in mapping:
                fail(FailureCode.INVALID_VALUE, "historical universe has duplicate sessions")
            if snapshot.session not in sessions:
                fail(FailureCode.INVALID_TIME_ORDER, "historical universe uses a future/non-calendar session")
            mapping[snapshot.session] = frozenset(snapshot.symbols)
        leakage = any(
            row.session not in mapping or row.symbol not in mapping[row.session]
            for row in frozen_input.rows
        )
        sizes = tuple(len(value) for value in mapping.values())
        union = tuple(sorted({symbol for value in mapping.values() for symbol in value}))
        return HistoricalUniverseCoverage(
            calendar_session_count=len(frozen_input.calendar_sessions),
            membership_session_count=len(mapping),
            union_symbols=union,
            minimum_members_per_session=min(sizes),
            maximum_members_per_session=max(sizes),
            session_coverage=len(mapping) / len(frozen_input.calendar_sessions),
            membership_hash=stable_content_hash(frozen_input.historical_universe),
            future_membership_leakage=leakage,
        )

    def _field_coverage(self, frozen_input: FrozenAshareScaleInput) -> tuple[FieldCoverage, ...]:
        declared_features = set(frozen_input.feature_fields)
        declared_labels = set(frozen_input.label_fields)
        if not set(self.policy.required_feature_fields) <= declared_features:
            fail(FailureCode.MISSING_REQUIRED_FIELD, "required scale feature fields are not declared")
        if not declared_labels <= set(self.policy.allowed_label_fields):
            fail(FailureCode.UNKNOWN_FIELD, "unknown future-return label field")
        for row in frozen_input.rows:
            if not set(row.features) <= declared_features or not set(row.labels) <= declared_labels:
                fail(FailureCode.UNKNOWN_FIELD, "row contains undeclared feature or label")
            if any("future_return" in field_name.lower() for field_name in row.features):
                fail(FailureCode.UNAUTHORIZED_CAPABILITY, "future return cannot enter feature coverage")
        output: list[FieldCoverage] = []
        total = len(frozen_input.rows)
        for field_name in frozen_input.feature_fields:
            present = sum(row.features.get(field_name) is not None for row in frozen_input.rows)
            output.append(
                FieldCoverage(
                    field_name=field_name,
                    present_count=present,
                    total_count=total,
                    coverage=present / total,
                )
            )
        return tuple(output)

    def _time_semantics(self, frozen_input: FrozenAshareScaleInput) -> TimeSemanticsReport:
        ordered_violations = sum(row.event_time > row.available_at for row in frozen_input.rows)
        last_decision = max(item.decision_time for item in frozen_input.vintages)
        future_events = sum(row.event_time > last_decision for row in frozen_input.rows)
        visible = sum(
            row.available_at <= vintage.decision_time and row.session <= vintage.session
            for vintage in frozen_input.vintages
            for row in frozen_input.rows
        )
        return TimeSemanticsReport(
            row_count=len(frozen_input.rows),
            event_available_order_violations=ordered_violations,
            as_of_visible_observations=visible,
            future_event_violations=future_events,
            availability_policy_hash=frozen_input.availability_policy_hash,
        )

    def _validate_revision_history(self, vintages: tuple[FrozenDataVintage, ...]) -> None:
        seen: dict[str, FrozenDataVintage] = {}
        for vintage in sorted(vintages, key=lambda item: (item.decision_time, item.vintage_id)):
            if vintage.vintage_id in seen:
                fail(
                    FailureCode.ARTIFACT_COLLISION,
                    "vintage IDs are immutable and cannot be overwritten",
                    scale_failure_code=ScaleQualificationFailureCode.REVISION_HISTORY_INVALID.value,
                )
            if vintage.supersedes_vintage_id is not None and vintage.supersedes_vintage_id not in seen:
                fail(
                    FailureCode.INVALID_STATE,
                    "a revision must reference an earlier immutable vintage",
                    scale_failure_code=ScaleQualificationFailureCode.REVISION_HISTORY_INVALID.value,
                )
            seen[vintage.vintage_id] = vintage

    def _qualification_findings(
        self,
        frozen_input: FrozenAshareScaleInput,
        manifest: FrozenAshareScaleManifest,
        vintage_plan: DataVintagePlan | None,
    ) -> tuple[tuple[ScaleQualificationFailureCode, ...], tuple[str, ...]]:
        failures: list[ScaleQualificationFailureCode] = []
        reasons: list[str] = []

        if frozen_input.input_origin is not InputOrigin.FROZEN_REAL:
            failures.append(ScaleQualificationFailureCode.SYNTHETIC_NOT_REAL)
            reasons.append("test/synthetic input cannot satisfy the operational real-data smoke gate")
        if manifest.symbol_count < self.policy.min_symbols or manifest.session_count < self.policy.min_sessions:
            failures.append(ScaleQualificationFailureCode.BELOW_MINIMUM_SCALE)
            reasons.append("expanded snapshot must contain at least 20 historical symbols and 120 sessions")
        if manifest.symbol_count > self.policy.max_symbols or manifest.session_count > self.policy.max_sessions:
            failures.append(ScaleQualificationFailureCode.ABOVE_BOUNDED_SCALE)
            reasons.append("expanded snapshot exceeds the 50-symbol or 250-session bounded envelope")
        low_coverage = tuple(
            item.field_name
            for item in manifest.field_coverage
            if item.field_name in self.policy.required_feature_fields
            and item.coverage < self.policy.min_required_field_coverage
        )
        if low_coverage or manifest.universe_coverage.session_coverage < 1.0:
            failures.append(ScaleQualificationFailureCode.COVERAGE_BELOW_POLICY)
            reasons.append("required field or historical-universe session coverage is below policy")
        if manifest.universe_coverage.future_membership_leakage:
            failures.append(ScaleQualificationFailureCode.FUTURE_MEMBERSHIP_LEAKAGE)
            reasons.append("a row uses a symbol outside that session's historical universe")
        if (
            manifest.time_semantics.event_available_order_violations
            or manifest.time_semantics.future_event_violations
        ):
            failures.append(ScaleQualificationFailureCode.TIME_SEMANTICS_VIOLATION)
            reasons.append("event/availability/decision clocks violate as-of semantics")
        if vintage_plan is None:
            failures.append(ScaleQualificationFailureCode.VINTAGE_PLAN_INCOMPLETE)
            reasons.append("at least 20 distinct immutable decision vintages are required")
        return tuple(dict.fromkeys(failures)), tuple(dict.fromkeys(reasons))

    def _no_artifact_outcome(
        self,
        status: ScaleQualificationStatus,
        code: ScaleQualificationFailureCode,
        reason: str,
        operational_status: str,
    ) -> ScaleQualificationOutcome:
        verdict = ScaleQualificationVerdict(
            status=status,
            eligible=False,
            policy_hash=self.policy.artifact_hash,
            manifest_hash=None,
            vintage_plan_hash=None,
            failure_codes=(code,),
            reasons=(reason,),
            operational_smoke_status=operational_status,
        )
        return ScaleQualificationOutcome(verdict=verdict, manifest=None, vintage_plan=None)


def _map_contract_failure(exc: ContractValidationError) -> ScaleQualificationFailureCode:
    declared = exc.details.get("scale_failure_code") if hasattr(exc, "details") else None
    if declared is not None:
        try:
            return ScaleQualificationFailureCode(declared)
        except ValueError:
            return ScaleQualificationFailureCode.MISSING_REQUIRED_FIELD
    if exc.code is FailureCode.INVALID_HASH:
        return ScaleQualificationFailureCode.HASH_MISMATCH
    if exc.code in {FailureCode.INVALID_TIME_ORDER, FailureCode.INVALID_TIMESTAMP}:
        return ScaleQualificationFailureCode.TIME_SEMANTICS_VIOLATION
    if exc.code is FailureCode.UNAUTHORIZED_CAPABILITY:
        return ScaleQualificationFailureCode.FEATURE_LABEL_LEAKAGE
    return ScaleQualificationFailureCode.MISSING_REQUIRED_FIELD


__all__ = [
    "APPROVED_PROVIDER_CLASS",
    "APPROVED_SOURCE_CLASS",
    "BoundedAdapterUnavailable",
    "BoundedAshareScaleInputAdapter",
    "BoundedScaleInputRequest",
    "InMemoryBoundedScaleInputAdapter",
    "RealAshareScalePlanner",
]
