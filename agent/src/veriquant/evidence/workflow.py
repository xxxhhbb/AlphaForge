"""Bounded attempt orchestration that charges every started failure."""

from __future__ import annotations

from dataclasses import dataclass

from ..contracts import FailureCode
from .ledger import LedgerEvent, TrialLedger, TrialOutcome
from .models import ExperimentIdentity


@dataclass(frozen=True, kw_only=True)
class StartedAttempt:
    trial_id: str
    identity_hash: str
    reserve_event: LedgerEvent
    start_event: LedgerEvent


class AttemptController:
    def __init__(self, ledger: TrialLedger) -> None:
        self.ledger = ledger

    def start(
        self,
        *,
        family_id: str,
        trial_id: str,
        identity: ExperimentIdentity,
        proposer_id: str,
        units: int = 1,
    ) -> StartedAttempt:
        identity_hash = self.ledger.register_identity(identity)
        reserve = self.ledger.reserve_attempt(
            family_id=family_id,
            trial_id=trial_id,
            identity_hash=identity_hash,
            proposer_id=proposer_id,
            units=units,
        )
        start = self.ledger.start_attempt(reserve.trial_id)
        return StartedAttempt(
            trial_id=reserve.trial_id,
            identity_hash=identity_hash,
            reserve_event=reserve,
            start_event=start,
        )

    def commit_failure(
        self,
        attempt: StartedAttempt,
        *,
        outcome: TrialOutcome,
        failure_code: FailureCode,
    ) -> LedgerEvent:
        if outcome is TrialOutcome.SUCCESS:
            raise ValueError("commit_failure cannot record SUCCESS")
        return self.ledger.commit_outcome(
            attempt.trial_id,
            outcome=outcome,
            failure_code=failure_code,
        )

    def commit_success(self, attempt: StartedAttempt) -> LedgerEvent:
        return self.ledger.commit_outcome(
            attempt.trial_id,
            outcome=TrialOutcome.SUCCESS,
        )

