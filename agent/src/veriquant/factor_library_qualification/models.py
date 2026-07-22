"""Static factor-library qualification contracts.

The catalog is a read model over legacy factor files.  It records static
metadata and compatibility hints only; it is not a FactorDAG, not official
Evidence, and not a Trial Ledger.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field, replace
from enum import Enum
from types import MappingProxyType
from typing import Any, ClassVar, Mapping


FACTOR_LIBRARY_QUALIFICATION_SCHEMA_VERSION = "veriquant.factor-library-qualification/v1"
FACTOR_LIBRARY_QUALIFICATION_RULE_VERSION = "veriquant.factor-library-qualification/v1"

_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,254}$")
_HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_FIELD_RE = re.compile(r"^(open|high|low|close|volume|vwap|amount|fund:[A-Za-z0-9_.:-]+|sector|industry)$")
_TRAVERSAL_TOKENS = ("..", "~", "$", "%2e", "%2f", "%5c")


class FactorQualificationError(ValueError):
    """Fail-closed static qualification error with a machine-readable code."""

    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.code = code
        self.details = MappingProxyType(dict(details))


class StrEnum(str, Enum):
    """Python 3.10-compatible string enum."""


class QualificationStatus(StrEnum):
    DISCOVERED = "DISCOVERED"
    METADATA_VALIDATED = "METADATA_VALIDATED"
    DAG_PORTED = "DAG_PORTED"
    PIT_TESTED = "PIT_TESTED"
    AUXILIARY_ONLY = "AUXILIARY_ONLY"
    REJECTED = "REJECTED"


class FactorDagCompatibility(StrEnum):
    DIRECT_FACTOR_DAG = "DIRECT_FACTOR_DAG"
    DIRECTLY_REPRESENTABLE = "directly_representable"
    NEEDS_DETERMINISTIC_OPERATOR = "needs_deterministic_operator"
    UNSAFE_AUXILIARY_ONLY = "UNSAFE_AUXILIARY_ONLY"
    AUXILIARY_ONLY = "auxiliary_only"
    REJECTED = "rejected"


class FactorSourceKind(StrEnum):
    PYTHON_FILE = "PYTHON_FILE"
    ALPHA_BENCH_RESULT = "ALPHA_BENCH_RESULT"
    QLIB_RESULT = "QLIB_RESULT"
    METADATA_ONLY = "METADATA_ONLY"
    CALLABLE = "CALLABLE"


class FactorExecutionTrust(StrEnum):
    STATIC_METADATA_ONLY = "STATIC_METADATA_ONLY"
    AUXILIARY_LEGACY_ONLY = "AUXILIARY_LEGACY_ONLY"
    NEVER_TRUSTED_EXECUTION = "NEVER_TRUSTED_EXECUTION"


class EvidenceAuthorityClass(StrEnum):
    NONE = "NONE"
    AUXILIARY_ONLY = "AUXILIARY_ONLY"


@dataclass(frozen=True, kw_only=True)
class FactorQualificationRecord:
    """One static catalog item.

    Required task fields are kept explicit: factor_id, zoo, theme, market,
    fields, frequency, warmup, source_hash, metadata_hash, family, and
    qualification_status.
    """

    factor_id: str
    zoo: str
    theme: tuple[str, ...]
    market: tuple[str, ...]
    fields: tuple[str, ...]
    frequency: tuple[str, ...]
    warmup: int
    source_hash: str
    metadata_hash: str
    family: str
    qualification_status: QualificationStatus
    compatibility: FactorDagCompatibility
    compatibility_reasons: tuple[str, ...] = ()
    source_kind: FactorSourceKind = FactorSourceKind.PYTHON_FILE
    source_path: str = ""
    execution_trust: FactorExecutionTrust = FactorExecutionTrust.STATIC_METADATA_ONLY
    evidence_authority: EvidenceAuthorityClass = EvidenceAuthorityClass.AUXILIARY_ONLY

    def __post_init__(self) -> None:
        require_identifier(self.factor_id, "factor_id")
        require_identifier(self.zoo, "zoo")
        require_text_tuple(self.theme, "theme")
        require_text_tuple(self.market, "market")
        require_text_tuple(self.fields, "fields")
        for field_name in self.fields:
            if _FIELD_RE.fullmatch(field_name) is None:
                raise FactorQualificationError(
                    "INVALID_FIELD",
                    "factor field is outside the static qualification allowlist",
                    field=field_name,
                )
        require_text_tuple(self.frequency, "frequency")
        if isinstance(self.warmup, bool) or not isinstance(self.warmup, int) or self.warmup < 0:
            raise FactorQualificationError("INVALID_WARMUP", "warmup must be a non-negative integer")
        require_hash(self.source_hash, "source_hash")
        require_hash(self.metadata_hash, "metadata_hash")
        require_identifier(self.family, "family")
        _coerce_enum(self, "qualification_status", QualificationStatus)
        _coerce_enum(self, "compatibility", FactorDagCompatibility)
        require_text_tuple(self.compatibility_reasons, "compatibility_reasons", nonempty=False)
        _coerce_enum(self, "source_kind", FactorSourceKind)
        _coerce_enum(self, "execution_trust", FactorExecutionTrust)
        _coerce_enum(self, "evidence_authority", EvidenceAuthorityClass)
        if self.source_path:
            require_safe_relative_path(self.source_path)
        if self.source_path.lower().replace("\\", "/").endswith("/factor.py") or self.source_path.lower() == "factor.py":
            if self.execution_trust is not FactorExecutionTrust.NEVER_TRUSTED_EXECUTION:
                raise FactorQualificationError(
                    "FACTOR_PY_NOT_TRUSTED",
                    "generated or generic factor.py cannot enter trusted execution status",
                    factor_id=self.factor_id,
                )
        if self.source_kind in {
            FactorSourceKind.CALLABLE,
            FactorSourceKind.ALPHA_BENCH_RESULT,
            FactorSourceKind.QLIB_RESULT,
        }:
            if self.execution_trust is not FactorExecutionTrust.NEVER_TRUSTED_EXECUTION:
                raise FactorQualificationError(
                    "TRUSTED_EXECUTION_REJECTED",
                    "callable and legacy result sources can never enter trusted execution status",
                    factor_id=self.factor_id,
                )
        if self.source_kind in {FactorSourceKind.CALLABLE, FactorSourceKind.ALPHA_BENCH_RESULT, FactorSourceKind.QLIB_RESULT}:
            if self.qualification_status not in {
                QualificationStatus.AUXILIARY_ONLY,
                QualificationStatus.REJECTED,
            }:
                raise FactorQualificationError(
                    "LEGACY_RESULT_NOT_AUXILIARY",
                    "legacy Python/bench/Qlib outputs must remain auxiliary or rejected",
                    factor_id=self.factor_id,
                )
        if self.qualification_status in {QualificationStatus.DAG_PORTED, QualificationStatus.PIT_TESTED}:
            if self.source_kind is not FactorSourceKind.METADATA_ONLY:
                raise FactorQualificationError(
                    "DAG_STATUS_OUT_OF_SCOPE",
                    "this package cannot mark legacy files as DAG_PORTED or PIT_TESTED",
                    factor_id=self.factor_id,
                )

    def stable_payload(self) -> dict[str, Any]:
        return {
            "factor_id": self.factor_id,
            "zoo": self.zoo,
            "theme": list(self.theme),
            "market": list(self.market),
            "fields": list(self.fields),
            "frequency": list(self.frequency),
            "warmup": self.warmup,
            "source_hash": self.source_hash,
            "metadata_hash": self.metadata_hash,
            "family": self.family,
            "qualification_status": self.qualification_status.value,
            "compatibility": self.compatibility.value,
            "compatibility_reasons": list(self.compatibility_reasons),
            "source_kind": self.source_kind.value,
            "source_path": self.source_path,
            "execution_trust": self.execution_trust.value,
            "evidence_authority": self.evidence_authority.value,
        }

    def to_dict(self) -> dict[str, Any]:
        return {"schema_version": FACTOR_LIBRARY_QUALIFICATION_SCHEMA_VERSION, **self.stable_payload()}


FactorLibraryEntry = FactorQualificationRecord
FactorQualificationStatus = QualificationStatus
FactorCompatibilityClass = FactorDagCompatibility


@dataclass(frozen=True, kw_only=True)
class FactorLibraryCatalog:
    entries: tuple[FactorQualificationRecord, ...]
    generated_at: str
    source_root: str = "agent/src/factors/zoo"
    rule_version: str = FACTOR_LIBRARY_QUALIFICATION_RULE_VERSION

    def __post_init__(self) -> None:
        if not isinstance(self.entries, tuple):
            raise FactorQualificationError("INVALID_ENTRIES", "entries must be a tuple")
        if not all(isinstance(entry, FactorQualificationRecord) for entry in self.entries):
            raise FactorQualificationError("INVALID_ENTRY", "entries must contain FactorQualificationRecord")
        ids = tuple(entry.factor_id for entry in self.entries)
        if len(set(ids)) != len(ids):
            duplicates = tuple(sorted({factor_id for factor_id in ids if ids.count(factor_id) > 1}))
            raise FactorQualificationError("DUPLICATE_FACTOR_ID", "duplicate factor_id in catalog", duplicates=duplicates)
        require_identifier(self.rule_version, "rule_version")
        require_safe_relative_path(self.source_root)
        require_nonempty_text(self.generated_at, "generated_at")
        object.__setattr__(self, "entries", tuple(sorted(self.entries, key=lambda item: item.factor_id)))

    def stable_payload(self) -> dict[str, Any]:
        """Payload intentionally excludes generated_at."""

        return {
            "schema_version": FACTOR_LIBRARY_QUALIFICATION_SCHEMA_VERSION,
            "rule_version": self.rule_version,
            "source_root": self.source_root,
            "entries": [entry.stable_payload() for entry in self.entries],
        }

    @property
    def catalog_hash(self) -> str:
        return canonical_hash(self.stable_payload())

    def with_generated_at(self, generated_at: str) -> "FactorLibraryCatalog":
        return replace(self, generated_at=generated_at)

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.stable_payload(),
            "generated_at": self.generated_at,
            "catalog_hash": self.catalog_hash,
        }


@dataclass(frozen=True, kw_only=True)
class FactorSeed:
    """A static DRAFT seed for later compilation by the trusted FactorDAG service."""

    factor_id: str
    family: str
    source_hash: str
    metadata_hash: str
    dag_template_hash: str
    dag_template: Mapping[str, Any]
    compatibility: FactorDagCompatibility = FactorDagCompatibility.DIRECT_FACTOR_DAG
    qualification_status: QualificationStatus = QualificationStatus.METADATA_VALIDATED

    def __post_init__(self) -> None:
        require_identifier(self.factor_id, "factor_id")
        require_identifier(self.family, "family")
        require_hash(self.source_hash, "source_hash")
        require_hash(self.metadata_hash, "metadata_hash")
        require_hash(self.dag_template_hash, "dag_template_hash")
        _coerce_enum(self, "compatibility", FactorDagCompatibility)
        _coerce_enum(self, "qualification_status", QualificationStatus)
        if self.compatibility is not FactorDagCompatibility.DIRECT_FACTOR_DAG:
            raise FactorQualificationError("INVALID_SEED_COMPATIBILITY", "only direct FactorDAG templates may be seeded")
        if self.qualification_status in {QualificationStatus.DAG_PORTED, QualificationStatus.PIT_TESTED}:
            raise FactorQualificationError(
                "UNAUTHORIZED_QUALIFICATION",
                "the static qualification package cannot claim DAG_PORTED or PIT_TESTED",
            )
        payload = _canonicalize(self.dag_template)
        if not isinstance(payload, dict):
            raise FactorQualificationError("INVALID_DAG_TEMPLATE", "dag_template must be a mapping")
        if canonical_hash(payload) != self.dag_template_hash:
            raise FactorQualificationError("DAG_TEMPLATE_HASH_MISMATCH", "dag_template hash does not match payload")
        object.__setattr__(self, "dag_template", MappingProxyType(payload))

    def stable_payload(self) -> dict[str, Any]:
        return {
            "factor_id": self.factor_id,
            "family": self.family,
            "source_hash": self.source_hash,
            "metadata_hash": self.metadata_hash,
            "dag_template_hash": self.dag_template_hash,
            "dag_template": _canonicalize(self.dag_template),
            "compatibility": self.compatibility.value,
            "qualification_status": self.qualification_status.value,
            "authority": "DRAFT_ONLY",
        }


@dataclass(frozen=True, kw_only=True)
class FactorSeedManifest:
    """Content-addressed seed selection; it performs no factor execution."""

    catalog_hash: str
    seeds: tuple[FactorSeed, ...]
    generated_at: str
    selection_rule_version: str = FACTOR_LIBRARY_QUALIFICATION_RULE_VERSION

    def __post_init__(self) -> None:
        require_hash(self.catalog_hash, "catalog_hash")
        require_identifier(self.selection_rule_version, "selection_rule_version")
        require_nonempty_text(self.generated_at, "generated_at")
        if not isinstance(self.seeds, tuple) or not all(isinstance(seed, FactorSeed) for seed in self.seeds):
            raise FactorQualificationError("INVALID_SEEDS", "seeds must be a tuple of FactorSeed")
        ordered = tuple(sorted(self.seeds, key=lambda seed: seed.factor_id))
        ids = tuple(seed.factor_id for seed in ordered)
        if len(set(ids)) != len(ids):
            raise FactorQualificationError("DUPLICATE_FACTOR_ID", "duplicate seed factor_id")
        if not (1 <= len(ordered) <= 24):
            raise FactorQualificationError("INVALID_SEED_COUNT", "seed manifest must contain 1-24 factors")
        object.__setattr__(self, "seeds", ordered)

    def stable_payload(self) -> dict[str, Any]:
        return {
            "schema_version": FACTOR_LIBRARY_QUALIFICATION_SCHEMA_VERSION,
            "catalog_hash": self.catalog_hash,
            "selection_rule_version": self.selection_rule_version,
            "authority": "DRAFT_LIBRARY_SEEDS_ONLY",
            "seeds": [seed.stable_payload() for seed in self.seeds],
        }

    @property
    def manifest_hash(self) -> str:
        return canonical_hash(self.stable_payload())

    def to_dict(self) -> dict[str, Any]:
        return {**self.stable_payload(), "generated_at": self.generated_at, "manifest_hash": self.manifest_hash}


@dataclass(frozen=True, kw_only=True)
class FactorQualificationReport:
    catalog: FactorLibraryCatalog
    compatibility_buckets: Mapping[str, tuple[str, ...]]
    recommended_baseline_factor_ids: tuple[str, ...]
    rejected_factor_ids: tuple[str, ...] = ()
    legacy_result_policy: str = "AUXILIARY_ONLY_NEVER_OFFICIAL"
    notes: tuple[str, ...] = (
        "static catalog only; no legacy Python factor execution",
        "selected candidates require a later FactorDAG port plus Trial Ledger and Evidence run",
    )

    def __post_init__(self) -> None:
        if not isinstance(self.catalog, FactorLibraryCatalog):
            raise FactorQualificationError("INVALID_CATALOG", "catalog must be FactorLibraryCatalog")
        buckets: dict[str, tuple[str, ...]] = {}
        known_ids = {entry.factor_id for entry in self.catalog.entries}
        for key, ids in self.compatibility_buckets.items():
            require_identifier(key, "compatibility_buckets.key")
            require_text_tuple(tuple(ids), f"compatibility_buckets.{key}", nonempty=False)
            if set(ids) - known_ids:
                raise FactorQualificationError("UNKNOWN_BUCKET_ID", "compatibility bucket references unknown factor")
            buckets[key] = tuple(sorted(ids))
        object.__setattr__(self, "compatibility_buckets", MappingProxyType(dict(sorted(buckets.items()))))
        require_text_tuple(self.recommended_baseline_factor_ids, "recommended_baseline_factor_ids", nonempty=False)
        if set(self.recommended_baseline_factor_ids) - known_ids:
            raise FactorQualificationError("UNKNOWN_RECOMMENDATION", "recommendation references unknown factor")
        if not (0 <= len(self.recommended_baseline_factor_ids) <= 24):
            raise FactorQualificationError("INVALID_RECOMMENDATION_COUNT", "recommendations must be 0-24 ids")
        require_text_tuple(self.rejected_factor_ids, "rejected_factor_ids", nonempty=False)
        if set(self.rejected_factor_ids) - known_ids:
            raise FactorQualificationError("UNKNOWN_REJECTED_ID", "rejection list references unknown factor")
        if self.legacy_result_policy != "AUXILIARY_ONLY_NEVER_OFFICIAL":
            raise FactorQualificationError("INVALID_LEGACY_POLICY", "legacy bench/Qlib results cannot be official")
        require_text_tuple(self.notes, "notes", nonempty=False)

    @property
    def report_hash(self) -> str:
        return canonical_hash(self.stable_payload())

    def stable_payload(self) -> dict[str, Any]:
        return {
            "schema_version": FACTOR_LIBRARY_QUALIFICATION_SCHEMA_VERSION,
            "catalog_hash": self.catalog.catalog_hash,
            "compatibility_buckets": {key: list(ids) for key, ids in self.compatibility_buckets.items()},
            "recommended_baseline_factor_ids": list(self.recommended_baseline_factor_ids),
            "rejected_factor_ids": list(self.rejected_factor_ids),
            "legacy_result_policy": self.legacy_result_policy,
            "notes": list(self.notes),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.stable_payload(),
            "report_hash": self.report_hash,
            "catalog": self.catalog.to_dict(),
        }


def canonical_hash(value: Any) -> str:
    rendered = json.dumps(_canonicalize(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(rendered.encode("utf-8")).hexdigest()


def require_hash(value: str, field_name: str) -> None:
    if not isinstance(value, str) or _HASH_RE.fullmatch(value) is None:
        raise FactorQualificationError("INVALID_HASH", f"{field_name} must be sha256:<64 lowercase hex>")


def require_identifier(value: str, field_name: str) -> None:
    if not isinstance(value, str) or _IDENTIFIER_RE.fullmatch(value) is None:
        raise FactorQualificationError("INVALID_IDENTIFIER", f"{field_name} is not a stable identifier", value=value)


def require_nonempty_text(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise FactorQualificationError("INVALID_TEXT", f"{field_name} must be non-empty")


def require_text_tuple(value: tuple[str, ...], field_name: str, *, nonempty: bool = True) -> None:
    if not isinstance(value, tuple) or (nonempty and not value):
        raise FactorQualificationError("INVALID_TUPLE", f"{field_name} must be a tuple")
    if len(set(value)) != len(value):
        raise FactorQualificationError("DUPLICATE_VALUE", f"{field_name} contains duplicates")
    for item in value:
        require_nonempty_text(item, field_name)


def require_safe_relative_path(path: str) -> None:
    if not isinstance(path, str) or not path:
        raise FactorQualificationError("INVALID_PATH", "path must be non-empty text")
    normalized = path.replace("\\", "/").lower()
    if normalized.startswith("/") or ":" in normalized:
        raise FactorQualificationError("PATH_TRAVERSAL", "absolute paths are not allowed in catalog entries", path=path)
    if any(token in normalized.split("/") for token in ("..", "~")):
        raise FactorQualificationError("PATH_TRAVERSAL", "path traversal is prohibited", path=path)
    if any(token in normalized for token in _TRAVERSAL_TOKENS):
        raise FactorQualificationError("PATH_TRAVERSAL", "path traversal is prohibited", path=path)


def _coerce_enum(obj: Any, field_name: str, enum_type: type[Enum]) -> None:
    value = getattr(obj, field_name)
    if not isinstance(value, enum_type):
        try:
            object.__setattr__(obj, field_name, enum_type(value))
        except (TypeError, ValueError) as exc:
            raise FactorQualificationError("INVALID_ENUM", f"{field_name} has an invalid enum value") from exc


def _canonicalize(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Mapping):
        return {str(key): _canonicalize(value[key]) for key in sorted(value)}
    if isinstance(value, (tuple, list)):
        return [_canonicalize(item) for item in value]
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        if value != value or value in {float("inf"), float("-inf")}:
            raise FactorQualificationError("NON_FINITE_NUMBER", "non-finite float is not canonical")
        return value
    raise FactorQualificationError("INVALID_CANONICAL_VALUE", f"unsupported canonical type {type(value).__name__}")


__all__ = [
    "FACTOR_LIBRARY_QUALIFICATION_RULE_VERSION",
    "FACTOR_LIBRARY_QUALIFICATION_SCHEMA_VERSION",
    "EvidenceAuthorityClass",
    "FactorCompatibilityClass",
    "FactorDagCompatibility",
    "FactorExecutionTrust",
    "FactorLibraryCatalog",
    "FactorLibraryEntry",
    "FactorQualificationRecord",
    "FactorQualificationError",
    "FactorQualificationReport",
    "FactorQualificationStatus",
    "FactorSeed",
    "FactorSeedManifest",
    "FactorSourceKind",
    "QualificationStatus",
    "canonical_hash",
]
