"""Single deterministic PIT/calculation/evidence authority shared by every Desk."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Sequence

from ..adapters import DraftProposal
from ..contracts import CanonicalContract, ContractValidationError, FailureCode
from ..contracts.base import require_aware_datetime, require_hash, require_identifier
from ..data import (
    LABEL_FIELD,
    DatasetRegistry,
    MarketSnapshot,
    build_synthetic_market_fixture,
    canonical_content_hash,
)
from ..evidence import (
    AttemptController,
    CostModel,
    EvaluationRow,
    EvidenceService,
    ExperimentIdentity,
    OfficialEvidence,
    StartedAttempt,
    TrialLedger,
    TrialOutcome,
)
from ..evidence.models import VISIBLE_SUMMARY_METRICS
from ..factors import FactorResult, execute_factor_dag
from .models import DeskId, DeskSpec
from .providers import FeedbackEvidenceSummary


def _fail(code: FailureCode, message: str, **details) -> None:
    raise ContractValidationError(code, message, details=details)


@dataclass(frozen=True, kw_only=True)
class ResearchDatasetContext:
    registry: DatasetRegistry
    dataset_id: str
    decision_time: datetime
    evaluation_time: datetime
    seed: int

    def __post_init__(self) -> None:
        if not isinstance(self.registry, DatasetRegistry):
            _fail(FailureCode.INVALID_VALUE, "registry must be DatasetRegistry")
        require_identifier(self.dataset_id, "dataset_id")
        require_aware_datetime(self.decision_time, "decision_time")
        require_aware_datetime(self.evaluation_time, "evaluation_time")
        if self.decision_time > self.evaluation_time:
            _fail(FailureCode.INVALID_TIME_ORDER, "decision_time follows evaluation_time")


def build_default_research_dataset(*, seed: int = 7) -> ResearchDatasetContext:
    fixture = build_synthetic_market_fixture(seed=seed, dataset_id="research-dag-fixture-v1")
    registry = DatasetRegistry()
    card = fixture.register(registry)
    return ResearchDatasetContext(
        registry=registry,
        dataset_id=fixture.dataset_id,
        decision_time=fixture.calendar[-8],
        evaluation_time=card.decision_time,
        seed=seed,
    )


@dataclass(frozen=True, kw_only=True)
class CalculationResult(CanonicalContract):
    branch_id: str
    snapshot: MarketSnapshot
    factor_result: FactorResult
    pit_valid: bool

    def __post_init__(self) -> None:
        require_identifier(self.branch_id, "branch_id")
        if not isinstance(self.snapshot, MarketSnapshot) or not isinstance(self.factor_result, FactorResult):
            _fail(FailureCode.INVALID_VALUE, "calculation result has invalid artifacts")
        if self.pit_valid is not True:
            _fail(FailureCode.INVALID_STATE, "successful calculation must have passed PIT")


@dataclass(frozen=True, kw_only=True)
class ExperimentManifest(CanonicalContract):
    branch_id: str
    evidence_authority_id: str
    identity_hash: str
    dataset_hash: str
    universe_hash: str
    factor_dag_hash: str
    snapshot_hash: str
    factor_result_hash: str
    split_hash: str
    label_hash: str
    cost_hash: str
    code_hash: str
    availability_hash: str
    seed: int
    decision_time: datetime
    evaluation_time: datetime
    pit_valid: bool

    def __post_init__(self) -> None:
        require_identifier(self.branch_id, "branch_id")
        require_identifier(self.evidence_authority_id, "evidence_authority_id")
        for name in (
            "identity_hash",
            "dataset_hash",
            "universe_hash",
            "factor_dag_hash",
            "snapshot_hash",
            "factor_result_hash",
            "split_hash",
            "label_hash",
            "cost_hash",
            "code_hash",
            "availability_hash",
        ):
            require_hash(getattr(self, name), name)
        require_aware_datetime(self.decision_time, "decision_time")
        require_aware_datetime(self.evaluation_time, "evaluation_time")
        if self.pit_valid is not True:
            _fail(FailureCode.INVALID_STATE, "experiment manifest requires PIT success")


@dataclass(frozen=True, kw_only=True)
class EvaluationAttempt:
    identity: ExperimentIdentity
    started: StartedAttempt


@dataclass(frozen=True, kw_only=True)
class OfficialEvaluation(CanonicalContract):
    manifest: ExperimentManifest
    evidence: OfficialEvidence
    feedback_summary: FeedbackEvidenceSummary

    def __post_init__(self) -> None:
        if self.evidence.identity_hash != self.manifest.identity_hash:
            _fail(FailureCode.INVALID_HASH, "official evidence does not bind experiment manifest")
        if self.feedback_summary.identity_hash != self.evidence.identity_hash:
            _fail(FailureCode.INVALID_HASH, "feedback summary does not bind official evidence")


@dataclass(frozen=True, kw_only=True)
class AuthorityCall:
    authority_object_id: int
    authority_id: str
    run_id: str
    branch_id: str
    operation: str


class DeterministicOfficialEvidenceAuthority:
    """One object owns PIT calculation and official EvidenceService calls for a run."""

    authority_id = "official-evidence-authority-v1"

    def __init__(
        self,
        *,
        dataset: ResearchDatasetContext | None = None,
        cost_model: CostModel | None = None,
    ) -> None:
        self.dataset = dataset or build_default_research_dataset()
        self.cost_model = cost_model or CostModel(spread_bps=0.0, linear_impact_bps=0.0)
        self.service = EvidenceService()
        self._attempts: dict[str, AttemptController] = {}
        self._calls: list[AuthorityCall] = []
        self._lock = threading.Lock()

    @property
    def calls(self) -> tuple[AuthorityCall, ...]:
        with self._lock:
            return tuple(self._calls)

    def _record(self, *, run_id: str, branch_id: str, operation: str) -> None:
        with self._lock:
            self._calls.append(
                AuthorityCall(
                    authority_object_id=id(self),
                    authority_id=self.authority_id,
                    run_id=run_id,
                    branch_id=branch_id,
                    operation=operation,
                )
            )

    def prepare_run(self, *, run_id: str, run_dir: Path, desks: Sequence[DeskSpec]) -> None:
        require_identifier(run_id, "run_id")
        with self._lock:
            if run_id in self._attempts:
                _fail(FailureCode.ARTIFACT_COLLISION, "evidence authority run already exists")
            ledger = TrialLedger(
                run_dir / "trial_ledger.sqlite3",
                clock=lambda: self.dataset.evaluation_time,
            )
            for desk in desks:
                ledger.create_family(
                    f"{run_id}:{desk.desk_id.value}",
                    desk.budget.family_units,
                )
            self._attempts[run_id] = AttemptController(ledger)

    def _identity(
        self, *, run_id: str, desk: DeskSpec, proposal: DraftProposal
    ) -> ExperimentIdentity:
        dataset = self.dataset.registry.get(self.dataset.dataset_id)
        universe_hash = canonical_content_hash(dataset.memberships)
        split_hash = canonical_content_hash(
            {
                "partition": "VALIDATION",
                "decision_time": self.dataset.decision_time,
                "evaluation_time": self.dataset.evaluation_time,
            }
        )
        return ExperimentIdentity(
            experiment_id=f"{run_id}:{desk.desk_id.value}:experiment",
            dataset_hash=dataset.card.snapshot_hash,
            universe_hash=universe_hash,
            factor_hash=proposal.factor_dag.artifact_hash,
            split_hash=split_hash,
            label_hash=canonical_content_hash({"label": LABEL_FIELD, "horizon": "5d"}),
            cost_hash=self.cost_model.artifact_hash,
            code_hash=canonical_content_hash({"authority": self.authority_id}),
            availability_hash=dataset.card.artifact_hash,
            seed=self.dataset.seed,
        )

    def start_attempt(
        self, *, run_id: str, desk: DeskSpec, proposal: DraftProposal
    ) -> EvaluationAttempt:
        controller = self._attempts.get(run_id)
        if controller is None:
            _fail(FailureCode.INVALID_STATE, "evidence authority run is not prepared")
        identity = self._identity(run_id=run_id, desk=desk, proposal=proposal)
        started = controller.start(
            family_id=f"{run_id}:{desk.desk_id.value}",
            trial_id=f"{run_id}:{desk.desk_id.value}:trial",
            identity=identity,
            proposer_id=f"desk:{desk.desk_id.value}",
            units=1,
        )
        self._record(run_id=run_id, branch_id=desk.desk_id.value, operation="RESERVE_START")
        return EvaluationAttempt(identity=identity, started=started)

    def calculate(
        self,
        *,
        run_id: str,
        desk: DeskSpec,
        proposal: DraftProposal,
        decision_time: datetime,
    ) -> CalculationResult:
        if decision_time != self.dataset.decision_time:
            _fail(FailureCode.INVALID_TIME_ORDER, "question decision_time does not match frozen research dataset")
        dataset = self.dataset.registry.get(self.dataset.dataset_id)
        fields = tuple(sorted(set(proposal.factor_dag.required_fields) | {"volume", "macro_rate"}))
        snapshot = self.dataset.registry.as_of_snapshot(
            self.dataset.dataset_id,
            decision_time=decision_time,
            fields=fields,
            start_time=dataset.calendar[0],
        )
        factor_result = execute_factor_dag(
            proposal.factor_dag,
            snapshot,
            seed=self.dataset.seed,
            config={"authority": self.authority_id, "desk": desk.desk_id.value},
        )
        self._record(
            run_id=run_id,
            branch_id=desk.desk_id.value,
            operation="FACTOR_COMPILE_CALCULATE",
        )
        return CalculationResult(
            branch_id=desk.desk_id.value,
            snapshot=snapshot,
            factor_result=factor_result,
            pit_valid=True,
        )

    def evaluate(
        self,
        *,
        run_id: str,
        desk: DeskSpec,
        proposal: DraftProposal,
        attempt: EvaluationAttempt,
        calculation: CalculationResult,
    ) -> OfficialEvaluation:
        labels = self.dataset.registry.labels(
            self.dataset.dataset_id,
            label_field=LABEL_FIELD,
            evaluation_time=self.dataset.evaluation_time,
        )
        label_by_key = {(row.event_time, row.symbol): row.value for row in labels}
        snapshot_by_key = {
            (row.event_time, row.symbol): row.values for row in calculation.snapshot.rows
        }
        rows: list[EvaluationRow] = []
        for value in calculation.factor_result.values:
            key = (value.event_time, value.symbol)
            inputs = snapshot_by_key.get(key)
            label = label_by_key.get(key)
            if value.value is None or label is None or inputs is None:
                continue
            volume = inputs.get("volume")
            if not isinstance(volume, (int, float)) or isinstance(volume, bool) or volume <= 0:
                continue
            macro_rate = inputs.get("macro_rate")
            regime = "HIGH_RATE" if isinstance(macro_rate, (int, float)) and macro_rate >= 0.025 else "LOW_RATE"
            rows.append(
                EvaluationRow(
                    event_time=value.event_time,
                    symbol=value.symbol,
                    score=float(value.value),
                    label=float(label),
                    market_regime=regime,
                    volume=float(volume),
                )
            )
        if not rows:
            _fail(FailureCode.INVALID_VALUE, "calculated factor produced no evaluable visible rows")
        evidence = self.service.evaluate(
            identity=attempt.identity,
            rows=rows,
            partition="VALIDATION",
            cost_model=self.cost_model,
        )
        self._attempts[run_id].commit_success(attempt.started)
        metrics = evidence.metrics.flat()
        summary = FeedbackEvidenceSummary(
            identity_hash=evidence.identity_hash,
            visible_metrics={
                key: metrics[key]
                for key in sorted(VISIBLE_SUMMARY_METRICS)
                if key in metrics
            },
        )
        manifest = ExperimentManifest(
            branch_id=desk.desk_id.value,
            evidence_authority_id=self.authority_id,
            identity_hash=attempt.identity.identity_hash,
            dataset_hash=attempt.identity.dataset_hash,
            universe_hash=attempt.identity.universe_hash,
            factor_dag_hash=proposal.factor_dag.artifact_hash,
            snapshot_hash=calculation.snapshot.artifact_hash,
            factor_result_hash=calculation.factor_result.artifact_hash,
            split_hash=attempt.identity.split_hash,
            label_hash=attempt.identity.label_hash,
            cost_hash=attempt.identity.cost_hash,
            code_hash=attempt.identity.code_hash,
            availability_hash=attempt.identity.availability_hash,
            seed=attempt.identity.seed,
            decision_time=self.dataset.decision_time,
            evaluation_time=self.dataset.evaluation_time,
            pit_valid=True,
        )
        self._record(run_id=run_id, branch_id=desk.desk_id.value, operation="OFFICIAL_EVALUATE")
        return OfficialEvaluation(
            manifest=manifest,
            evidence=evidence,
            feedback_summary=summary,
        )

    def fail_started_attempt(
        self,
        *,
        run_id: str,
        attempt: EvaluationAttempt,
        outcome: TrialOutcome,
        failure_code: FailureCode,
    ) -> None:
        self._attempts[run_id].commit_failure(
            attempt.started,
            outcome=outcome,
            failure_code=failure_code,
        )
