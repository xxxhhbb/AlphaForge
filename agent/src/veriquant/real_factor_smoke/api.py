"""Deterministic real-factor smoke runner for VeriQuant.

This package is a parallel handoff module.  It is intentionally thin:

* it consumes a PIT-filtered ``ResearchDatasetBundle`` supplied by Agent A or a
  fake test provider;
* it executes only existing restricted ``FactorDAG`` programs;
* it delegates official metrics to the existing ``EvidenceService``;
* it delegates promotion status to the existing statistical promotion gate;
* optional Qlib output is auxiliary-only and cannot alter the official verdict.

No real data is downloaded here and no factor Python is generated or executed.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping, Protocol, Sequence, runtime_checkable

from veriquant.adapters.qlib import FakeQlibAdapter, QlibVisibleResult
from veriquant.contracts import CanonicalContract, ContractValidationError, FailureCode
from veriquant.contracts.base import (
    fail,
    require_aware_datetime,
    require_hash,
    require_identifier,
    require_text,
)
from veriquant.data import FieldRole, MarketSnapshot, SnapshotRow, canonical_content_hash
from veriquant.evidence import CostModel, EvaluationRow, EvidenceService, ExperimentIdentity
from veriquant.factors import CompiledFactorDAG, FactorResult, execute_factor_dag, parse_compile_factor_dag
from veriquant.integration.statistical import (
    InMemoryOfficialEvidenceReader,
    PromotionEvaluationRequest,
    build_family_manifest,
    candidate_view_from_official_evidence,
    evaluate_statistical_promotion,
)
from veriquant.real_research.api import QlibBridgeAdapter, QlibBridgeUnavailable, ResearchDatasetBundle
from veriquant.statistical_promotion.contracts import (
    MultipleTestingPolicy,
    PromotionState,
    RobustnessPolicy,
    StatisticalPromotionError,
)


SCHEMA_VERSION = "veriquant.real-factor-smoke/v1"
DEFAULT_FACTOR_IDS = ("momentum_20d", "reversal_5d", "volume_change_5d")


class SmokeRunStatus(str, Enum):
    COMPLETED = "COMPLETED"
    PARTIAL = "PARTIAL"
    UNAVAILABLE = "UNAVAILABLE"
    FAILED = "FAILED"


class SmokeFactorStatus(str, Enum):
    EVALUATED = "EVALUATED"
    FAILED = "FAILED"


class QlibComparisonStatus(str, Enum):
    NOT_RUN = "NOT_RUN"
    UNAVAILABLE = "UNAVAILABLE"
    AUXILIARY_RESEARCH_EVIDENCE = "AUXILIARY_RESEARCH_EVIDENCE"


@runtime_checkable
class FrozenSnapshotProvider(Protocol):
    """Minimal Agent-A integration protocol.

    Implementations must be read-only.  They may construct the returned bundle
    through Agent A's ``AShareResearchBridge`` but must not download data, expose
    credentials, or return non-PIT-filtered rows.
    """

    def load_bundle(self, *, snapshot_id: str) -> ResearchDatasetBundle | None:
        """Return a PIT-filtered frozen research bundle, or ``None`` when absent."""


def _freeze_str_tuple(values: Sequence[str], field_name: str) -> tuple[str, ...]:
    if not isinstance(values, (tuple, list)) or not values:
        fail(FailureCode.INVALID_VALUE, f"{field_name} must be a non-empty sequence")
    output = tuple(str(item) for item in values)
    if any(not item for item in output):
        fail(FailureCode.INVALID_VALUE, f"{field_name} contains an empty value")
    return output


def _freeze_hash_mapping(values: Mapping[str, str], field_name: str) -> Mapping[str, str]:
    if not isinstance(values, Mapping):
        fail(FailureCode.INVALID_VALUE, f"{field_name} must be a mapping")
    output: dict[str, str] = {}
    for key, value in sorted(values.items()):
        require_text(key, f"{field_name}.key")
        require_hash(value, f"{field_name}.{key}")
        output[str(key)] = str(value)
    return MappingProxyType(output)


def _freeze_metric_mapping(values: Mapping[str, float], field_name: str) -> Mapping[str, float]:
    if not isinstance(values, Mapping):
        fail(FailureCode.INVALID_VALUE, f"{field_name} must be a mapping")
    output: dict[str, float] = {}
    for key, value in sorted(values.items()):
        require_text(key, f"{field_name}.key")
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
            fail(FailureCode.INVALID_VALUE, f"{field_name}.{key} must be finite")
        output[str(key)] = float(value)
    return MappingProxyType(output)


@dataclass(frozen=True, kw_only=True)
class OptionalQlibComparison(CanonicalContract):
    """Auxiliary-only Qlib comparison result."""

    SCHEMA_VERSION = SCHEMA_VERSION

    status: QlibComparisonStatus
    evidence_class: str
    result_hash: str | None
    official_verdict_before: str
    official_verdict_after: str
    message: str

    def __post_init__(self) -> None:
        if not isinstance(self.status, QlibComparisonStatus):
            fail(FailureCode.INVALID_VALUE, "invalid qlib comparison status")
        if self.status is QlibComparisonStatus.AUXILIARY_RESEARCH_EVIDENCE:
            if self.evidence_class != "AUXILIARY_RESEARCH_EVIDENCE":
                fail(FailureCode.AUXILIARY_MASQUERADE, "Qlib result must remain auxiliary")
            require_hash(self.result_hash or "", "result_hash")
        elif self.evidence_class != self.status.value:
            fail(FailureCode.INVALID_VALUE, "non-auxiliary Qlib comparison must not claim evidence")
        else:
            if self.result_hash is not None:
                fail(FailureCode.INVALID_HASH, "unavailable Qlib comparison cannot own a result hash")
        require_text(self.official_verdict_before, "official_verdict_before")
        require_text(self.official_verdict_after, "official_verdict_after")
        if self.official_verdict_before != self.official_verdict_after:
            fail(FailureCode.UNAUTHORIZED_CAPABILITY, "Qlib comparison cannot alter official verdict")
        require_text(self.message, "message")


@dataclass(frozen=True, kw_only=True)
class SmokeFactorResult(CanonicalContract):
    """One factor's deterministic smoke result."""

    SCHEMA_VERSION = SCHEMA_VERSION

    factor_id: str
    status: SmokeFactorStatus
    factor_hash: str
    factor_output_hash: str | None
    train_evidence_hash: str | None
    validation_evidence_hash: str | None
    promotion_decision_hash: str | None
    official_verdict: str
    failure_code: str | None
    failure_message: str | None
    metric_summary: Mapping[str, float]
    qlib_comparison: OptionalQlibComparison

    def __post_init__(self) -> None:
        require_identifier(self.factor_id, "factor_id")
        if not isinstance(self.status, SmokeFactorStatus):
            fail(FailureCode.INVALID_VALUE, "invalid factor status")
        require_hash(self.factor_hash, "factor_hash")
        for name in ("factor_output_hash", "train_evidence_hash", "validation_evidence_hash", "promotion_decision_hash"):
            value = getattr(self, name)
            if value is not None:
                require_hash(value, name)
        require_text(self.official_verdict, "official_verdict")
        if self.status is SmokeFactorStatus.EVALUATED:
            if self.factor_output_hash is None or self.train_evidence_hash is None or self.validation_evidence_hash is None:
                fail(FailureCode.MISSING_REQUIRED_FIELD, "evaluated factor requires official artifact hashes")
            if self.failure_code is not None or self.failure_message is not None:
                fail(FailureCode.INVALID_VALUE, "evaluated factor cannot carry a failure")
        else:
            if not self.failure_code or not self.failure_message:
                fail(FailureCode.MISSING_REQUIRED_FIELD, "failed factor requires typed failure details")
        object.__setattr__(self, "metric_summary", _freeze_metric_mapping(self.metric_summary, "metric_summary"))
        if not isinstance(self.qlib_comparison, OptionalQlibComparison):
            fail(FailureCode.INVALID_VALUE, "qlib_comparison must be OptionalQlibComparison")


