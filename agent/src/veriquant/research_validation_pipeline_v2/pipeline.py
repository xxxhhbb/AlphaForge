"""Thin orchestration over existing FactorDAG, PIT, evidence and tournament truth."""

from __future__ import annotations

import math
from pathlib import Path

from ..candidate_tournament import CandidateMetricBundle, CandidateTournament, CandidateTournamentEntry
from ..candidate_translation import CandidateDraftEnvelope, CandidateTranslationStatus, SafeCandidateTranslator
from ..evidence.models import CostModel, CourtOutcome, WalkForwardSpec
from ..research_evidence_bridge import (
    CandidateEvidenceValidationRequest,
    FrozenEvidenceInputIdentity,
    ResearchEvidenceBridge,
    ValidationBridgeStatus,
)
from ..research_group_runtime.runtime import GroupBranchStatus
from ..research_os import ValidationDisposition, ValidationResultRef
from .models import (
    BestObservedResearchClue,
    CandidateTerminalRecord,
    CandidateTerminalStatus,
    PipelineFailureCode,
    ResearchValidationPipelineRequest,
    ResearchValidationPipelineResult,
)


class VisibleMetricProjector:
    """Versioned deterministic normalization; RankIC is deliberately unused."""

    REQUIRED = (
        "validation_icir",
        "validation_subperiod_positive_fraction",
        "validation_regime_positive_fraction",
        "validation_turnover",
        "validation_gross_return",
        "validation_net_return",
        "validation_max_registered_abs_correlation",
        "validation_incremental_ic_mean",
    )

    def project(self, metrics, *, evidence_hash: str, family_budget_score: float) -> CandidateMetricBundle:
        missing = tuple(name for name in self.REQUIRED if name not in metrics)
        if missing:
            raise ValueError(f"missing visible comparison metrics: {','.join(missing)}")
        net_icir = _symmetric(float(metrics["validation_icir"]))
        stability = _clamp(
            0.5 * float(metrics["validation_subperiod_positive_fraction"])
            + 0.5 * float(metrics["validation_regime_positive_fraction"])
        )
        turnover = max(0.0, float(metrics["validation_turnover"]))
        gross = float(metrics["validation_gross_return"])
        net = float(metrics["validation_net_return"])
        cost_drag = max(0.0, gross - net)
        cost_capacity = _clamp(1.0 / (1.0 + turnover + 10.0 * cost_drag))
        novelty = _clamp(1.0 - abs(float(metrics["validation_max_registered_abs_correlation"])))
        incrementality = _symmetric(float(metrics["validation_incremental_ic_mean"]))
        return CandidateMetricBundle(
            evidence_pack_hash=evidence_hash,
            net_icir_score=net_icir,
            stability_score=stability,
            cost_capacity_score=cost_capacity,
            novelty_score=novelty,
            incrementality_score=incrementality,
            family_budget_score=_clamp(family_budget_score),
        )


