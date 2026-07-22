"""Integration-owned bridge from official Evidence Service summaries to Agent B.

The bridge is deliberately thin: it never exposes raw sealed rows and only
constructs the `EvidenceReader` view required by the parallel statistical
promotion package from already-frozen official train/validation evidence.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Mapping, Sequence

from ..evidence.models import OfficialEvidence
from ..statistical_promotion.contracts import (
    CandidateEvidenceView,
    CandidateVersionRef,
    FactorFamilyManifest,
    MultipleTestingPolicy,
    PromotionState,
    RobustnessPolicy,
    StatisticalPromotionError,
    StatisticalPromotionFailureCode,
    content_hash,
)
from ..statistical_promotion.reader import SealedAggregateHandle, SealedDecisionResponse
from ..statistical_promotion.sealed_gate import GateResult
from ..statistical_promotion.service import StatisticalPromotionService


def build_family_manifest(
    *,
    family_id: str,
    manifest_version: str,
    frozen_at: datetime,
    candidates: Sequence[tuple[str, str]],
) -> FactorFamilyManifest:
    """Build the frozen family manifest consumed by the statistical gate."""

    return FactorFamilyManifest(
        family_id=family_id,
        manifest_version=manifest_version,
        frozen_at=frozen_at,
        candidate_versions=tuple(
            CandidateVersionRef(candidate_id=candidate_id, candidate_version=version)
            for candidate_id, version in candidates
        ),
    )


def _require_partition(evidence: OfficialEvidence, partition: str) -> None:
    if not isinstance(evidence, OfficialEvidence) or evidence.partition != partition:
        raise StatisticalPromotionError(
            StatisticalPromotionFailureCode.UNAUTHORIZED_EVIDENCE_VIEW,
            f"expected official {partition} evidence",
        )


def candidate_view_from_official_evidence(
    *,
    candidate_id: str,
    candidate_version: str,
    family_manifest: FactorFamilyManifest,
    train: OfficialEvidence,
    validation: OfficialEvidence,
    proposer_id: str,
    p_value: float,
    state: PromotionState = PromotionState.VALIDATED_RESEARCH,
    capacity_utilization: float = 0.10,
    coverage: float = 1.0,
    max_abs_industry_exposure: float = 0.0,
    max_abs_market_cap_exposure: float = 0.0,
    parameter_sensitivity_max_delta: float = 0.0,
) -> CandidateEvidenceView:
    """Project official train/validation evidence into Agent B's redacted view.

    `p_value` is a deterministic visible family statistic supplied by the
    caller.  This helper rejects SEALED evidence objects: sealed promotion may
    only happen through opaque handles inside `InMemoryOfficialEvidenceReader`.
    """

    _require_partition(train, "TRAIN")
    _require_partition(validation, "VALIDATION")
    if train.identity_hash != validation.identity_hash:
        raise StatisticalPromotionError(
            StatisticalPromotionFailureCode.INVALID_CONTRACT,
            "train and validation evidence must bind the same ExperimentIdentity",
        )
    cost_sensitivity_loss = validation.metrics.spread_cost + validation.metrics.linear_impact_cost
    robustness = {
        "subperiod_positive_fraction": validation.metrics.subperiod_positive_fraction,
        "subperiod_min_ic": validation.metrics.subperiod_min_ic,
        "regime_positive_fraction": validation.metrics.regime_positive_fraction,
        "regime_min_ic": validation.metrics.regime_min_ic,
        "max_abs_industry_exposure": max_abs_industry_exposure,
        "max_abs_market_cap_exposure": max_abs_market_cap_exposure,
        "turnover": validation.metrics.turnover,
        "gross_return_before_cost": validation.metrics.gross_return,
        "net_return_after_cost": validation.metrics.net_return,
        "cost_sensitivity_loss": cost_sensitivity_loss,
        "capacity_utilization": capacity_utilization,
        "coverage": coverage,
        "parameter_sensitivity_max_delta": parameter_sensitivity_max_delta,
    }
    return CandidateEvidenceView(
        candidate_id=candidate_id,
        candidate_version=candidate_version,
        family_id=family_manifest.family_id,
        family_manifest_hash=family_manifest.artifact_hash,
        identity_hash=train.identity_hash,
        evidence_hash=content_hash(
            {
                "train_evidence_hash": train.artifact_hash,
                "validation_evidence_hash": validation.artifact_hash,
            }
        ),
        input_hash=content_hash(
            {
                "train_input_hashes": train.input_hashes,
                "validation_input_hashes": validation.input_hashes,
                "view": "train-validation-official-only",
            }
        ),
        proposer_id=proposer_id,
        state=state,
        visible_metrics={
            "TRAIN": dict(train.metrics.flat()),
            "VALIDATION": dict(validation.metrics.flat()),
        },
        robustness_metrics=robustness,
        p_value=p_value,
    )


@dataclass(frozen=True, kw_only=True)
class PromotionEvaluationRequest:
    candidate_id: str
    candidate_version: str
    family_id: str
    manifest_version: str
    idempotency_key: str
    proposer_id: str
    approver_id: str
    decision_time: datetime
    current_state: PromotionState = PromotionState.VALIDATED_RESEARCH


class InMemoryOfficialEvidenceReader:
    """Offline `EvidenceReader` for integration and acceptance tests.

    It stores only `CandidateEvidenceView` values plus a boolean sealed verdict.
    No method returns sealed rows, sealed rankings, or private promotion metrics.
    """

    def __init__(
        self,
        *,
        manifest: FactorFamilyManifest,
        views: Sequence[CandidateEvidenceView],
        sealed_passed: bool | Mapping[str, bool] = True,
        reader_id: str = "integration-official-evidence-reader",
    ) -> None:
        self._manifest = manifest
        self._views = {view.key: view for view in views}
        self._sealed_passed = sealed_passed
        self._reader_id = reader_id
        self._handles: dict[str, SealedAggregateHandle] = {}
        self._consumed: dict[str, str] = {}

    @property
    def reader_id(self) -> str:
        return self._reader_id

    def read_visible_evidence(self, *, candidate_id: str, candidate_version: str) -> CandidateEvidenceView:
        key = f"{candidate_id}@{candidate_version}"
        try:
            return self._views[key]
        except KeyError as exc:
            raise StatisticalPromotionError(
                StatisticalPromotionFailureCode.MISSING_EVIDENCE,
                "visible candidate evidence is missing",
            ) from exc

    def read_family_manifest(self, *, family_id: str, manifest_version: str) -> FactorFamilyManifest:
        if family_id != self._manifest.family_id or manifest_version != self._manifest.manifest_version:
            raise StatisticalPromotionError(
                StatisticalPromotionFailureCode.FAMILY_MANIFEST_MISMATCH,
                "requested family manifest does not match the frozen manifest",
            )
        return self._manifest

    def read_family_visible_evidence(self, *, manifest: FactorFamilyManifest) -> tuple[CandidateEvidenceView, ...]:
        if manifest.artifact_hash != self._manifest.artifact_hash:
            raise StatisticalPromotionError(
                StatisticalPromotionFailureCode.FAMILY_MANIFEST_MISMATCH,
                "family manifest hash mismatch",
            )
        return tuple(self._views[item.key] for item in manifest.candidate_versions if item.key in self._views)

    def read_sealed_handle(
        self,
        *,
        candidate: CandidateEvidenceView,
        input_hash: str,
        policy_hash: str,
    ) -> SealedAggregateHandle:
        handle_hash = content_hash(
            {
                "candidate_key": candidate.key,
                "input_hash": input_hash,
                "policy_hash": policy_hash,
                "reader_id": self.reader_id,
            }
        )
        handle = SealedAggregateHandle(
            handle_id="sealed.integration." + handle_hash.removeprefix("sha256:")[:24],
            handle_hash=handle_hash,
            candidate_id=candidate.candidate_id,
            candidate_version=candidate.candidate_version,
            input_hash=input_hash,
            policy_hash=policy_hash,
        )
        self._handles[handle.handle_hash] = handle
        return handle

    def read_authorized_sealed_decision(
        self,
        *,
        handle: SealedAggregateHandle,
        request_hash: str,
    ) -> SealedDecisionResponse:
        expected = self._handles.get(handle.handle_hash)
        if expected != handle:
            raise StatisticalPromotionError(
                StatisticalPromotionFailureCode.FORGED_SEALED_HANDLE,
                "sealed handle was not minted by this reader",
            )
        previous = self._consumed.get(handle.handle_hash)
        if previous is not None and previous != request_hash:
            raise StatisticalPromotionError(
                StatisticalPromotionFailureCode.SEALED_REPLAY,
                "sealed handle has already been used for another request",
            )
        self._consumed[handle.handle_hash] = request_hash
        key = f"{handle.candidate_id}@{handle.candidate_version}"
        passed = self._sealed_passed.get(key, False) if isinstance(self._sealed_passed, Mapping) else bool(self._sealed_passed)
        return SealedDecisionResponse(
            request_hash=request_hash,
            passed=passed,
            failure_code=None if passed else StatisticalPromotionFailureCode.SEALED_DECISION_REJECTED,
        )


def evaluate_statistical_promotion(
    *,
    reader: InMemoryOfficialEvidenceReader,
    request: PromotionEvaluationRequest,
    robustness_policy: RobustnessPolicy | None = None,
    multiple_testing_policy: MultipleTestingPolicy | None = None,
) -> GateResult:
    """Run Agent B behind the Integration-owned EvidenceReader bridge."""

    service = StatisticalPromotionService(
        reader=reader,
        robustness_policy=robustness_policy,
        multiple_testing_policy=multiple_testing_policy,
    )
    return service.evaluate_candidate(
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


__all__ = [
    "InMemoryOfficialEvidenceReader",
    "PromotionEvaluationRequest",
    "build_family_manifest",
    "candidate_view_from_official_evidence",
    "evaluate_statistical_promotion",
]