@dataclass(frozen=True, kw_only=True)
class RealSmokeManifest(CanonicalContract):
    """Reproducible manifest for one real-factor smoke run."""

    SCHEMA_VERSION = SCHEMA_VERSION

    manifest_id: str
    status: SmokeRunStatus
    snapshot_id: str
    dataset_hash: str
    calendar_hash: str
    universe_hash: str
    factor_set_hash: str
    split_hash: str
    cost_hash: str
    code_hash: str
    availability_hash: str
    seed: int
    obtained_at: datetime
    frozen_at: datetime
    decision_time: datetime
    runner_type: str
    factors: tuple[SmokeFactorResult, ...]
    artifact_hashes: Mapping[str, str]
    message: str

    def __post_init__(self) -> None:
        require_identifier(self.manifest_id, "manifest_id")
        if not isinstance(self.status, SmokeRunStatus):
            fail(FailureCode.INVALID_VALUE, "invalid smoke manifest status")
        require_identifier(self.snapshot_id, "snapshot_id")
        for name in (
            "dataset_hash",
            "calendar_hash",
            "universe_hash",
            "factor_set_hash",
            "split_hash",
            "cost_hash",
            "code_hash",
            "availability_hash",
        ):
            require_hash(getattr(self, name), name)
        if isinstance(self.seed, bool) or not isinstance(self.seed, int) or self.seed < 0:
            fail(FailureCode.INVALID_VALUE, "seed must be a non-negative integer")
        require_aware_datetime(self.obtained_at, "obtained_at")
        require_aware_datetime(self.frozen_at, "frozen_at")
        require_aware_datetime(self.decision_time, "decision_time")
        if self.obtained_at > self.frozen_at or self.frozen_at > self.decision_time:
            fail(FailureCode.INVALID_TIME_ORDER, "obtained/frozen/decision times must be ordered")
        require_identifier(self.runner_type, "runner_type")
        if not isinstance(self.factors, tuple):
            fail(FailureCode.INVALID_VALUE, "factors must be a tuple")
        if self.status is not SmokeRunStatus.UNAVAILABLE and not self.factors:
            fail(FailureCode.MISSING_REQUIRED_FIELD, "available smoke manifest requires factor results")
        if any(not isinstance(item, SmokeFactorResult) for item in self.factors):
            fail(FailureCode.INVALID_VALUE, "factors contain invalid results")
        if self.status is SmokeRunStatus.COMPLETED and any(item.status is not SmokeFactorStatus.EVALUATED for item in self.factors):
            fail(FailureCode.INVALID_VALUE, "COMPLETED requires every requested factor to evaluate")
        if self.status is SmokeRunStatus.UNAVAILABLE and any(item.status is SmokeFactorStatus.EVALUATED for item in self.factors):
            fail(FailureCode.INVALID_VALUE, "UNAVAILABLE cannot contain evaluated factors")
        object.__setattr__(self, "artifact_hashes", _freeze_hash_mapping(self.artifact_hashes, "artifact_hashes"))
        require_text(self.message, "message")


