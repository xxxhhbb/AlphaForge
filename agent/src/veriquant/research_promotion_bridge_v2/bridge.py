"""Thin authority adapter over the existing deterministic sealed gate."""

from __future__ import annotations

from ..statistical_promotion.contracts import (
    MultipleTestingPolicy,
    PromotionState,
    RobustnessPolicy,
    StatisticalPromotionError,
    StatisticalPromotionFailureCode,
)
from ..statistical_promotion.reader import EvidenceReader
from ..statistical_promotion.service import StatisticalPromotionService
from .contracts import OpaqueTerminalPromotionReceipt, PromotionBridgeRequest


class ResearchPromotionBridgeV2:
    """Own the sealed reader capability and return only opaque terminal receipts.

    The bridge deliberately has no method that returns its reader, a sealed
    handle, robustness details, family ranks, or adjusted p-values.  Deployment
    should place this authority in a separate process/capability namespace.
    """

    def __init__(
        self,
        *,
        sealed_reader: EvidenceReader,
        robustness_policy: RobustnessPolicy | None = None,
        multiple_testing_policy: MultipleTestingPolicy | None = None,
        gate_id: str = "independent-promotion-authority",
    ) -> None:
        self._sealed_reader = sealed_reader
        self._service = StatisticalPromotionService(
            reader=sealed_reader,
            robustness_policy=robustness_policy,
            multiple_testing_policy=multiple_testing_policy,
            gate_id=gate_id,
        )

    @property
    def policy_hash(self) -> str:
        return self._service.gate.combined_policy_hash

    def evaluate(self, request: PromotionBridgeRequest) -> OpaqueTerminalPromotionReceipt:
        if not isinstance(request, PromotionBridgeRequest):
            raise StatisticalPromotionError(
                StatisticalPromotionFailureCode.INVALID_CONTRACT,
                "promotion request must be PromotionBridgeRequest",
            )
        if request.approver_id != self._service.gate.gate_id:
            raise StatisticalPromotionError(
                StatisticalPromotionFailureCode.INVALID_CONTRACT,
                "request approver does not match the configured independent authority",
            )

        view = self._sealed_reader.read_visible_evidence(
            candidate_id=request.candidate_id,
            candidate_version=request.candidate_version,
        )
        if (
            view.state is not PromotionState.VALIDATED_RESEARCH
            or view.produced_by != "EVIDENCE_SERVICE"
            or view.evidence_class != "OFFICIAL_EVIDENCE"
        ):
            raise StatisticalPromotionError(
                StatisticalPromotionFailureCode.UNAUTHORIZED_EVIDENCE_VIEW,
                "promotion accepts only official VALIDATED_RESEARCH evidence",
            )
        if view.proposer_id != request.proposer_id:
            raise StatisticalPromotionError(
                StatisticalPromotionFailureCode.INVALID_CONTRACT,
                "request proposer does not match official evidence",
            )
        expected = {
            "evidence_hash": request.expected_evidence_hash,
            "identity_hash": request.expected_identity_hash,
            "family_manifest_hash": request.expected_family_manifest_hash,
        }
        actual = {
            "evidence_hash": view.evidence_hash,
            "identity_hash": view.identity_hash,
            "family_manifest_hash": view.family_manifest_hash,
        }
        if actual != expected:
            raise StatisticalPromotionError(
                StatisticalPromotionFailureCode.MISSING_EVIDENCE,
                "promotion request hashes do not match frozen official evidence",
            )

        manifest = self._sealed_reader.read_family_manifest(
            family_id=request.family_id,
            manifest_version=request.manifest_version,
        )
        if manifest.artifact_hash != request.expected_family_manifest_hash:
            raise StatisticalPromotionError(
                StatisticalPromotionFailureCode.FAMILY_MANIFEST_MISMATCH,
                "promotion request does not bind the frozen family manifest",
            )

        result = self._service.evaluate_candidate(
            candidate_id=request.candidate_id,
            candidate_version=request.candidate_version,
            family_id=request.family_id,
            manifest_version=request.manifest_version,
            idempotency_key=request.idempotency_key,
            proposer_id=request.proposer_id,
            approver_id=request.approver_id,
            decision_time=request.decision_time,
            current_state=request.current_state,
        )
        return OpaqueTerminalPromotionReceipt.from_gate_result(
            gate_result=result,
            evidence_hash=view.evidence_hash,
        )
