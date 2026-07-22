"""Read-only reconciliation for stale V1 acceptance artifacts.

This package intentionally does not update the global VeriQuant build state.
It produces a mechanical read model that tells an Integration Agent whether
old final/UI acceptance failures have been superseded by later C8 evidence.
"""

from .reconciler import (
    AcceptanceConflict,
    AcceptanceEvidenceSnapshot,
    AcceptanceReconcileFailureCode,
    AcceptanceReconcileVerdict,
    AcceptanceReconciler,
)

__all__ = [
    "AcceptanceConflict",
    "AcceptanceEvidenceSnapshot",
    "AcceptanceReconcileFailureCode",
    "AcceptanceReconcileVerdict",
    "AcceptanceReconciler",
]
