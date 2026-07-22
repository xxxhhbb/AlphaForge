"""Optional Tencent operational-rehearsal projection.

Tencent artifacts are deliberately excluded from FactorDAG/PIT/Evidence,
promotion, CHALLENGER, and the mandatory real-Tushare acceptance gate.
"""

from __future__ import annotations

from typing import Any

from ..contracts import ContractValidationError, FailureCode
from ..tencent_rehearsal.api import (
    AdjustmentSafetyVerdict,
    TencentDataAvailability,
    TencentMarketVintage,
    TencentSnapshotManifest,
)


TENCENT_SOURCE = "TENCENT_OPERATIONAL_REHEARSAL"
TENCENT_AUTHORITY = "AUXILIARY_RESEARCH_EVIDENCE"


def tencent_workbench_projection(
    artifact: TencentDataAvailability | TencentSnapshotManifest | TencentMarketVintage,
) -> dict[str, Any]:
    """Return an explicitly auxiliary, sealed-free Workbench summary."""

    if isinstance(artifact, TencentDataAvailability):
        return {
            "source": artifact.source,
            "authority_class": TENCENT_AUTHORITY,
            "status": artifact.status.value,
            "network_enabled": artifact.network_enabled,
            "transport_available": artifact.transport_available,
            "promotion_eligible": False,
            "official_v1_tushare_smoke": False,
            "warning": "Operational rehearsal only; no official Evidence or promotion authority",
        }
    if isinstance(artifact, TencentSnapshotManifest):
        _require_auxiliary_snapshot(artifact)
        return {
            "source": artifact.source,
            "authority_class": artifact.authority_class,
            "universe_policy": artifact.universe_policy,
            "adjustment_verdict": artifact.adjustment_verdict.value,
            "pit_eligible": artifact.pit_eligible,
            "promotion_eligible": False,
            "official_v1_tushare_smoke": False,
            "manifest_hash": artifact.artifact_hash,
            "calendar_hash": artifact.calendar_hash,
            "row_count": artifact.row_count,
            "warning": "Fixed operational sample; not a historical universe or V1 Tushare smoke",
        }
    if isinstance(artifact, TencentMarketVintage):
        if artifact.source != TENCENT_SOURCE or artifact.authority_class != TENCENT_AUTHORITY:
            raise ContractValidationError(FailureCode.AUXILIARY_MASQUERADE, "Tencent vintage authority changed")
        return {
            "source": artifact.source,
            "authority_class": artifact.authority_class,
            "adjustment_verdict": artifact.adjustment_verdict.value,
            "pit_eligible": artifact.pit_eligible,
            "promotion_eligible": False,
            "official_v1_tushare_smoke": False,
            "vintage_hash": artifact.vintage_hash,
            "visible_row_count": len(artifact.rows),
            "warning": "Auxiliary as-of rehearsal only",
        }
    raise ContractValidationError(FailureCode.INVALID_VALUE, "unsupported Tencent rehearsal artifact")


def reject_tencent_official_use(
    artifact: TencentSnapshotManifest | TencentMarketVintage,
    *,
    requested_authority: str,
) -> None:
    """Fail closed before any official Evidence, promotion, or Gate-2 adapter."""

    if isinstance(artifact, TencentSnapshotManifest):
        _require_auxiliary_snapshot(artifact)
    if requested_authority in {
        "OFFICIAL_EVIDENCE",
        "PIT_VALIDATED",
        "PROMOTION",
        "CHALLENGER",
        "V1_REAL_TUSHARE_SMOKE",
    }:
        raise ContractValidationError(
            FailureCode.UNAUTHORIZED_CAPABILITY,
            "Tencent operational rehearsal cannot enter the trusted research or promotion path",
        )


def _require_auxiliary_snapshot(artifact: TencentSnapshotManifest) -> None:
    if (
        artifact.source != TENCENT_SOURCE
        or artifact.authority_class != TENCENT_AUTHORITY
        or artifact.promotion_eligible
        or artifact.official_v1_tushare_smoke
        or artifact.historical_universe
    ):
        raise ContractValidationError(FailureCode.AUXILIARY_MASQUERADE, "Tencent snapshot authority changed")
    if artifact.adjustment_verdict in {AdjustmentSafetyVerdict.QFQ_ONLY, AdjustmentSafetyVerdict.UNKNOWN} and artifact.pit_eligible:
        raise ContractValidationError(FailureCode.INVALID_TIME_ORDER, "adjusted Tencent snapshot cannot be PIT eligible")

