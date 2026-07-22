"""Canonical results for the real approved-proxy visible-evidence adapter."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import ClassVar

from veriquant.contracts import CanonicalContract, EvidenceClass, FailureCode
from veriquant.contracts.base import (
    fail,
    require_aware_datetime,
    require_hash,
    require_identifier,
    require_text,
)
from veriquant.evidence import OfficialEvidence


SCHEMA_VERSION = "veriquant.real-official-evidence/v1"


class FactorAttemptStatus(str, Enum):
    OFFICIAL_VISIBLE_EVIDENCE = "OFFICIAL_VISIBLE_EVIDENCE"
    FAILED_CONSUMED = "FAILED_CONSUMED"


@dataclass(frozen=True, kw_only=True)
class RealOfficialEvidenceFactorResult(CanonicalContract):
    SCHEMA_VERSION: ClassVar[str] = SCHEMA_VERSION

    factor_id: str
    status: FactorAttemptStatus
    trial_id: str
    identity_hash: str
    candidate_payload_hash: str
    factor_dag_hash: str | None
    train_factor_result_hash: str | None
    validation_factor_result_hash: str | None
    train_evidence: OfficialEvidence | None
    validation_evidence: OfficialEvidence | None
    ledger_terminal_event_hash: str
    failure_code: FailureCode | None
    failure_message: str | None

    def __post_init__(self) -> None:
        require_identifier(self.factor_id, "factor_id")
        require_identifier(self.trial_id, "trial_id")
        require_hash(self.identity_hash, "identity_hash")
        require_hash(self.candidate_payload_hash, "candidate_payload_hash")
        require_hash(self.ledger_terminal_event_hash, "ledger_terminal_event_hash")
        if self.factor_dag_hash is not None:
            require_hash(self.factor_dag_hash, "factor_dag_hash")
        for name in ("train_factor_result_hash", "validation_factor_result_hash"):
            value = getattr(self, name)
            if value is not None:
                require_hash(value, name)
        if self.status is FactorAttemptStatus.OFFICIAL_VISIBLE_EVIDENCE:
            if self.failure_code is not None or self.failure_message is not None:
                fail(FailureCode.INVALID_STATE, "successful factor result cannot contain a failure")
            if not all(
                (
                    self.factor_dag_hash,
                    self.train_factor_result_hash,
                    self.validation_factor_result_hash,
                    self.train_evidence,
                    self.validation_evidence,
                )
            ):
                fail(FailureCode.INCOMPLETE_RESULT, "successful factor result is incomplete")
            assert self.train_evidence is not None and self.validation_evidence is not None
            if self.train_evidence.partition != "TRAIN" or self.validation_evidence.partition != "VALIDATION":
                fail(FailureCode.INVALID_VALUE, "visible evidence partitions are invalid")
            if (
                self.train_evidence.evidence_class is not EvidenceClass.OFFICIAL_EVIDENCE
                or self.validation_evidence.evidence_class is not EvidenceClass.OFFICIAL_EVIDENCE
            ):
                fail(FailureCode.AUXILIARY_MASQUERADE, "evidence class is not official")
            if self.train_evidence.identity_hash != self.identity_hash:
                fail(FailureCode.INVALID_HASH, "train evidence identity changed")
            if self.validation_evidence.identity_hash != self.identity_hash:
                fail(FailureCode.INVALID_HASH, "validation evidence identity changed")
        else:
            if self.failure_code is None or self.failure_message is None:
                fail(FailureCode.INCOMPLETE_RESULT, "failed attempt requires a typed failure")
            require_text(self.failure_message, "failure_message")
            if self.train_evidence is not None or self.validation_evidence is not None:
                fail(FailureCode.INVALID_STATE, "failed attempt cannot publish official evidence")


@dataclass(frozen=True, kw_only=True)
class RealOfficialEvidenceRun(CanonicalContract):
    SCHEMA_VERSION: ClassVar[str] = SCHEMA_VERSION

    run_id: str
    status: str
    source_class: str
    provider_class: str
    official_tushare_source: bool
    decision_time: datetime
    qualification_hash: str
    auxiliary_result_hash: str
    snapshot_hash: str
    calendar_hash: str
    universe_hash: str
    research_bundle_hash: str
    bridge_manifest_hash: str
    trial_ledger_tip_hash: str
    family_budget: int
    budget_consumed: int
    factors: tuple[RealOfficialEvidenceFactorResult, ...]
    official_evidence_run: bool
    sealed_evidence_run: bool
    promotion_run: bool
    challenger_created: bool

    def __post_init__(self) -> None:
        require_identifier(self.run_id, "run_id")
        if self.status not in {"COMPLETED_OFFICIAL_VISIBLE", "PARTIAL_OFFICIAL_VISIBLE", "FAILED_CONSUMED"}:
            fail(FailureCode.INVALID_STATE, "unknown real official evidence run status")
        if self.source_class != "TUSHARE_COMPATIBLE_PROXY":
            fail(FailureCode.INVALID_VALUE, "source must remain TUSHARE_COMPATIBLE_PROXY")
        if self.provider_class != "APPROVED_PROXY_DATA_VENDOR":
            fail(FailureCode.INVALID_VALUE, "provider class must remain APPROVED_PROXY_DATA_VENDOR")
        if self.official_tushare_source:
            fail(FailureCode.AUXILIARY_MASQUERADE, "approved proxy cannot claim OFFICIAL_TUSHARE")
        require_aware_datetime(self.decision_time, "decision_time")
        for name in (
            "qualification_hash",
            "auxiliary_result_hash",
            "snapshot_hash",
            "calendar_hash",
            "universe_hash",
            "research_bundle_hash",
            "bridge_manifest_hash",
            "trial_ledger_tip_hash",
        ):
            require_hash(getattr(self, name), name)
        if isinstance(self.family_budget, bool) or self.family_budget < 1:
            fail(FailureCode.INVALID_VALUE, "family_budget must be positive")
        if isinstance(self.budget_consumed, bool) or not 0 <= self.budget_consumed <= self.family_budget:
            fail(FailureCode.INVALID_VALUE, "budget consumption is invalid")
        if not self.factors or len(self.factors) != self.budget_consumed:
            fail(FailureCode.INCOMPLETE_RESULT, "every factor attempt must consume one budget unit")
        successful = tuple(
            item for item in self.factors if item.status is FactorAttemptStatus.OFFICIAL_VISIBLE_EVIDENCE
        )
        if self.official_evidence_run != bool(successful):
            fail(FailureCode.INVALID_STATE, "official evidence run flag disagrees with factor results")
        if self.status == "COMPLETED_OFFICIAL_VISIBLE" and len(successful) != len(self.factors):
            fail(FailureCode.INVALID_STATE, "completed run contains a failed factor")
        if self.status == "FAILED_CONSUMED" and successful:
            fail(FailureCode.INVALID_STATE, "failed run contains official evidence")
        if self.sealed_evidence_run or self.promotion_run or self.challenger_created:
            fail(FailureCode.UNAUTHORIZED_CAPABILITY, "visible Evidence adapter crossed an authority boundary")

