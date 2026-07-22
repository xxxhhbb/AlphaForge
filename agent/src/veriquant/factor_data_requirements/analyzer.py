"""Mechanical classification of the real DATA_FIELD_MISSING projection."""

from __future__ import annotations

from typing import Iterable, Protocol

from .models import (
    SCHEMA_VERSION,
    FactorDataRequirementReport,
    FactorFieldRequirement,
    RequirementDisposition,
)


class ProjectionRecord(Protocol):
    factor_id: str
    source_hash: str
    required_fields: tuple[str, ...]
    missing_fields: tuple[str, ...]


_VENDOR_FIELDS = {
    "fund:asset_growth": (
        ("balancesheet",),
        ("total_assets",),
        "(total_assets[t] / total_assets[prior comparable period]) - 1; revisions selected as-of decision_time",
    ),
    "fund:roe": (
        ("fina_indicator",),
        ("roe", "ann_date", "end_date"),
        None,
    ),
}


class FactorDataRequirementAnalyzer:
    """Classify fields without granting execution or inventing substitutions."""

    def analyze(
        self,
        records: Iterable[ProjectionRecord],
        *,
        projection_hash: str,
    ) -> FactorDataRequirementReport:
        output = tuple(
            sorted(
                (self._classify(record, projection_hash=projection_hash) for record in records),
                key=lambda item: item.factor_id,
            )
        )
        return FactorDataRequirementReport(
            schema_version=SCHEMA_VERSION,
            projection_hash=projection_hash,
            records=output,
        )

    @staticmethod
    def _classify(record: ProjectionRecord, *, projection_hash: str) -> FactorFieldRequirement:
        missing = tuple(sorted(set(record.missing_fields)))
        required = tuple(sorted(set(record.required_fields)))
        missing_set = set(missing)
        if missing_set.intersection({"sector", "industry"}):
            disposition = RequirementDisposition.PIT_TIME_UNPROVEN
            apis = ("stock_basic",)
            source_fields = ("industry", "industry_available_date", "industry_effective_date")
            derivation = None
            pit = "effective_at and available_at history must be frozen and each row must be visible by decision_time"
            reasons = ("CURRENT_CLASSIFICATION_MUST_NOT_BE_BACKFILLED", "PIT_INDUSTRY_HISTORY_UNPROVEN")
        elif len(missing) == 1 and missing[0] in _VENDOR_FIELDS:
            apis, source_fields, derivation = _VENDOR_FIELDS[missing[0]]
            disposition = RequirementDisposition.AVAILABLE_FROM_APPROVED_PROXY_VENDOR
            pit = "freeze every revision with published_at <= available_at <= decision_time before derivation"
            reasons = ("APPROVED_PROXY_CAPABILITY_EXISTS", "FROZEN_BITEMPORAL_SNAPSHOT_REQUIRED")
        elif "fund:shares_diluted" in missing_set:
            disposition = RequirementDisposition.SOURCE_UNAVAILABLE
            apis = source_fields = ()
            derivation = None
            pit = "an exact diluted-share history with publication and availability clocks is required"
            reasons = ("NO_APPROVED_EXACT_DILUTED_SHARE_FIELD", "SILENT_TOTAL_SHARE_PROXY_FORBIDDEN")
        elif "fund:gross_profitability" in missing_set:
            disposition = RequirementDisposition.SOURCE_UNAVAILABLE
            apis = source_fields = ()
            derivation = None
            pit = "an approved gross-profit numerator and denominator definition with bitemporal revisions is required"
            reasons = ("NO_APPROVED_EXACT_GROSS_PROFITABILITY_FIELD", "GROSS_MARGIN_SUBSTITUTION_FORBIDDEN")
        else:
            disposition = RequirementDisposition.AUXILIARY_ONLY
            apis = source_fields = ()
            derivation = None
            pit = "unknown fields remain outside the official path until an operator-approved policy is supplied"
            reasons = ("NO_CLOSED_APPROVED_FIELD_POLICY",)
        return FactorFieldRequirement(
            factor_id=record.factor_id,
            source_hash=record.source_hash,
            projection_hash=projection_hash,
            required_fields=required,
            missing_fields=missing,
            disposition=disposition,
            approved_apis=tuple(sorted(apis)),
            approved_source_fields=tuple(sorted(source_fields)),
            derivation=derivation,
            pit_requirement=pit,
            reason_codes=tuple(sorted(reasons)),
        )


def build_current_missing_field_report() -> FactorDataRequirementReport:
    """Analyze the actual 461 projection; do not maintain a parallel factor list."""

    from veriquant.qlib_factor_library_projection import (
        QlibExecutionQualification,
        build_qlib_factor_library_projection,
    )

    projection = build_qlib_factor_library_projection()
    records = tuple(
        record
        for record in projection.records
        if record.execution_qualification is QlibExecutionQualification.DATA_FIELD_MISSING
    )
    return FactorDataRequirementAnalyzer().analyze(
        records,
        projection_hash=projection.projection_hash,
    )

