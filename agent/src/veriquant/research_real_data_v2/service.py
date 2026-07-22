"""Thin qualification and PIT-registry bridge for frozen A-share inputs.

The bridge validates provenance and data completeness, then delegates all
as-of selection to the existing :mod:`veriquant.data.bitemporal` registry.
It performs no factor, evidence, promotion, portfolio, or ledger calculation.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time
from typing import Protocol

from ..contracts import Component, DatasetCard, PointInTimeRecord
from ..data.bitemporal import DatasetRegistry, FieldRole, UniverseMembership, canonical_content_hash
from ..real_ashare_scale import FrozenAshareScaleInput, InputOrigin
from .models import (
    APPROVED_PROVIDER_CLASS,
    APPROVED_SOURCE_CLASS,
    ResearchDataFailureCode,
    ResearchDataManifest,
    ResearchDataPolicy,
    ResearchDataQualification,
    ResearchDataRequest,
    ResearchDataStatus,
)


class FrozenResearchDataProvider(Protocol):
    """Minimal adapter boundary; credentials are deliberately absent."""

    provider_class: str
    source_class: str
    network_capable: bool

    def load(self, request: ResearchDataRequest) -> FrozenAshareScaleInput | None: ...


@dataclass(frozen=True)
class PreparedResearchData:
    qualification: ResearchDataQualification
    registry: DatasetRegistry | None
    dataset_card: DatasetCard | None


class InMemoryFrozenResearchDataProvider:
    provider_class = APPROVED_PROVIDER_CLASS
    source_class = APPROVED_SOURCE_CLASS
    network_capable = False

    def __init__(self, frozen_input: FrozenAshareScaleInput | None) -> None:
        self._frozen_input = frozen_input

    def load(self, request: ResearchDataRequest) -> FrozenAshareScaleInput | None:
        return self._frozen_input


class ResearchRealDataService:
    """Qualify frozen inputs and register them with the existing PIT authority."""

    def __init__(self, policy: ResearchDataPolicy | None = None) -> None:
        self.policy = policy or ResearchDataPolicy()

    def prepare(
        self,
        provider: FrozenResearchDataProvider,
        request: ResearchDataRequest,
    ) -> PreparedResearchData:
        if provider.provider_class != APPROVED_PROVIDER_CLASS or provider.source_class != APPROVED_SOURCE_CLASS:
            return self._blocked(request, ResearchDataFailureCode.SOURCE_LABEL_REJECTED, message="数据来源未获批准")
        if provider.network_capable and not request.allow_network:
            return self._blocked(request, ResearchDataFailureCode.NETWORK_NOT_ENABLED, message="网络获取未显式启用")
        frozen = provider.load(request)
        if frozen is None:
            return self._blocked(request, ResearchDataFailureCode.ADAPTER_UNAVAILABLE, message="冻结数据提供器不可用", unavailable=True)
        if frozen.dataset_id != request.dataset_id:
            return self._blocked(request, ResearchDataFailureCode.HASH_MISMATCH, message="数据集身份与请求不一致")
        failures = self._validate(frozen, request)
        if failures:
            return self._blocked(request, *failures, message="冻结 A 股数据未通过资格检查")
        try:
            registry, card = self._register(frozen)
        except Exception as exc:
            # Existing typed PIT registry remains the authority.  Do not repair
            # or relax its rejection here.
            return self._blocked(
                request,
                ResearchDataFailureCode.TIME_SEMANTICS_VIOLATION,
                message=f"PIT 注册失败：{type(exc).__name__}",
            )
        manifest = self._manifest(frozen, request)
        status = ResearchDataStatus.READY if frozen.input_origin is InputOrigin.FROZEN_REAL else ResearchDataStatus.FIXTURE_ONLY
        qualification = ResearchDataQualification(
            request_hash=request.artifact_hash,
            status=status,
            failure_codes=(),
            message="冻结数据已通过资格检查并注册到现有 PIT 服务",
            manifest=manifest,
            dataset_card_hash=card.artifact_hash,
        )
        return PreparedResearchData(qualification=qualification, registry=registry, dataset_card=card)

    def _validate(self, frozen: FrozenAshareScaleInput, request: ResearchDataRequest) -> tuple[ResearchDataFailureCode, ...]:
        failures: set[ResearchDataFailureCode] = set()
        if frozen.provider_class != APPROVED_PROVIDER_CLASS or frozen.source_class != APPROVED_SOURCE_CLASS:
            failures.add(ResearchDataFailureCode.SOURCE_LABEL_REJECTED)
        sessions = frozen.calendar_sessions
        if len(sessions) < self.policy.min_sessions:
            failures.add(ResearchDataFailureCode.BELOW_MINIMUM_SCALE)
        if request.lookback_sessions >= 50 and len(sessions) < self.policy.recommended_sessions_for_50d:
            failures.add(ResearchDataFailureCode.INSUFFICIENT_LOOKBACK_HISTORY)
        if len(sessions) > self.policy.max_sessions:
            failures.add(ResearchDataFailureCode.ABOVE_BOUNDED_SCALE)
        if frozen.calendar_hash != canonical_content_hash(sessions):
            failures.add(ResearchDataFailureCode.HASH_MISMATCH)
        membership_by_session = {item.session: item.symbols for item in frozen.historical_universe}
        if set(membership_by_session) != set(sessions) or len(membership_by_session) != len(frozen.historical_universe):
            failures.add(ResearchDataFailureCode.UNIVERSE_INCOMPLETE)
        if frozen.universe_hash != canonical_content_hash(frozen.historical_universe):
            failures.add(ResearchDataFailureCode.HASH_MISMATCH)
        member_counts = [len(membership_by_session.get(session, ())) for session in sessions]
        if not member_counts or min(member_counts) < self.policy.min_symbols_per_session:
            failures.add(ResearchDataFailureCode.BELOW_MINIMUM_SCALE)
        if member_counts and max(member_counts) > self.policy.max_symbols_per_session:
            failures.add(ResearchDataFailureCode.ABOVE_BOUNDED_SCALE)
        declared_features = set(frozen.feature_fields)
        declared_labels = set(frozen.label_fields)
        if not set(request.required_feature_fields) <= declared_features:
            failures.add(ResearchDataFailureCode.MISSING_REQUIRED_FIELD)
        if not set(self.policy.required_label_fields) <= declared_labels:
            failures.add(ResearchDataFailureCode.MISSING_REQUIRED_FIELD)
        if any("future_return" in name.lower() for name in declared_features):
            failures.add(ResearchDataFailureCode.FEATURE_LABEL_LEAKAGE)
        if frozen.data_hash != canonical_content_hash(frozen.rows):
            failures.add(ResearchDataFailureCode.HASH_MISMATCH)

        latest: dict[tuple[str, str], object] = {}
        for row in frozen.rows:
            if row.session not in membership_by_session:
                failures.add(ResearchDataFailureCode.CALENDAR_INCOMPLETE)
                continue
            if row.symbol not in membership_by_session[row.session]:
                failures.add(ResearchDataFailureCode.FUTURE_MEMBERSHIP_LEAKAGE)
            if row.available_at > frozen.capture_time:
                failures.add(ResearchDataFailureCode.TIME_SEMANTICS_VIOLATION)
            key = (row.session, row.symbol)
            current = latest.get(key)
            if current is None or (row.available_at, row.revision_id) > (current.available_at, current.revision_id):
                latest[key] = row
        expected = sum(member_counts)
        for field in request.required_feature_fields:
            present = sum(getattr(row, "features").get(field) is not None for row in latest.values())
            if expected == 0 or present / expected < self.policy.min_field_coverage:
                failures.add(ResearchDataFailureCode.COVERAGE_BELOW_POLICY)
        for field in self.policy.required_label_fields:
            present = sum(getattr(row, "labels").get(field) is not None for row in latest.values())
            if expected == 0 or present / expected < self.policy.min_field_coverage:
                failures.add(ResearchDataFailureCode.COVERAGE_BELOW_POLICY)
        if len(latest) < expected:
            failures.add(ResearchDataFailureCode.COVERAGE_BELOW_POLICY)
        return tuple(sorted(failures, key=lambda item: item.value))

    def _register(self, frozen: FrozenAshareScaleInput) -> tuple[DatasetRegistry, DatasetCard]:
        membership_by_session = {item.session: item.symbols for item in frozen.historical_universe}
        latest = {}
        for row in frozen.rows:
            key = (row.session, row.symbol)
            current = latest.get(key)
            if current is None or (row.available_at, row.revision_id) > (current.available_at, current.revision_id):
                latest[key] = row
        records: list[PointInTimeRecord] = []
        for row in sorted(latest.values(), key=lambda item: (item.session, item.symbol)):
            published_at = row.available_at
            for field, value in sorted(row.features.items()):
                records.append(PointInTimeRecord(
                    record_id=f"{row.revision_id}.{field}", dataset_id=frozen.dataset_id,
                    entity_id=row.symbol, field_name=field, value=value,
                    event_time=row.event_time, published_at=published_at,
                    available_at=row.available_at, source=APPROVED_PROVIDER_CLASS,
                    produced_by=Component.DATA_SERVICE,
                ))
            for field, value in sorted(row.labels.items()):
                records.append(PointInTimeRecord(
                    record_id=f"{row.revision_id}.{field}", dataset_id=frozen.dataset_id,
                    entity_id=row.symbol, field_name=field, value=value,
                    event_time=row.event_time, published_at=published_at,
                    available_at=row.available_at, source=APPROVED_PROVIDER_CLASS,
                    produced_by=Component.DATA_SERVICE,
                ))
        calendar = tuple(_session_time(session) for session in frozen.calendar_sessions)
        memberships = _membership_intervals(frozen.dataset_id, frozen.calendar_sessions, membership_by_session)
        field_roles = {field: FieldRole.FEATURE for field in frozen.feature_fields}
        field_roles.update({field: FieldRole.LABEL for field in frozen.label_fields})
        registry = DatasetRegistry()
        card = registry.register(
            dataset_id=frozen.dataset_id,
            records=records,
            memberships=memberships,
            field_roles=field_roles,
            calendar=calendar,
            seed=0,
            source=APPROVED_PROVIDER_CLASS,
        )
        return registry, card

    def _manifest(self, frozen: FrozenAshareScaleInput, request: ResearchDataRequest) -> ResearchDataManifest:
        memberships = {item.session: item.symbols for item in frozen.historical_universe}
        latest = {}
        for row in frozen.rows:
            key = (row.session, row.symbol)
            current = latest.get(key)
            if current is None or (row.available_at, row.revision_id) > (current.available_at, current.revision_id):
                latest[key] = row
        expected = sum(len(value) for value in memberships.values())
        coverage = {}
        for field in frozen.feature_fields:
            coverage[field] = sum(row.features.get(field) is not None for row in latest.values()) / expected
        for field in request.optional_feature_fields:
            coverage.setdefault(field, 0.0)
        for field in frozen.label_fields:
            coverage[field] = sum(row.labels.get(field) is not None for row in latest.values()) / expected
        content_hash = canonical_content_hash({
            "calendar": frozen.calendar_sessions,
            "historical_universe": frozen.historical_universe,
            "rows": tuple(sorted(frozen.rows, key=lambda item: (item.session, item.symbol, item.available_at, item.revision_id))),
            "feature_fields": frozen.feature_fields,
            "label_fields": frozen.label_fields,
            "availability_policy_hash": frozen.availability_policy_hash,
        })
        counts = tuple(len(value) for value in memberships.values())
        available_optional = tuple(sorted(set(request.optional_feature_fields) & set(frozen.feature_fields)))
        missing_optional = tuple(sorted(set(request.optional_feature_fields) - set(frozen.feature_fields)))
        return ResearchDataManifest(
            manifest_id=f"manifest.{frozen.dataset_id}", dataset_id=frozen.dataset_id,
            provider_class=frozen.provider_class, source_class=frozen.source_class,
            input_origin=frozen.input_origin.value, calendar_id=frozen.calendar_id,
            calendar_hash=frozen.calendar_hash, universe_hash=frozen.universe_hash,
            content_hash=content_hash, source_adapter_manifest_hash=frozen.adapter_manifest_hash,
            availability_policy_hash=frozen.availability_policy_hash,
            symbol_count=len({symbol for value in memberships.values() for symbol in value}),
            minimum_symbols_per_session=min(counts), maximum_symbols_per_session=max(counts),
            session_count=len(frozen.calendar_sessions), row_count=len(frozen.rows),
            feature_fields=frozen.feature_fields, label_fields=frozen.label_fields,
            coverage_by_field=coverage,
            recommended_50d_history_satisfied=len(frozen.calendar_sessions) >= self.policy.recommended_sessions_for_50d,
            network_used_at_capture=frozen.capture_network_used,
            requested_required_feature_fields=request.required_feature_fields,
            requested_optional_feature_fields=request.optional_feature_fields,
            available_optional_feature_fields=available_optional,
            missing_optional_feature_fields=missing_optional,
            limitations=tuple(f"OPTIONAL_FIELD_UNAVAILABLE:{field}" for field in missing_optional),
        )

    def _blocked(self, request: ResearchDataRequest, *codes: ResearchDataFailureCode, message: str, unavailable: bool = False) -> PreparedResearchData:
        status = ResearchDataStatus.UNAVAILABLE if unavailable else ResearchDataStatus.BLOCKED
        return PreparedResearchData(
            qualification=ResearchDataQualification(
                request_hash=request.artifact_hash, status=status,
                failure_codes=tuple(sorted(set(codes), key=lambda item: item.value)), message=message,
            ),
            registry=None,
            dataset_card=None,
        )


def _session_time(session: str) -> datetime:
    parsed = datetime.fromisoformat(session)
    return datetime.combine(parsed.date(), time(15, 0), tzinfo=parsed.tzinfo) if parsed.tzinfo else datetime.fromisoformat(session + "T15:00:00+08:00")


def _membership_intervals(dataset_id: str, sessions: tuple[str, ...], membership_by_session: dict[str, tuple[str, ...]]) -> tuple[UniverseMembership, ...]:
    times = tuple(_session_time(session) for session in sessions)
    symbols = sorted({symbol for members in membership_by_session.values() for symbol in members})
    output: list[UniverseMembership] = []
    for symbol in symbols:
        start_index = None
        for index, session in enumerate(sessions):
            active = symbol in membership_by_session[session]
            if active and start_index is None:
                start_index = index
            at_end = start_index is not None and (not active or index == len(sessions) - 1)
            if at_end:
                last_index = index if active else index - 1
                member_from = times[start_index]
                member_until = times[last_index + 1] if last_index + 1 < len(times) else None
                output.append(UniverseMembership(
                    dataset_id=dataset_id, symbol=symbol, member_from=member_from,
                    member_until=member_until, published_at=member_from, available_at=member_from,
                ))
                start_index = None
    return tuple(output)


__all__ = [
    "FrozenResearchDataProvider",
    "InMemoryFrozenResearchDataProvider",
    "PreparedResearchData",
    "ResearchRealDataService",
]
