"""End-to-end orchestration for a bounded, evidence-backed research answer.

This module owns orchestration only.  Research groups remain proposal-only;
FactorDAG/PIT/Evidence and Trial Ledger truth remain in their existing services.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from ..candidate_tournament import CandidateMetricBundle, CandidateTournament, CandidateTournamentEntry
from ..candidate_translation import CandidateDraftEnvelope, SafeCandidateTranslator
from ..data import MarketSnapshot, canonical_content_hash
from ..data_planning import DataAvailabilitySnapshot, DeterministicDataPlanner
from ..evidence.models import CostModel, CourtOutcome, WalkForwardSpec
from ..formal_report_v2 import (
    CandidateReportV2,
    DataCoverageV2,
    FormalResearchReportV2,
    ResearchGroupReportV2,
    ResearchabilitySectionV2,
)
from ..research_campaign import MethodModel
from ..research_closure_contracts import MetricDirection, OfficialMetricRecord, ResearchOutcomeStatus
from ..research_evidence_bridge import (
    CandidateEvidenceValidationRequest,
    CandidateEvidenceValidationResult,
    FrozenEvidenceInputIdentity,
    ResearchEvidenceBridge,
    ValidationBridgeStatus,
)
from ..research_group_runtime import (
    DeterministicResearchGroupSelector,
    GroupSelectionContext,
    ResearchGroupProposalRuntime,
)
from ..research_os import AnswerLevel, ValidationDisposition, ValidationResultRef
from ..research_os_publication import ResearchOSPublicationReceipt, ResearchOSPublicationRepository
from ..research_planner import DeterministicResearchQuestionPlanner, QuestionAssessmentRequest, QuestionAssessmentResult


@dataclass(frozen=True)
class ResearchOSRunResult:
    run_id: str
    campaign_hash: str
    route_plan_hash: str
    planning: QuestionAssessmentResult
    selected_group_ids: tuple[str, ...]
    candidate_count: int
    terminal_candidate_count: int
    tournament_status: str
    best_observed_candidate_id: str | None
    report: FormalResearchReportV2
    publication: ResearchOSPublicationReceipt
    validation_results: tuple[CandidateEvidenceValidationResult, ...]


class AlphaForgeResearchOS:
    """Run one confirmed research question through the trusted offline chain."""

    def __init__(
        self,
        *,
        artifact_root: str | Path,
        group_runtime: ResearchGroupProposalRuntime | None = None,
        min_symbols: int = 4,
    ) -> None:
        root = Path(artifact_root)
        root.mkdir(parents=True, exist_ok=True)
        self.root = root
        self.planner = DeterministicResearchQuestionPlanner(
            data_planner=DeterministicDataPlanner(
                train_sessions=20,
                validation_sessions=10,
                buffer_sessions=5,
                min_symbol_count=min_symbols,
            )
        )
        self.selector = DeterministicResearchGroupSelector()
        self.group_runtime = group_runtime or ResearchGroupProposalRuntime()
        self.translator = SafeCandidateTranslator()
        self.evidence = ResearchEvidenceBridge(ledger_path=root / "trial_ledger.sqlite3")
        self.publisher = ResearchOSPublicationRepository(root / "published")

    def run(
        self,
        *,
        run_id: str,
        question: str,
        snapshot: MarketSnapshot,
        labels: Mapping[str, float],
        source_label: str = "DETERMINISTIC_OFFLINE_FIXTURE",
        seed: int = 7,
        max_groups: int = 2,
        max_candidates_per_group: int = 2,
    ) -> ResearchOSRunResult:
        """Execute and publish one idempotent run.

        The caller supplies a frozen snapshot and labels.  Missing research data
        degrades the answer level; it never authorizes fabricated evidence.
        """

        snapshot = _align_snapshot_to_labels(snapshot, labels)
        availability = DataAvailabilitySnapshot(
            availability_id=f"availability.{run_id}",
            available_fields=tuple(sorted(snapshot.fields)),
            available_sessions=len(snapshot.historical_universe),
            available_symbols=len({row.symbol for row in snapshot.rows}),
            provider_label=source_label,
            snapshot_hash=snapshot.artifact_hash,
        )
        planning = self.planner.assess(
            QuestionAssessmentRequest(question=question, decision_time=snapshot.decision_time, availability=availability)
        )
        campaign_hash = canonical_content_hash(
            {"run_id": run_id, "question": planning.sanitized_question, "planning": planning.artifact_hash}
        )
        selection = self.selector.select(
            GroupSelectionContext(
                campaign_hash=campaign_hash,
                mode=planning.assessment.intent.mode,
                method_models=_methods_for(planning.assessment.intent.mode.value),
                question_tags=_question_tags(planning.sanitized_question),
                available_fields=tuple(sorted(snapshot.fields)),
                max_groups=max_groups,
                max_candidates_per_group=max_candidates_per_group,
                context_keys=("question", "data_scope", "visible_factor_summary"),
            )
        )
        group_run = self.group_runtime.run(
            selection=selection,
            intent=planning.assessment.intent,
            campaign_hash=campaign_hash,
        )
        input_identity = FrozenEvidenceInputIdentity.from_snapshot_labels(
            snapshot=snapshot, labels=labels, source_label=source_label
        )

        report_groups: list[ResearchGroupReportV2] = []
        validations: list[CandidateEvidenceValidationResult] = []
        entries: list[CandidateTournamentEntry] = []
        submissions = []
        candidate_reports: list[CandidateReportV2] = []
        observed_scores: list[tuple[float, str]] = []

        for branch in group_run.branches:
            if branch.proposal is None or branch.submission is None:
                continue
            submissions.append(branch.submission)
            proposal = branch.proposal
            first = proposal.candidates[0]
            report_groups.append(
                ResearchGroupReportV2(
                    group_id=branch.group_id,
                    group_label=_group_label(branch.group_id),
                    hypothesis=proposal.hypothesis,
                    candidate_name=first.candidate_id,
                    candidate_expression=first.expression,
                    status="研究草案已提交，等待机器逐项验证",
                    source_hashes=(proposal.artifact_hash,),
                )
            )
            for candidate in proposal.candidates:
                envelope = CandidateDraftEnvelope(
                    campaign_hash=campaign_hash,
                    group_id=branch.group_id,
                    candidate_id=candidate.candidate_id,
                    expression=candidate.expression,
                    requested_lookback_sessions=planning.assessment.intent.factor_lookback_sessions,
                    declared_lookback_sessions=candidate.lookback_sessions,
                    allowed_fields=tuple(sorted(snapshot.fields)),
                    source_draft_hash=candidate.artifact_hash,
                )
                translation = self.translator.translate(envelope)
                family_id = f"family.{run_id}.{branch.group_id}"
                validation = self.evidence.validate_candidate(
                    CandidateEvidenceValidationRequest(
                        candidate_id=candidate.candidate_id,
                        research_group_id=branch.group_id,
                        family_id=family_id,
                        proposer_id=f"research.{branch.group_id}",
                        translation=translation,
                        snapshot=snapshot,
                        labels=labels,
                        input_identity=input_identity,
                        walk_forward=WalkForwardSpec(
                            train_periods=20, validation_periods=10, sealed_periods=5, step_periods=10
                        ),
                        cost_model=CostModel(spread_bps=5.0, linear_impact_bps=10.0),
                        family_budget=max_candidates_per_group,
                        seed=seed,
                        approver_id="evidence.court",
                    )
                )
                validations.append(validation)
                disposition = _disposition(validation)
                os_ref = ValidationResultRef(
                    request_hash=validation.request_hash,
                    evidence_pack_hash=_evidence_hash(validation),
                    disposition=disposition,
                    failure_codes=_failure_codes(validation),
                )
                bundle = _metric_bundle(validation)
                if bundle is not None:
                    observed_scores.append((bundle.composite_score, candidate.candidate_id))
                entries.append(
                    CandidateTournamentEntry(
                        group_id=branch.group_id,
                        candidate_draft_hash=candidate.artifact_hash,
                        validation=os_ref,
                        metrics=bundle if disposition is ValidationDisposition.VALIDATED_RESEARCH else None,
                    )
                )
                candidate_reports.append(
                    _candidate_report(
                        group_id=branch.group_id,
                        candidate=candidate,
                        translation_status=translation.status.value,
                        validation=validation,
                    )
                )

        if not submissions:
            raise RuntimeError("no professional research group produced a candidate")
        tournament = CandidateTournament().compare(submissions=tuple(submissions), entries=tuple(entries))
        observed_scores.sort(key=lambda item: (-item[0], item[1]))
        best_observed = observed_scores[0][1] if observed_scores else None
        report = _build_report(
            run_id=run_id,
            campaign_hash=campaign_hash,
            planning=planning,
            source_label=source_label,
            snapshot=snapshot,
            selected_groups=tuple(report_groups),
            candidates=tuple(candidate_reports),
            best_observed=best_observed,
            tournament_status=tournament.status.value,
        )
        run_evidence_hash = canonical_content_hash(
            {
                "validations": [
                    {
                        "identity_hash": item.result_ref.identity_hash,
                        "evidence_hashes": item.result_ref.evidence_hashes,
                        "summary_hash": item.result_ref.summary_hash,
                        "decision_hash": item.disposition.decision_hash if item.disposition else None,
                    }
                    for item in validations
                ],
                "tournament": tournament.artifact_hash,
            }
        )
        acceptance_hash = canonical_content_hash(
            {
                "selected_equals_executed": tuple(item.group_id for item in selection.assignments)
                == tuple(item.group_id for item in group_run.branches),
                "candidate_count": len(candidate_reports),
                "terminal_count": len(validations),
                "report_hash": report.artifact_hash,
            }
        )
        publication = self.publisher.publish(
            report=report, acceptance_report_hash=acceptance_hash, run_evidence_hash=run_evidence_hash
        )
        return ResearchOSRunResult(
            run_id=run_id,
            campaign_hash=campaign_hash,
            route_plan_hash=selection.artifact_hash,
            planning=planning,
            selected_group_ids=tuple(item.group_id for item in selection.assignments),
            candidate_count=len(candidate_reports),
            terminal_candidate_count=len(validations),
            tournament_status=tournament.status.value,
            best_observed_candidate_id=best_observed,
            report=report,
            publication=publication,
            validation_results=tuple(validations),
        )


def _align_snapshot_to_labels(snapshot: MarketSnapshot, labels: Mapping[str, float]) -> MarketSnapshot:
    """Use only feature rows whose forward label is frozen and available.

    This is an evaluation split boundary, not look-ahead: the feature values
    remain the original PIT values and the final unlabeled horizon is omitted.
    """

    rows = tuple(
        row for row in snapshot.rows
        if f"{row.event_time.isoformat()}|{row.symbol}" in labels
    )
    if not rows:
        return snapshot
    times = {row.event_time.isoformat() for row in rows}
    history = {key: value for key, value in snapshot.historical_universe.items() if key in times}
    coverage = {}
    for field in snapshot.fields:
        present = sum(1 for row in rows if row.values.get(field) is not None)
        coverage[field] = present / len(rows)
    return MarketSnapshot(
        dataset_id=snapshot.dataset_id,
        decision_time=snapshot.decision_time,
        start_time=snapshot.start_time,
        fields=snapshot.fields,
        rows=rows,
        historical_universe=history,
        coverage_by_field=coverage,
    )


def _methods_for(mode: str) -> tuple[MethodModel, ...]:
    if mode == "DIAGNOSE_FACTOR_PORTFOLIO":
        return (MethodModel.DIAGNOSTIC_ATTRIBUTION, MethodModel.REGIME_CONDITIONING, MethodModel.ADVERSARIAL_FALSIFICATION)
    if mode == "PORTFOLIO_GAP_SEARCH":
        return (MethodModel.RESIDUAL_INCREMENTAL, MethodModel.ADVERSARIAL_FALSIFICATION)
    return (MethodModel.HYPOTHESIS_DRIVEN, MethodModel.DATA_ANOMALY, MethodModel.ADVERSARIAL_FALSIFICATION)


def _question_tags(question: str) -> tuple[str, ...]:
    lowered = question.lower()
    tags = ["momentum" if ("动量" in question or "momentum" in lowered) else "alpha"]
    if "成交" in question or "volume" in lowered:
        tags.append("volume")
    tags.append("cost")
    return tuple(tags)


def _group_label(group_id: str) -> str:
    return {
        "market_structure": "市场结构研究组",
        "meta_alpha": "组合增量研究组",
        "fundamental": "基本面研究组",
        "macro_regime": "宏观状态研究组",
        "expectations_events": "预期与事件研究组",
        "alternative_data": "另类数据研究组",
    }.get(group_id, "专业研究组")


def _disposition(result: CandidateEvidenceValidationResult) -> ValidationDisposition:
    if result.disposition is None:
        return ValidationDisposition.NOT_EVALUATED
    return {
        CourtOutcome.REJECT: ValidationDisposition.REJECT,
        CourtOutcome.REVISE: ValidationDisposition.REVISE,
        CourtOutcome.VALIDATED_RESEARCH: ValidationDisposition.VALIDATED_RESEARCH,
    }[result.disposition.outcome]


def _evidence_hash(result: CandidateEvidenceValidationResult) -> str | None:
    if result.disposition is not None:
        return result.disposition.evidence_hash
    if result.result_ref.evidence_hashes:
        return result.result_ref.evidence_hashes[0]
    return None


def _failure_codes(result: CandidateEvidenceValidationResult) -> tuple[str, ...]:
    if result.result_ref.failure_code is not None:
        return (result.result_ref.failure_code.value,)
    return ()


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def _metric_bundle(result: CandidateEvidenceValidationResult) -> CandidateMetricBundle | None:
    if result.visible_metrics is None or result.disposition is None:
        return None
    m = result.visible_metrics.metrics
    icir = float(m.get("validation_icir", 0.0))
    net_return = float(m.get("validation_net_return", 0.0))
    gross_return = float(m.get("validation_gross_return", 0.0))
    turnover = abs(float(m.get("validation_turnover", 0.0)))
    cost_drag = max(0.0, gross_return - net_return)
    return CandidateMetricBundle(
        evidence_pack_hash=result.disposition.evidence_hash,
        net_icir_score=_clamp(0.5 + math.atan(icir) / math.pi),
        stability_score=_clamp(float(m.get("validation_subperiod_positive_fraction", 0.0))),
        cost_capacity_score=_clamp(1.0 / (1.0 + turnover + 100.0 * cost_drag)),
        novelty_score=_clamp(1.0 - abs(float(m.get("validation_max_registered_abs_correlation", 0.0)))),
        incrementality_score=_clamp(0.5 + math.atan(float(m.get("validation_incremental_ic_mean", 0.0))) / math.pi),
        family_budget_score=0.5,
    )


def _official_metrics(result: CandidateEvidenceValidationResult) -> tuple[OfficialMetricRecord, ...]:
    if result.visible_metrics is None:
        return ()
    evidence_hash = result.visible_metrics.metric_source_hash
    records = []
    for name, value in sorted(result.visible_metrics.metrics.items()):
        direction = MetricDirection.LOWER_IS_BETTER if any(token in name for token in ("turnover", "cost", "drawdown", "correlation")) else MetricDirection.HIGHER_IS_BETTER
        records.append(
            OfficialMetricRecord(
                metric_id=f"metric.{result.result_ref.candidate_id}.{name}",
                candidate_id=result.result_ref.candidate_id,
                metric_name=name,
                value=value,
                direction=direction,
                evidence_hash=evidence_hash,
                split="train_validation",
                policy_version="evidence_bridge_v1",
            )
        )
    return tuple(records)


def _candidate_report(*, group_id: str, candidate, translation_status: str, validation: CandidateEvidenceValidationResult) -> CandidateReportV2:
    disposition = _disposition(validation)
    outcome = {
        ValidationDisposition.REJECT: ResearchOutcomeStatus.REJECT,
        ValidationDisposition.REVISE: ResearchOutcomeStatus.REVISE,
        ValidationDisposition.VALIDATED_RESEARCH: ResearchOutcomeStatus.VALIDATED_RESEARCH,
        ValidationDisposition.NOT_EVALUATED: ResearchOutcomeStatus.NO_OFFICIAL_EVIDENCE,
    }[disposition]
    reasons = validation.disposition.reason_codes if validation.disposition is not None else _failure_codes(validation)
    return CandidateReportV2(
        candidate_id=candidate.candidate_id,
        group_label=_group_label(group_id),
        hypothesis=candidate.rationale,
        expression=candidate.expression,
        translation_status=translation_status,
        pit_status="已完成" if validation.visible_metrics is not None else "未通过",
        evidence_status="机器证据已生成" if validation.visible_metrics is not None else "没有机器证据",
        critic_status=disposition.value,
        outcome=outcome,
        official_metrics=_official_metrics(validation),
        evidence_refs=tuple(validation.result_ref.evidence_hashes),
        # The immutable experiment identity is stable on idempotent replay;
        # replay status metadata is deliberately not part of the report hash.
        attempt_hash=validation.result_ref.identity_hash,
        failure_reasons=tuple(reasons),
        next_actions=("根据失败原因修订后重新验证",) if outcome in {ResearchOutcomeStatus.REJECT, ResearchOutcomeStatus.REVISE, ResearchOutcomeStatus.NO_OFFICIAL_EVIDENCE} else ("进入独立封闭检验，当前结果不等于晋级",),
    )


def _build_report(*, run_id: str, campaign_hash: str, planning: QuestionAssessmentResult, source_label: str, snapshot: MarketSnapshot, selected_groups: tuple[ResearchGroupReportV2, ...], candidates: tuple[CandidateReportV2, ...], best_observed: str | None, tournament_status: str) -> FormalResearchReportV2:
    plan = planning.data_planning.data_plan
    missing = tuple(sorted(item.field_name for item in planning.data_planning.field_availability if item.required_for_official and not item.available))
    validated = [item for item in candidates if item.outcome is ResearchOutcomeStatus.VALIDATED_RESEARCH]
    answer = (
        f"共验证 {len(candidates)} 个候选，{len(validated)} 个达到可见研究验证标准。"
        + (f" 当前证据表现最优的研究线索是 {best_observed}，但仍须独立封闭检验。" if best_observed else " 当前没有候选形成可比较机器证据。")
    )
    return FormalResearchReportV2(
        report_id=f"report.{run_id}",
        campaign_id=f"campaign.{run_id}",
        run_id=run_id,
        question=planning.sanitized_question,
        executive_answer=answer,
        research_mode_label=planning.assessment.intent.mode.value,
        research_depth_label=planning.assessment.intent.depth.value,
        collaboration_label="标准协作",
        researchability=ResearchabilitySectionV2(
            strongest_answer_level=planning.data_planning.strongest_answer_level,
            answerable_levels=planning.assessment.answerable_levels,
            official_evaluation_ready=planning.data_planning.official_evaluation_ready,
            official_blockers=tuple(planning.data_planning.official_blockers),
            clarification_questions=tuple(planning.assessment.clarification_questions),
        ),
        data_coverage=DataCoverageV2(
            provider_label=source_label,
            available_sessions=len(snapshot.historical_universe),
            required_sessions=plan.minimum_total_sessions,
            available_symbols=len({row.symbol for row in snapshot.rows}),
            required_symbols=plan.minimum_symbol_count,
            lookback_sessions=plan.factor_lookback_sessions,
            label_horizon_sessions=plan.label_horizon_sessions,
            required_fields=tuple(sorted(item.field_name for item in planning.data_planning.field_availability if item.required_for_official)),
            optional_fields=tuple(sorted(item.field_name for item in planning.data_planning.field_availability if not item.required_for_official)),
            missing_required_fields=missing,
            field_availability_hash=planning.data_planning.artifact_hash,
        ),
        selected_research_groups=selected_groups,
        candidates=candidates,
        comparison_summary=f"候选竞赛状态：{tournament_status}。所有候选均已逐项记账；研究线索排序不等于晋级。",
        limitations=("当前只完成可见训练与验证证据；独立封闭检验由晋级控制器专有。", "研究小组只产生草案，机器证据服务拥有指标真值。"),
        next_actions=("对通过可见验证的候选执行独立封闭检验。", "对拒绝或修订候选按机器原因有界重试。"),
        artifact_hashes=tuple(sorted({campaign_hash, planning.artifact_hash, snapshot.artifact_hash, *(item.artifact_hash for item in selected_groups), *(item.artifact_hash for item in candidates)})),
    )
