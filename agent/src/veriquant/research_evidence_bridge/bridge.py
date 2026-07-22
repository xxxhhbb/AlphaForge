"""Official-evidence bridge for accepted AlphaForge research candidates.

The bridge is deliberately narrow:

* it accepts a candidate translation result and frozen point-in-time inputs;
* it reserves Trial Ledger budget before any parse/PIT/evaluation action;
* it executes either a legacy restricted FactorDAG or a certified Qlib
  expression through the common score boundary, then runs the deterministic
  Evidence Service and returns only visible train/validation metrics;
* it never owns promotion, portfolio, risk, execution, ledger mutation beyond
  Trial Ledger attempt events, or sealed-test exposure.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Mapping, Sequence

from ..candidate_translation import CandidateTranslationResult, CandidateTranslationStatus
from ..qlib_candidate_translation import (
    QlibCandidateTranslationResult,
    QlibCandidateTranslationStatus,
)
from ..contracts import CanonicalContract, ContractValidationError, FailureCode
from ..contracts.base import (
    fail,
    freeze_mapping,
    require_hash,
    require_identifier,
    require_text,
)
from ..data import FieldRole, MarketSnapshot, canonical_content_hash
from ..evidence.ledger import (
    LedgerIntegrityError,
    TrialLedger,
    TrialOutcome,
    TrialStatus,
)
from ..evidence.critics import EvidenceCourt, EvidenceCourtPolicy
from ..evidence.models import (
    CostModel,
    CourtOutcome,
    EvaluationRow,
    ExperimentIdentity,
    RegisteredFactor,
    ResearchSummary,
    WalkForwardSpec,
)
from ..evidence.service import EvidenceService
from ..factors import CompiledFactorDAG, parse_compile_factor_dag
from ..formula_execution import (
    LEGACY_POLICY_HASH,
    LEGACY_RUNTIME_HASH,
    CertifiedQlibProgramBundle,
    FormulaExecutionRequest,
    FormulaExecutionService,
    FormulaProgramIdentity,
    FormulaProgramKind,
    LegacyFactorDAGBackend,
    configured_isolated_qlib_backend,
    evaluation_rows_from_scores,
)
from ..frozen_qlib_provider import FrozenQlibProviderManifest
from ..factor_review import FactorReviewDecision, FactorReviewVerdict
from ..qlib_operator_worker.api import QlibRuntimeIdentity


BRIDGE_SCHEMA_VERSION = "alphaforge.research-evidence-bridge/v2"
BRIDGE_CODE_HASH = canonical_content_hash(
    {
        "package": "research_evidence_bridge",
        "schema": BRIDGE_SCHEMA_VERSION,
        "trusted_path": (
            "CandidateTranslationResult",
            "FormulaProgramIdentity",
            "FactorScoreArtifact",
            "MarketSnapshot",
            "EvidenceService.evaluate_visible_walk_forward",
            "TrialLedger",
        ),
        "sealed_exposure": "none",
    }
)
_FORBIDDEN_SOURCE_LABELS = frozenset({"OFFICIAL_TUSHARE"})
_VISIBLE_PREFIXES = ("train", "validation")


class ValidationBridgeStatus(str, Enum):
    OFFICIAL_EVIDENCE_READY = "OFFICIAL_EVIDENCE_READY"
    PARSE_REJECTED = "PARSE_REJECTED"
    PIT_REJECTED = "PIT_REJECTED"
    EVALUATION_REJECTED = "EVALUATION_REJECTED"
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"
    LEDGER_BLOCKED = "LEDGER_BLOCKED"
    IDEMPOTENT_REPLAYED = "IDEMPOTENT_REPLAYED"


class ValidationBridgeFailureCode(str, Enum):
    NON_ACCEPTED_TRANSLATION = "NON_ACCEPTED_TRANSLATION"
    FACTOR_DAG_RECONSTRUCTION_FAILED = "FACTOR_DAG_RECONSTRUCTION_FAILED"
    SNAPSHOT_FIELD_MISSING = "SNAPSHOT_FIELD_MISSING"
    LABEL_MISSING = "LABEL_MISSING"
    VOLUME_MISSING = "VOLUME_MISSING"
    INSUFFICIENT_OBSERVATIONS = "INSUFFICIENT_OBSERVATIONS"
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"
    EVALUATION_FAILED = "EVALUATION_FAILED"
    LEDGER_CORRUPTION = "LEDGER_CORRUPTION"
    DRAFT_REVIEW_REJECTED = "DRAFT_REVIEW_REJECTED"


@dataclass(frozen=True, kw_only=True)
class FrozenEvidenceInputIdentity(CanonicalContract):
    """Content identity for one frozen market snapshot and its labels."""

    snapshot_hash: str
    universe_hash: str
    availability_hash: str
    label_hash: str
    code_hash: str = BRIDGE_CODE_HASH
    source_label: str = "DETERMINISTIC_OFFLINE_FIXTURE"

    SCHEMA_VERSION = BRIDGE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        for name in (
            "snapshot_hash",
            "universe_hash",
            "availability_hash",
            "label_hash",
            "code_hash",
        ):
            require_hash(getattr(self, name), name)
        require_text(self.source_label, "source_label")
        if self.source_label in _FORBIDDEN_SOURCE_LABELS:
            fail(
                FailureCode.INVALID_VALUE,
                "approved proxy or fixture data must not be labeled as official Tushare",
                source_label=self.source_label,
            )

    @classmethod
    def from_snapshot_labels(
        cls,
        *,
        snapshot: MarketSnapshot,
        labels: Mapping[str, float],
        source_label: str = "DETERMINISTIC_OFFLINE_FIXTURE",
        code_hash: str = BRIDGE_CODE_HASH,
    ) -> "FrozenEvidenceInputIdentity":
        return cls(
            snapshot_hash=snapshot.artifact_hash,
            universe_hash=canonical_content_hash(snapshot.historical_universe),
            availability_hash=canonical_content_hash(
                {
                    "decision_time": snapshot.decision_time,
                    "coverage_by_field": dict(snapshot.coverage_by_field),
                    "fields": snapshot.fields,
                }
            ),
            label_hash=canonical_content_hash(dict(sorted(labels.items()))),
            code_hash=code_hash,
            source_label=source_label,
        )


@dataclass(frozen=True, kw_only=True)
class CandidateEvidenceValidationRequest(CanonicalContract):
    """One selected candidate accounting/evaluation request."""

    candidate_id: str
    research_group_id: str
    family_id: str
    proposer_id: str
    translation: CandidateTranslationResult | QlibCandidateTranslationResult
    snapshot: MarketSnapshot
    labels: Mapping[str, float]
    input_identity: FrozenEvidenceInputIdentity
    walk_forward: WalkForwardSpec
    cost_model: CostModel
    family_budget: int
    seed: int = 0
    approver_id: str = "evidence.court"
    court_policy: EvidenceCourtPolicy | None = None
    market_regimes: Mapping[str, str] | None = None
    registered_factors: tuple[RegisteredFactor, ...] = ()
    qlib_provider_manifest: FrozenQlibProviderManifest | None = None
    draft_review: FactorReviewDecision | None = None
    certified_qlib_only: bool = False
    frozen_provider_hash: str | None = None
    source_draft_hash: str | None = None

    SCHEMA_VERSION = BRIDGE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        for value, name in (
            (self.candidate_id, "candidate_id"),
            (self.research_group_id, "research_group_id"),
            (self.family_id, "family_id"),
            (self.proposer_id, "proposer_id"),
        ):
            require_identifier(value, name)
        if not isinstance(
            self.translation,
            (CandidateTranslationResult, QlibCandidateTranslationResult),
        ):
            fail(FailureCode.INVALID_VALUE, "translation must be a supported formula translation")
        if isinstance(self.translation, QlibCandidateTranslationResult):
            if (
                self.translation.status is QlibCandidateTranslationStatus.CERTIFIED
                and not isinstance(self.qlib_provider_manifest, FrozenQlibProviderManifest)
                and not self.certified_qlib_only
            ):
                fail(FailureCode.MISSING_REQUIRED_FIELD, "certified Qlib candidate requires PIT provider manifest")
        elif self.qlib_provider_manifest is not None:
            fail(FailureCode.INVALID_VALUE, "legacy FactorDAG request cannot carry a Qlib provider manifest")
        if not isinstance(self.certified_qlib_only, bool):
            fail(FailureCode.INVALID_VALUE, "certified_qlib_only must be boolean")
        if self.certified_qlib_only:
            if not isinstance(self.translation, QlibCandidateTranslationResult):
                fail(FailureCode.UNAUTHORIZED_CAPABILITY, "new product trials require Qlib expression translation")
            if self.frozen_provider_hash is None:
                fail(FailureCode.MISSING_REQUIRED_FIELD, "new product trial requires frozen provider identity")
            require_hash(self.frozen_provider_hash, "frozen_provider_hash")
            if self.source_draft_hash is None:
                fail(FailureCode.MISSING_REQUIRED_FIELD, "new product trial requires source draft identity")
            require_hash(self.source_draft_hash, "source_draft_hash")
            if self.draft_review is not None:
                if not isinstance(self.draft_review, FactorReviewDecision):
                    fail(FailureCode.INVALID_VALUE, "draft_review must be FactorReviewDecision")
                if (
                    self.draft_review.candidate_hash != self.source_draft_hash
                    or self.draft_review.proposer_id != self.proposer_id
                ):
                    fail(FailureCode.INVALID_HASH, "draft review does not bind candidate and proposer identity")
        elif self.draft_review is not None or self.frozen_provider_hash is not None or self.source_draft_hash is not None:
            fail(FailureCode.INVALID_STATE, "legacy compatibility request cannot carry product review identity")
        if not isinstance(self.snapshot, MarketSnapshot):
            fail(FailureCode.INVALID_VALUE, "snapshot must be MarketSnapshot")
        if not isinstance(self.input_identity, FrozenEvidenceInputIdentity):
            fail(FailureCode.INVALID_VALUE, "input_identity must be FrozenEvidenceInputIdentity")
        if self.input_identity.snapshot_hash != self.snapshot.artifact_hash:
            fail(FailureCode.INVALID_HASH, "input identity does not match frozen snapshot")
        if self.input_identity.label_hash != canonical_content_hash(dict(sorted(self.labels.items()))):
            fail(FailureCode.INVALID_HASH, "input identity does not match labels")
        if not isinstance(self.walk_forward, WalkForwardSpec):
            fail(FailureCode.INVALID_VALUE, "walk_forward must be WalkForwardSpec")
        if not isinstance(self.cost_model, CostModel):
            fail(FailureCode.INVALID_VALUE, "cost_model must be CostModel")
        if isinstance(self.family_budget, bool) or not isinstance(self.family_budget, int) or self.family_budget < 1:
            fail(FailureCode.INVALID_VALUE, "family_budget must be positive")
        if isinstance(self.seed, bool) or not isinstance(self.seed, int) or self.seed < 0:
            fail(FailureCode.INVALID_VALUE, "seed must be a non-negative integer")
        require_identifier(self.approver_id, "approver_id")
        if self.approver_id == self.proposer_id:
            fail(FailureCode.SELF_APPROVAL, "visible research disposition approver must be independent")
        if self.court_policy is not None and not isinstance(self.court_policy, EvidenceCourtPolicy):
            fail(FailureCode.INVALID_VALUE, "court_policy must be EvidenceCourtPolicy")
        frozen_labels: dict[str, float] = {}
        for key, value in self.labels.items():
            require_text(key, "labels.key")
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
                fail(FailureCode.INVALID_VALUE, "labels must be finite numeric values")
            frozen_labels[key] = float(value)
        object.__setattr__(self, "labels", MappingProxyType(dict(sorted(frozen_labels.items()))))
        regimes = self.market_regimes or {}
        frozen_regimes: dict[str, str] = {}
        for key, value in regimes.items():
            require_text(key, "market_regimes.key")
            require_text(value, "market_regimes.value")
            frozen_regimes[key] = value
        object.__setattr__(self, "market_regimes", MappingProxyType(dict(sorted(frozen_regimes.items()))))
        if not all(isinstance(item, RegisteredFactor) for item in self.registered_factors):
            fail(FailureCode.INVALID_VALUE, "registered_factors must contain RegisteredFactor values")


@dataclass(frozen=True, kw_only=True)
class VisibleComparisonMetrics(CanonicalContract):
    candidate_id: str
    identity_hash: str
    metrics: Mapping[str, float]
    metric_source_hash: str
    evidence_class: str = "OFFICIAL_EVIDENCE"

    SCHEMA_VERSION = BRIDGE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        require_identifier(self.candidate_id, "candidate_id")
        require_hash(self.identity_hash, "identity_hash")
        require_hash(self.metric_source_hash, "metric_source_hash")
        if self.evidence_class != "OFFICIAL_EVIDENCE":
            fail(FailureCode.AUXILIARY_MASQUERADE, "visible comparison metrics must come from official evidence")
        frozen: dict[str, float] = {}
        for key, value in self.metrics.items():
            require_text(key, "metrics.key")
            if key.startswith("sealed") or ".sealed" in key:
                fail(FailureCode.UNAUTHORIZED_CAPABILITY, "sealed metrics cannot be exposed")
            if not any(key.startswith(prefix + "_") for prefix in _VISIBLE_PREFIXES):
                fail(FailureCode.UNAUTHORIZED_CAPABILITY, "metric key is not visible-allowlisted", metric=key)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
                fail(FailureCode.INVALID_VALUE, "visible metrics must be finite numeric values")
            frozen[key] = float(value)
        object.__setattr__(self, "metrics", MappingProxyType(dict(sorted(frozen.items()))))


@dataclass(frozen=True, kw_only=True)
class ValidationResultRef(CanonicalContract):
    candidate_id: str
    family_id: str
    trial_id: str
    identity_hash: str
    status: ValidationBridgeStatus
    budget_consumed: bool
    ledger_last_hash: str | None
    terminal_event_hash: str | None
    evidence_hashes: tuple[str, ...] = ()
    summary_hash: str | None = None
    failure_code: ValidationBridgeFailureCode | None = None
    failure_message: str = ""

    SCHEMA_VERSION = BRIDGE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        for value, name in (
            (self.candidate_id, "candidate_id"),
            (self.family_id, "family_id"),
            (self.trial_id, "trial_id"),
        ):
            require_identifier(value, name)
        require_hash(self.identity_hash, "identity_hash")
        if not isinstance(self.status, ValidationBridgeStatus):
            fail(FailureCode.INVALID_VALUE, "status is invalid")
        if self.ledger_last_hash is not None:
            require_hash(self.ledger_last_hash, "ledger_last_hash")
        if self.terminal_event_hash is not None:
            require_hash(self.terminal_event_hash, "terminal_event_hash")
        if self.summary_hash is not None:
            require_hash(self.summary_hash, "summary_hash")
        for value in self.evidence_hashes:
            require_hash(value, "evidence_hashes")
        if self.status is ValidationBridgeStatus.OFFICIAL_EVIDENCE_READY:
            if not self.budget_consumed or not self.evidence_hashes or self.failure_code is not None:
                fail(FailureCode.INVALID_STATE, "successful validation requires evidence and no failure")
        elif self.status is not ValidationBridgeStatus.IDEMPOTENT_REPLAYED and self.failure_code is None:
            fail(FailureCode.MISSING_REQUIRED_FIELD, "failed validation requires failure_code")


@dataclass(frozen=True, kw_only=True)
class VisibleResearchDisposition(CanonicalContract):
    candidate_id: str
    identity_hash: str
    decision_hash: str
    evidence_hash: str
    proposer_id: str
    approver_id: str
    outcome: CourtOutcome
    reason_codes: tuple[str, ...]
    sealed_passed: None = None

    SCHEMA_VERSION = BRIDGE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        require_identifier(self.candidate_id, "candidate_id")
        require_hash(self.identity_hash, "identity_hash")
        require_hash(self.decision_hash, "decision_hash")
        require_hash(self.evidence_hash, "evidence_hash")
        require_identifier(self.proposer_id, "proposer_id")
        require_identifier(self.approver_id, "approver_id")
        if self.proposer_id == self.approver_id:
            fail(FailureCode.SELF_APPROVAL, "visible disposition approver must be independent")
        if self.outcome is CourtOutcome.PROMOTE_CHALLENGER:
            fail(FailureCode.UNAUTHORIZED_CAPABILITY, "visible disposition cannot promote a challenger")
        if self.outcome not in {CourtOutcome.REJECT, CourtOutcome.REVISE, CourtOutcome.VALIDATED_RESEARCH}:
            fail(FailureCode.INVALID_VALUE, "unsupported visible research outcome")
        if self.sealed_passed is not None:
            fail(FailureCode.UNAUTHORIZED_CAPABILITY, "sealed outcome cannot enter visible disposition")
        if not self.reason_codes:
            fail(FailureCode.INVALID_VALUE, "visible disposition requires reason codes")
        for code in self.reason_codes:
            require_text(code, "reason_codes")


@dataclass(frozen=True, kw_only=True)
class CandidateEvidenceValidationResult(CanonicalContract):
    request_hash: str
    translation_hash: str
    result_ref: ValidationResultRef
    visible_metrics: VisibleComparisonMetrics | None
    disposition: VisibleResearchDisposition | None = None

    SCHEMA_VERSION = BRIDGE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        require_hash(self.request_hash, "request_hash")
        require_hash(self.translation_hash, "translation_hash")
        if not isinstance(self.result_ref, ValidationResultRef):
            fail(FailureCode.INVALID_VALUE, "result_ref must be ValidationResultRef")
        if self.visible_metrics is not None and not isinstance(self.visible_metrics, VisibleComparisonMetrics):
            fail(FailureCode.INVALID_VALUE, "visible_metrics must be VisibleComparisonMetrics")
        if self.disposition is not None and not isinstance(self.disposition, VisibleResearchDisposition):
            fail(FailureCode.INVALID_VALUE, "disposition must be VisibleResearchDisposition")


class ResearchEvidenceBridge:
    """Adapter from accepted DRAFT candidates to deterministic official evidence."""

    def __init__(
        self,
        *,
        ledger_path: str | Path,
        evidence_service: EvidenceService | None = None,
        ledger: TrialLedger | None = None,
        formula_execution_service: FormulaExecutionService | None = None,
    ) -> None:
        self.ledger = ledger or TrialLedger(ledger_path)
        self.evidence_service = evidence_service or EvidenceService()
        if formula_execution_service is None:
            backends = [LegacyFactorDAGBackend()]
            isolated_qlib = configured_isolated_qlib_backend()
            if isolated_qlib is not None:
                backends.append(isolated_qlib)
            formula_execution_service = FormulaExecutionService(*backends)
        self.formula_execution_service = formula_execution_service

    def validate_candidates(
        self, requests: Sequence[CandidateEvidenceValidationRequest]
    ) -> tuple[CandidateEvidenceValidationResult, ...]:
        return tuple(self.validate_candidate(request) for request in requests)

    def validate_candidate(
        self, request: CandidateEvidenceValidationRequest
    ) -> CandidateEvidenceValidationResult:
        identity = self._identity_for(request)
        trial_id = self._trial_id(request, identity)
        try:
            self.ledger.create_family(request.family_id, request.family_budget)
            identity_hash = self.ledger.register_identity(identity)
            reserve = self.ledger.reserve_attempt(
                family_id=request.family_id,
                trial_id=trial_id,
                identity_hash=identity_hash,
                proposer_id=request.proposer_id,
                units=1,
            )
            replay = self.ledger.replay()
        except LedgerIntegrityError as exc:
            return self._blocked_by_ledger(request, identity, str(exc))
        except ContractValidationError as exc:
            if exc.code is FailureCode.INVALID_VALUE:
                return self._budget_exhausted(request, identity, trial_id, str(exc))
            return self._blocked_by_ledger(request, identity, str(exc))

        existing = replay.trials.get(reserve.trial_id)
        if existing is not None and existing.status is TrialStatus.TERMINAL:
            return self._replay_terminal(request, identity, existing)

        try:
            if existing is None or existing.status is TrialStatus.RESERVED:
                self.ledger.start_attempt(reserve.trial_id)
        except (LedgerIntegrityError, ContractValidationError) as exc:
            return self._blocked_by_ledger(request, identity, str(exc))

        if request.certified_qlib_only and (
            request.draft_review is None
            or request.draft_review.verdict is not FactorReviewVerdict.ACCEPT_FOR_CERTIFICATION
        ):
            return self._commit_failure(
                request,
                identity,
                reserve.trial_id,
                TrialOutcome.PARSE_REJECTION,
                FailureCode.INVALID_STATE,
                ValidationBridgeStatus.PARSE_REJECTED,
                ValidationBridgeFailureCode.DRAFT_REVIEW_REJECTED,
                "independent DRAFT review did not accept candidate for certification",
            )

        if not _translation_is_accepted(request.translation):
            return self._commit_failure(
                request,
                identity,
                reserve.trial_id,
                TrialOutcome.PARSE_REJECTION,
                FailureCode.INVALID_FACTOR_DAG,
                ValidationBridgeStatus.PARSE_REJECTED,
                ValidationBridgeFailureCode.NON_ACCEPTED_TRANSLATION,
                request.translation.message or "candidate translation was not accepted",
            )

        try:
            program = self._reconstruct_program(request)
        except (ContractValidationError, ValueError, TypeError, json.JSONDecodeError) as exc:
            return self._commit_failure(
                request,
                identity,
                reserve.trial_id,
                TrialOutcome.PARSE_REJECTION,
                FailureCode.INVALID_FACTOR_DAG,
                ValidationBridgeStatus.PARSE_REJECTED,
                ValidationBridgeFailureCode.FACTOR_DAG_RECONSTRUCTION_FAILED,
                str(exc),
            )

        try:
            rows = self._evaluation_rows(request, program)
        except ContractValidationError as exc:
            return self._commit_failure(
                request,
                identity,
                reserve.trial_id,
                TrialOutcome.PIT_REJECTION,
                exc.code,
                ValidationBridgeStatus.PIT_REJECTED,
                _pit_failure_code(exc),
                str(exc),
            )

        try:
            visible = self.evidence_service.evaluate_visible_walk_forward(
                identity=identity,
                rows=rows,
                spec=request.walk_forward,
                cost_model=request.cost_model,
                registered_factors=request.registered_factors,
            )
            summary = self.evidence_service.research_summary(identity, visible)
        except ContractValidationError as exc:
            return self._commit_failure(
                request,
                identity,
                reserve.trial_id,
                TrialOutcome.EVALUATION_REJECTION,
                exc.code,
                ValidationBridgeStatus.EVALUATION_REJECTED,
                _evaluation_failure_code(exc),
                str(exc),
            )

        try:
            terminal = self.ledger.commit_outcome(reserve.trial_id, outcome=TrialOutcome.SUCCESS)
            replay_after = self.ledger.replay()
        except (LedgerIntegrityError, ContractValidationError) as exc:
            return self._blocked_by_ledger(request, identity, str(exc))
        metrics = _visible_metrics(request.candidate_id, identity, summary)
        disposition = _visible_disposition(request, visible[-1].validation)
        evidence_hashes = tuple(
            evidence.artifact_hash
            for fold in visible
            for evidence in (fold.train, fold.validation)
        )
        return CandidateEvidenceValidationResult(
            request_hash=request.artifact_hash,
            translation_hash=request.translation.artifact_hash,
            visible_metrics=metrics,
            disposition=disposition,
            result_ref=ValidationResultRef(
                candidate_id=request.candidate_id,
                family_id=request.family_id,
                trial_id=reserve.trial_id,
                identity_hash=identity.identity_hash,
                status=ValidationBridgeStatus.OFFICIAL_EVIDENCE_READY,
                budget_consumed=True,
                ledger_last_hash=replay_after.last_hash,
                terminal_event_hash=terminal.event_hash,
                evidence_hashes=evidence_hashes,
                summary_hash=summary.artifact_hash,
            ),
        )

    @staticmethod
    def _identity_for(request: CandidateEvidenceValidationRequest) -> ExperimentIdentity:
        factor_hash = _translation_program_hash(request.translation)
        qlib = request.translation if isinstance(request.translation, QlibCandidateTranslationResult) else None
        return ExperimentIdentity(
            experiment_id=f"research-evidence.{request.candidate_id}",
            dataset_hash=request.input_identity.snapshot_hash,
            universe_hash=request.input_identity.universe_hash,
            factor_hash=factor_hash,
            split_hash=request.walk_forward.artifact_hash,
            label_hash=request.input_identity.label_hash,
            cost_hash=request.cost_model.artifact_hash,
            code_hash=request.input_identity.code_hash,
            availability_hash=request.input_identity.availability_hash,
            seed=request.seed,
            operator_manifest_hash=qlib.manifest_hash if request.certified_qlib_only and qlib else None,
            formula_policy_hash=qlib.policy_hash if request.certified_qlib_only and qlib else None,
            frozen_provider_hash=request.frozen_provider_hash if request.certified_qlib_only else None,
            review_policy_hash=(
                request.draft_review.policy_hash
                if request.certified_qlib_only and request.draft_review is not None
                else canonical_content_hash({"policy": "missing-independent-draft-review"})
                if request.certified_qlib_only
                else None
            ),
            source_draft_hash=request.source_draft_hash if request.certified_qlib_only else None,
        )

    @staticmethod
    def _trial_id(request: CandidateEvidenceValidationRequest, identity: ExperimentIdentity) -> str:
        return f"trial.{request.candidate_id}.{identity.identity_hash.removeprefix('sha256:')[:16]}"

    @staticmethod
    def _reconstruct_factor_dag(request: CandidateEvidenceValidationRequest) -> CompiledFactorDAG:
        if not isinstance(request.translation, CandidateTranslationResult):
            fail(FailureCode.INVALID_VALUE, "legacy reconstruction requires FactorDAG translation")
        raw = json.loads(request.translation.factor_dag_json or "")
        if isinstance(raw, Mapping) and raw.get("contract_type") == "CompiledFactorDAG":
            dag = CompiledFactorDAG.from_payload(raw)
        else:
            field_roles = {field: FieldRole.FEATURE for field in request.snapshot.fields}
            dag = parse_compile_factor_dag(raw, field_roles=field_roles)
        if request.translation.factor_dag_hash and dag.artifact_hash != request.translation.factor_dag_hash:
            fail(FailureCode.INVALID_HASH, "translation FactorDAG hash does not match reconstructed DAG")
        return dag

    @classmethod
    def _reconstruct_program(cls, request: CandidateEvidenceValidationRequest):
        if isinstance(request.translation, CandidateTranslationResult):
            return cls._reconstruct_factor_dag(request)
        if request.translation.status is not QlibCandidateTranslationStatus.CERTIFIED:
            fail(FailureCode.INVALID_FACTOR_DAG, "Qlib translation is not certified")
        if request.qlib_provider_manifest is None:
            fail(FailureCode.QLIB_DATA_UNAVAILABLE, "certified Qlib program lacks frozen provider manifest")
        return CertifiedQlibProgramBundle(
            translation=request.translation,
            provider_manifest=request.qlib_provider_manifest,
        )

    def _evaluation_rows(
        self,
        request: CandidateEvidenceValidationRequest,
        program,
    ) -> tuple[EvaluationRow, ...]:
        translation = request.translation
        if isinstance(translation, CandidateTranslationResult):
            assert isinstance(program, CompiledFactorDAG)
            program_identity = FormulaProgramIdentity(
                program_id=f"legacy.{request.candidate_id}",
                kind=FormulaProgramKind.LEGACY_FACTOR_DAG,
                program_hash=program.artifact_hash,
                required_fields=program.required_fields,
                runtime_hash=LEGACY_RUNTIME_HASH,
                policy_hash=LEGACY_POLICY_HASH,
            )
        else:
            assert translation.program_hash is not None
            program_identity = FormulaProgramIdentity(
                program_id=f"qlib.{request.candidate_id}",
                kind=FormulaProgramKind.CERTIFIED_QLIB_EXPRESSION,
                program_hash=translation.program_hash,
                required_fields=translation.required_fields,
                runtime_hash=QlibRuntimeIdentity.expected().runtime_hash,
                policy_hash=translation.policy_hash,
            )
        scores = self.formula_execution_service.execute(
            request=FormulaExecutionRequest(
                program=program_identity,
                snapshot_hash=request.input_identity.snapshot_hash,
                universe_hash=request.input_identity.universe_hash,
                seed=request.seed,
            ),
            program=program,
            snapshot=request.snapshot,
        )
        return evaluation_rows_from_scores(
            scores=scores,
            snapshot=request.snapshot,
            labels=request.labels,
            market_regimes=request.market_regimes,
        )

    def _replay_terminal(
        self,
        request: CandidateEvidenceValidationRequest,
        identity: ExperimentIdentity,
        trial_state,
    ) -> CandidateEvidenceValidationResult:
        replay = self.ledger.replay()
        if trial_state.outcome is TrialOutcome.SUCCESS:
            try:
                program = self._reconstruct_program(request)
                rows = self._evaluation_rows(request, program)
                visible = self.evidence_service.evaluate_visible_walk_forward(
                    identity=identity,
                    rows=rows,
                    spec=request.walk_forward,
                    cost_model=request.cost_model,
                    registered_factors=request.registered_factors,
                )
                summary = self.evidence_service.research_summary(identity, visible)
                metrics = _visible_metrics(request.candidate_id, identity, summary)
                evidence_hashes = tuple(
                    evidence.artifact_hash
                    for fold in visible
                    for evidence in (fold.train, fold.validation)
                )
                summary_hash = summary.artifact_hash
            except ContractValidationError:
                metrics = None
                evidence_hashes = ()
                summary_hash = None
            status = ValidationBridgeStatus.IDEMPOTENT_REPLAYED
            failure = None
        else:
            metrics = None
            evidence_hashes = ()
            summary_hash = None
            status = _status_from_trial_outcome(trial_state.outcome)
            failure = _failure_from_trial_state(trial_state)
        return CandidateEvidenceValidationResult(
            request_hash=request.artifact_hash,
            translation_hash=request.translation.artifact_hash,
            visible_metrics=metrics,
            disposition=_visible_disposition(request, visible[-1].validation) if metrics is not None and visible else None,
            result_ref=ValidationResultRef(
                candidate_id=request.candidate_id,
                family_id=request.family_id,
                trial_id=trial_state.trial_id,
                identity_hash=identity.identity_hash,
                status=status,
                budget_consumed=True,
                ledger_last_hash=replay.last_hash,
                terminal_event_hash=None,
                evidence_hashes=evidence_hashes,
                summary_hash=summary_hash,
                failure_code=failure,
                failure_message="terminal result replayed from Trial Ledger",
            ),
        )

    def _commit_failure(
        self,
        request: CandidateEvidenceValidationRequest,
        identity: ExperimentIdentity,
        trial_id: str,
        outcome: TrialOutcome,
        ledger_failure: FailureCode,
        status: ValidationBridgeStatus,
        bridge_failure: ValidationBridgeFailureCode,
        message: str,
    ) -> CandidateEvidenceValidationResult:
        try:
            terminal = self.ledger.commit_outcome(
                trial_id,
                outcome=outcome,
                failure_code=ledger_failure,
            )
            replay = self.ledger.replay()
            terminal_hash = terminal.event_hash
            ledger_last_hash = replay.last_hash
        except (LedgerIntegrityError, ContractValidationError) as exc:
            return self._blocked_by_ledger(request, identity, str(exc))
        return CandidateEvidenceValidationResult(
            request_hash=request.artifact_hash,
            translation_hash=request.translation.artifact_hash,
            visible_metrics=None,
            result_ref=ValidationResultRef(
                candidate_id=request.candidate_id,
                family_id=request.family_id,
                trial_id=trial_id,
                identity_hash=identity.identity_hash,
                status=status,
                budget_consumed=True,
                ledger_last_hash=ledger_last_hash,
                terminal_event_hash=terminal_hash,
                failure_code=bridge_failure,
                failure_message=message,
            ),
        )

    @staticmethod
    def _budget_exhausted(
        request: CandidateEvidenceValidationRequest,
        identity: ExperimentIdentity,
        trial_id: str,
        message: str,
    ) -> CandidateEvidenceValidationResult:
        return CandidateEvidenceValidationResult(
            request_hash=request.artifact_hash,
            translation_hash=request.translation.artifact_hash,
            visible_metrics=None,
            result_ref=ValidationResultRef(
                candidate_id=request.candidate_id,
                family_id=request.family_id,
                trial_id=trial_id,
                identity_hash=identity.identity_hash,
                status=ValidationBridgeStatus.BUDGET_EXHAUSTED,
                budget_consumed=False,
                ledger_last_hash=None,
                terminal_event_hash=None,
                failure_code=ValidationBridgeFailureCode.BUDGET_EXHAUSTED,
                failure_message=message,
            ),
        )

    @staticmethod
    def _blocked_by_ledger(
        request: CandidateEvidenceValidationRequest,
        identity: ExperimentIdentity,
        message: str,
    ) -> CandidateEvidenceValidationResult:
        return CandidateEvidenceValidationResult(
            request_hash=request.artifact_hash,
            translation_hash=request.translation.artifact_hash,
            visible_metrics=None,
            result_ref=ValidationResultRef(
                candidate_id=request.candidate_id,
                family_id=request.family_id,
                trial_id=ResearchEvidenceBridge._trial_id(request, identity),
                identity_hash=identity.identity_hash,
                status=ValidationBridgeStatus.LEDGER_BLOCKED,
                budget_consumed=False,
                ledger_last_hash=None,
                terminal_event_hash=None,
                failure_code=ValidationBridgeFailureCode.LEDGER_CORRUPTION,
                failure_message=message,
            ),
        )


def _row_key(event_time, symbol: str) -> str:
    return f"{event_time.isoformat()}|{symbol}"


def _translation_is_accepted(
    translation: CandidateTranslationResult | QlibCandidateTranslationResult,
) -> bool:
    if isinstance(translation, CandidateTranslationResult):
        return translation.status is CandidateTranslationStatus.ACCEPTED
    return translation.status is QlibCandidateTranslationStatus.CERTIFIED


def _translation_program_hash(
    translation: CandidateTranslationResult | QlibCandidateTranslationResult,
) -> str:
    if isinstance(translation, CandidateTranslationResult):
        return translation.factor_dag_hash or translation.draft_hash
    return translation.program_hash or translation.draft_hash


def _visible_metrics(
    candidate_id: str,
    identity: ExperimentIdentity,
    summary: ResearchSummary,
) -> VisibleComparisonMetrics:
    metrics: dict[str, float] = {}
    for prefix, source in (
        ("train", summary.train_metrics),
        ("validation", summary.validation_metrics),
    ):
        for key, value in source.items():
            metrics[f"{prefix}_{key}"] = float(value)
    return VisibleComparisonMetrics(
        candidate_id=candidate_id,
        identity_hash=identity.identity_hash,
        metrics=metrics,
        metric_source_hash=summary.artifact_hash,
    )


def _visible_disposition(
    request: CandidateEvidenceValidationRequest,
    validation_evidence,
) -> VisibleResearchDisposition:
    court = EvidenceCourt(policy=request.court_policy)
    decision = court.decide(
        evidence=validation_evidence,
        identity_hash=validation_evidence.identity_hash,
        proposer_id=request.proposer_id,
        approver_id=request.approver_id,
        decided_at=request.snapshot.decision_time,
        pit_valid=True,
        sealed_passed=None,
    )
    visible_outcome = decision.outcome
    visible_reasons = decision.reason_codes
    if visible_outcome is CourtOutcome.PROMOTE_CHALLENGER:
        # Visible train/validation evidence is allowed to say "validated
        # research", but challenger promotion belongs to the independent
        # sealed-review controller.  Keep the positive visible signal while
        # preventing a public/visible artifact from carrying promotion
        # authority.
        visible_outcome = CourtOutcome.VALIDATED_RESEARCH
        visible_reasons = tuple(
            dict.fromkeys(
                (
                    *visible_reasons,
                    "VISIBLE_RESEARCH_PASS_REQUIRES_SEALED_REVIEW",
                )
            )
        )
    return VisibleResearchDisposition(
        candidate_id=request.candidate_id,
        identity_hash=decision.identity_hash,
        decision_hash=decision.artifact_hash,
        evidence_hash=decision.evidence_hash,
        proposer_id=decision.proposer_id,
        approver_id=decision.approver_id,
        outcome=visible_outcome,
        reason_codes=visible_reasons,
        sealed_passed=None,
    )


def _pit_failure_code(exc: ContractValidationError) -> ValidationBridgeFailureCode:
    if exc.code is FailureCode.UNKNOWN_FIELD:
        return ValidationBridgeFailureCode.SNAPSHOT_FIELD_MISSING
    if exc.code is FailureCode.MISSING_REQUIRED_FIELD:
        return ValidationBridgeFailureCode.LABEL_MISSING
    if exc.code is FailureCode.INVALID_VALUE:
        return ValidationBridgeFailureCode.VOLUME_MISSING
    return ValidationBridgeFailureCode.EVALUATION_FAILED


def _evaluation_failure_code(exc: ContractValidationError) -> ValidationBridgeFailureCode:
    if exc.code is FailureCode.INVALID_VALUE:
        return ValidationBridgeFailureCode.INSUFFICIENT_OBSERVATIONS
    return ValidationBridgeFailureCode.EVALUATION_FAILED


def _status_from_trial_outcome(outcome: TrialOutcome | None) -> ValidationBridgeStatus:
    if outcome is TrialOutcome.PARSE_REJECTION:
        return ValidationBridgeStatus.PARSE_REJECTED
    if outcome is TrialOutcome.PIT_REJECTION:
        return ValidationBridgeStatus.PIT_REJECTED
    return ValidationBridgeStatus.EVALUATION_REJECTED


def _failure_from_trial_state(trial_state) -> ValidationBridgeFailureCode:
    if trial_state.outcome is TrialOutcome.PARSE_REJECTION:
        return ValidationBridgeFailureCode.FACTOR_DAG_RECONSTRUCTION_FAILED
    if trial_state.outcome is TrialOutcome.PIT_REJECTION:
        if trial_state.failure_code is FailureCode.UNKNOWN_FIELD:
            return ValidationBridgeFailureCode.SNAPSHOT_FIELD_MISSING
        if trial_state.failure_code is FailureCode.MISSING_REQUIRED_FIELD:
            return ValidationBridgeFailureCode.LABEL_MISSING
        return ValidationBridgeFailureCode.VOLUME_MISSING
    return ValidationBridgeFailureCode.EVALUATION_FAILED