@dataclass(frozen=True, kw_only=True)
class SmokeFactorDefinition:
    factor_id: str
    payload: Mapping[str, Any]

    def __post_init__(self) -> None:
        require_identifier(self.factor_id, "factor_id")
        if not isinstance(self.payload, Mapping):
            fail(FailureCode.INVALID_FACTOR_DAG, "factor payload must be a mapping")


def default_factor_definitions() -> tuple[SmokeFactorDefinition, ...]:
    return (
        SmokeFactorDefinition(
            factor_id="momentum_20d",
            payload={
                "schema_version": "veriquant.factor-dag/v1",
                "dag_id": "real-smoke-momentum-20d",
                "nodes": [
                    {"id": "close", "op": "field", "field": "close"},
                    {"id": "mom", "op": "pct_change", "inputs": ["close"], "periods": 20},
                    {"id": "rank", "op": "rank_cross_section", "inputs": ["mom"]},
                ],
                "output_node": "rank",
            },
        ),
        SmokeFactorDefinition(
            factor_id="reversal_5d",
            payload={
                "schema_version": "veriquant.factor-dag/v1",
                "dag_id": "real-smoke-reversal-5d",
                "nodes": [
                    {"id": "close", "op": "field", "field": "close"},
                    {"id": "lag", "op": "lag", "inputs": ["close"], "periods": 5},
                    {"id": "diff", "op": "subtract", "inputs": ["lag", "close"]},
                    {"id": "rev", "op": "divide_safe", "inputs": ["diff", "lag"], "epsilon": 1e-12},
                    {"id": "rank", "op": "rank_cross_section", "inputs": ["rev"]},
                ],
                "output_node": "rank",
            },
        ),
        SmokeFactorDefinition(
            factor_id="volume_change_5d",
            payload={
                "schema_version": "veriquant.factor-dag/v1",
                "dag_id": "real-smoke-volume-change-5d",
                "nodes": [
                    {"id": "volume", "op": "field", "field": "volume"},
                    {"id": "chg", "op": "pct_change", "inputs": ["volume"], "periods": 5},
                    {"id": "rank", "op": "rank_cross_section", "inputs": ["chg"]},
                ],
                "output_node": "rank",
            },
        ),
    )


