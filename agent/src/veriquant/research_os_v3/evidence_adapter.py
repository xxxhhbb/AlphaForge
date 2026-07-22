"""U6 adapter from committed U4 specialist DRAFTs to existing evidence truth.

This module deliberately contains no factor execution, PIT calculation, metric
calculation, court policy, or promotion logic.  It verifies the committed U4
artifacts, translates each DRAFT through the existing safe translator and then
delegates every terminal attempt to :class:`ResearchEvidenceBridge`.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import ClassVar, Mapping

from ..candidate_translation import (
    CandidateDraftEnvelope,
    CandidateTranslationResult,
    CandidateTranslationStatus,
    SafeCandidateTranslator,
)
from ..contracts import CanonicalContract, FailureCode
from ..contracts import Component, DatasetCard
from ..contracts.base import fail, require_hash, require_identifier
from ..data import canonical_content_hash
from ..frozen_qlib_provider import (
    FrozenQlibProvider,
    FrozenQlibProviderError,
    QlibDataRequirement,
)
from ..factor_review import FactorReviewAgent, FactorReviewPolicy, FactorReviewRequest
from ..qlib_candidate_translation import (
    QlibCandidateFailureCode,
    QlibCandidateDraftEnvelope,
    QlibCandidateTranslationResult,
    QlibCandidateTranslationStatus,
    SafeQlibCandidateTranslator,
)
from ..research_campaign import CampaignState
from ..evidence.models import CostModel, RegisteredFactor, WalkForwardSpec
from ..research_evidence_bridge import (
    CandidateEvidenceValidationRequest,
    FrozenEvidenceInputIdentity,
    ResearchEvidenceBridge,
    ValidationBridgeStatus,
    VisibleComparisonMetrics,
)
from ..research_validation_pipeline_v2 import FrozenValidationData
from ..specialist_research import CandidatePreflightResult, PreflightStatus, SpecialistAgentOutcome
from .multi_models import MultiSpecialistCandidateRef, MultiSpecialistRunResult


SCHEMA_VERSION = "alphaforge.research-os-v3-evidence-adapter/v2"


class EvidenceAdapterStatus(str, Enum):
    OFFICIAL_VISIBLE_EVALUATED = "OFFICIAL_VISIBLE_EVALUATED"
    TRANSLATION_REJECTED = "TRANSLATION_REJECTED"
    PIT_REJECTED = "PIT_REJECTED"
    EVALUATION_REJECTED = "EVALUATION_REJECTED"
    BUDGET_BLOCKED = "BUDGET_BLOCKED"
    LEDGER_BLOCKED = "LEDGER_BLOCKED"


class EvidenceAdapterFailureCode(str, Enum):
    OUTCOME_MISSING = "OUTCOME_MISSING"
    OUTCOME_HASH_MISMATCH = "OUTCOME_HASH_MISMATCH"
    CANDIDATE_MISSING = "CANDIDATE_MISSING"
    CANDIDATE_HASH_MISMATCH = "CANDIDATE_HASH_MISMATCH"
    PREFLIGHT_MISSING = "PREFLIGHT_MISSING"
    PREFLIGHT_DRAFT_HASH_MISMATCH = "PREFLIGHT_DRAFT_HASH_MISMATCH"
    PREFLIGHT_STATUS_MISMATCH = "PREFLIGHT_STATUS_MISMATCH"
    FACTOR_DAG_HASH_MISMATCH = "FACTOR_DAG_HASH_MISMATCH"
    DRAFT_TRANSLATION_REJECTED = "DRAFT_TRANSLATION_REJECTED"
    DRAFT_REVIEW_REJECTED = "DRAFT_REVIEW_REJECTED"


@dataclass(frozen=True, kw_only=True)
class U4EvidenceAdapterRequest(CanonicalContract):
    run: MultiSpecialistRunResult
    outcomes: Mapping[str, SpecialistAgentOutcome]
    frozen_data: FrozenValidationData
    walk_forward: WalkForwardSpec
    cost_model: CostModel
    requested_lookback_sessions: int | None = None
    family_budget: int = 16
    seed: int = 0
    approver_id: str = "evidence.court.visible"
    registered_factors: tuple[RegisteredFactor, ...] = ()
    trusted_product_mode: bool = False
    SCHEMA_VERSION: ClassVar[str] = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if not isinstance(self.run, MultiSpecialistRunResult):
            fail(FailureCode.INVALID_VALUE, "run must be MultiSpecialistRunResult")
        if not isinstance(self.frozen_data, FrozenValidationData):
            fail(FailureCode.INVALID_VALUE, "frozen_data must be FrozenValidationData")
        if not isinstance(self.walk_forward, WalkForwardSpec):
            fail(FailureCode.INVALID_VALUE, "walk_forward must be WalkForwardSpec")
        if not isinstance(self.cost_model, CostModel):
            fail(FailureCode.INVALID_VALUE, "cost_model must be CostModel")
        if self.requested_lookback_sessions is not None and (
            isinstance(self.requested_lookback_sessions, bool)
            or not isinstance(self.requested_lookback_sessions, int)
            or self.requested_lookback_sessions < 1
        ):
            fail(FailureCode.INVALID_VALUE, "requested lookback must be positive")
        if isinstance(self.family_budget, bool) or not isinstance(self.family_budget, int) or self.family_budget < 1:
            fail(FailureCode.INVALID_VALUE, "family_budget must be positive")
        if isinstance(self.seed, bool) or not isinstance(self.seed, int) or self.seed < 0:
            fail(FailureCode.INVALID_VALUE, "seed must be non-negative")
        require_identifier(self.approver_id, "approver_id")
        if not isinstance(self.registered_factors, tuple) or not all(
            isinstance(item, RegisteredFactor) for item in self.registered_factors
        ):
            fail(FailureCode.INVALID_VALUE, "registered_factors must contain RegisteredFactor values")
        if not isinstance(self.trusted_product_mode, bool):
            fail(FailureCode.INVALID_VALUE, "trusted_product_mode must be boolean")
        branch_ids = {item.specialist_id for item in self.run.branches}
        frozen: dict[str, SpecialistAgentOutcome] = {}
        for specialist_id, outcome in self.outcomes.items():
            require_identifier(specialist_id, "outcomes.key")
            if specialist_id not in branch_ids:
                fail(FailureCode.INVALID_FIELD_OWNER, "outcome belongs to an unselected specialist")
            if not isinstance(outcome, SpecialistAgentOutcome) or outcome.specialist_id != specialist_id:
                fail(FailureCode.INVALID_FIELD_OWNER, "outcome specialist identity mismatch")
            frozen[specialist_id] = outcome
        object.__setattr__(self, "outcomes", MappingProxyType(dict(sorted(frozen.items()))))


@dataclass(frozen=True, kw_only=True)
class CandidateEvidenceTerminal(CanonicalContract):
    specialist_id: str
    candidate_id: str
    candidate_hash: str
    translation_hash: str
    identity_hash: str
    trial_id: str
    status: EvidenceAdapterStatus
    budget_consumed: bool
    evidence_hashes: tuple[str, ...]
    summary_hash: str | None
    visible_metrics: VisibleComparisonMetrics | None
    disposition: "EvidenceDispositionRef | None"
    failure_codes: tuple[str, ...]
    SCHEMA_VERSION: ClassVar[str] = SCHEMA_VERSION

    def __post_init__(self) -> None:
        require_identifier(self.specialist_id, "specialist_id")
        require_identifier(self.candidate_id, "candidate_id")
        for name in ("candidate_hash", "translation_hash", "identity_hash"):
            require_hash(getattr(self, name), name)
        require_identifier(self.trial_id, "trial_id")
        if not isinstance(self.status, EvidenceAdapterStatus):
            fail(FailureCode.INVALID_VALUE, "candidate terminal status is invalid")
        for value in self.evidence_hashes:
            require_hash(value, "evidence_hashes")
        if self.summary_hash is not None:
            require_hash(self.summary_hash, "summary_hash")
        for value in self.failure_codes:
            require_identifier(value, "failure_codes")
        if self.status is EvidenceAdapterStatus.OFFICIAL_VISIBLE_EVALUATED:
            if not self.budget_consumed or not self.evidence_hashes or self.visible_metrics is None:
                fail(FailureCode.INVALID_STATE, "official visible evaluation needs service-produced evidence")
        elif self.visible_metrics is not None or self.evidence_hashes:
            fail(FailureCode.AUXILIARY_MASQUERADE, "failed candidate cannot carry official evidence")
        if self.disposition is not None and self.visible_metrics is None:
            fail(FailureCode.INVALID_STATE, "disposition requires visible official evidence")


@dataclass(frozen=True, kw_only=True)
class EvidenceDispositionRef(CanonicalContract):
    """Allowlisted visible court reference; hidden-partition fields do not exist."""

    candidate_id: str
    identity_hash: str
    decision_hash: str
    evidence_hash: str
    proposer_id: str
    approver_id: str
    outcome: str
    reason_codes: tuple[str, ...]
    SCHEMA_VERSION: ClassVar[str] = SCHEMA_VERSION

    def __post_init__(self) -> None:
        require_identifier(self.candidate_id, "candidate_id")
        for name in ("identity_hash", "decision_hash", "evidence_hash"):
            require_hash(getattr(self, name), name)
        require_identifier(self.proposer_id, "proposer_id")
        require_identifier(self.approver_id, "approver_id")
        require_identifier(self.outcome, "outcome")
        if self.proposer_id == self.approver_id:
            fail(FailureCode.SELF_APPROVAL, "visible court reference cannot be self-approved")
        if self.outcome == "PROMOTE_CHALLENGER":
            fail(FailureCode.UNAUTHORIZED_CAPABILITY, "visible evidence adapter cannot promote")
        for value in self.reason_codes:
            require_identifier(value, "reason_codes")


@dataclass(frozen=True, kw_only=True)
class U4EvidenceAdapterResult(CanonicalContract):
    run_id: str
    source_run_hash: str
    frozen_data_hash: str
    terminals: tuple[CandidateEvidenceTerminal, ...]
    official_evidence_hash: str | None
    authority: str = "EXISTING_EVIDENCE_SERVICE"
    promotion_hash: None = None
    SCHEMA_VERSION: ClassVar[str] = SCHEMA_VERSION

    def __post_init__(self) -> None:
        require_identifier(self.run_id, "run_id")
        require_hash(self.source_run_hash, "source_run_hash")
        require_hash(self.frozen_data_hash, "frozen_data_hash")
        expected = tuple(sorted(self.terminals, key=lambda item: (item.specialist_id, item.candidate_id, item.candidate_hash)))
        if not expected or self.terminals != expected:
            fail(FailureCode.INVALID_STATE, "every U4 candidate needs one stable terminal record")
        keys = {(item.specialist_id, item.candidate_id, item.candidate_hash) for item in self.terminals}
        if len(keys) != len(self.terminals):
            fail(FailureCode.INVALID_VALUE, "duplicate terminal candidate")
        official = tuple(item for item in self.terminals if item.status is EvidenceAdapterStatus.OFFICIAL_VISIBLE_EVALUATED)
        if official and self.official_evidence_hash is None:
            fail(FailureCode.MISSING_REQUIRED_FIELD, "official evidence aggregate hash is missing")
        if not official and self.official_evidence_hash is not None:
            fail(FailureCode.AUXILIARY_MASQUERADE, "aggregate cannot claim absent official evidence")
        if self.official_evidence_hash is not None:
            require_hash(self.official_evidence_hash, "official_evidence_hash")
        if self.authority != "EXISTING_EVIDENCE_SERVICE" or self.promotion_hash is not None:
            fail(FailureCode.UNAUTHORIZED_CAPABILITY, "adapter has no metric or promotion authority")

    @property
    def reproducibility_hash(self) -> str:
        return self.artifact_hash


class U4OfficialEvidenceAdapter:
    """Verify U4 commits and delegate all financial truth to existing services."""

    def __init__(
        self,
        *,
        ledger_path: str | Path,
        translator: SafeCandidateTranslator | None = None,
        qlib_translator: SafeQlibCandidateTranslator | None = None,
        qlib_provider: FrozenQlibProvider | None = None,
        bridge: ResearchEvidenceBridge | None = None,
        reviewer: FactorReviewAgent | None = None,
    ) -> None:
        self.translator = translator or SafeCandidateTranslator()
        self.qlib_translator = qlib_translator or SafeQlibCandidateTranslator()
        self.qlib_provider = qlib_provider or FrozenQlibProvider()
        self.bridge = bridge or ResearchEvidenceBridge(ledger_path=ledger_path)
        self.reviewer = reviewer or FactorReviewAgent()

    def run(self, request: U4EvidenceAdapterRequest) -> U4EvidenceAdapterResult:
        identity = FrozenEvidenceInputIdentity.from_snapshot_labels(
            snapshot=request.frozen_data.snapshot,
            labels=request.frozen_data.labels,
            source_label=request.frozen_data.source_label,
        )
        allowed_fields = tuple(
            field for field in request.frozen_data.snapshot.fields
            if "future_return" not in field.lower()
        )
        branches = {item.specialist_id: item for item in request.run.branches}
        terminals: list[CandidateEvidenceTerminal] = []
        for ref in request.run.candidate_refs:
            translation, qlib_manifest, adapter_failure, family_name, draft_review, frozen_provider_hash = self._translate(
                request=request,
                ref=ref,
                branch=branches[ref.specialist_id],
                allowed_fields=allowed_fields,
            )
            proposer_id = f"research-specialist.{ref.specialist_id}"
            if proposer_id == request.approver_id:
                fail(FailureCode.SELF_APPROVAL, "specialist proposer cannot approve itself")
            validation = self.bridge.validate_candidate(CandidateEvidenceValidationRequest(
                candidate_id=ref.candidate_id,
                research_group_id=ref.specialist_id,
                family_id=_family_id(ref.specialist_id, family_name),
                proposer_id=proposer_id,
                translation=translation,
                qlib_provider_manifest=qlib_manifest,
                snapshot=request.frozen_data.snapshot,
                labels=request.frozen_data.labels,
                input_identity=identity,
                walk_forward=request.walk_forward,
                cost_model=request.cost_model,
                family_budget=request.family_budget,
                seed=request.seed,
                approver_id=request.approver_id,
                registered_factors=request.registered_factors,
                draft_review=draft_review,
                certified_qlib_only=request.trusted_product_mode,
                frozen_provider_hash=frozen_provider_hash,
                source_draft_hash=ref.candidate_hash if request.trusted_product_mode else None,
            ))
            failures = []
            if adapter_failure is not None:
                failures.append(adapter_failure.value)
            if (
                request.trusted_product_mode
                and (
                    draft_review is None
                    or draft_review.verdict.value != "ACCEPT_FOR_CERTIFICATION"
                )
            ):
                failures.append(EvidenceAdapterFailureCode.DRAFT_REVIEW_REJECTED.value)
            if not _translation_accepted(translation):
                failures.append(EvidenceAdapterFailureCode.DRAFT_TRANSLATION_REJECTED.value)
            translation_failure = _translation_failure_code(translation)
            if translation_failure is not None:
                failures.append(translation_failure)
            if _translation_accepted(translation) and validation.result_ref.failure_code is not None:
                failures.append(validation.result_ref.failure_code.value)
            if validation.disposition is not None:
                failures.extend(validation.disposition.reason_codes)
            terminals.append(CandidateEvidenceTerminal(
                specialist_id=ref.specialist_id,
                candidate_id=ref.candidate_id,
                candidate_hash=ref.candidate_hash,
                translation_hash=validation.translation_hash,
                identity_hash=validation.result_ref.identity_hash,
                trial_id=validation.result_ref.trial_id,
                status=_terminal_status(validation.result_ref.status),
                budget_consumed=validation.result_ref.budget_consumed,
                evidence_hashes=validation.result_ref.evidence_hashes,
                summary_hash=validation.result_ref.summary_hash,
                visible_metrics=validation.visible_metrics,
                disposition=_disposition_ref(validation.disposition),
                failure_codes=tuple(dict.fromkeys(failures)),
            ))
        stable = tuple(sorted(terminals, key=lambda item: (item.specialist_id, item.candidate_id, item.candidate_hash)))
        official = tuple(item for item in stable if item.status is EvidenceAdapterStatus.OFFICIAL_VISIBLE_EVALUATED)
        official_hash = None
        if official:
            official_hash = canonical_content_hash({
                "authority": "ResearchEvidenceBridge",
                "frozen_data_hash": request.frozen_data.artifact_hash,
                "records": tuple({
                    "candidate_hash": item.candidate_hash,
                    "identity_hash": item.identity_hash,
                    "evidence_hashes": item.evidence_hashes,
                    "summary_hash": item.summary_hash,
                    "metrics_hash": item.visible_metrics.artifact_hash if item.visible_metrics else None,
                    "disposition_hash": item.disposition.artifact_hash if item.disposition else None,
                } for item in official),
            })
        return U4EvidenceAdapterResult(
            run_id=request.run.run_id,
            source_run_hash=request.run.artifact_hash,
            frozen_data_hash=request.frozen_data.artifact_hash,
            terminals=stable,
            official_evidence_hash=official_hash,
        )

    def _translate(self, *, request, ref, branch, allowed_fields):
        outcome = request.outcomes.get(ref.specialist_id)
        failure: EvidenceAdapterFailureCode | None = None
        candidate = None
        family_name = ref.candidate_id
        if outcome is None:
            failure = EvidenceAdapterFailureCode.OUTCOME_MISSING
        elif branch.outcome_hash != outcome.artifact_hash:
            failure = EvidenceAdapterFailureCode.OUTCOME_HASH_MISMATCH
        else:
            candidate = next((item for item in outcome.candidates if item.candidate_id == ref.candidate_id), None)
            if candidate is None:
                failure = EvidenceAdapterFailureCode.CANDIDATE_MISSING
            elif candidate.artifact_hash != ref.candidate_hash:
                failure = EvidenceAdapterFailureCode.CANDIDATE_HASH_MISMATCH
            else:
                family_name = candidate.family_id
                preflight = next((item for item in outcome.preflight_results if item.candidate_id == ref.candidate_id), None)
                if preflight is None:
                    failure = EvidenceAdapterFailureCode.PREFLIGHT_MISSING
                elif preflight.draft_hash != candidate.artifact_hash:
                    failure = EvidenceAdapterFailureCode.PREFLIGHT_DRAFT_HASH_MISMATCH
                elif preflight.status.value != ref.preflight_status:
                    failure = EvidenceAdapterFailureCode.PREFLIGHT_STATUS_MISMATCH
                elif preflight.factor_dag_hash != ref.factor_dag_hash:
                    failure = EvidenceAdapterFailureCode.FACTOR_DAG_HASH_MISMATCH
                elif preflight.formula_program_hash != ref.formula_program_hash:
                    failure = EvidenceAdapterFailureCode.FACTOR_DAG_HASH_MISMATCH
                elif preflight.formula_program_kind != ref.formula_program_kind:
                    failure = EvidenceAdapterFailureCode.FACTOR_DAG_HASH_MISMATCH
                elif preflight.formula_manifest_hash != ref.formula_manifest_hash:
                    failure = EvidenceAdapterFailureCode.FACTOR_DAG_HASH_MISMATCH
        if failure is not None or candidate is None:
            if request.trusted_product_mode:
                return (
                    self._forced_qlib_rejection(
                        request=request,
                        ref=ref,
                        allowed_fields=allowed_fields,
                    ),
                    None,
                    failure or EvidenceAdapterFailureCode.CANDIDATE_MISSING,
                    family_name,
                    None,
                    canonical_content_hash(
                        {
                            "schema": "alphaforge.frozen-qlib-provider-input/v1",
                            "snapshot_hash": request.frozen_data.snapshot.artifact_hash,
                            "allowed_fields": allowed_fields,
                            "source_label": request.frozen_data.source_label,
                        }
                    ),
                )
            return _forced_rejection(ref.candidate_hash, failure or EvidenceAdapterFailureCode.CANDIDATE_MISSING), None, failure, family_name, None, None
        if request.trusted_product_mode and ref.preflight_status != "ACCEPTED":
            return (
                self._forced_qlib_rejection(
                    request=request,
                    ref=ref,
                    allowed_fields=allowed_fields,
                ),
                None,
                EvidenceAdapterFailureCode.PREFLIGHT_STATUS_MISMATCH,
                family_name,
                None,
                canonical_content_hash(
                    {
                        "schema": "alphaforge.frozen-qlib-provider-input/v1",
                        "snapshot_hash": request.frozen_data.snapshot.artifact_hash,
                        "allowed_fields": allowed_fields,
                        "source_label": request.frozen_data.source_label,
                    }
                ),
            )
        if request.trusted_product_mode:
            translation = self.qlib_translator.translate(QlibCandidateDraftEnvelope(
                campaign_hash=request.run.campaign_hash,
                campaign_state=CampaignState.CONFIRMED,
                group_id=ref.specialist_id,
                candidate_id=ref.candidate_id,
                expression=candidate.expression,
                confirmed_field_whitelist=allowed_fields,
                requested_lookback_sessions=request.requested_lookback_sessions,
                source_draft_hash=candidate.artifact_hash,
            ))
            review_preflight = _qlib_review_preflight(candidate, translation)
            draft_review = self.reviewer.review(FactorReviewRequest(
                proposer_id=f"research-specialist.{ref.specialist_id}",
                reviewer_id="critic.factor-review",
                candidate=candidate,
                preflight=review_preflight,
                available_fields=allowed_fields,
                policy=FactorReviewPolicy(),
            ))
            provider_input_hash = canonical_content_hash({
                "schema": "alphaforge.frozen-qlib-provider-input/v1",
                "snapshot_hash": request.frozen_data.snapshot.artifact_hash,
                "allowed_fields": allowed_fields,
                "source_label": request.frozen_data.source_label,
            })
            qlib_manifest = None
            if translation.status is QlibCandidateTranslationStatus.CERTIFIED:
                try:
                    qlib_manifest = self.qlib_provider.from_market_snapshot(
                        request.frozen_data.snapshot,
                        dataset_card=_dataset_card(request.frozen_data),
                        requirement=QlibDataRequirement(
                            required_fields=translation.required_fields,
                            lookback_bars=translation.maximum_lookback or 0,
                            warmup_bars=translation.maximum_lookback or 0,
                        ),
                    )
                except FrozenQlibProviderError:
                    failure = EvidenceAdapterFailureCode.DRAFT_TRANSLATION_REJECTED
            return (
                translation,
                qlib_manifest,
                failure,
                family_name,
                draft_review,
                qlib_manifest.manifest_hash if qlib_manifest is not None else provider_input_hash,
            )
        if ref.formula_program_kind == "CERTIFIED_QLIB_EXPRESSION":
            translation = self.qlib_translator.translate(QlibCandidateDraftEnvelope(
                campaign_hash=request.run.campaign_hash,
                campaign_state=CampaignState.CONFIRMED,
                group_id=ref.specialist_id,
                candidate_id=ref.candidate_id,
                expression=candidate.expression,
                confirmed_field_whitelist=allowed_fields,
                requested_lookback_sessions=request.requested_lookback_sessions,
                source_draft_hash=candidate.artifact_hash,
            ))
            if (
                translation.status is not QlibCandidateTranslationStatus.CERTIFIED
                or translation.program_hash != ref.formula_program_hash
                or translation.manifest_hash != ref.formula_manifest_hash
            ):
                failure = EvidenceAdapterFailureCode.FACTOR_DAG_HASH_MISMATCH
                return _forced_rejection(ref.candidate_hash, failure), None, failure, family_name, None, None
            try:
                qlib_manifest = self.qlib_provider.from_market_snapshot(
                    request.frozen_data.snapshot,
                    dataset_card=_dataset_card(request.frozen_data),
                    requirement=QlibDataRequirement(
                        required_fields=translation.required_fields,
                        lookback_bars=translation.maximum_lookback or 0,
                        warmup_bars=translation.maximum_lookback or 0,
                    ),
                )
            except FrozenQlibProviderError:
                failure = EvidenceAdapterFailureCode.DRAFT_TRANSLATION_REJECTED
                return _forced_rejection(ref.candidate_hash, failure), None, failure, family_name, None, None
            return translation, qlib_manifest, None, family_name, None, None
        translation = self.translator.translate(CandidateDraftEnvelope(
            campaign_hash=request.run.campaign_hash,
            group_id=ref.specialist_id,
            candidate_id=ref.candidate_id,
            expression=candidate.expression,
            requested_lookback_sessions=request.requested_lookback_sessions,
            declared_lookback_sessions=candidate.lookback_sessions,
            allowed_fields=allowed_fields,
            source_draft_hash=candidate.artifact_hash,
        ))
        if ref.preflight_status == "ACCEPTED" and (
            translation.status is not CandidateTranslationStatus.ACCEPTED
            or translation.factor_dag_hash != ref.factor_dag_hash
        ):
            failure = EvidenceAdapterFailureCode.FACTOR_DAG_HASH_MISMATCH
            translation = _forced_rejection(ref.candidate_hash, failure)
        elif ref.preflight_status != "ACCEPTED" and translation.status is CandidateTranslationStatus.ACCEPTED:
            failure = EvidenceAdapterFailureCode.PREFLIGHT_STATUS_MISMATCH
            translation = _forced_rejection(ref.candidate_hash, failure)
        return translation, None, failure, family_name, None, None

    def _forced_qlib_rejection(self, *, request, ref, allowed_fields) -> QlibCandidateTranslationResult:
        """Represent preflight-missing product candidates as Qlib terminal rejections.

        Trusted product evaluation is Qlib-expression only. A candidate that
        already failed preflight still has to consume its trial budget and
        produce a stable terminal record; it must not re-enter through the
        legacy FactorDAG rejection helper because that would abort the whole
        product batch before accepted siblings can be evaluated.
        """

        return QlibCandidateTranslationResult(
            draft_hash=ref.candidate_hash,
            candidate_id=ref.candidate_id,
            confirmed_field_whitelist=tuple(sorted(allowed_fields)),
            status=QlibCandidateTranslationStatus.UNSUPPORTED,
            policy_hash=canonical_content_hash(
                {
                    "schema": "alphaforge.forced-qlib-rejection-policy/v1",
                    "campaign_hash": request.run.campaign_hash,
                    "candidate_hash": ref.candidate_hash,
                    "trusted_product_mode": True,
                }
            ),
            manifest_hash=self.qlib_translator.manifest.manifest_hash,
            failure_code=QlibCandidateFailureCode.EXPRESSION_REJECTED,
            expression_failure_code=None,
            message="candidate did not have a certified Qlib expression in the trusted product path",
        )


def _qlib_review_preflight(candidate, translation: QlibCandidateTranslationResult) -> CandidatePreflightResult:
    if translation.status is QlibCandidateTranslationStatus.CERTIFIED:
        return CandidatePreflightResult(
            candidate_id=candidate.candidate_id,
            draft_hash=candidate.artifact_hash,
            status=PreflightStatus.ACCEPTED,
            normalized_expression=translation.canonical_expression,
            formula_program_hash=translation.program_hash,
            formula_program_kind="CERTIFIED_QLIB_EXPRESSION",
            formula_manifest_hash=translation.manifest_hash,
            message="Qlib certification accepted for independent DRAFT review.",
        )
    return CandidatePreflightResult(
        candidate_id=candidate.candidate_id,
        draft_hash=candidate.artifact_hash,
        status=PreflightStatus.REVISE,
        failure_code=(
            translation.expression_failure_code.value
            if translation.expression_failure_code is not None
            else translation.failure_code.value
        ),
        message=translation.message,
    )


def _forced_rejection(draft_hash: str, code: EvidenceAdapterFailureCode) -> CandidateTranslationResult:
    return CandidateTranslationResult(
        draft_hash=draft_hash,
        status=CandidateTranslationStatus.UNSUPPORTED,
        normalized_expression=None,
        factor_dag_hash=None,
        factor_dag_json=None,
        failure_code=code.value,
        message="committed U4 candidate could not be losslessly adapted",
    )


def _translation_accepted(
    value: CandidateTranslationResult | QlibCandidateTranslationResult,
) -> bool:
    if isinstance(value, CandidateTranslationResult):
        return value.status is CandidateTranslationStatus.ACCEPTED
    return value.status is QlibCandidateTranslationStatus.CERTIFIED


def _translation_failure_code(
    value: CandidateTranslationResult | QlibCandidateTranslationResult,
) -> str | None:
    if value.failure_code is not None:
        return value.failure_code.value if isinstance(value.failure_code, Enum) else value.failure_code
    if isinstance(value, QlibCandidateTranslationResult) and value.expression_failure_code is not None:
        return value.expression_failure_code.value
    return None


def _dataset_card(frozen_data: FrozenValidationData) -> DatasetCard:
    snapshot = frozen_data.snapshot
    universe = tuple(
        sorted({symbol for members in snapshot.historical_universe.values() for symbol in members})
    )
    if frozen_data.row_provenance:
        published_at = max(item.published_at for item in frozen_data.row_provenance)
        available_at = max(item.available_at for item in frozen_data.row_provenance)
    else:
        published_at = snapshot.decision_time
        available_at = snapshot.decision_time
    return DatasetCard(
        dataset_id=snapshot.dataset_id,
        source=frozen_data.source_label,
        universe=universe,
        horizon="daily",
        decision_time=snapshot.decision_time,
        availability_assumptions=(
            "available_at <= decision_time",
            "historical universe membership is resolved per session",
        ),
        fields=snapshot.fields,
        event_time=snapshot.start_time,
        published_at=published_at,
        available_at=available_at,
        snapshot_hash=snapshot.artifact_hash,
        record_count=len(snapshot.rows),
        produced_by=Component.DATA_SERVICE,
    )


def _disposition_ref(value) -> EvidenceDispositionRef | None:
    if value is None:
        return None
    return EvidenceDispositionRef(
        candidate_id=value.candidate_id,
        identity_hash=value.identity_hash,
        decision_hash=value.decision_hash,
        evidence_hash=value.evidence_hash,
        proposer_id=value.proposer_id,
        approver_id=value.approver_id,
        outcome=value.outcome.value,
        reason_codes=value.reason_codes,
    )


def _family_id(specialist_id: str, family_name: str) -> str:
    digest = canonical_content_hash((specialist_id, family_name)).split(":", 1)[1][:12]
    return f"u6-{specialist_id}-{digest}"


def _terminal_status(status: ValidationBridgeStatus) -> EvidenceAdapterStatus:
    if status in {ValidationBridgeStatus.OFFICIAL_EVIDENCE_READY, ValidationBridgeStatus.IDEMPOTENT_REPLAYED}:
        return EvidenceAdapterStatus.OFFICIAL_VISIBLE_EVALUATED
    if status is ValidationBridgeStatus.PARSE_REJECTED:
        return EvidenceAdapterStatus.TRANSLATION_REJECTED
    if status is ValidationBridgeStatus.PIT_REJECTED:
        return EvidenceAdapterStatus.PIT_REJECTED
    if status is ValidationBridgeStatus.EVALUATION_REJECTED:
        return EvidenceAdapterStatus.EVALUATION_REJECTED
    if status is ValidationBridgeStatus.BUDGET_EXHAUSTED:
        return EvidenceAdapterStatus.BUDGET_BLOCKED
    return EvidenceAdapterStatus.LEDGER_BLOCKED


__all__ = [
    "CandidateEvidenceTerminal",
    "EvidenceDispositionRef",
    "EvidenceAdapterFailureCode",
    "EvidenceAdapterStatus",
    "U4EvidenceAdapterRequest",
    "U4EvidenceAdapterResult",
    "U4OfficialEvidenceAdapter",
]
