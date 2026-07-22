"""Versioned safe semantics for Qlib operators with known differences."""

from .models import (
    ADAPTER_CATALOG_VERSION,
    ADAPTER_SCHEMA_VERSION,
    AdapterFailureCode,
    SemanticAdapterError,
    SemanticAdapterPolicy,
    SemanticAdapterResult,
    canonical_hash,
)
from .runtime import (
    ADAPTER_POLICY_HASH,
    BUILTIN_ADAPTER_POLICIES,
    apply_builtin_adapter,
    safe_idx_max,
    safe_idx_min,
    safe_if,
    safe_wma,
)

__all__ = [
    "ADAPTER_CATALOG_VERSION",
    "ADAPTER_POLICY_HASH",
    "ADAPTER_SCHEMA_VERSION",
    "AdapterFailureCode",
    "BUILTIN_ADAPTER_POLICIES",
    "SemanticAdapterError",
    "SemanticAdapterPolicy",
    "SemanticAdapterResult",
    "apply_builtin_adapter",
    "canonical_hash",
    "safe_idx_max",
    "safe_idx_min",
    "safe_if",
    "safe_wma",
]