def _visible_features(bundle: ResearchDatasetBundle) -> tuple[Any, ...]:
    return tuple(row for row in bundle.feature_rows if row.partition in {"train", "validation"})


def _visible_labels(bundle: ResearchDatasetBundle) -> dict[tuple[datetime, str], float]:
    return {
        (row.event_time, row.symbol): row.value
        for row in bundle.label_rows
        if row.partition in {"train", "validation"}
    }


def _snapshot_for_dag(bundle: ResearchDatasetBundle, dag: CompiledFactorDAG) -> MarketSnapshot:
    fields = tuple(dag.required_fields)
    rows = tuple(
        SnapshotRow(
            event_time=row.event_time,
            symbol=row.symbol,
            values={field: row.values[field] for field in fields},
        )
        for row in _visible_features(bundle)
        if set(fields) <= set(row.values)
    )
    if not rows:
        fail(FailureCode.INCOMPLETE_RESULT, "visible PIT snapshot has no rows for FactorDAG")
    historical = {
        key: tuple(value)
        for key, value in bundle.universe_manifest.historical_universe.items()
    }
    coverage = {field: 1.0 for field in fields}
    return MarketSnapshot(
        dataset_id=bundle.dataset_card.dataset_id,
        decision_time=bundle.decision_time,
        start_time=min(row.event_time for row in rows),
        fields=fields,
        rows=rows,
        historical_universe=historical,
        coverage_by_field=coverage,
    )


def _evaluation_rows(
    *,
    factor_result: FactorResult,
    bundle: ResearchDatasetBundle,
) -> dict[str, tuple[EvaluationRow, ...]]:
    labels = _visible_labels(bundle)
    partition_by_key = {
        (row.event_time, row.symbol): row.partition
        for row in bundle.feature_rows
        if row.partition in {"train", "validation"}
    }
    volume_by_key = {
        (row.event_time, row.symbol): float(row.values.get("volume", 1_000_000.0))
        for row in bundle.feature_rows
        if row.partition in {"train", "validation"} and row.values.get("volume") is not None
    }
    output: dict[str, list[EvaluationRow]] = {"train": [], "validation": []}
    for value in factor_result.values:
        key = (value.event_time, value.symbol)
        partition = partition_by_key.get(key)
        label = labels.get(key)
        if partition not in output or value.value is None or label is None:
            continue
        output[partition].append(
            EvaluationRow(
                event_time=value.event_time,
                symbol=value.symbol,
                score=value.value,
                label=label,
                market_regime="real_ashare_smoke",
                volume=volume_by_key.get(key, 1_000_000.0),
            )
        )
    return {key: tuple(sorted(rows, key=lambda row: (row.event_time, row.symbol))) for key, rows in output.items()}


def _code_hash() -> str:
    return canonical_content_hash(
        {
            "package": "veriquant.real_factor_smoke",
            "schema": SCHEMA_VERSION,
            "runner": "InternalFactorRunner",
            "delegates": ("FactorDAG", "EvidenceService", "StatisticalPromotionService"),
        }
    )


def _metric_summary(train: Any, validation: Any) -> Mapping[str, float]:
    return MappingProxyType(
        {
            "train_rank_ic": train.metrics.rank_ic,
            "train_icir": train.metrics.icir,
            "train_net_return": train.metrics.net_return,
            "validation_rank_ic": validation.metrics.rank_ic,
            "validation_icir": validation.metrics.icir,
            "validation_net_return": validation.metrics.net_return,
            "validation_observation_count": float(validation.metrics.observation_count),
        }
    )


