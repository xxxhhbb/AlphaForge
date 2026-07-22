"""Source-aware bitemporal normalization and inert research feature views."""

from .features import as_of_join, build_feature_view, build_historical_feature_rows
from .models import (
    BitemporalError,
    EvidenceEligibility,
    FactorFeatureView,
    FeatureRow,
    NormalizationBatch,
    NormalizationIssue,
    NormalizedRecord,
    SourceKind,
    UnknownTimePolicy,
    canonical_hash,
    canonical_record_hash,
)
from .normalizers import SHANGHAI, normalize_row, normalize_rows
from .store import BitemporalStore

__all__ = [
    "BitemporalError",
    "BitemporalStore",
    "EvidenceEligibility",
    "FactorFeatureView",
    "FeatureRow",
    "NormalizationBatch",
    "NormalizationIssue",
    "NormalizedRecord",
    "SHANGHAI",
    "SourceKind",
    "UnknownTimePolicy",
    "as_of_join",
    "build_feature_view",
    "build_historical_feature_rows",
    "canonical_hash",
    "canonical_record_hash",
    "normalize_row",
    "normalize_rows",
]
