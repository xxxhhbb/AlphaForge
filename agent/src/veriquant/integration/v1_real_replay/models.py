"""Typed gate results for the V1 real 20-session shadow replay adapter."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import ClassVar

from veriquant.contracts import CanonicalContract, FailureCode
from veriquant.contracts.base import fail, require_aware_datetime, require_hash, require_identifier, require_text


SCHEMA_VERSION = "veriquant.v1-real-shadow-replay/v1"


class RealReplayGateStatus(str, Enum):
    READY = "READY"
    BLOCKED = "BLOCKED"
    COMPLETED = "COMPLETED"


@dataclass(frozen=True, kw_only=True)
class PromotionChallengerReceipt(CanonicalContract):
    """Minimal, sealed-free receipt from an independent Promotion authority."""

    SCHEMA_VERSION: ClassVar[str] = SCHEMA_VERSION

    candidate_id: str
    version: str
    state: str
    source_authority: str
    official_run_hash: str
    official_factor_identity_hash: str
    policy_version: str
    policy_hash: str
    promotion_decision_hash: str
    evidence_hash: str
    factor_hash: str
    available_time: datetime

    def __post_init__(self) -> None:
        for name in ("candidate_id", "version", "policy_version"):
            require_identifier(getattr(self, name), name)
        if self.state != "CHALLENGER":
            fail(FailureCode.INVALID_ALPHA_SOURCE, "promotion receipt is not CHALLENGER")
        if self.source_authority != "INDEPENDENT_PROMOTION_SERVICE":
            fail(FailureCode.UNAUTHORIZED_CAPABILITY, "challenger was not issued by independent Promotion")
        for name in (
            "official_run_hash",
            "official_factor_identity_hash",
            "policy_hash",
            "promotion_decision_hash",
            "evidence_hash",
            "factor_hash",
        ):
            require_hash(getattr(self, name), name)
        require_aware_datetime(self.available_time, "available_time")


@dataclass(frozen=True, kw_only=True)
class RealReplayGateResult(CanonicalContract):
    """Machine result; BLOCKED is a valid, non-success terminal gate outcome."""

    SCHEMA_VERSION: ClassVar[str] = SCHEMA_VERSION

    status: RealReplayGateStatus
    official_run_hash: str
    official_trial_ledger_tip_hash: str
    promotion_receipt_hash: str | None
    replay_manifest_hash: str | None
    journal_tip_hash: str | None
    sessions_committed: int
    fixture_only: bool
    real_challenger_used: bool
    failure_code: FailureCode | None = None
    failure_message: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.status, RealReplayGateStatus):
            object.__setattr__(self, "status", RealReplayGateStatus(self.status))
        require_hash(self.official_run_hash, "official_run_hash")
        require_hash(self.official_trial_ledger_tip_hash, "official_trial_ledger_tip_hash")
        for name in ("promotion_receipt_hash", "replay_manifest_hash", "journal_tip_hash"):
            value = getattr(self, name)
            if value is not None:
                require_hash(value, name)
        if isinstance(self.sessions_committed, bool) or not isinstance(self.sessions_committed, int):
            fail(FailureCode.INVALID_VALUE, "sessions_committed must be an integer")
        if not 0 <= self.sessions_committed <= 20:
            fail(FailureCode.INVALID_VALUE, "sessions_committed must be in [0, 20]")
        if type(self.fixture_only) is not bool or type(self.real_challenger_used) is not bool:
            fail(FailureCode.INVALID_VALUE, "fixture and challenger flags must be boolean")
        if self.fixture_only and self.real_challenger_used:
            fail(FailureCode.INVALID_STATE, "fixture replay cannot claim a real challenger")
        if self.status is RealReplayGateStatus.BLOCKED:
            if self.failure_code is None or not self.failure_message:
                fail(FailureCode.INCOMPLETE_RESULT, "blocked result requires typed failure")
            require_text(self.failure_message, "failure_message")
            if self.sessions_committed != 0 or self.replay_manifest_hash is not None:
                fail(FailureCode.INVALID_STATE, "blocked gate cannot publish a replay")
        elif self.failure_code is not None or self.failure_message is not None:
            fail(FailureCode.INVALID_STATE, "non-blocked result cannot contain failure")
        if self.status is RealReplayGateStatus.COMPLETED:
            if self.sessions_committed != 20 or self.replay_manifest_hash is None or self.journal_tip_hash is None:
                fail(FailureCode.INCOMPLETE_RESULT, "completed replay requires 20 sessions and durable hashes")