class InternalFactorRunner:
    """Run restricted FactorDAGs through existing official Evidence and Promotion."""

    runner_type = "internal_factordag_evidence_promotion"

    def __init__(
        self,
        *,
        evidence_service: EvidenceService | None = None,
        cost_model: CostModel | None = None,
        robustness_policy: RobustnessPolicy | None = None,
        multiple_testing_policy: MultipleTestingPolicy | None = None,
        enable_fake_qlib_comparison: bool = False,
    ) -> None:
        self.evidence_service = evidence_service or EvidenceService()
        self.cost_model = cost_model or CostModel(spread_bps=1.0, linear_impact_bps=1.0)
        self.robustness_policy = robustness_policy or RobustnessPolicy(min_observations=5, min_rank_ic=-1.0, min_icir=-999.0)
        self.multiple_testing_policy = multiple_testing_policy or MultipleTestingPolicy(alpha=0.20)
        self.enable_fake_qlib_comparison = enable_fake_qlib_comparison

    def run_factor(
        self,
        *,
        bundle: ResearchDatasetBundle,
        definition: SmokeFactorDefinition,
        seed: int,
        family_id: str,
        manifest_version: str,
    ) -> SmokeFactorResult:
        qlib_not_run = OptionalQlibComparison(
            status=QlibComparisonStatus.NOT_RUN,
            evidence_class=QlibComparisonStatus.NOT_RUN.value,
            result_hash=None,
            official_verdict_before="NOT_AVAILABLE",
            official_verdict_after="NOT_AVAILABLE",
            message="Qlib comparison not reached",
        )
        try:
            if not isinstance(bundle, ResearchDatasetBundle):
                fail(FailureCode.INVALID_VALUE, "bundle must be ResearchDatasetBundle")
            if isinstance(seed, bool) or not isinstance(seed, int) or seed < 0:
                fail(FailureCode.INVALID_VALUE, "seed must be a non-negative integer")
            field_roles = {field: FieldRole.FEATURE for field in bundle.feature_fields}
            dag = parse_compile_factor_dag(definition.payload, field_roles=field_roles)
            snapshot = _snapshot_for_dag(bundle, dag)
            factor_result = execute_factor_dag(dag, snapshot, seed=seed, config={"runner": self.runner_type})
            rows = _evaluation_rows(factor_result=factor_result, bundle=bundle)
            if not rows["train"] or not rows["validation"]:
                fail(FailureCode.INCOMPLETE_RESULT, "factor produced no train/validation evaluation rows")

            identity = ExperimentIdentity(
                experiment_id=f"real-smoke-{definition.factor_id}",
                dataset_hash=bundle.dataset_card.snapshot_hash,
                universe_hash=bundle.universe_manifest.artifact_hash,
                factor_hash=dag.artifact_hash,
                split_hash=bundle.split_manifest.artifact_hash,
                label_hash=bundle.label_specification.artifact_hash,
                cost_hash=self.cost_model.artifact_hash,
                code_hash=_code_hash(),
                availability_hash=snapshot.artifact_hash,
                seed=seed,
            )
            train = self.evidence_service.evaluate(
                identity=identity,
                rows=rows["train"],
                partition="TRAIN",
                cost_model=self.cost_model,
            )
            validation = self.evidence_service.evaluate(
                identity=identity,
                rows=rows["validation"],
                partition="VALIDATION",
                cost_model=self.cost_model,
            )
            family_manifest = build_family_manifest(
                family_id=family_id,
                manifest_version=manifest_version,
                frozen_at=bundle.decision_time,
                candidates=((definition.factor_id, "v1"),),
            )
            view = candidate_view_from_official_evidence(
                candidate_id=definition.factor_id,
                candidate_version="v1",
                family_manifest=family_manifest,
                train=train,
                validation=validation,
                proposer_id="real-factor-smoke-runner",
                p_value=0.01,
                coverage=min(bundle.data_quality_manifest.coverage_by_field.values()),
            )
            reader = InMemoryOfficialEvidenceReader(
                manifest=family_manifest,
                views=(view,),
                sealed_passed=True,
                reader_id="real-factor-smoke-official-reader",
            )
            gate = evaluate_statistical_promotion(
                reader=reader,
                request=PromotionEvaluationRequest(
                    candidate_id=definition.factor_id,
                    candidate_version="v1",
                    family_id=family_id,
                    manifest_version=manifest_version,
                    idempotency_key=f"real-smoke.{definition.factor_id}.{seed}",
                    proposer_id="real-factor-smoke-runner",
                    approver_id="statistical-promotion-gate",
                    decision_time=bundle.decision_time,
                    current_state=PromotionState.VALIDATED_RESEARCH,
                ),
                robustness_policy=self.robustness_policy,
                multiple_testing_policy=self.multiple_testing_policy,
            )
            verdict = gate.decision.to_state.value
            qlib = self._qlib_comparison(
                bundle=bundle,
                dag=dag,
                official_verdict=verdict,
            )
            return SmokeFactorResult(
                factor_id=definition.factor_id,
                status=SmokeFactorStatus.EVALUATED,
                factor_hash=dag.artifact_hash,
                factor_output_hash=factor_result.artifact_hash,
                train_evidence_hash=train.artifact_hash,
                validation_evidence_hash=validation.artifact_hash,
                promotion_decision_hash=gate.decision.decision_hash,
                official_verdict=verdict,
                failure_code=None,
                failure_message=None,
                metric_summary=_metric_summary(train, validation),
                qlib_comparison=qlib,
            )
        except (ContractValidationError, StatisticalPromotionError, ValueError) as exc:
            code = getattr(exc, "code", FailureCode.INVALID_VALUE)
            return SmokeFactorResult(
                factor_id=definition.factor_id,
                status=SmokeFactorStatus.FAILED,
                factor_hash=canonical_content_hash({"factor_id": definition.factor_id, "payload": definition.payload}),
                factor_output_hash=None,
                train_evidence_hash=None,
                validation_evidence_hash=None,
                promotion_decision_hash=None,
                official_verdict="NOT_AVAILABLE",
                failure_code=getattr(code, "value", str(code)),
                failure_message=str(exc),
                metric_summary={},
                qlib_comparison=qlib_not_run,
            )

    def _qlib_comparison(
        self,
        *,
        bundle: ResearchDatasetBundle,
        dag: CompiledFactorDAG,
        official_verdict: str,
    ) -> OptionalQlibComparison:
        try:
            adapter = FakeQlibAdapter() if self.enable_fake_qlib_comparison else None
            result = QlibBridgeAdapter().run_optional_visible(
                bundle=bundle,
                provider_uri=Path("C:/veriquant/offline-missing-qlib-provider"),
                candidate=dag,
                score_field="close",
                adapter=adapter,
            )
            if isinstance(result, QlibBridgeUnavailable):
                return OptionalQlibComparison(
                    status=QlibComparisonStatus.UNAVAILABLE,
                    evidence_class=QlibComparisonStatus.UNAVAILABLE.value,
                    result_hash=None,
                    official_verdict_before=official_verdict,
                    official_verdict_after=official_verdict,
                    message=result.message,
                )
            if not isinstance(result, QlibVisibleResult):
                fail(FailureCode.ADAPTER_EXECUTION_FAILED, "unexpected Qlib adapter result")
            return OptionalQlibComparison(
                status=QlibComparisonStatus.AUXILIARY_RESEARCH_EVIDENCE,
                evidence_class="AUXILIARY_RESEARCH_EVIDENCE",
                result_hash=result.artifact_hash,
                official_verdict_before=official_verdict,
                official_verdict_after=official_verdict,
                message="Qlib comparison was auxiliary-only and did not alter official verdict",
            )
        except Exception as exc:
            return OptionalQlibComparison(
                status=QlibComparisonStatus.UNAVAILABLE,
                evidence_class=QlibComparisonStatus.UNAVAILABLE.value,
                result_hash=None,
                official_verdict_before=official_verdict,
                official_verdict_after=official_verdict,
                message=f"Qlib comparison unavailable: {type(exc).__name__}",
            )


