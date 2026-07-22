"""Static compatibility qualification for the legacy factor zoo.

The builder consumes the existing pure-static factor-library catalog and adds
FactorDAG compatibility classes.  It never imports, calls, or executes legacy
factor modules.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable

from ..factor_library import FactorLibraryCatalogBuilder as StaticCatalogBuilder
from ..factor_library import FactorLibraryRecord as StaticFactorRecord
from .models import (
    EvidenceAuthorityClass,
    FactorCompatibilityClass,
    FactorExecutionTrust,
    FactorLibraryCatalog,
    FactorLibraryEntry,
    FactorQualificationError,
    FactorQualificationReport,
    FactorQualificationStatus,
    FactorSourceKind,
)


DIRECT_Q_LIB_RE = re.compile(r"^(ma|vma|roc|std|vstd)(5|10|20|30|60)$")
NEEDS_OPERATOR_PREFIXES = (
    "corr",
    "cord",
    "beta",
    "rank",
    "max",
    "min",
    "imax",
    "imin",
    "imxd",
    "rsv",
    "rsqr",
    "res",
    "sum",
    "cnt",
    "qt",
    "wvma",
)


class FactorLibraryQualificationBuilder:
    """Build a deterministic static qualification report."""

    def __init__(self, zoo_root: str | Path | None = None) -> None:
        self.zoo_root = zoo_root

    def build(self) -> FactorQualificationReport:
        static_catalog = StaticCatalogBuilder(self.zoo_root).build()
        entries = tuple(_entry_from_record(record) for record in static_catalog.records)
        catalog = FactorLibraryCatalog(
            entries=entries,
            generated_at="not-in-catalog-hash",
            source_root="agent/src/factors/zoo" if self.zoo_root is None else "external_factor_zoo",
        )
        buckets = _compatibility_buckets(entries)
        recommended = _recommended_baselines(entries, require_minimum=self.zoo_root is None)
        rejected = tuple(entry.factor_id for entry in entries if entry.qualification_status is FactorQualificationStatus.REJECTED)
        return FactorQualificationReport(
            catalog=catalog,
            compatibility_buckets=buckets,
            recommended_baseline_factor_ids=recommended,
            rejected_factor_ids=rejected,
        )


def build_factor_library_qualification_report(zoo_root: str | Path | None = None) -> FactorQualificationReport:
    return FactorLibraryQualificationBuilder(zoo_root).build()


def _entry_from_record(record: StaticFactorRecord) -> FactorLibraryEntry:
    compatibility, reasons = _compatibility_for(record)
    status = FactorQualificationStatus.METADATA_VALIDATED
    if compatibility is FactorCompatibilityClass.AUXILIARY_ONLY:
        status = FactorQualificationStatus.AUXILIARY_ONLY
    elif compatibility is FactorCompatibilityClass.REJECTED:
        status = FactorQualificationStatus.REJECTED
    return FactorLibraryEntry(
        factor_id=record.factor_id,
        zoo=record.zoo,
        theme=record.themes,
        market=record.markets,
        fields=tuple(dict.fromkeys((*record.fields, *record.extras_required))),
        frequency=record.frequency,
        warmup=record.warmup,
        source_hash=record.source_hash,
        metadata_hash=record.metadata_hash,
        family=record.family,
        qualification_status=status,
        compatibility=compatibility,
        compatibility_reasons=reasons,
        source_kind=FactorSourceKind.PYTHON_FILE,
        source_path=record.source_relpath,
        execution_trust=FactorExecutionTrust.STATIC_METADATA_ONLY,
        evidence_authority=EvidenceAuthorityClass.AUXILIARY_ONLY,
    )


def _compatibility_for(record: StaticFactorRecord) -> tuple[FactorCompatibilityClass, tuple[str, ...]]:
    factor_id = record.factor_id
    stem = Path(record.source_relpath).stem.lower()
    if record.zoo == "fundamental":
        return (
            FactorCompatibilityClass.DIRECTLY_REPRESENTABLE,
            ("fundamental field plus cross-sectional zscore/rank can be expressed by current FactorDAG",),
        )
    if record.zoo == "qlib158" and DIRECT_Q_LIB_RE.fullmatch(stem):
        return (
            FactorCompatibilityClass.DIRECTLY_REPRESENTABLE,
            ("qlib baseline maps to field, rolling_mean/rolling_std or pct_change with current FactorDAG operators",),
        )
    if record.zoo in {"alpha101", "gtja191"}:
        return (
            FactorCompatibilityClass.NEEDS_DETERMINISTIC_OPERATOR,
            ("formulaic alpha family often needs conditional, time-series rank, argmax, decay, signed power, or correlation operators",),
        )
    if record.zoo == "qlib158" and stem.startswith(NEEDS_OPERATOR_PREFIXES):
        return (
            FactorCompatibilityClass.NEEDS_DETERMINISTIC_OPERATOR,
            ("qlib feature requires a deterministic operator outside the current FactorDAG whitelist",),
        )
    if record.zoo == "academic":
        return (
            FactorCompatibilityClass.NEEDS_DETERMINISTIC_OPERATOR,
            ("academic factor requires explicit portfolio construction or regression semantics before FactorDAG port",),
        )
    return (
        FactorCompatibilityClass.AUXILIARY_ONLY,
        ("source remains static metadata only until a later audited FactorDAG port",),
    )


def _compatibility_buckets(entries: Iterable[FactorLibraryEntry]) -> dict[str, tuple[str, ...]]:
    buckets: dict[str, list[str]] = {item.value: [] for item in FactorCompatibilityClass}
    for entry in entries:
        buckets[entry.compatibility.value].append(entry.factor_id)
    return {key: tuple(sorted(value)) for key, value in sorted(buckets.items())}


def _recommended_baselines(entries: tuple[FactorLibraryEntry, ...], *, require_minimum: bool) -> tuple[str, ...]:
    direct = [entry for entry in entries if entry.compatibility is FactorCompatibilityClass.DIRECTLY_REPRESENTABLE]
    preferred: list[str] = []
    for zoo in ("fundamental", "qlib158"):
        for entry in direct:
            if entry.zoo == zoo and entry.factor_id not in preferred:
                preferred.append(entry.factor_id)
            if len(preferred) >= 24:
                break
        if len(preferred) >= 24:
            break
    if require_minimum and len(preferred) < 12:
        raise FactorQualificationError(
            "INSUFFICIENT_BASELINES",
            "expected at least 12 conservative directly representable baseline candidates",
            count=len(preferred),
        )
    return tuple(preferred[:24])


__all__ = [
    "FactorLibraryQualificationBuilder",
    "build_factor_library_qualification_report",
]
