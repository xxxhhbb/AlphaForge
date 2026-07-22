"""Content-bound inputs for replaying real frozen daily vintages."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import Mapping

from veriquant.contracts import CanonicalContract, ContractValidationError, FailureCode
from veriquant.contracts.base import require_aware_datetime, require_hash, require_identifier
from veriquant.shadow_portfolio.models import DataVintage


def _fail(code: FailureCode, message: str, **details: object) -> None:
    raise ContractValidationError(code, message, details=details)


@dataclass(frozen=True, kw_only=True)
class FrozenDailyVintage(CanonicalContract):
    """One verified source snapshot and its PIT-visible replay projection.

    The source hashes remain outside financial truth, but are bound into the
    adapted ``DataVintage`` identity before ShadowReplay sees it.
    """

    snapshot_id: str
    session_date: str
    snapshot_hash: str
    manifest_hash: str
    calendar_hash: str
    universe_hash: str
    revision_available_time: datetime
    captured_at: datetime
    vintage: DataVintage

    def __post_init__(self) -> None:
        require_identifier(self.snapshot_id, "snapshot_id")
        for field_name in ("snapshot_hash", "manifest_hash", "calendar_hash", "universe_hash"):
            require_hash(getattr(self, field_name), field_name)
        require_aware_datetime(self.revision_available_time, "revision_available_time")
        require_aware_datetime(self.captured_at, "captured_at")
        if not isinstance(self.vintage, DataVintage):
            _fail(FailureCode.INVALID_VALUE, "vintage must be a DataVintage")
        expected_session = self.vintage.decision_time.date().isoformat()
        if self.session_date != expected_session:
            _fail(
                FailureCode.INVALID_VALUE,
                "session_date must match vintage decision date",
                expected=expected_session,
                actual=self.session_date,
            )
        if self.revision_available_time > self.vintage.decision_time:
            _fail(FailureCode.INVALID_TIME_ORDER, "revision is not available at the daily decision time")


@dataclass(frozen=True, kw_only=True)
class FrozenVintageBinding(CanonicalContract):
    """One replay-day request into a verified frozen MarketVintageProvider."""

    snapshot_id: str
    manifest_path: str
    decision_time: datetime
    revision_id: str

    def __post_init__(self) -> None:
        require_identifier(self.snapshot_id, "snapshot_id")
        require_identifier(self.revision_id, "revision_id")
        if not isinstance(self.manifest_path, str) or not self.manifest_path.strip():
            _fail(FailureCode.MISSING_REQUIRED_FIELD, "manifest_path is required")
        require_aware_datetime(self.decision_time, "decision_time")


@dataclass(frozen=True, kw_only=True)
class FrozenVintageMetadata(CanonicalContract):
    """PIT-visible risk metadata required by the shadow portfolio authority."""

    corporate_action_available_time: datetime
    beta_by_symbol: Mapping[str, float]
    industry_by_symbol: Mapping[str, str]
    adv_by_symbol: Mapping[str, float]

    def __post_init__(self) -> None:
        require_aware_datetime(self.corporate_action_available_time, "corporate_action_available_time")
        object.__setattr__(self, "beta_by_symbol", MappingProxyType(dict(self.beta_by_symbol)))
        object.__setattr__(self, "industry_by_symbol", MappingProxyType(dict(self.industry_by_symbol)))
        object.__setattr__(self, "adv_by_symbol", MappingProxyType(dict(self.adv_by_symbol)))