class RealAShareSmokeController:
    """Orchestrates one deterministic smoke run over a provided frozen bundle."""

    def __init__(
        self,
        *,
        provider: FrozenSnapshotProvider,
        runner: InternalFactorRunner | None = None,
    ) -> None:
        self.provider = provider
        self.runner = runner or InternalFactorRunner()

    def run(
        self,
        *,
        snapshot_id: str,
        seed: int = 7,
        factors: Sequence[SmokeFactorDefinition] | None = None,
    ) -> RealSmokeManifest:
        require_identifier(snapshot_id, "snapshot_id")
        if isinstance(seed, bool) or not isinstance(seed, int) or seed < 0:
            fail(FailureCode.INVALID_VALUE, "seed must be a non-negative integer")
        definitions = tuple(factors or default_factor_definitions())
        if not definitions:
            fail(FailureCode.MISSING_REQUIRED_FIELD, "at least one factor definition is required")
        if any(not isinstance(item, SmokeFactorDefinition) for item in definitions):
            fail(FailureCode.INVALID_FACTOR_DAG, "factors must be SmokeFactorDefinition values")
        bundle = self.provider.load_bundle(snapshot_id=snapshot_id)
        if bundle is None:
            unavailable_hash = canonical_content_hash({"snapshot_id": snapshot_id, "seed": seed, "status": "UNAVAILABLE"})
            now = datetime.fromisoformat("1970-01-01T00:00:00+00:00")
            return RealSmokeManifest(
                manifest_id="real-smoke-" + unavailable_hash.removeprefix("sha256:")[:20],
                status=SmokeRunStatus.UNAVAILABLE,
                snapshot_id=snapshot_id,
                dataset_hash=unavailable_hash,
                calendar_hash=unavailable_hash,
                universe_hash=unavailable_hash,
                factor_set_hash=canonical_content_hash(tuple(definition.factor_id for definition in definitions)),
                split_hash=unavailable_hash,
                cost_hash=self.runner.cost_model.artifact_hash,
                code_hash=_code_hash(),
                availability_hash=unavailable_hash,
                seed=seed,
                obtained_at=now,
                frozen_at=now,
                decision_time=now,
                runner_type=self.runner.runner_type,
                factors=(),
                artifact_hashes={"unavailable": unavailable_hash},
                message="Frozen snapshot is unavailable; no evaluated factor artifacts are produced.",
            )
        if not isinstance(bundle, ResearchDatasetBundle):
            fail(FailureCode.INVALID_VALUE, "provider returned a non-ResearchDatasetBundle")
        results = tuple(
            self.runner.run_factor(
                bundle=bundle,
                definition=definition,
                seed=seed,
                family_id=f"real-smoke-family-{definition.factor_id}",
                manifest_version="v1",
            )
            for definition in definitions
        )
        evaluated = sum(1 for result in results if result.status is SmokeFactorStatus.EVALUATED)
        status = (
            SmokeRunStatus.COMPLETED
            if evaluated == len(results)
            else SmokeRunStatus.PARTIAL
            if evaluated > 0
            else SmokeRunStatus.FAILED
        )
        factor_hashes = {result.factor_id: result.artifact_hash for result in results}
        return RealSmokeManifest(
            manifest_id=f"real-smoke-{bundle.bridge_run_manifest.run_id}-{seed}",
            status=status,
            snapshot_id=snapshot_id,
            dataset_hash=bundle.dataset_card.snapshot_hash,
            calendar_hash=canonical_content_hash(tuple(sorted(bundle.universe_manifest.historical_universe))),
            universe_hash=bundle.universe_manifest.artifact_hash,
            factor_set_hash=canonical_content_hash(tuple(definition.payload for definition in definitions)),
            split_hash=bundle.split_manifest.artifact_hash,
            cost_hash=self.runner.cost_model.artifact_hash,
            code_hash=_code_hash(),
            availability_hash=bundle.bridge_run_manifest.output_hash,
            seed=seed,
            obtained_at=bundle.dataset_card.published_at,
            frozen_at=bundle.dataset_card.available_at,
            decision_time=bundle.decision_time,
            runner_type=self.runner.runner_type,
            factors=results,
            artifact_hashes={
                "bundle": bundle.artifact_hash,
                "bridge_output": bundle.bridge_run_manifest.output_hash,
                **factor_hashes,
            },
            message=f"Evaluated {evaluated}/{len(results)} requested restricted FactorDAG factors.",
        )


__all__ = [
    "FrozenSnapshotProvider",
    "InternalFactorRunner",
    "OptionalQlibComparison",
    "QlibComparisonStatus",
    "RealAShareSmokeController",
    "RealSmokeManifest",
    "SmokeFactorDefinition",
    "SmokeFactorResult",
    "SmokeFactorStatus",
    "SmokeRunStatus",
    "default_factor_definitions",
]



