"""Public, redacted contracts for the Research OS promotion boundary."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from ..research_closure_contracts import OpaquePromotionReceiptRef
from ..statistical_promotion.contracts import (
    PromotionContract,
    PromotionState,
    PromotionVerdict,
    StatisticalPromotionError,
    StatisticalPromotionFailureCode,
    _require_datetime,
    _require_hash,
    _require_identifier,
    content_hash,
)


class PromotionBridgeTerminalState(str, Enum):
    """Only the minimum terminal state that may cross the sealed boundary."""

    CHALLENGER = "CHALLENGER"
    REJECTED = "REJECTED"


@dataclass(frozen=True, kw_only=True)
class PromotionBridgeRequest(PromotionContract):
    """Hash-bound request for an already validated official candidate."""

    candidate_id: str
    candidate_version: str
    family_id: str
    manifest_version: str
    idempotency_key: str
    proposer_id: str
    approver_id: str
    decision_time: datetime
    expected_evidence_hash: str
    expected_identity_hash: str
    expected_family_manifest_hash: str
    current_state: PromotionState = PromotionState.VALIDATED_RESEARCH

    def __post_init__(self) -> None:
        for name in (
            "candidate_id",
            "candidate_version",
            "family_id",
            "manifest_version",
            "idempotency_key",
            "proposer_id",
            "approver_id",
        ):
            _require_identifier(getattr(self, name), name)
        for name in (
            "expected_evidence_hash",
            "expected_identity_hash",
            "expected_family_manifest_hash",
        ):
            _require_hash(getattr(self, name), name)
        _require_datetime(self.decision_time, "decision_time")
        if self.current_state is not PromotionState.VALIDATED_RESEARCH:
            raise StatisticalPromotionError(
                StatisticalPromotionFailureCode.INVALID_STATE_TRANSITION,
                "promotion bridge accepts only VALIDATED_RESEARCH candidates",
            )
        if self.proposer_id == self.approver_id:
            raise StatisticalPromotionError(
                StatisticalPromotionFailureCode.SELF_APPROVAL,
                "proposer cannot approve itself",
            )


@dataclass(frozen=True, kw_only=True)
class OpaqueTerminalPromotionReceipt(PromotionContract):
    """Minimal public result; no handle, metric, rank, or private reason escapes."""

    receipt_id: str
    receipt_hash: str
    candidate_id: str
    candidate_version: str
    family_id: str
    terminal_state: PromotionBridgeTerminalState
    verdict: PromotionVerdict
    decision_hash: str
    audit_hash: str
    evidence_hash: str
    policy_version: str
    approver_id: str
    redacted: bool = True
    sealed_detail_disclosed: bool = False

    def __post_init__(self) -> None:
        for name in (
            "receipt_id",
            "candidate_id",
            "candidate_version",
            "family_id",
            "policy_version",
            "approver_id",
        ):
            _require_identifier(getattr(self, name), name)
        for name in (
            "receipt_hash",
            "decision_hash",
            "audit_hash",
            "evidence_hash",
        ):
            _require_hash(getattr(self, name), name)
        if self.redacted is not True or self.sealed_detail_disclosed is not False:
            raise StatisticalPromotionError(
                StatisticalPromotionFailureCode.LEAKAGE_DETECTED,
                "public promotion receipt must remain opaque",
            )
        expected_verdict = {
            PromotionBridgeTerminalState.CHALLENGER: PromotionVerdict.PROMOTE_CHALLENGER,
            PromotionBridgeTerminalState.REJECTED: PromotionVerdict.REJECT,
        }[self.terminal_state]
        if self.verdict is not expected_verdict:
            raise StatisticalPromotionError(
                StatisticalPromotionFailureCode.INVALID_STATE_TRANSITION,
                "terminal state and verdict disagree",
            )

    @classmethod
    def from_gate_result(cls, *, gate_result, evidence_hash: str) -> "OpaqueTerminalPromotionReceipt":
        decision = gate_result.decision
        if decision.to_state is PromotionState.CHALLENGER:
            terminal_state = PromotionBridgeTerminalState.CHALLENGER
        elif decision.to_state is PromotionState.REJECTED:
            terminal_state = PromotionBridgeTerminalState.REJECTED
        else:
            raise StatisticalPromotionError(
                StatisticalPromotionFailureCode.INVALID_STATE_TRANSITION,
                "promotion gate did not produce a permitted terminal result",
            )
        audit_hash = gate_result.audit.artifact_hash
        receipt_hash = content_hash(
            {
                "decision_hash": decision.decision_hash,
                "audit_hash": audit_hash,
                "evidence_hash": evidence_hash,
                "policy_version": decision.policy_version,
                "terminal_state": terminal_state.value,
            }
        )
        return cls(
            receipt_id="promotion-receipt-" + receipt_hash.removeprefix("sha256:")[:24],
            receipt_hash=receipt_hash,
            candidate_id=decision.candidate_id,
            candidate_version=decision.candidate_version,
            family_id=decision.family_id,
            terminal_state=terminal_state,
            verdict=decision.verdict,
            decision_hash=decision.decision_hash,
            audit_hash=audit_hash,
            evidence_hash=evidence_hash,
            policy_version=decision.policy_version,
            approver_id=decision.approver_id,
        )

    def to_closure_ref(self) -> OpaquePromotionReceiptRef:
        """Create the existing closure reference only for a real challenger."""

        if self.terminal_state is not PromotionBridgeTerminalState.CHALLENGER:
            raise StatisticalPromotionError(
                StatisticalPromotionFailureCode.INVALID_STATE_TRANSITION,
                "only CHALLENGER may carry a promotion receipt in closure output",
            )
        return OpaquePromotionReceiptRef(
            receipt_id=self.receipt_id,
            receipt_hash=self.receipt_hash,
            policy_version=self.policy_version,
        )

