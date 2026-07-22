"""Thin Trial Ledger submission boundary for specialist DRAFT candidates."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import ClassVar, Mapping

from ..contracts import CanonicalContract, FailureCode
from ..contracts.base import fail, require_hash, require_identifier
from ..evidence import ExperimentIdentity, TrialLedger, TrialOutcome, TrialStatus
from .models import SCHEMA_VERSION, PreflightStatus, SpecialistAgentOutcome, SpecialistStage


class SpecialistSubmissionStatus(str, Enum):
    READY_FOR_EVIDENCE = "READY_FOR_EVIDENCE"
    COMPILE_REJECTED = "COMPILE_REJECTED"
    REPLAYED = "REPLAYED"


@dataclass(frozen=True)
class SpecialistTrialSubmission(CanonicalContract):
    candidate_id: str
    family_id: str
    trial_id: str
    identity_hash: str
    status: SpecialistSubmissionStatus
    failure_code: str | None = None
    SCHEMA_VERSION: ClassVar[str] = SCHEMA_VERSION

    def __post_init__(self) -> None:
        for name in ("candidate_id", "family_id", "trial_id"):
            require_identifier(getattr(self, name), name)
        require_hash(self.identity_hash, "identity_hash")
        if not isinstance(self.status, SpecialistSubmissionStatus):
            fail(FailureCode.INVALID_VALUE, "submission status is invalid")
        if self.status is SpecialistSubmissionStatus.COMPILE_REJECTED and not self.failure_code:
            fail(FailureCode.MISSING_REQUIRED_FIELD, "compile rejection requires failure_code")


class SpecialistTrialSubmissionService:
    """Reserve/start every candidate; Evidence Service still owns identities and metrics."""

    def __init__(self, ledger: TrialLedger) -> None:
        self._ledger = ledger

    def submit(
        self,
        *,
        outcome: SpecialistAgentOutcome,
        identities: Mapping[str, ExperimentIdentity],
        family_trial_budget: int,
        research_depth: str = "STANDARD_RESEARCH",
    ) -> tuple[SpecialistTrialSubmission, ...]:
        if outcome.status is not SpecialistStage.COMPLETED:
            fail(FailureCode.INVALID_STATE, "only a completed DRAFT outcome can submit trials")
        if research_depth == "QUICK_EXPLORATION":
            fail(FailureCode.UNAUTHORIZED_CAPABILITY, "quick exploration cannot submit official Evidence trials")
        candidate_ids = {item.candidate_id for item in outcome.candidates}
        if set(identities) != candidate_ids:
            fail(FailureCode.INVALID_FIELD_OWNER, "Evidence identities must cover every specialist candidate exactly once")
        preflight_by_id = {item.candidate_id: item for item in outcome.preflight_results}
        if not candidate_ids <= set(preflight_by_id):
            fail(FailureCode.INCOMPLETE_RESULT, "every submitted candidate requires a preflight result")
        if isinstance(family_trial_budget, bool) or not isinstance(family_trial_budget, int) or family_trial_budget < 1:
            fail(FailureCode.INVALID_VALUE, "family_trial_budget must be positive")

        results: list[SpecialistTrialSubmission] = []
        for candidate in sorted(outcome.candidates, key=lambda item: item.candidate_id):
            preflight = preflight_by_id[candidate.candidate_id]
            identity = identities[candidate.candidate_id]
            expected_factor_hash = preflight.factor_dag_hash or candidate.artifact_hash
            if identity.factor_hash != expected_factor_hash:
                fail(FailureCode.INVALID_HASH, "Evidence identity does not bind the candidate FactorDAG/draft")
            family_id = f"{outcome.specialist_id}:{candidate.family_id}"
            self._ledger.create_family(family_id, family_trial_budget)
            identity_hash = self._ledger.register_identity(identity)
            trial_id = "specialist-" + identity_hash.split(":", 1)[1][:32]
            replay = self._ledger.replay()
            prior = replay.trials.get(trial_id)
            if prior is not None and prior.status is TrialStatus.TERMINAL:
                results.append(
                    SpecialistTrialSubmission(
                        candidate_id=candidate.candidate_id,
                        family_id=family_id,
                        trial_id=trial_id,
                        identity_hash=identity_hash,
                        status=SpecialistSubmissionStatus.REPLAYED,
                        failure_code=None if prior.failure_code is None else prior.failure_code.value,
                    )
                )
                continue
            reserve = self._ledger.reserve_attempt(
                family_id=family_id,
                trial_id=trial_id,
                identity_hash=identity_hash,
                proposer_id=outcome.specialist_id,
            )
            self._ledger.start_attempt(reserve.trial_id)
            if preflight.status is PreflightStatus.ACCEPTED:
                status = SpecialistSubmissionStatus.READY_FOR_EVIDENCE
                failure_code = None
            else:
                typed = _typed_preflight_failure(preflight.failure_code)
                self._ledger.commit_outcome(
                    reserve.trial_id,
                    outcome=TrialOutcome.PARSE_REJECTION,
                    failure_code=typed,
                )
                status = SpecialistSubmissionStatus.COMPILE_REJECTED
                failure_code = typed.value
            results.append(
                SpecialistTrialSubmission(
                    candidate_id=candidate.candidate_id,
                    family_id=family_id,
                    trial_id=trial_id,
                    identity_hash=identity_hash,
                    status=status,
                    failure_code=failure_code,
                )
            )
        return tuple(results)


def _typed_preflight_failure(raw: str | None) -> FailureCode:
    if raw is not None:
        try:
            return FailureCode(raw)
        except ValueError:
            return FailureCode.INVALID_FACTOR_DAG
    return FailureCode.INVALID_FACTOR_DAG
