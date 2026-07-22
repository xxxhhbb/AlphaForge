"""Official Evidence admission for sandbox-produced research tables.

The research process never receives labels.  This adapter admits only a
``ScoreTable`` from an OS-enforced sandbox, independently joins frozen labels
and market capacity fields, creates the Evidence identity, and delegates all
metrics to :class:`veriquant.evidence.EvidenceService`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Mapping, Sequence

from ..contracts import ContractValidationError
from ..data import LabelRow, MarketSnapshot, canonical_content_hash
from ..evidence import (
    CostModel,
    EvaluationRow,
    EvidenceService,
    ExperimentIdentity,
    OfficialEvidence,
    RegisteredFactor,
)
from .contracts import (
    DatasetAssessmentTable,
    DiagnosticTable,
    EventTable,
    ExperimentCodeIdentity,
    ExperimentDataIdentity,
    ExperimentReviewDecision,
    MechanicalReviewStatus,
    RegressionTable,
    RegimeComparisonTable,
    ResearchOutputClass,
    SandboxAmendmentIdentity,
    ScoreTable,
)
from .process_runner import (
    ProcessIsolationLevel,
    ProcessRunStatus,
    ResearchProcessReceipt,
    canonical_hash,
)


AuxiliaryTable = (
    EventTable
    | RegressionTable
    | RegimeComparisonTable
    | DiagnosticTable
    | DatasetAssessmentTable
)
ResearchTable = ScoreTable | AuxiliaryTable


class EvidenceAdmissionStatus(str, Enum):
    OFFICIAL_EVIDENCE = "OFFICIAL_EVIDENCE"
    AUXILIARY_ONLY = "AUXILIARY_ONLY"
    BLOCKED = "BLOCKED"


class EvidenceAdmissionFailureCode(str, Enum):
    NON_SCORE_TABLE_AUXILIARY_ONLY = "NON_SCORE_TABLE_AUXILIARY_ONLY"
    EXPERIMENT_IDENTITY_MISMATCH = "EXPERIMENT_IDENTITY_MISMATCH"
    AMENDMENT_IDENTITY_MISMATCH = "AMENDMENT_IDENTITY_MISMATCH"
    CODE_IDENTITY_MISMATCH = "CODE_IDENTITY_MISMATCH"
    DATA_IDENTITY_MISMATCH = "DATA_IDENTITY_MISMATCH"
    REVIEW_IDENTITY_MISMATCH = "REVIEW_IDENTITY_MISMATCH"
    REVIEW_NOT_ACCEPTED = "REVIEW_NOT_ACCEPTED"
    RUNNER_NOT_COMPLETED = "RUNNER_NOT_COMPLETED"
    RUNNER_NOT_OS_SANDBOXED = "RUNNER_NOT_OS_SANDBOXED"
    RUNNER_NOT_OFFICIAL_ELIGIBLE = "RUNNER_NOT_OFFICIAL_ELIGIBLE"
    RUNNER_OUTPUT_TAMPERED = "RUNNER_OUTPUT_TAMPERED"
    SNAPSHOT_IDENTITY_MISMATCH = "SNAPSHOT_IDENTITY_MISMATCH"
    SCORE_OUTSIDE_DATA_WINDOW = "SCORE_OUTSIDE_DATA_WINDOW"
    SCORE_NOT_IN_SNAPSHOT = "SCORE_NOT_IN_SNAPSHOT"
    HISTORICAL_UNIVERSE_MISMATCH = "HISTORICAL_UNIVERSE_MISMATCH"
    AVAILABILITY_VIOLATION = "AVAILABILITY_VIOLATION"
    LABEL_MISSING = "LABEL_MISSING"
    LABEL_DUPLICATED = "LABEL_DUPLICATED"
    LABEL_TIME_VIOLATION = "LABEL_TIME_VIOLATION"
    CAPACITY_FIELD_INVALID = "CAPACITY_FIELD_INVALID"
    EVIDENCE_REJECTED = "EVIDENCE_REJECTED"


@dataclass(frozen=True, slots=True)
class SandboxEvidenceResult:
    status: EvidenceAdmissionStatus
    table_hash: str
    failure_code: EvidenceAdmissionFailureCode | None = None
    experiment_identity: ExperimentIdentity | None = None
    official_evidence: OfficialEvidence | None = None
    auxiliary_output_class: ResearchOutputClass | None = None
    evaluation_row_count: int = 0

    @property
    def artifact_hash(self) -> str:
        return canonical_hash(
            {
                "status": self.status,
                "table_hash": self.table_hash,
                "failure_code": self.failure_code,
                "experiment_identity_hash": (
                    None if self.experiment_identity is None else self.experiment_identity.identity_hash
                ),
                "official_evidence_hash": (
                    None if self.official_evidence is None else self.official_evidence.artifact_hash
                ),
                "auxiliary_output_class": self.auxiliary_output_class,
                "evaluation_row_count": self.evaluation_row_count,
            }
        )


class SandboxEvidenceEvaluator:
    """Fail-closed bridge from reviewed sandbox output to official Evidence."""

    def __init__(self, evidence_service: EvidenceService | None = None) -> None:
        self._evidence = evidence_service or EvidenceService()

    def evaluate(
        self,
        *,
        table: ResearchTable,
        amendment: SandboxAmendmentIdentity,
        code_identity: ExperimentCodeIdentity,
        data_identity: ExperimentDataIdentity,
        review_decision: ExperimentReviewDecision,
        runner_receipt: ResearchProcessReceipt,
        snapshot: MarketSnapshot,
        labels: Sequence[LabelRow],
        split_hash: str,
        cost_model: CostModel,
        seed: int,
        partition: str = "VALIDATION",
        market_regimes: Mapping[str, str] | None = None,
        registered_factors: Sequence[RegisteredFactor] = (),
        annualization: int = 252,
        quantile_count: int = 2,
        event_outcomes: Mapping[str, Mapping[str, float]] | None = None,
        field_coverage: Mapping[str, float] | None = None,
        condition_number: float | None = None,
        event_window_start: int = 0,
        event_window_end: int = 5,
    ) -> SandboxEvidenceResult:
        # These contexts are consumed by sibling deterministic evaluators in
        # the unified product gateway.  ScoreTable keeps its established
        # EvidenceService semantics and deliberately ignores them.
        del event_outcomes, field_coverage, condition_number, event_window_start, event_window_end
        if not isinstance(table, ScoreTable):
            return SandboxEvidenceResult(
                status=EvidenceAdmissionStatus.AUXILIARY_ONLY,
                table_hash=table.artifact_hash,
                failure_code=EvidenceAdmissionFailureCode.NON_SCORE_TABLE_AUXILIARY_ONLY,
                auxiliary_output_class=ResearchOutputClass.AUXILIARY_RESEARCH_EVIDENCE,
            )

        failure = validate_research_table_identities(
            table=table,
            amendment=amendment,
            code_identity=code_identity,
            data_identity=data_identity,
            review_decision=review_decision,
            receipt=runner_receipt,
            snapshot=snapshot,
        )
        if failure is not None:
            return self._blocked(table, failure)

        try:
            joined = self._join_rows(
                table=table,
                data_identity=data_identity,
                snapshot=snapshot,
                labels=labels,
                market_regimes=market_regimes or {},
            )
        except (ContractValidationError, ValueError, TypeError):
            return self._blocked(table, EvidenceAdmissionFailureCode.EVIDENCE_REJECTED)
        if isinstance(joined, EvidenceAdmissionFailureCode):
            return self._blocked(table, joined)
        evaluation_rows, used_labels = joined

        try:
            identity = ExperimentIdentity(
                experiment_id=table.experiment_id,
                dataset_hash=data_identity.dataset_hash,
                universe_hash=data_identity.universe_hash,
                factor_hash=table.artifact_hash,
                split_hash=split_hash,
                label_hash=canonical_content_hash(used_labels),
                cost_hash=cost_model.artifact_hash,
                code_hash=code_identity.artifact_hash,
                availability_hash=data_identity.availability_policy_hash,
                seed=seed,
            )
            evidence = self._evidence.evaluate(
                identity=identity,
                rows=evaluation_rows,
                partition=partition,
                cost_model=cost_model,
                registered_factors=registered_factors,
                annualization=annualization,
                quantile_count=quantile_count,
            )
        except (ContractValidationError, ValueError, TypeError):
            return self._blocked(table, EvidenceAdmissionFailureCode.EVIDENCE_REJECTED)
        return SandboxEvidenceResult(
            status=EvidenceAdmissionStatus.OFFICIAL_EVIDENCE,
            table_hash=table.artifact_hash,
            experiment_identity=identity,
            official_evidence=evidence,
            evaluation_row_count=len(evaluation_rows),
        )

    @staticmethod
    def _validate_identities(
        *,
        table: ResearchTable,
        amendment: SandboxAmendmentIdentity,
        code_identity: ExperimentCodeIdentity,
        data_identity: ExperimentDataIdentity,
        review_decision: ExperimentReviewDecision,
        receipt: ResearchProcessReceipt,
        snapshot: MarketSnapshot,
    ) -> EvidenceAdmissionFailureCode | None:
        return validate_research_table_identities(
            table=table,
            amendment=amendment,
            code_identity=code_identity,
            data_identity=data_identity,
            review_decision=review_decision,
            receipt=receipt,
            snapshot=snapshot,
        )

    @staticmethod
    def _join_rows(
        *,
        table: ScoreTable,
        data_identity: ExperimentDataIdentity,
        snapshot: MarketSnapshot,
        labels: Sequence[LabelRow],
        market_regimes: Mapping[str, str],
    ) -> tuple[tuple[EvaluationRow, ...], tuple[LabelRow, ...]] | EvidenceAdmissionFailureCode:
        label_by_key: dict[tuple[object, str], LabelRow] = {}
        for label in labels:
            key = (label.event_time.date(), label.symbol)
            if key in label_by_key:
                return EvidenceAdmissionFailureCode.LABEL_DUPLICATED
            label_by_key[key] = label
        snapshot_by_key: dict[tuple[object, str], object] = {}
        for row in snapshot.rows:
            key = (row.event_time.date(), row.symbol)
            if key in snapshot_by_key:
                return EvidenceAdmissionFailureCode.SNAPSHOT_IDENTITY_MISMATCH
            snapshot_by_key[key] = row
        universe_by_date: dict[object, tuple[str, ...]] = {}
        for raw_time, members in snapshot.historical_universe.items():
            try:
                session = datetime.fromisoformat(str(raw_time)).date()
            except ValueError:
                return EvidenceAdmissionFailureCode.HISTORICAL_UNIVERSE_MISMATCH
            if session in universe_by_date and universe_by_date[session] != tuple(members):
                return EvidenceAdmissionFailureCode.HISTORICAL_UNIVERSE_MISMATCH
            universe_by_date[session] = tuple(members)
        result: list[EvaluationRow] = []
        used_labels: list[LabelRow] = []
        for score in table.rows:
            if not data_identity.decision_start <= score.decision_time <= data_identity.decision_end:
                return EvidenceAdmissionFailureCode.SCORE_OUTSIDE_DATA_WINDOW
            if score.source_available_at > score.decision_time:
                return EvidenceAdmissionFailureCode.AVAILABILITY_VIOLATION
            key = (score.decision_time.date(), score.symbol)
            source = snapshot_by_key.get(key)
            if source is None:
                return EvidenceAdmissionFailureCode.SCORE_NOT_IN_SNAPSHOT
            if source.event_time > score.decision_time:
                return EvidenceAdmissionFailureCode.AVAILABILITY_VIOLATION
            members = universe_by_date.get(score.decision_time.date())
            if members is None or score.symbol not in members:
                return EvidenceAdmissionFailureCode.HISTORICAL_UNIVERSE_MISMATCH
            label = label_by_key.get(key)
            if label is None or label.value is None:
                return EvidenceAdmissionFailureCode.LABEL_MISSING
            if label.available_at < score.decision_time:
                return EvidenceAdmissionFailureCode.LABEL_TIME_VIOLATION
            volume = source.values.get("volume")
            if isinstance(volume, bool) or not isinstance(volume, (int, float)) or float(volume) <= 0:
                return EvidenceAdmissionFailureCode.CAPACITY_FIELD_INVALID
            regime_key = f"{score.decision_time.isoformat()}|{score.symbol}"
            result.append(
                EvaluationRow(
                    event_time=score.decision_time,
                    symbol=score.symbol,
                    score=score.score,
                    label=float(label.value),
                    market_regime=market_regimes.get(regime_key, "all"),
                    volume=float(volume),
                )
            )
            used_labels.append(label)
        return (
            tuple(sorted(result, key=lambda row: (row.event_time, row.symbol))),
            tuple(sorted(used_labels, key=lambda row: (row.event_time, row.symbol))),
        )

    @staticmethod
    def _blocked(table: ResearchTable, failure: EvidenceAdmissionFailureCode) -> SandboxEvidenceResult:
        return SandboxEvidenceResult(
            status=EvidenceAdmissionStatus.BLOCKED,
            table_hash=table.artifact_hash,
            failure_code=failure,
        )


def validate_research_table_identities(
    *,
    table: ResearchTable,
    amendment: SandboxAmendmentIdentity,
    code_identity: ExperimentCodeIdentity,
    data_identity: ExperimentDataIdentity,
    review_decision: ExperimentReviewDecision,
    receipt: ResearchProcessReceipt,
    snapshot: MarketSnapshot,
    require_official_eligibility: bool = True,
) -> EvidenceAdmissionFailureCode | None:
    """Validate the immutable review/runner/data chain for any standard table."""

    if table.experiment_id != receipt.experiment_id or table.experiment_id != review_decision.experiment_id:
        return EvidenceAdmissionFailureCode.EXPERIMENT_IDENTITY_MISMATCH
    if code_identity.amendment_hash != amendment.artifact_hash or receipt.amendment_hash != amendment.artifact_hash:
        return EvidenceAdmissionFailureCode.AMENDMENT_IDENTITY_MISMATCH
    if table.code_identity_hash != code_identity.artifact_hash or receipt.code_hash != code_identity.source_hash:
        return EvidenceAdmissionFailureCode.CODE_IDENTITY_MISMATCH
    if table.data_identity_hash != data_identity.artifact_hash:
        return EvidenceAdmissionFailureCode.DATA_IDENTITY_MISMATCH
    if review_decision.status is not MechanicalReviewStatus.ACCEPTED:
        return EvidenceAdmissionFailureCode.REVIEW_NOT_ACCEPTED
    if (
        review_decision.source_hash != code_identity.source_hash
        or review_decision.policy_hash != code_identity.review_policy_hash
        or review_decision.amendment_hash != amendment.artifact_hash
    ):
        return EvidenceAdmissionFailureCode.REVIEW_IDENTITY_MISMATCH
    if receipt.status is not ProcessRunStatus.COMPLETED_AUXILIARY:
        return EvidenceAdmissionFailureCode.RUNNER_NOT_COMPLETED
    if receipt.isolation_level is not ProcessIsolationLevel.OS_SANDBOXED:
        return EvidenceAdmissionFailureCode.RUNNER_NOT_OS_SANDBOXED
    if require_official_eligibility and receipt.official_evidence_eligible is not True:
        return EvidenceAdmissionFailureCode.RUNNER_NOT_OFFICIAL_ELIGIBLE
    if receipt.output_kind != type(table).__name__ or receipt.output is None or receipt.output_hash is None:
        return EvidenceAdmissionFailureCode.RUNNER_OUTPUT_TAMPERED
    expected_output_hash = canonical_hash(
        {"input_hash": receipt.input_hash, "output_kind": receipt.output_kind, "output": receipt.output}
    )
    if (
        receipt.output_hash != expected_output_hash
        or canonical_hash(receipt.output) != canonical_hash(table.to_dict())
    ):
        return EvidenceAdmissionFailureCode.RUNNER_OUTPUT_TAMPERED
    if snapshot.artifact_hash != data_identity.snapshot_hash:
        return EvidenceAdmissionFailureCode.SNAPSHOT_IDENTITY_MISMATCH
    return None


__all__ = [
    "EvidenceAdmissionFailureCode",
    "EvidenceAdmissionStatus",
    "SandboxEvidenceEvaluator",
    "SandboxEvidenceResult",
    "validate_research_table_identities",
]
