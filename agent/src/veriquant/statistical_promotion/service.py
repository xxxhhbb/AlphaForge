"""Service facade for the statistical promotion package."""

from __future__ import annotations

from datetime import datetime

from .contracts import (
    MultipleTestingPolicy,
    PromotionState,
    RobustnessPolicy,
)
from .reader import EvidenceReader, SealedAggregateHandle
from .sealed_gate import GateResult, SealedPromotionGate


class StatisticalPromotionService:
    """Thin deterministic facade around SealedPromotionGate."""

    def __init__(
        self,
        *,
        reader: EvidenceReader,
        robustness_policy: RobustnessPolicy | None = None,
        multiple_testing_policy: MultipleTestingPolicy | None = None,
        gate_id: str = "statistical-promotion-gate",
    ) -> None:
        self.gate = SealedPromotionGate(
            reader=reader,
            robustness_policy=robustness_policy,
            multiple_testing_policy=multiple_testing_policy,
            gate_id=gate_id,
        )

    def evaluate_candidate(
        self,
        *,
        candidate_id: str,
        candidate_version: str,
        family_id: str,
        manifest_version: str,
        idempotency_key: str,
        proposer_id: str,
        approver_id: str,
        decision_time: datetime,
        current_state: PromotionState = PromotionState.VALIDATED_RESEARCH,
        sealed_handle: SealedAggregateHandle | None = None,
    ) -> GateResult:
        return self.gate.evaluate(
            candidate_id=candidate_id,
            candidate_version=candidate_version,
            family_id=family_id,
            manifest_version=manifest_version,
            idempotency_key=idempotency_key,
            proposer_id=proposer_id,
            approver_id=approver_id,
            decision_time=decision_time,
            current_state=current_state,
            sealed_handle=sealed_handle,
        )

