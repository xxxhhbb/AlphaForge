"""Sealed statistical promotion gate."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Mapping

from .contracts import (
    CandidateEvidenceView,
    FactorFamilyManifest,
    MultipleTestingPolicy,
    PromotionAuditManifest,
    PromotionDecision,
    PromotionState,
    PromotionVerdict,
    RobustnessPolicy,
    StatisticalPromotionFailureCode,
    StatisticalPromotionError,
    content_hash,
    _fail,
    _require_hash,
    _require_identifier,
)
from .multiplicity import MultipleTestingAssessment, apply_multiple_testing
from .reader import EvidenceReader, SealedAggregateHandle
from .robustness import RobustnessAssessment, evaluate_robustness


@dataclass(frozen=True, kw_only=True)
class GateResult:
    decision: PromotionDecision
    audit: PromotionAuditManifest
    robustness: RobustnessAssessment | None
    multiple_testing: MultipleTestingAssessment | None


class SealedPromotionGate:
    """Mechanical authority that can produce CHALLENGER from official evidence."""

    def __init__(
        self,
        *,
        reader: EvidenceReader,
        robustness_policy: RobustnessPolicy | None = None,
        multiple_testing_policy: MultipleTestingPolicy | None = None,
        gate_id: str = "statistical-promotion-gate",
    ) -> None:
        _require_identifier(gate_id, "gate_id")
        self.reader = reader
        self.robustness_policy = robustness_policy or RobustnessPolicy()
        self.multiple_testing_policy = multiple_testing_policy or MultipleTestingPolicy()
        self.gate_id = gate_id
        self._decisions_by_idempotency: dict[str, GateResult] = {}
        self._input_by_idempotency: dict[str, str] = {}
        self._consumed_handles: dict[str, str] = {}

    @property
    def policy_version(self) -> str:
        return (
            f"robustness:{self.robustness_policy.policy_version}/"
            f"multiple:{self.multiple_testing_policy.policy_version}"
        )

    @property
    def combined_policy_hash(self) -> str:
        return content_hash(
            {
                "robustness_policy": self.robustness_policy.to_dict(),
                "multiple_testing_policy": self.multiple_testing_policy.to_dict(),
                "gate_id": self.gate_id,
            }
        )

    def evaluate(
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
        _require_identifier(candidate_id, "candidate_id")
        _require_identifier(candidate_version, "candidate_version")
        _require_identifier(family_id, "family_id")
        _require_identifier(manifest_version, "manifest_version")
        _require_identifier(idempotency_key, "idempotency_key")
        _require_identifier(proposer_id, "proposer_id")
        _require_identifier(approver_id, "approver_id")
        if proposer_id == approver_id:
            raise StatisticalPromotionError(
                StatisticalPromotionFailureCode.SELF_APPROVAL,
                "proposer cannot approve itself",
            )

        view = self.reader.read_visible_evidence(
            candidate_id=candidate_id,
            candidate_version=candidate_version,
        )
        manifest = self.reader.read_family_manifest(
            family_id=family_id,
            manifest_version=manifest_version,
        )
        self._validate_view_and_manifest(view, manifest)
        peers = self.reader.read_family_visible_evidence(manifest=manifest)
        input_hash = self._input_hash(
            view=view,
            manifest=manifest,
            current_state=current_state,
        )
        key_hash = content_hash({"idempotency_key": idempotency_key})

        cached = self._decisions_by_idempotency.get(idempotency_key)
        if cached is not None:
            if self._input_by_idempotency[idempotency_key] != input_hash:
                raise StatisticalPromotionError(
                    StatisticalPromotionFailureCode.IDEMPOTENCY_CONFLICT,
                    "idempotency key was reused for different promotion input",
                )
            return cached
        if idempotency_key in self._input_by_idempotency:
            raise StatisticalPromotionError(
                StatisticalPromotionFailureCode.IDEMPOTENCY_CONFLICT,
                "idempotency key has a conflicting partial record",
            )
        self._input_by_idempotency[idempotency_key] = input_hash

        robustness: RobustnessAssessment | None = None
        multiplicity: MultipleTestingAssessment | None = None
        try:
            if current_state is not PromotionState.VALIDATED_RESEARCH:
                return self._record_result(
                    view=view,
                    manifest=manifest,
                    idempotency_key=idempotency_key,
                    key_hash=key_hash,
                    input_hash=input_hash,
                    decision_time=decision_time,
                    from_state=current_state,
                    to_state=PromotionState.INCOMPLETE,
                    verdict=PromotionVerdict.INCOMPLETE,
                    reason_codes=("INVALID_INITIAL_STATE",),
                    failure_code=StatisticalPromotionFailureCode.INVALID_STATE_TRANSITION,
                    sealed_handle_hash=None,
                    robustness=None,
                    multiplicity=None,
                )

            robustness = evaluate_robustness(view, self.robustness_policy)
            if not robustness.passed:
                first_code = robustness.failure_codes[0]
                return self._record_result(
                    view=view,
                    manifest=manifest,
                    idempotency_key=idempotency_key,
                    key_hash=key_hash,
                    input_hash=input_hash,
                    decision_time=decision_time,
                    from_state=current_state,
                    to_state=PromotionState.REJECTED,
                    verdict=PromotionVerdict.REJECT,
                    reason_codes=tuple(item.check_name for item in robustness.findings if not item.passed),
                    failure_code=first_code,
                    sealed_handle_hash=None,
                    robustness=robustness,
                    multiplicity=None,
                )

            p_values = self._family_p_values(peers, manifest)
            multiplicity = apply_multiple_testing(p_values, self.multiple_testing_policy)
            if not multiplicity.passed.get(view.key, False):
                return self._record_result(
                    view=view,
                    manifest=manifest,
                    idempotency_key=idempotency_key,
                    key_hash=key_hash,
                    input_hash=input_hash,
                    decision_time=decision_time,
                    from_state=current_state,
                    to_state=PromotionState.REJECTED,
                    verdict=PromotionVerdict.REJECT,
                    reason_codes=("MULTIPLE_TESTING_FAILED", self.multiple_testing_policy.method.value),
                    failure_code=StatisticalPromotionFailureCode.MULTIPLE_TESTING_FAILED,
                    sealed_handle_hash=None,
                    robustness=robustness,
                    multiplicity=multiplicity,
                )

            handle = sealed_handle or self.reader.read_sealed_handle(
                candidate=view,
                input_hash=input_hash,
                policy_hash=self.combined_policy_hash,
            )
            self._validate_handle(handle, view=view, input_hash=input_hash)
            handle_hash = handle.handle_hash
            previous = self._consumed_handles.get(handle_hash)
            if previous is not None:
                raise StatisticalPromotionError(
                    StatisticalPromotionFailureCode.SEALED_REPLAY,
                    "sealed handle was already consumed by another decision",
                    details={"decision_hash": previous},
                )
            request_hash = content_hash(
                {
                    "candidate_key": view.key,
                    "input_hash": input_hash,
                    "handle_hash": handle_hash,
                    "policy_hash": self.combined_policy_hash,
                }
            )
            sealed = self.reader.read_authorized_sealed_decision(
                handle=handle,
                request_hash=request_hash,
            )
            if sealed.request_hash != request_hash:
                raise StatisticalPromotionError(
                    StatisticalPromotionFailureCode.FORGED_SEALED_HANDLE,
                    "sealed decision response does not match request",
                )
            if sealed.passed:
                result = self._record_result(
                    view=view,
                    manifest=manifest,
                    idempotency_key=idempotency_key,
                    key_hash=key_hash,
                    input_hash=input_hash,
                    decision_time=decision_time,
                    from_state=current_state,
                    to_state=PromotionState.CHALLENGER,
                    verdict=PromotionVerdict.PROMOTE_CHALLENGER,
                    reason_codes=("VISIBLE_ROBUSTNESS_PASS", "MULTIPLE_TESTING_PASS", "SEALED_DECISION_PASS"),
                    failure_code=None,
                    sealed_handle_hash=handle_hash,
                    robustness=robustness,
                    multiplicity=multiplicity,
                )
            else:
                result = self._record_result(
                    view=view,
                    manifest=manifest,
                    idempotency_key=idempotency_key,
                    key_hash=key_hash,
                    input_hash=input_hash,
                    decision_time=decision_time,
                    from_state=current_state,
                    to_state=PromotionState.REJECTED,
                    verdict=PromotionVerdict.REJECT,
                    reason_codes=("SEALED_DECISION_REJECTED",),
                    failure_code=(
                        sealed.failure_code
                        or StatisticalPromotionFailureCode.SEALED_DECISION_REJECTED
                    ),
                    sealed_handle_hash=handle_hash,
                    robustness=robustness,
                    multiplicity=multiplicity,
                )
            self._consumed_handles[handle_hash] = result.decision.decision_hash
            return result
        except StatisticalPromotionError:
            raise
        except Exception as exc:
            raise StatisticalPromotionError(
                StatisticalPromotionFailureCode.INVALID_CONTRACT,
                "promotion gate failed closed",
                details={"error_type": type(exc).__name__},
            ) from exc

    def _validate_view_and_manifest(
        self,
        view: CandidateEvidenceView,
        manifest: FactorFamilyManifest,
    ) -> None:
        if not isinstance(view, CandidateEvidenceView):
            _fail(
                StatisticalPromotionFailureCode.UNAUTHORIZED_EVIDENCE_VIEW,
                "reader returned a non-candidate evidence view",
            )
        if not isinstance(manifest, FactorFamilyManifest):
            _fail(
                StatisticalPromotionFailureCode.FAMILY_MANIFEST_MISMATCH,
                "reader returned a non-family manifest",
            )
        if view.family_id != manifest.family_id or view.family_manifest_hash != manifest.artifact_hash:
            _fail(
                StatisticalPromotionFailureCode.FAMILY_MANIFEST_MISMATCH,
                "candidate family must match the frozen manifest",
            )
        if not manifest.contains(view):
            _fail(
                StatisticalPromotionFailureCode.FAMILY_MANIFEST_MISMATCH,
                "candidate/version is not in the frozen family manifest",
                candidate=view.key,
            )

    def _family_p_values(
        self,
        peers: tuple[CandidateEvidenceView, ...],
        manifest: FactorFamilyManifest,
    ) -> Mapping[str, float]:
        expected = {item.key for item in manifest.candidate_versions}
        observed: dict[str, float] = {}
        for peer in peers:
            self._validate_view_and_manifest(peer, manifest)
            if peer.key in observed:
                _fail(
                    StatisticalPromotionFailureCode.DUPLICATE_CANDIDATE,
                    "duplicate peer evidence for candidate/version",
                    candidate=peer.key,
                )
            observed[peer.key] = peer.p_value
        if set(observed) != expected:
            _fail(
                StatisticalPromotionFailureCode.MISSING_EVIDENCE,
                "family-level multiple testing requires every manifest candidate",
                missing=tuple(sorted(expected - set(observed))),
            )
        return observed

    def _input_hash(
        self,
        *,
        view: CandidateEvidenceView,
        manifest: FactorFamilyManifest,
        current_state: PromotionState,
    ) -> str:
        return content_hash(
            {
                "candidate_evidence_hash": view.artifact_hash,
                "family_manifest_hash": manifest.artifact_hash,
                "robustness_policy_hash": self.robustness_policy.artifact_hash,
                "multiple_testing_policy_hash": self.multiple_testing_policy.artifact_hash,
                "combined_policy_hash": self.combined_policy_hash,
                "current_state": current_state.value,
            }
        )

    def _validate_handle(
        self,
        handle: SealedAggregateHandle,
        *,
        view: CandidateEvidenceView,
        input_hash: str,
    ) -> None:
        if not isinstance(handle, SealedAggregateHandle):
            raise StatisticalPromotionError(
                StatisticalPromotionFailureCode.FORGED_SEALED_HANDLE,
                "sealed handle is not a valid opaque handle",
            )
        if handle.candidate_id != view.candidate_id or handle.candidate_version != view.candidate_version:
            raise StatisticalPromotionError(
                StatisticalPromotionFailureCode.FORGED_SEALED_HANDLE,
                "sealed handle belongs to a different candidate/version",
            )
        if handle.input_hash != input_hash:
            raise StatisticalPromotionError(
                StatisticalPromotionFailureCode.POLICY_TAMPERED,
                "sealed handle was not minted for this exact promotion input",
            )
        if handle.policy_hash != self.combined_policy_hash:
            raise StatisticalPromotionError(
                StatisticalPromotionFailureCode.POLICY_TAMPERED,
                "sealed handle policy hash does not match gate policy",
            )
        _require_hash(handle.handle_hash, "handle_hash")

    def _record_result(
        self,
        *,
        view: CandidateEvidenceView,
        manifest: FactorFamilyManifest,
        idempotency_key: str,
        key_hash: str,
        input_hash: str,
        decision_time: datetime,
        from_state: PromotionState,
        to_state: PromotionState,
        verdict: PromotionVerdict,
        reason_codes: tuple[str, ...],
        failure_code: StatisticalPromotionFailureCode | None,
        sealed_handle_hash: str | None,
        robustness: RobustnessAssessment | None,
        multiplicity: MultipleTestingAssessment | None,
    ) -> GateResult:
        _require_hash(input_hash, "input_hash")
        if sealed_handle_hash is not None:
            _require_hash(sealed_handle_hash, "sealed_handle_hash")
        decision_seed = content_hash(
            {
                "candidate": view.key,
                "family_id": view.family_id,
                "input_hash": input_hash,
                "verdict": verdict.value,
                "to_state": to_state.value,
                "reason_codes": reason_codes,
                "decided_at": decision_time,
            }
        )
        decision = PromotionDecision(
            decision_id="promotion-" + decision_seed.removeprefix("sha256:")[:24],
            candidate_id=view.candidate_id,
            candidate_version=view.candidate_version,
            family_id=view.family_id,
            from_state=from_state,
            to_state=to_state,
            verdict=verdict,
            reason_codes=reason_codes,
            failure_code=failure_code,
            proposer_id=view.proposer_id,
            approver_id=self.gate_id,
            decided_at=decision_time,
            policy_version=self.policy_version,
            input_hash=input_hash,
        )
        status = {
            PromotionVerdict.PROMOTE_CHALLENGER: "PROMOTED",
            PromotionVerdict.REJECT: "REJECTED",
            PromotionVerdict.INCOMPLETE: "INCOMPLETE",
        }[verdict]
        audit_seed = content_hash(
            {
                "decision_hash": decision.decision_hash,
                "input_hash": input_hash,
                "idempotency_key_hash": key_hash,
            }
        )
        audit = PromotionAuditManifest(
            audit_id="promotion-audit-" + audit_seed.removeprefix("sha256:")[:20],
            decision_hash=decision.decision_hash,
            input_hash=input_hash,
            candidate_evidence_hash=view.artifact_hash,
            family_manifest_hash=manifest.artifact_hash,
            robustness_policy_hash=self.robustness_policy.artifact_hash,
            multiple_testing_policy_hash=self.multiple_testing_policy.artifact_hash,
            policy_version=self.policy_version,
            idempotency_key_hash=key_hash,
            sealed_handle_hash=sealed_handle_hash,
            reader_id=self.reader.reader_id,
            gate_id=self.gate_id,
            status=status,
            failure_code=failure_code,
            produced_at=decision_time,
        )
        result = GateResult(
            decision=decision,
            audit=audit,
            robustness=robustness,
            multiple_testing=multiplicity,
        )
        self._decisions_by_idempotency[idempotency_key] = result
        return result

