"""Deterministic data requirement and capability assessment services."""

from __future__ import annotations

from collections import Counter
from statistics import median
import re
from typing import Iterable, Protocol

from ..bitemporal_features import NormalizedRecord, SourceKind
from ..data import canonical_content_hash
from ..dynamic_data_planning import DynamicDataPlan, UniverseKind
from .models import (
    APPROVED_SOURCE_LABEL,
    SCHEMA_VERSION,
    CapabilityStatus,
    DataGapCode,
    DataRequirementPlan,
    DatasetCapabilityReport,
    FieldCoverageProof,
    FreezeExpansionRequest,
    FrozenResearchReceipt,
)


class FrozenResearchData(Protocol):
    """Minimal read-only view; financial validation packages stay decoupled."""

    snapshot: object
    labels: object
    source_label: str


class DataRequirementPlanner:
    """Add split leakage controls and dynamic sample needs to an existing plan."""

    def build(self, plan: DynamicDataPlan) -> DataRequirementPlan:
        if not isinstance(plan, DynamicDataPlan):
            raise ValueError("plan must be DynamicDataPlan")
        purge = plan.history.label_sessions
        embargo = max(1, (plan.history.label_sessions + 1) // 2)
        minimum_sessions = plan.history.minimum_total_sessions + purge + embargo
        minimum_cross_section = _minimum_cross_section(plan)
        # Listing-event columns prove how an as-of universe was constructed;
        # they are not per-row factor features. Their authority is checked by
        # ``historical_universe_verified`` and row provenance below.
        structural_universe_fields = {
            "list_date", "delist_date", "in_date", "out_date",
            "ann_date", "f_ann_date", "end_date", "report_type",
        }
        fields = tuple(sorted({
            field
            for item in plan.requirements
            for field in item.required_fields
            if field not in structural_universe_fields
        }))
        coverage = {field: _required_coverage(field, plan) for field in fields}
        return DataRequirementPlan(
            schema_version=SCHEMA_VERSION,
            dynamic_plan_hash=plan.plan_hash,
            question=plan.question,
            required_datasets=tuple(item.dataset_id for item in plan.requirements),
            required_fields=fields,
            warmup_sessions=plan.history.warmup_sessions,
            train_sessions=plan.history.train_sessions,
            validation_sessions=plan.history.validation_sessions,
            sealed_sessions=plan.history.sealed_sessions,
            label_horizon_sessions=plan.history.label_sessions,
            purge_sessions=purge,
            embargo_sessions=embargo,
            buffer_sessions=plan.history.buffer_sessions,
            minimum_sessions=minimum_sessions,
            minimum_cross_section=minimum_cross_section,
            minimum_rows=minimum_sessions * minimum_cross_section,
            minimum_field_coverage=coverage,
            split_policy="walk_forward_with_label_purge_and_post_validation_embargo",
        )


class DatasetCapabilityAssessor:
    """Prove usability from frozen observations, not declared field names."""

    def assess(
        self,
        *,
        requirement: DataRequirementPlan,
        frozen_data: FrozenResearchData,
        bitemporal_records: Iterable[NormalizedRecord] = (),
    ) -> DatasetCapabilityReport:
        if frozen_data.source_label != APPROVED_SOURCE_LABEL:
            raise ValueError("capability assessment accepts only APPROVED_PROXY_DATA_VENDOR")
        snapshot = frozen_data.snapshot
        by_session: dict[object, set[str]] = {}
        symbols: set[str] = set()
        field_rows: dict[str, int] = Counter()
        field_sessions: dict[str, set[object]] = {field: set() for field in requirement.required_fields}
        for row in snapshot.rows:
            by_session.setdefault(row.event_time, set()).add(row.symbol)
            symbols.add(row.symbol)
            for field in requirement.required_fields:
                if _field_observed(field, row):
                    field_rows[field] += 1
                    field_sessions[field].add(row.event_time)
        total_rows = len(snapshot.rows)
        expected_label_keys = {
            f"{row.event_time.isoformat()}|{row.symbol}" for row in snapshot.rows
        }
        field_proofs = tuple(
            FieldCoverageProof(
                field_name=field,
                observed_rows=field_rows[field],
                total_rows=total_rows,
                observed_sessions=len(field_sessions[field]),
                coverage=(field_rows[field] / total_rows if total_rows else 0.0),
                required_coverage=requirement.minimum_field_coverage[field],
                usable=(
                    field_rows[field] > 0
                    and total_rows > 0
                    and field_rows[field] / total_rows >= requirement.minimum_field_coverage[field]
                ),
            )
            for field in requirement.required_fields
        )
        records = tuple(bitemporal_records)
        if not records and getattr(frozen_data, "row_provenance", ()):
            values_by_key = {
                (row.symbol, row.event_time): row.values
                for row in snapshot.rows
            }
            records = tuple(
                NormalizedRecord(
                    source=SourceKind(item.source_kind),
                    entity_id=item.symbol,
                    event_time=item.event_time,
                    published_at=item.published_at,
                    available_at=item.available_at,
                    revision_id=item.revision_id,
                    values=values_by_key[(item.symbol, item.event_time)],
                    raw_hash=item.source_record_hash,
                    decision_time=snapshot.decision_time,
                )
                for item in frozen_data.row_provenance
            )
        expected_clock_keys = {(row.symbol, row.event_time) for row in snapshot.rows}
        observed_clock_keys = {(item.entity_id, item.event_time) for item in records}
        available_at_verified = (
            bool(records)
            and expected_clock_keys <= observed_clock_keys
            and all(item.available_at <= snapshot.decision_time for item in records)
        )
        revision_verified = (
            bool(records)
            and expected_clock_keys <= observed_clock_keys
            and len({item.revision_key for item in records}) == len(records)
        )
        embedded_provenance = tuple(getattr(frozen_data, "row_provenance", ()))
        source_provenance_verified = (
            bool(embedded_provenance)
            and expected_clock_keys
            == {(item.symbol, item.event_time) for item in embedded_provenance}
            and all(item.source_label == APPROVED_SOURCE_LABEL for item in embedded_provenance)
        )
        universe_verified = _historical_universe_verified(snapshot)
        cross_sections = tuple(sorted(len(value) for value in by_session.values()))
        label_rows = sum(key in frozen_data.labels for key in expected_label_keys)
        label_coverage = label_rows / total_rows if total_rows else 0.0
        gaps: set[DataGapCode] = set()
        if len(by_session) < requirement.minimum_sessions:
            gaps.add(DataGapCode.HISTORY_TOO_SHORT)
        if not cross_sections or min(cross_sections) < requirement.minimum_cross_section:
            gaps.add(DataGapCode.CROSS_SECTION_TOO_NARROW)
        for proof in field_proofs:
            if proof.observed_rows == 0:
                gaps.add(DataGapCode.FIELD_HAS_NO_OBSERVATIONS)
            elif not proof.usable:
                gaps.add(DataGapCode.FIELD_COVERAGE_INSUFFICIENT)
        if not available_at_verified:
            gaps.add(DataGapCode.AVAILABLE_AT_UNVERIFIED)
        if not revision_verified:
            gaps.add(DataGapCode.REVISION_SEMANTICS_UNVERIFIED)
        if not source_provenance_verified:
            gaps.add(DataGapCode.SOURCE_PROVENANCE_UNVERIFIED)
        if not universe_verified:
            gaps.add(DataGapCode.HISTORICAL_UNIVERSE_UNVERIFIED)
        if not frozen_data.labels:
            gaps.add(DataGapCode.LABELS_MISSING)
        elif label_coverage < 1.0:
            gaps.add(DataGapCode.LABEL_COVERAGE_INSUFFICIENT)
        critical = {
            DataGapCode.HISTORY_TOO_SHORT,
            DataGapCode.CROSS_SECTION_TOO_NARROW,
            DataGapCode.FIELD_HAS_NO_OBSERVATIONS,
            DataGapCode.AVAILABLE_AT_UNVERIFIED,
            DataGapCode.SOURCE_PROVENANCE_UNVERIFIED,
            DataGapCode.HISTORICAL_UNIVERSE_UNVERIFIED,
            DataGapCode.LABELS_MISSING,
        }
        status = CapabilityStatus.READY if not gaps else CapabilityStatus.UNAVAILABLE if gaps & critical else CapabilityStatus.PARTIAL
        sessions = tuple(sorted(by_session))
        return DatasetCapabilityReport(
            schema_version=SCHEMA_VERSION,
            requirement_plan_hash=requirement.artifact_hash,
            source_label=APPROVED_SOURCE_LABEL,
            snapshot_hash=snapshot.artifact_hash,
            observed_start=sessions[0] if sessions else None,
            observed_end=sessions[-1] if sessions else None,
            observed_sessions=len(sessions),
            observed_symbols=len(symbols),
            minimum_cross_section_observed=min(cross_sections) if cross_sections else 0,
            median_cross_section_observed=float(median(cross_sections)) if cross_sections else 0.0,
            maximum_cross_section_observed=max(cross_sections) if cross_sections else 0,
            field_proofs=field_proofs,
            available_at_verified=available_at_verified,
            revision_semantics_verified=revision_verified,
            source_provenance_verified=source_provenance_verified,
            historical_universe_verified=universe_verified,
            label_rows=label_rows,
            label_coverage=label_coverage,
            status=status,
            gap_codes=tuple(gaps),
        )

    @staticmethod
    def receipt(
        *,
        requirement: DataRequirementPlan,
        frozen_data: FrozenResearchData,
        report: DatasetCapabilityReport,
        freeze_manifest_hash: str | None,
    ) -> FrozenResearchReceipt:
        gaps = set(report.gap_codes)
        if freeze_manifest_hash is None:
            gaps.add(DataGapCode.FREEZE_MANIFEST_MISSING)
        status = report.status if freeze_manifest_hash is not None else CapabilityStatus.UNAVAILABLE
        return FrozenResearchReceipt(
            schema_version=SCHEMA_VERSION,
            requirement_plan_hash=requirement.artifact_hash,
            capability_report_hash=report.artifact_hash,
            snapshot_hash=frozen_data.snapshot.artifact_hash,
            labels_hash=canonical_content_hash(dict(sorted(frozen_data.labels.items()))),
            freeze_manifest_hash=freeze_manifest_hash,
            source_label=APPROVED_SOURCE_LABEL,
            status=status,
            gap_codes=tuple(gaps),
        )

    @staticmethod
    def expansion_request(
        *,
        dynamic_plan: DynamicDataPlan,
        requirement: DataRequirementPlan,
        report: DatasetCapabilityReport,
        approved_local_source_available: bool,
    ) -> FreezeExpansionRequest:
        missing_fields = tuple(item.field_name for item in report.field_proofs if not item.usable)
        missing_sessions = max(0, requirement.minimum_sessions - report.observed_sessions)
        needed = bool(report.gap_codes)
        status = (
            "NOT_REQUIRED" if not needed
            else "OFFLINE_ACQUISITION_REQUIRED" if approved_local_source_available
            else "BLOCKED_NO_APPROVED_LOCAL_SOURCE"
        )
        return FreezeExpansionRequest(
            requirement_plan_hash=requirement.artifact_hash,
            source_label=APPROVED_SOURCE_LABEL,
            missing_sessions=missing_sessions,
            missing_fields=missing_fields,
            requested_api_names=dynamic_plan.requested_api_names,
            status=status,
            network_allowed=False,
        )


def _minimum_cross_section(plan: DynamicDataPlan) -> int:
    if plan.universe.kind is UniverseKind.EXPLICIT_SYMBOLS:
        return len(plan.universe.symbols)
    match = re.search(r"(\d+)\s*(?:分位|quantile)", plan.question, re.IGNORECASE)
    quantiles = int(match.group(1)) if match else max(2, len(plan.themes) + 1)
    return max(2, quantiles * 2)


def _required_coverage(field: str, plan: DynamicDataPlan) -> float:
    if field in {"ts_code", "trade_date", "close", "vol", "adj_factor", "list_date", "delist_date"}:
        return 0.95
    if "ann_date" in field or field in {"end_date", "report_type"}:
        return 0.8
    return 0.6 if any("fundamental" in item.dataset_id for item in plan.requirements) else 0.8


def _historical_universe_verified(snapshot) -> bool:
    if not snapshot.rows or not snapshot.historical_universe:
        return False
    for row in snapshot.rows:
        members = snapshot.historical_universe.get(row.event_time.isoformat())
        if members is None or row.symbol not in members:
            return False
    return True


def _field_observed(field: str, row) -> bool:
    if field == "ts_code":
        return bool(row.symbol)
    if field in {"trade_date", "date"}:
        return row.event_time is not None
    aliases = {
        "vol": "volume",
        "total_mv": "market_cap",
        "circ_mv": "float_market_cap",
        "n_income": "net_income",
        "n_income_attr_p": "net_income_parent",
        "n_cashflow_act": "operating_cashflow",
        "grossprofit_margin": "gross_margin",
        "netprofit_margin": "net_margin",
        "assets_turn": "asset_turnover",
        "or_yoy": "revenue_yoy",
        "netprofit_yoy": "net_income_yoy",
        "ocf_to_or": "operating_cashflow_to_revenue",
        "bps": "book_value_per_share",
        "net_mf_amount": "net_moneyflow_amount",
        "on": "shibor_overnight",
        "1w": "shibor_1w",
        "pct_chg": "index_pct_change",
        "p_change_min": "forecast_profit_change_min",
        "p_change_max": "forecast_profit_change_max",
    }
    key = field if field in row.values else aliases.get(field, field)
    if key not in row.values:
        return False
    # A null delisting date is an observed statement that the security had not
    # delisted; other feature nulls are missing observations.
    return field == "delist_date" or row.values.get(key) is not None
