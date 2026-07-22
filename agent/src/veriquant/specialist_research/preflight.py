"""Deterministic formula preflight for specialist DRAFT candidates."""

from __future__ import annotations

from ..candidate_translation import CandidateDraftEnvelope, CandidateTranslationStatus, SafeCandidateTranslator
from ..qlib_candidate_translation import (
    QlibCandidateDraftEnvelope,
    QlibCandidateTranslationStatus,
    SafeQlibCandidateTranslator,
)
from ..research_campaign import CampaignState
from ..formula_execution import KNOWN_DIFFERENCE_OPERATOR_NAMES
from .models import CandidatePreflightResult, PreflightStatus, SpecialistCandidateDraft, SpecialistResearchRequest


class SpecialistFactorPreflight:
    """Qlib-expression primary preflight with legacy FactorDAG compatibility."""

    def __init__(
        self,
        translator: SafeCandidateTranslator | None = None,
        qlib_translator: SafeQlibCandidateTranslator | None = None,
        *,
        allow_legacy_factor_dag: bool = True,
    ) -> None:
        if not isinstance(allow_legacy_factor_dag, bool):
            raise TypeError("allow_legacy_factor_dag must be bool")
        self._translator = translator or SafeCandidateTranslator()
        self._qlib_translator = qlib_translator or SafeQlibCandidateTranslator()
        self._allow_legacy_factor_dag = allow_legacy_factor_dag

    def check(self, request: SpecialistResearchRequest, candidate: SpecialistCandidateDraft) -> CandidatePreflightResult:
        missing = tuple(sorted(set(candidate.required_fields) - set(request.available_fields)))
        if missing:
            return CandidatePreflightResult(
                candidate_id=candidate.candidate_id,
                draft_hash=candidate.artifact_hash,
                status=PreflightStatus.CAPABILITY_REQUEST,
                failure_code="MISSING_DATA_FIELDS",
                message="missing approved fields: " + ",".join(missing),
            )
        qlib = self._qlib_translator.translate(
            QlibCandidateDraftEnvelope(
                campaign_hash=request.campaign_hash,
                campaign_state=CampaignState.CONFIRMED,
                group_id=request.specialist_id,
                candidate_id=candidate.candidate_id,
                expression=candidate.expression,
                confirmed_field_whitelist=request.available_fields,
                requested_lookback_sessions=request.requested_lookback_sessions,
                source_draft_hash=candidate.artifact_hash,
            )
        )
        if qlib.status is QlibCandidateTranslationStatus.CERTIFIED:
            blocked = tuple(sorted(set(qlib.operators) & KNOWN_DIFFERENCE_OPERATOR_NAMES))
            if blocked:
                return CandidatePreflightResult(
                    candidate_id=candidate.candidate_id,
                    draft_hash=candidate.artifact_hash,
                    status=PreflightStatus.REVISE,
                    normalized_expression=qlib.canonical_expression,
                    failure_code="QLIB_SEMANTICS_PARITY_REQUIRED",
                    message="operators require semantic parity repair: " + ",".join(blocked),
                )
            return CandidatePreflightResult(
                candidate_id=candidate.candidate_id,
                draft_hash=candidate.artifact_hash,
                status=PreflightStatus.ACCEPTED,
                normalized_expression=qlib.canonical_expression,
                formula_program_hash=qlib.program_hash,
                formula_program_kind="CERTIFIED_QLIB_EXPRESSION",
                formula_manifest_hash=qlib.manifest_hash,
                message="Qlib Expression preflight accepted; PIT and machine evaluation have not run.",
            )

        if not self._allow_legacy_factor_dag:
            status = (
                PreflightStatus.CAPABILITY_REQUEST
                if qlib.status is QlibCandidateTranslationStatus.UNSUPPORTED
                else PreflightStatus.REVISE
            )
            return CandidatePreflightResult(
                candidate_id=candidate.candidate_id,
                draft_hash=candidate.artifact_hash,
                status=status,
                normalized_expression=qlib.canonical_expression,
                failure_code=(
                    qlib.expression_failure_code.value
                    if qlib.expression_failure_code is not None
                    else "QLIB_EXPRESSION_REQUIRED"
                ),
                message=(
                    qlib.message
                    or "The product path requires a certified Qlib Expression; legacy FactorDAG fallback is disabled."
                ),
            )

        translated = self._translator.translate(
            CandidateDraftEnvelope(
                campaign_hash=request.campaign_hash,
                group_id=request.specialist_id,
                candidate_id=candidate.candidate_id,
                expression=candidate.expression,
                requested_lookback_sessions=request.requested_lookback_sessions,
                declared_lookback_sessions=candidate.lookback_sessions,
                allowed_fields=request.available_fields,
                source_draft_hash=candidate.artifact_hash,
            )
        )
        if translated.status is CandidateTranslationStatus.ACCEPTED:
            return CandidatePreflightResult(
                candidate_id=candidate.candidate_id,
                draft_hash=candidate.artifact_hash,
                status=PreflightStatus.ACCEPTED,
                normalized_expression=translated.normalized_expression,
                factor_dag_hash=translated.factor_dag_hash,
                formula_program_kind="LEGACY_FACTOR_DAG",
                message="FactorDAG preflight accepted; PIT and machine evaluation have not run.",
            )
        status = (
            PreflightStatus.CAPABILITY_REQUEST
            if translated.status is CandidateTranslationStatus.UNSUPPORTED
            else PreflightStatus.REVISE
        )
        return CandidatePreflightResult(
            candidate_id=candidate.candidate_id,
            draft_hash=candidate.artifact_hash,
            status=status,
            normalized_expression=translated.normalized_expression,
            failure_code=qlib.expression_failure_code.value if qlib.expression_failure_code else translated.failure_code,
            message=qlib.message if qlib.message else translated.message,
        )