class ResearchValidationPipelineV2:
    def __init__(
        self,
        *,
        ledger_path: str | Path,
        walk_forward: WalkForwardSpec,
        cost_model: CostModel,
        translator: SafeCandidateTranslator | None = None,
        bridge: ResearchEvidenceBridge | None = None,
        projector: VisibleMetricProjector | None = None,
    ) -> None:
        self.translator = translator or SafeCandidateTranslator()
        self.bridge = bridge or ResearchEvidenceBridge(ledger_path=ledger_path)
        self.walk_forward = walk_forward
        self.cost_model = cost_model
        self.projector = projector or VisibleMetricProjector()

    def run(self, request: ResearchValidationPipelineRequest) -> ResearchValidationPipelineResult:
        completed = tuple(
            branch for branch in request.runtime.branches
            if branch.status is GroupBranchStatus.COMPLETED and branch.proposal is not None and branch.submission is not None
        )
        submissions = tuple(branch.submission for branch in completed if branch.submission is not None)
        if not submissions:
            raise ValueError(PipelineFailureCode.NO_COMPLETED_RESEARCH_GROUP.value)
        identity = FrozenEvidenceInputIdentity.from_snapshot_labels(
            snapshot=request.frozen_data.snapshot,
            labels=request.frozen_data.labels,
            source_label=request.frozen_data.source_label,
        )
        terminal: list[CandidateTerminalRecord] = []
        entries: list[CandidateTournamentEntry] = []
        scored_clues: list[tuple[float, str, str, bool]] = []
        allowed_fields = tuple(
            field for field in request.frozen_data.snapshot.fields
            if "future_return" not in field.lower()
        )
        for branch in sorted(completed, key=lambda item: item.group_id):
            assert branch.proposal is not None and branch.submission is not None
            drafts_by_hash = {item.artifact_hash: item for item in branch.proposal.candidates}
            for draft_hash in branch.submission.candidate_draft_hashes:
                candidate = drafts_by_hash[draft_hash]
                translation = self.translator.translate(CandidateDraftEnvelope(
                    campaign_hash=request.campaign_hash,
                    group_id=branch.group_id,
                    candidate_id=candidate.candidate_id,
                    expression=candidate.expression,
                    requested_lookback_sessions=request.requested_lookback_sessions,
                    declared_lookback_sessions=candidate.lookback_sessions,
                    allowed_fields=allowed_fields,
                    source_draft_hash=draft_hash,
                ))
                family_id = f"family-{branch.group_id}-{candidate.candidate_id}"
                validation = self.bridge.validate_candidate(CandidateEvidenceValidationRequest(
                    candidate_id=candidate.candidate_id,
                    research_group_id=branch.group_id,
                    family_id=family_id,
                    proposer_id=f"research-group.{branch.group_id}",
                    translation=translation,
                    snapshot=request.frozen_data.snapshot,
                    labels=request.frozen_data.labels,
                    input_identity=identity,
                    walk_forward=self.walk_forward,
                    cost_model=self.cost_model,
                    family_budget=request.family_budget_per_candidate,
                    seed=request.seed,
                    approver_id="evidence.court.visible",
                ))
                disposition = _disposition(validation)
                evidence_hash = validation.visible_metrics.metric_source_hash if validation.visible_metrics else None
                score_bundle = None
                if validation.visible_metrics is not None and evidence_hash is not None:
                    score_bundle = self.projector.project(
                        validation.visible_metrics.metrics,
                        evidence_hash=evidence_hash,
                        family_budget_score=1.0 / request.family_budget_per_candidate,
                    )
                    scored_clues.append((score_bundle.composite_score, draft_hash, branch.group_id, disposition is ValidationDisposition.VALIDATED_RESEARCH))
                os_ref = ValidationResultRef(
                    request_hash=validation.request_hash,
                    evidence_pack_hash=evidence_hash,
                    disposition=disposition,
                    failure_codes=_failure_codes(validation, translation.status),
                )
                entries.append(CandidateTournamentEntry(
                    group_id=branch.group_id,
                    candidate_draft_hash=draft_hash,
                    validation=os_ref,
                    metrics=score_bundle if disposition is ValidationDisposition.VALIDATED_RESEARCH else None,
                ))
                terminal.append(CandidateTerminalRecord(
                    group_id=branch.group_id,
                    candidate_id=candidate.candidate_id,
                    candidate_draft_hash=draft_hash,
                    translation=translation,
                    validation=validation,
                    status=_terminal_status(validation.result_ref.status),
                    disposition=disposition,
                    failure_codes=os_ref.failure_codes,
                    composite_score=score_bundle.composite_score if score_bundle else None,
                ))
        tournament = CandidateTournament().compare(submissions=submissions, entries=tuple(entries))
        best = None
        if scored_clues:
            score, draft_hash, group_id, eligible = sorted(scored_clues, key=lambda item: (-item[0], item[1]))[0]
            best = BestObservedResearchClue(
                candidate_draft_hash=draft_hash,
                group_id=group_id,
                composite_score=score,
                eligible_for_selection=eligible,
            )
        return ResearchValidationPipelineResult(
            runtime_hash=request.runtime.artifact_hash,
            frozen_data_hash=request.frozen_data.artifact_hash,
            terminal_records=tuple(terminal),
            tournament=tournament,
            best_observed=best,
        )


def _disposition(result) -> ValidationDisposition:
    if result.disposition is None:
        return ValidationDisposition.NOT_EVALUATED
    return {
        CourtOutcome.REJECT: ValidationDisposition.REJECT,
        CourtOutcome.REVISE: ValidationDisposition.REVISE,
        CourtOutcome.VALIDATED_RESEARCH: ValidationDisposition.VALIDATED_RESEARCH,
    }[result.disposition.outcome]


def _failure_codes(result, translation_status: CandidateTranslationStatus) -> tuple[str, ...]:
    codes: list[str] = []
    if translation_status is not CandidateTranslationStatus.ACCEPTED:
        codes.append(PipelineFailureCode.DRAFT_TRANSLATION_REJECTED.value)
    if result.result_ref.failure_code is not None:
        codes.append(result.result_ref.failure_code.value)
    if result.disposition is not None:
        codes.extend(result.disposition.reason_codes)
    return tuple(dict.fromkeys(codes))


def _terminal_status(status: ValidationBridgeStatus) -> CandidateTerminalStatus:
    if status in {ValidationBridgeStatus.OFFICIAL_EVIDENCE_READY, ValidationBridgeStatus.IDEMPOTENT_REPLAYED}:
        return CandidateTerminalStatus.OFFICIAL_VISIBLE_EVALUATED
    if status is ValidationBridgeStatus.PARSE_REJECTED:
        return CandidateTerminalStatus.TRANSLATION_REJECTED
    if status is ValidationBridgeStatus.PIT_REJECTED:
        return CandidateTerminalStatus.PIT_REJECTED
    if status is ValidationBridgeStatus.EVALUATION_REJECTED:
        return CandidateTerminalStatus.EVALUATION_REJECTED
    if status is ValidationBridgeStatus.BUDGET_EXHAUSTED:
        return CandidateTerminalStatus.BUDGET_BLOCKED
    return CandidateTerminalStatus.LEDGER_BLOCKED


def _symmetric(value: float) -> float:
    return _clamp(0.5 + math.atan(value) / math.pi)


def _clamp(value: float) -> float:
    return round(min(1.0, max(0.0, float(value))), 8)
