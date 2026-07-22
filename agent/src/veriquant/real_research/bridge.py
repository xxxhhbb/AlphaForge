"""Deterministic A-share snapshot to ResearchDatasetBundle bridge."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime, time
from types import MappingProxyType
from typing import Mapping

from veriquant.contracts import Component, ContractValidationError, DatasetCard, FailureCode
from veriquant.contracts.base import (
    fail,
    require_aware_datetime,
    require_hash,
    require_identifier,
    require_nonempty_tuple,
)
from veriquant.data import canonical_content_hash
from veriquant.data.ashare.models import AShareSnapshotRow

from .models import (
    BridgeRunManifest,
    DataQualityManifest,
    LabelSpecification,
    PITRejection,
    ResearchDatasetBundle,
    ResearchFeatureRow,
    ResearchLabelRow,
    ResearchSplitManifest,
    UniverseManifest,
    bundle_payload_hash,
)
from .reader import FrozenSnapshotEnvelope, FrozenSnapshotReader, RowMarketState


BRIDGE_VERSION = "ashare-research-bridge/v1"
REQUIRED_MARKET_STATE_FIELDS = ("suspended", "delisted", "limit_up", "limit_down", "listed_at")


@dataclass(frozen=True, kw_only=True)
class AShareBridgeRequest:
    snapshot_id: str
    decision_time: datetime
    feature_fields: tuple[str, ...]
    label_specification: LabelSpecification
    split_manifest: ResearchSplitManifest
    min_listing_days: int = 3
    run_id: str = "ashare-bridge-run"

    def __post_init__(self) -> None:
        require_identifier(self.snapshot_id, "snapshot_id")
        require_aware_datetime(self.decision_time, "decision_time")
        require_nonempty_tuple(self.feature_fields, "feature_fields")
        if len(set(self.feature_fields)) != len(self.feature_fields):
            fail(FailureCode.INVALID_VALUE, "feature_fields must be unique")
        for field in self.feature_fields:
            require_identifier(field, "feature_fields")
        if not isinstance(self.label_specification, LabelSpecification):
            fail(FailureCode.INVALID_VALUE, "label_specification is required")
        if not isinstance(self.split_manifest, ResearchSplitManifest):
            fail(FailureCode.INVALID_VALUE, "split_manifest is required")
        if isinstance(self.min_listing_days, bool) or not isinstance(self.min_listing_days, int) or self.min_listing_days < 0:
            fail(FailureCode.INVALID_VALUE, "min_listing_days must be non-negative")
        require_identifier(self.run_id, "run_id")


def _end_of_day(value: datetime) -> datetime:
    require_aware_datetime(value, "value")
    return datetime.combine(value.date(), time.max, tzinfo=value.tzinfo)


def _source_row_hash(row: AShareSnapshotRow) -> str:
    return canonical_content_hash(row)


def _date_keys(value: datetime) -> tuple[str, ...]:
    return (value.isoformat(), value.date().isoformat())


def _universe_for(envelope: FrozenSnapshotEnvelope, event_time: datetime) -> tuple[str, ...] | None:
    for key in _date_keys(event_time):
        if key in envelope.snapshot.historical_universe:
            return tuple(envelope.snapshot.historical_universe[key])
    return None


def _calendar_index(calendar: tuple[datetime, ...]) -> dict[datetime, int]:
    return {timestamp: index for index, timestamp in enumerate(calendar)}


def _market_state_map(envelope: FrozenSnapshotEnvelope) -> dict[tuple[str, datetime], RowMarketState]:
    return {(item.symbol, item.event_time): item for item in envelope.row_market_state}


class AShareResearchBridge:
    """Build ResearchDatasetBundle from a Prompt 10 frozen A-share snapshot."""

    def __init__(self, reader: FrozenSnapshotReader) -> None:
        self.reader = reader

    def build_bundle(self, request: AShareBridgeRequest) -> ResearchDatasetBundle:
        envelope = self.reader.read_snapshot(request.snapshot_id)
        if not isinstance(envelope, FrozenSnapshotEnvelope):
            fail(FailureCode.INVALID_VALUE, "reader returned an invalid frozen snapshot envelope")
        self._validate_envelope(envelope, request)

        raw_rows = tuple(
            sorted(
                envelope.snapshot.rows,
                key=lambda row: (row.event_time, row.symbol, row.available_at, row.published_at),
            )
        )
        market_state = _market_state_map(envelope)
        latest_eligible = self._latest_eligible_rows(raw_rows, request.decision_time)
        selected_keys = {id(row) for row in latest_eligible.values()}
        calendar = tuple(sorted(envelope.manifest.trading_calendar))
        calendar_pos = _calendar_index(calendar)
        close_lookup = self._eligible_price_lookup(
            raw_rows,
            request.decision_time,
            request.label_specification.price_field,
        )

        feature_rows: list[ResearchFeatureRow] = []
        label_rows: list[ResearchLabelRow] = []
        rejections: list[PITRejection] = []
        reason_counts: Counter[str] = Counter()
        non_missing = Counter()

        def reject(row: AShareSnapshotRow, reason: str) -> None:
            reason_counts[reason] += 1
            rejections.append(
                PITRejection(
                    event_time=row.event_time,
                    symbol=row.symbol,
                    reason=reason,
                    source_row_hash=_source_row_hash(row),
                )
            )

        for row in raw_rows:
            reason = self._raw_row_rejection(row, request.decision_time)
            if reason is not None:
                reject(row, reason)
                continue
            if id(row) not in selected_keys:
                reject(row, "SUPERSEDED_REVISION")
                continue
            reason = self._selected_row_rejection(
                row=row,
                request=request,
                envelope=envelope,
                market_state=market_state.get((row.symbol, row.event_time)),
            )
            if reason is not None:
                reject(row, reason)
                continue
            partition = request.split_manifest.partition_for(row.event_time)
            if partition is None:
                reject(row, "OUTSIDE_SPLIT")
                continue
            label = self._label_for(
                row=row,
                request=request,
                calendar=calendar,
                calendar_pos=calendar_pos,
                close_lookup=close_lookup,
            )
            if isinstance(label, str):
                reject(row, label)
                continue
            values = {field: float(row.values[field]) for field in request.feature_fields}
            for field in values:
                non_missing[field] += 1
            feature_rows.append(
                ResearchFeatureRow(
                    partition=partition,
                    event_time=row.event_time,
                    symbol=row.symbol,
                    values=values,
                    source_row_hash=_source_row_hash(row),
                    available_at=row.available_at,
                )
            )
            label_rows.append(
                ResearchLabelRow(
                    partition=partition,
                    event_time=row.event_time,
                    symbol=row.symbol,
                    value=label["value"],
                    entry_time=label["entry_time"],
                    exit_time=label["exit_time"],
                    available_at=label["available_at"],
                    label_spec_hash=request.label_specification.artifact_hash,
                )
            )

        if not feature_rows or not label_rows:
            fail(FailureCode.INCOMPLETE_RESULT, "bridge produced no usable research rows")

        historical_universe = {
            key: tuple(value) for key, value in envelope.snapshot.historical_universe.items()
        }
        universe_manifest = UniverseManifest(
            universe_id=f"universe-{envelope.snapshot.request.index_code}",
            index_code=envelope.snapshot.request.index_code,
            decision_time=request.decision_time,
            symbols=tuple(sorted({row.symbol for row in feature_rows})),
            historical_universe=historical_universe,
            source_snapshot_hash=envelope.snapshot.snapshot_hash,
        )
        coverage = {
            field: non_missing[field] / len(feature_rows)
            for field in request.feature_fields
        }
        quality_manifest = DataQualityManifest(
            quality_id=f"quality-{request.run_id}",
            rows_seen=len(raw_rows),
            rows_used=len(feature_rows),
            rows_rejected=len(rejections),
            rejection_reasons=MappingProxyType(dict(sorted(reason_counts.items()))),
            coverage_by_field=MappingProxyType(dict(sorted(coverage.items()))),
            required_market_state_fields=REQUIRED_MARKET_STATE_FIELDS,
        )
        payload_hash = bundle_payload_hash(
            feature_rows=tuple(feature_rows),
            label_rows=tuple(label_rows),
            rejections=tuple(rejections),
            label_specification=request.label_specification,
            split_manifest=request.split_manifest,
            universe_manifest=universe_manifest,
            data_quality_manifest=quality_manifest,
        )
        input_hash = canonical_content_hash(
            {
                "request": request,
                "snapshot_hash": envelope.snapshot.snapshot_hash,
                "reader_manifest": envelope.manifest,
            }
        )
        run_manifest = BridgeRunManifest(
            run_id=request.run_id,
            bridge_version=BRIDGE_VERSION,
            snapshot_id=request.snapshot_id,
            snapshot_hash=envelope.snapshot.snapshot_hash,
            input_hash=input_hash,
            output_hash=payload_hash,
            label_spec_hash=request.label_specification.artifact_hash,
            split_manifest_hash=request.split_manifest.artifact_hash,
            universe_manifest_hash=universe_manifest.artifact_hash,
            data_quality_hash=quality_manifest.artifact_hash,
            generated_at=request.decision_time,
            sealed_metrics_recorded=False,
        )
        card = DatasetCard(
            dataset_id=f"research-{envelope.snapshot.request.dataset_id}",
            source="frozen-ashare-bridge",
            universe=universe_manifest.symbols,
            horizon=f"{request.label_specification.forward_days}D",
            decision_time=request.decision_time,
            availability_assumptions=(
                "features require row.available_at <= bridge decision_time",
                "historical universe membership is resolved per event date",
                "future return labels are stored separately and never exposed as features",
                f"execution_lag_days={request.label_specification.execution_lag_days}",
            ),
            fields=request.feature_fields,
            event_time=min(row.event_time for row in feature_rows),
            published_at=max(row.available_at for row in feature_rows),
            available_at=max(
                [row.available_at for row in feature_rows]
                + [row.available_at for row in label_rows]
            ),
            snapshot_hash=payload_hash,
            record_count=len(feature_rows) + len(label_rows),
            produced_by=Component.DATA_SERVICE,
        )
        return ResearchDatasetBundle(
            bundle_id=f"bundle-{request.run_id}",
            dataset_card=card,
            decision_time=request.decision_time,
            feature_fields=request.feature_fields,
            feature_rows=tuple(feature_rows),
            label_rows=tuple(label_rows),
            rejections=tuple(rejections),
            label_specification=request.label_specification,
            split_manifest=request.split_manifest,
            universe_manifest=universe_manifest,
            data_quality_manifest=quality_manifest,
            bridge_run_manifest=run_manifest,
        )

    def _validate_envelope(self, envelope: FrozenSnapshotEnvelope, request: AShareBridgeRequest) -> None:
        snapshot = envelope.snapshot
        if envelope.manifest.snapshot_id != request.snapshot_id:
            fail(FailureCode.INVALID_IDENTIFIER, "reader returned a different snapshot_id")
        if snapshot.snapshot_hash != envelope.manifest.content_hash:
            fail(FailureCode.INVALID_HASH, "snapshot hash changed under reader manifest")
        if snapshot.request.dataset_id != snapshot.dataset_card.dataset_id:
            fail(FailureCode.INVALID_VALUE, "snapshot DatasetCard is inconsistent with request")
        if not set(request.feature_fields) <= set(snapshot.request.fields):
            fail(FailureCode.UNKNOWN_FIELD, "feature_fields must come from the frozen snapshot")
        if request.label_specification.price_field not in snapshot.request.fields:
            fail(FailureCode.UNKNOWN_FIELD, "label price field must exist in the frozen snapshot")
        if request.decision_time > snapshot.request.decision_time:
            fail(FailureCode.INVALID_TIME_ORDER, "bridge decision_time exceeds frozen snapshot decision_time")
        if request.split_manifest.sealed_end > request.decision_time:
            fail(FailureCode.INVALID_TIME_ORDER, "sealed split end exceeds bridge decision_time")
        if not snapshot.historical_universe:
            fail(FailureCode.INCOMPLETE_RESULT, "historical universe is required")
        if not envelope.manifest.trading_calendar:
            fail(FailureCode.INCOMPLETE_RESULT, "trading calendar is required")

    @staticmethod
    def _latest_eligible_rows(
        raw_rows: tuple[AShareSnapshotRow, ...],
        decision_time: datetime,
    ) -> dict[tuple[str, datetime], AShareSnapshotRow]:
        latest: dict[tuple[str, datetime], AShareSnapshotRow] = {}
        for row in raw_rows:
            if row.event_time > decision_time or row.available_at > decision_time:
                continue
            key = (row.symbol, row.event_time)
            current = latest.get(key)
            if current is None or (
                row.available_at,
                row.published_at,
                _source_row_hash(row),
            ) > (
                current.available_at,
                current.published_at,
                _source_row_hash(current),
            ):
                latest[key] = row
        return latest

    @staticmethod
    def _eligible_price_lookup(
        raw_rows: tuple[AShareSnapshotRow, ...],
        decision_time: datetime,
        price_field: str,
    ) -> dict[tuple[str, datetime], tuple[float, datetime]]:
        latest = AShareResearchBridge._latest_eligible_rows(raw_rows, decision_time)
        output: dict[tuple[str, datetime], tuple[float, datetime]] = {}
        for key, row in latest.items():
            value = row.values.get(price_field)
            if value is None or isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            output[key] = (float(value), row.available_at)
        return output

    @staticmethod
    def _raw_row_rejection(row: AShareSnapshotRow, decision_time: datetime) -> str | None:
        if row.event_time > decision_time:
            return "FUTURE_EVENT"
        if row.published_at > decision_time or row.available_at > decision_time:
            return "ROW_UNAVAILABLE"
        if row.published_at > row.available_at:
            return "INVALID_ROW_TIME_ORDER"
        return None

    @staticmethod
    def _selected_row_rejection(
        *,
        row: AShareSnapshotRow,
        request: AShareBridgeRequest,
        envelope: FrozenSnapshotEnvelope,
        market_state: RowMarketState | None,
    ) -> str | None:
        members = _universe_for(envelope, row.event_time)
        if members is None:
            return "MISSING_UNIVERSE"
        if row.symbol not in members:
            return "UNIVERSE_NOT_MEMBER"
        if market_state is None:
            return "MISSING_MARKET_STATE"
        if market_state.available_at > request.decision_time:
            return "MARKET_STATE_UNAVAILABLE"
        for name in ("suspended", "delisted", "limit_up", "limit_down"):
            if getattr(market_state, name) is None:
                return f"MISSING_{name.upper()}"
        if market_state.suspended:
            return "SUSPENDED"
        if market_state.delisted:
            return "DELISTED"
        if market_state.limit_up or market_state.limit_down:
            return "PRICE_LIMIT"
        listing_age = (row.event_time.date() - market_state.listed_at.date()).days
        if listing_age < request.min_listing_days:
            return "LISTING_AGE_INSUFFICIENT"
        for field in request.feature_fields:
            value = row.values.get(field)
            if value is None:
                return f"MISSING_FEATURE_{field}"
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return f"INVALID_FEATURE_{field}"
        return None

    @staticmethod
    def _label_for(
        *,
        row: AShareSnapshotRow,
        request: AShareBridgeRequest,
        calendar: tuple[datetime, ...],
        calendar_pos: Mapping[datetime, int],
        close_lookup: Mapping[tuple[str, datetime], tuple[float, datetime]],
    ) -> Mapping[str, datetime | float] | str:
        if row.event_time not in calendar_pos:
            return "EVENT_NOT_IN_CALENDAR"
        entry_index = calendar_pos[row.event_time] + request.label_specification.execution_lag_days
        exit_index = entry_index + request.label_specification.forward_days
        if entry_index >= len(calendar) or exit_index >= len(calendar):
            return "LABEL_WINDOW_UNAVAILABLE"
        entry_time = calendar[entry_index]
        exit_time = calendar[exit_index]
        entry = close_lookup.get((row.symbol, entry_time))
        exit_ = close_lookup.get((row.symbol, exit_time))
        if entry is None or exit_ is None:
            return "LABEL_PRICE_UNAVAILABLE"
        entry_price, _entry_available_at = entry
        exit_price, label_available_at = exit_
        if entry_price <= 0.0:
            return "INVALID_ENTRY_PRICE"
        if label_available_at > request.decision_time:
            return "LABEL_UNAVAILABLE"
        return MappingProxyType(
            {
                "value": exit_price / entry_price - 1.0,
                "entry_time": entry_time,
                "exit_time": exit_time,
                "available_at": label_available_at,
            }
        )


def build_default_bridge_request(
    *,
    snapshot_id: str,
    decision_time: datetime,
    split_manifest: ResearchSplitManifest,
    forward_days: int = 2,
    execution_lag_days: int = 1,
    feature_fields: tuple[str, ...] = ("close", "volume", "amount"),
    run_id: str = "ashare-bridge-run",
) -> AShareBridgeRequest:
    """Convenience request used by the offline fixture tests."""

    label_spec = LabelSpecification(
        label_id=f"fwd-{forward_days}d-lag-{execution_lag_days}",
        name=f"future_{forward_days}d_return_lag{execution_lag_days}",
        price_field="close",
        forward_days=forward_days,
        execution_lag_days=execution_lag_days,
        return_type="simple_forward_return",
        label_start=split_manifest.train_start,
        label_end=split_manifest.sealed_end,
        availability_assumption="label is known only after exit bar availability; never a feature",
    )
    return AShareBridgeRequest(
        snapshot_id=snapshot_id,
        decision_time=decision_time,
        feature_fields=feature_fields,
        label_specification=label_spec,
        split_manifest=split_manifest,
        run_id=run_id,
    )
