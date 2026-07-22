"""Deterministic V3 candidate comparison over visible official evidence.

The selector has no Promotion authority.  It converts the allowlisted
train/validation metrics already emitted by the Evidence Service into the
existing Campaign selection policy and records which candidates may be sent to
an independent promotion review.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import ClassVar, Mapping

from ..contracts import CanonicalContract, Component, FailureCode
from ..contracts.base import fail, require_hash, require_identifier
from ..research_campaign.aggregation import (
    CandidateDraftSource,
    CandidateSelectionInput,
    CandidateSelectionPolicy,
    CandidateSelectionRecord,
    CandidateSelectionVerdict,
    SelectionMetricBundle,
)
from .evidence_adapter import EvidenceAdapterStatus, U4EvidenceAdapterResult


SCHEMA_VERSION = "alphaforge.research-os-v3-selection/v1"


@dataclass(frozen=True, kw_only=True)
class V3CandidateSelectionResult(CanonicalContract):
    run_id: str
    evidence_result_hash: str
    selection_policy_hash: str
    records: tuple[CandidateSelectionRecord, ...]
    promotion_review_candidate_hashes: tuple[str, ...]
    authority: str = "DETERMINISTIC_VISIBLE_SELECTION"
    promotion_verdict: None = None
    SCHEMA_VERSION: ClassVar[str] = SCHEMA_VERSION

    def __post_init__(self) -> None:
        require_identifier(self.run_id, "run_id")
        require_hash(self.evidence_result_hash, "evidence_result_hash")
        require_hash(self.selection_policy_hash, "selection_policy_hash")
        keys = tuple((item.candidate_id, item.candidate_hash) for item in self.records)
        if keys != tuple(sorted(keys)) or len(keys) != len(set(keys)):
            fail(FailureCode.INVALID_VALUE, "selection records must be unique and stably sorted")
        for value in self.promotion_review_candidate_hashes:
            require_hash(value, "promotion_review_candidate_hashes")
        selected = tuple(
            item.candidate_hash
            for item in sorted(
                (
                    record
                    for record in self.records
                    if record.verdict is CandidateSelectionVerdict.SELECTED_FOR_PROMOTION_REVIEW
                ),
                key=lambda record: (-(record.score or 0.0), record.candidate_id, record.candidate_hash),
            )
        )
        if self.promotion_review_candidate_hashes != selected:
            fail(FailureCode.INVALID_STATE, "promotion review list must match selected records")
        if self.authority != "DETERMINISTIC_VISIBLE_SELECTION" or self.promotion_verdict is not None:
            fail(FailureCode.UNAUTHORIZED_CAPABILITY, "selection cannot issue a Promotion verdict")


class DeterministicVisibleCandidateSelector:
    """Apply the existing multi-metric policy to visible Evidence only."""

    def __init__(self, policy: CandidateSelectionPolicy | None = None) -> None:
        self.policy = policy or CandidateSelectionPolicy()

    def select(
        self,
        *,
        evidence: U4EvidenceAdapterResult,
        family_ids: Mapping[str, str],
        family_budget_limit: int,
        factor_dag_hashes: Mapping[str, str] | None = None,
        formula_program_hashes: Mapping[str, str] | None = None,
    ) -> V3CandidateSelectionResult:
        if not isinstance(evidence, U4EvidenceAdapterResult):
            fail(FailureCode.INVALID_VALUE, "evidence must be U4EvidenceAdapterResult")
        if isinstance(family_budget_limit, bool) or not isinstance(family_budget_limit, int) or family_budget_limit < 1:
            fail(FailureCode.INVALID_VALUE, "family_budget_limit must be positive")
        frozen_family_ids = MappingProxyType(dict(sorted(family_ids.items())))
        frozen_dag_hashes = MappingProxyType(dict(sorted((factor_dag_hashes or {}).items())))
        frozen_program_hashes = MappingProxyType(dict(sorted((formula_program_hashes or {}).items())))
        if set(frozen_dag_hashes) & set(frozen_program_hashes):
            fail(FailureCode.INVALID_VALUE, "candidate cannot claim both FactorDAG and Qlib program identity")
        records: list[CandidateSelectionRecord] = []
        consumed_by_family: dict[str, int] = {}
        prepared: list[tuple[object, str]] = []
        for terminal in evidence.terminals:
            family_id = frozen_family_ids.get(terminal.candidate_hash)
            if family_id is None:
                fail(FailureCode.MISSING_REQUIRED_FIELD, "candidate family identity is required")
            require_identifier(family_id, "family_id")
            if terminal.budget_consumed:
                consumed_by_family[family_id] = consumed_by_family.get(family_id, 0) + 1
            prepared.append((terminal, family_id))

        for terminal, family_id in prepared:
            consumed = consumed_by_family.get(family_id, 0)
            selection_input = _selection_input(
                terminal=terminal,
                family_id=family_id,
                family_budget_consumed=consumed,
                family_budget_limit=family_budget_limit,
            )
            reasons = _eligibility_failures(selection_input, self.policy)
            if reasons:
                records.append(CandidateSelectionRecord(
                    candidate_id=terminal.candidate_id,
                    candidate_hash=terminal.candidate_hash,
                    family_id=family_id,
                    verdict=CandidateSelectionVerdict.NOT_ELIGIBLE,
                    score=None,
                    reason_codes=reasons,
                    official_evidence_hash=selection_input.official_evidence_hash,
                ))
            else:
                assert selection_input.selection_metrics is not None
                records.append(CandidateSelectionRecord(
                    candidate_id=terminal.candidate_id,
                    candidate_hash=terminal.candidate_hash,
                    family_id=family_id,
                    verdict=CandidateSelectionVerdict.SELECTED_FOR_PROMOTION_REVIEW,
                    score=self.policy.score(selection_input.selection_metrics),
                    reason_codes=("PIT_EVIDENCE_CRITIC_VISIBLE_SELECTION_PASS",),
                    official_evidence_hash=selection_input.official_evidence_hash,
                ))

        eligible = sorted(
            (item for item in records if item.verdict is CandidateSelectionVerdict.SELECTED_FOR_PROMOTION_REVIEW),
            key=lambda item: (-(item.score or 0.0), item.candidate_id, item.candidate_hash),
        )
        selected_hashes: set[str] = set()
        selected_dags: set[str] = set()
        duplicate_hashes: set[str] = set()
        for item in eligible:
            program_hash = frozen_program_hashes.get(item.candidate_hash) or frozen_dag_hashes.get(item.candidate_hash)
            if program_hash is not None:
                require_hash(program_hash, "formula_program_hashes")
                if program_hash in selected_dags:
                    duplicate_hashes.add(item.candidate_hash)
                    continue
            if len(selected_hashes) >= self.policy.max_selected:
                continue
            selected_hashes.add(item.candidate_hash)
            if program_hash is not None:
                selected_dags.add(program_hash)
        stable: list[CandidateSelectionRecord] = []
        for item in records:
            if item.verdict is CandidateSelectionVerdict.SELECTED_FOR_PROMOTION_REVIEW and item.candidate_hash not in selected_hashes:
                item = CandidateSelectionRecord(
                    candidate_id=item.candidate_id,
                    candidate_hash=item.candidate_hash,
                    family_id=item.family_id,
                    verdict=CandidateSelectionVerdict.REVISE,
                    score=item.score,
                    reason_codes=(
                        (
                            "DUPLICATE_FACTOR_DAG"
                            if item.candidate_hash in frozen_dag_hashes
                            else "DUPLICATE_FORMULA_PROGRAM"
                        )
                        if item.candidate_hash in duplicate_hashes
                        else "CANDIDATE_SELECTION_BUDGET_LIMIT",
                    ),
                    official_evidence_hash=item.official_evidence_hash,
                )
            stable.append(item)
        stable.sort(key=lambda item: (item.candidate_id, item.candidate_hash))
        selected = tuple(
            item.candidate_hash
            for item in sorted(
                (
                    record
                    for record in stable
                    if record.verdict is CandidateSelectionVerdict.SELECTED_FOR_PROMOTION_REVIEW
                ),
                key=lambda record: (-(record.score or 0.0), record.candidate_id, record.candidate_hash),
            )
        )
        return V3CandidateSelectionResult(
            run_id=evidence.run_id,
            evidence_result_hash=evidence.artifact_hash,
            selection_policy_hash=self.policy.artifact_hash,
            records=tuple(stable),
            promotion_review_candidate_hashes=selected,
        )


def _selection_input(*, terminal, family_id: str, family_budget_consumed: int, family_budget_limit: int):
    visible = terminal.visible_metrics
    disposition = terminal.disposition
    official = terminal.status is EvidenceAdapterStatus.OFFICIAL_VISIBLE_EVALUATED
    train: dict[str, float] = {}
    validation: dict[str, float] = {}
    metrics = None
    if visible is not None:
        for key, value in visible.metrics.items():
            if key.startswith("train_"):
                train[key[6:]] = float(value)
            elif key.startswith("validation_"):
                validation[key[11:]] = float(value)
        metrics = SelectionMetricBundle(
            net_icir=float(validation.get("icir", 0.0)),
            stability=_stability(validation),
            cost_capacity=_cost_capacity(validation),
            novelty=_novelty(validation),
            incrementality=_incrementality(validation),
            family_budget_remaining=max(0, family_budget_limit - family_budget_consumed),
            family_budget_consumed=family_budget_consumed,
            visible_train_evidence_hash=visible.metric_source_hash,
            visible_validation_evidence_hash=visible.metric_source_hash,
            visible_train_metrics=train,
            visible_validation_metrics=validation,
        )
    critic_passed = disposition is not None and disposition.outcome == "VALIDATED_RESEARCH"
    return CandidateSelectionInput(
        candidate_id=terminal.candidate_id,
        candidate_hash=terminal.candidate_hash,
        family_id=family_id,
        proposer_id=f"research-specialist.{terminal.specialist_id}",
        draft_source=CandidateDraftSource.RESEARCH_DESK,
        source_component=Component.RESEARCH_DESK,
        pit_validated=official,
        official_evidence_hash=disposition.evidence_hash if official and disposition is not None else None,
        critic_passed=critic_passed,
        critic_finding_hashes=(disposition.decision_hash,) if critic_passed else (),
        critic_reason_codes=disposition.reason_codes if critic_passed else (),
        selection_metrics=metrics,
    )


def _eligibility_failures(candidate: CandidateSelectionInput, policy: CandidateSelectionPolicy) -> tuple[str, ...]:
    reasons: list[str] = []
    if not candidate.pit_validated:
        reasons.append("PIT_NOT_VALIDATED")
    if candidate.official_evidence_hash is None:
        reasons.append("OFFICIAL_EVIDENCE_MISSING")
    if not candidate.critic_passed:
        reasons.append("CRITIC_NOT_PASSED")
    metrics = candidate.selection_metrics
    if metrics is None:
        reasons.append("VISIBLE_SELECTION_METRICS_MISSING")
        return tuple(reasons)
    if metrics.family_budget_remaining < 1:
        reasons.append("FAMILY_BUDGET_EXHAUSTED")
    if metrics.net_icir < policy.min_net_icir:
        reasons.append("NET_ICIR_BELOW_POLICY")
    if metrics.stability < policy.min_stability:
        reasons.append("STABILITY_BELOW_POLICY")
    if metrics.cost_capacity < policy.min_cost_capacity:
        reasons.append("COST_CAPACITY_BELOW_POLICY")
    if metrics.novelty < policy.min_novelty:
        reasons.append("NOVELTY_BELOW_POLICY")
    if metrics.incrementality < policy.min_incrementality:
        reasons.append("INCREMENTALITY_BELOW_POLICY")
    return tuple(reasons)


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def _stability(values: Mapping[str, float]) -> float:
    return _clamp((values.get("subperiod_positive_fraction", 0.0) + values.get("regime_positive_fraction", 0.0)) / 2.0)


def _cost_capacity(values: Mapping[str, float]) -> float:
    gross = float(values.get("gross_return", 0.0))
    net = float(values.get("net_return", 0.0))
    turnover = max(0.0, float(values.get("turnover", 0.0)))
    retained = _clamp(net / gross) if gross > 0.0 else 0.0
    return _clamp(retained / (1.0 + turnover))


def _novelty(values: Mapping[str, float]) -> float:
    return _clamp(1.0 - abs(float(values.get("max_registered_abs_correlation", 0.0))))


def _incrementality(values: Mapping[str, float]) -> float:
    return _clamp(max(0.0, float(values.get("incremental_ic_mean", 0.0))) / 0.05)


__all__ = ["DeterministicVisibleCandidateSelector", "V3CandidateSelectionResult"]
