"""Production identity binding for reviewed Agent research experiments.

This module deliberately contains no financial metric calculation.  It binds a
reviewed source artifact to one frozen data snapshot and supplies the exact
typed inputs consumed later by :class:`SandboxEvidenceEvaluator`.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from types import MappingProxyType
from typing import Any, Mapping, Sequence

from ..agent_research_sandbox.contracts import (
    DatasetAssessmentTable,
    DiagnosticTable,
    EventTable,
    ExperimentCodeIdentity,
    ExperimentDataIdentity,
    ExperimentReviewDecision,
    ExperimentReviewPolicy,
    ExperimentReviewRequest,
    RegressionTable,
    RegimeComparisonTable,
    SandboxAmendmentIdentity,
    ScoreTable,
)
from ..agent_research_sandbox.docker_runner import DockerResearchRequest, DockerTrainingObservation
from ..agent_research_sandbox.evaluator import ResearchTable
from ..agent_research_sandbox.process_runner import (
    AMENDMENT_VERSION,
    ResearchProcessReceipt,
    ResearchProcessRequest,
    canonical_hash,
)
from ..contracts.base import require_aware_datetime, require_hash, require_identifier
from ..data import LabelRow, MarketSnapshot, canonical_content_hash
from ..evidence import CostModel, RegisteredFactor
from ..research_agent_runtime import ResearchAgentOutcome
from ..research_run_controller import ResearchRunManifest


_TABLE_TYPES = {
    cls.__name__: cls
    for cls in (
        ScoreTable,
        EventTable,
        RegressionTable,
        RegimeComparisonTable,
        DiagnosticTable,
        DatasetAssessmentTable,
    )
}
_RESERVED_FEATURE_FIELDS = frozenset(
    {
        "label",
        "labels",
        "target",
        "target_value",
        "future_return",
        "official_metrics",
        "sealed",
        "promotion",
    }
)


@dataclass(frozen=True, slots=True)
class FrozenFeatureObservation:
    """One label-free row made available to generated research code."""

    symbol: str
    decision_time: datetime
    source_available_at: datetime
    features: Mapping[str, str | int | float | bool | None]

    def __post_init__(self) -> None:
        require_identifier(self.symbol, "symbol")
        require_aware_datetime(self.decision_time, "decision_time")
        require_aware_datetime(self.source_available_at, "source_available_at")
        if self.source_available_at > self.decision_time:
            raise ValueError("feature was unavailable at decision_time")
        features = dict(self.features)
        if not features or any(not isinstance(key, str) or not key for key in features):
            raise ValueError("features must be a non-empty string-keyed mapping")
        forbidden = {key.lower() for key in features} & _RESERVED_FEATURE_FIELDS
        if forbidden:
            raise ValueError("label, sealed, metric, and authority fields cannot enter prediction rows")
        object.__setattr__(self, "features", MappingProxyType(features))

    def to_runner_row(self) -> Mapping[str, Any]:
        # ``ResearchProcessRequest.input_hash`` canonicalizes plain JSON
        # mappings.  Immutability is provided by this frozen source contract;
        # the runner payload itself must remain serializable.
        return {
            "symbol": self.symbol,
            "decision_time": self.decision_time.isoformat(),
            "source_available_at": self.source_available_at.isoformat(),
            **dict(self.features),
        }


@dataclass(frozen=True, slots=True)
class ExperimentExecutionSpec:
    """Per-specialist output contract and optional separated training input."""

    output_kind: str
    feature_rows: tuple[FrozenFeatureObservation, ...]
    training_rows: tuple[DockerTrainingObservation, ...] = ()
    training_cutoff: str | None = None

    def __post_init__(self) -> None:
        if self.output_kind not in _TABLE_TYPES:
            raise ValueError("unsupported standard research table")
        if not self.feature_rows or not all(isinstance(row, FrozenFeatureObservation) for row in self.feature_rows):
            raise ValueError("feature_rows must be a non-empty typed tuple")
        keys = tuple((row.decision_time, row.symbol) for row in self.feature_rows)
        if len(keys) != len(set(keys)):
            raise ValueError("feature_rows contain duplicate decision_time/symbol keys")
        if tuple(sorted(keys)) != keys:
            raise ValueError("feature_rows must use deterministic decision_time/symbol ordering")
        if self.training_rows:
            if self.training_cutoff is None:
                raise ValueError("training_cutoff is required with training_rows")
        elif self.training_cutoff is not None:
            raise ValueError("training_cutoff without training_rows is invalid")


@dataclass(frozen=True, slots=True)
class ProductionEvidenceContext:
    """Frozen inputs that only the independent Evidence evaluator receives."""

    snapshot: MarketSnapshot
    labels: tuple[LabelRow, ...]
    dataset_hash: str
    availability_policy_hash: str
    split_hash: str
    cost_model: CostModel
    seed: int
    market_regimes: Mapping[str, str] = field(default_factory=dict)
    event_outcomes: Mapping[str, Mapping[str, float]] = field(default_factory=dict)
    field_coverage: Mapping[str, float] = field(default_factory=dict)
    condition_number: float | None = None
    event_window_start: int = 0
    event_window_end: int = 5
    registered_factors: tuple[RegisteredFactor, ...] = ()
    partition: str = "VALIDATION"
    annualization: int = 252
    quantile_count: int = 2
    authority_ceiling: str | None = None
    authority_reason: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.snapshot, MarketSnapshot):
            raise ValueError("typed MarketSnapshot is required")
        if not self.labels or not all(isinstance(row, LabelRow) for row in self.labels):
            raise ValueError("typed frozen labels are required")
        label_keys = tuple((row.event_time, row.symbol) for row in self.labels)
        if len(label_keys) != len(set(label_keys)) or tuple(sorted(label_keys)) != label_keys:
            raise ValueError("labels must be unique and deterministically ordered")
        for name in ("dataset_hash", "availability_policy_hash", "split_hash"):
            require_hash(getattr(self, name), name)
        if not isinstance(self.cost_model, CostModel):
            raise ValueError("typed CostModel is required")
        if isinstance(self.seed, bool) or not isinstance(self.seed, int) or self.seed < 0:
            raise ValueError("seed must be a non-negative integer")
        if self.partition not in {"TRAIN", "VALIDATION", "VISIBLE_TEST"}:
            raise ValueError("sealed partition is not accepted by the product binding")
        if self.annualization < 1 or not 2 <= self.quantile_count <= 10:
            raise ValueError("invalid Evidence evaluation policy")
        regimes = dict(self.market_regimes)
        if any(not isinstance(key, str) or not key or not isinstance(value, str) or not value for key, value in regimes.items()):
            raise ValueError("market_regimes must be a non-empty-string mapping")
        if not isinstance(self.registered_factors, tuple) or not all(
            isinstance(item, RegisteredFactor) for item in self.registered_factors
        ):
            raise ValueError("registered_factors must be a typed tuple")
        if self.authority_ceiling not in {None, "AUXILIARY_ONLY"}:
            raise ValueError("unsupported Evidence authority ceiling")
        if (self.authority_ceiling is None) != (self.authority_reason is None):
            raise ValueError("authority ceiling and reason must be declared together")
        if self.authority_reason is not None and not self.authority_reason.strip():
            raise ValueError("authority reason must be non-empty")
        outcomes: dict[str, Mapping[str, float]] = {}
        for event_id, values in dict(self.event_outcomes).items():
            if not isinstance(event_id, str) or not event_id or not isinstance(values, Mapping):
                raise ValueError("event_outcomes must be an event-id mapping")
            normalized = {}
            for name in ("car", "control_return", "placebo_car"):
                value = values.get(name)
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
                    raise ValueError("event outcomes must contain finite car/control/placebo values")
                normalized[name] = float(value)
            outcomes[event_id] = MappingProxyType(normalized)
        coverage = dict(self.field_coverage)
        if not coverage:
            total = len(self.snapshot.rows)
            coverage = {
                field_name: (
                    sum(row.values.get(field_name) is not None for row in self.snapshot.rows) / total
                    if total else 0.0
                )
                for field_name in self.snapshot.fields
            }
        if any(
            not isinstance(name, str) or not name
            or isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(float(value)) or not 0 <= float(value) <= 1
            for name, value in coverage.items()
        ):
            raise ValueError("field_coverage must contain finite fractions")
        if self.condition_number is not None and (
            isinstance(self.condition_number, bool)
            or not isinstance(self.condition_number, (int, float))
            or not math.isfinite(float(self.condition_number))
            or float(self.condition_number) < 0
        ):
            raise ValueError("condition_number must be finite and non-negative")
        if (
            isinstance(self.event_window_start, bool)
            or isinstance(self.event_window_end, bool)
            or not isinstance(self.event_window_start, int)
            or not isinstance(self.event_window_end, int)
            or self.event_window_start > self.event_window_end
        ):
            raise ValueError("event window is invalid")
        object.__setattr__(self, "market_regimes", MappingProxyType(regimes))
        object.__setattr__(self, "event_outcomes", MappingProxyType(outcomes))
        object.__setattr__(self, "field_coverage", MappingProxyType({
            key: float(value) for key, value in coverage.items()
        }))


class ProductionExperimentBinding:
    """Fail-closed production implementation of ``ExperimentBindingProtocol``."""

    def __init__(
        self,
        *,
        amendment: SandboxAmendmentIdentity,
        policy: ExperimentReviewPolicy,
        python_runtime_hash: str,
        dependency_manifest_hash: str,
        evidence_context: ProductionEvidenceContext,
        execution_specs: Mapping[str, ExperimentExecutionSpec],
        reviewer_id: str = "mechanical_reviewer",
    ) -> None:
        if not isinstance(amendment, SandboxAmendmentIdentity) or amendment.amendment_version != AMENDMENT_VERSION:
            raise ValueError("production binding requires the active typed sandbox amendment")
        if not isinstance(policy, ExperimentReviewPolicy):
            raise ValueError("typed review policy is required")
        require_hash(python_runtime_hash, "python_runtime_hash")
        require_hash(dependency_manifest_hash, "dependency_manifest_hash")
        require_identifier(reviewer_id, "reviewer_id")
        specs = dict(execution_specs)
        if not specs or not all(isinstance(value, ExperimentExecutionSpec) for value in specs.values()):
            raise ValueError("typed per-specialist execution specs are required")
        for specialist_id in specs:
            require_identifier(specialist_id, "specialist_id")
        self.amendment = amendment
        self.policy = policy
        self.python_runtime_hash = python_runtime_hash
        self.dependency_manifest_hash = dependency_manifest_hash
        self.evidence_context = evidence_context
        self.execution_specs = MappingProxyType(specs)
        self.reviewer_id = reviewer_id
        self.data_identity = ExperimentDataIdentity(
            dataset_hash=evidence_context.dataset_hash,
            snapshot_hash=evidence_context.snapshot.artifact_hash,
            universe_hash=canonical_hash(dict(evidence_context.snapshot.historical_universe)),
            schema_hash=canonical_hash({"fields": evidence_context.snapshot.fields}),
            availability_policy_hash=evidence_context.availability_policy_hash,
            decision_start=evidence_context.snapshot.start_time,
            decision_end=evidence_context.snapshot.decision_time,
        )
        self._validate_specs_against_snapshot()

    def review_request(
        self, *, manifest: ResearchRunManifest, outcome: ResearchAgentOutcome, source_text: str
    ) -> ExperimentReviewRequest:
        if not isinstance(manifest, ResearchRunManifest) or not isinstance(outcome, ResearchAgentOutcome):
            raise ValueError("typed manifest and Agent outcome are required")
        self._validate_manifest(manifest)
        if outcome.specialist_id not in self.execution_specs:
            raise ValueError("specialist has no frozen execution spec")
        source_hash = _source_hash(source_text)
        code = ExperimentCodeIdentity(
            source_hash=source_hash,
            python_runtime_hash=self.python_runtime_hash,
            dependency_manifest_hash=self.dependency_manifest_hash,
            review_policy_hash=self.policy.artifact_hash,
            amendment_hash=self.amendment.artifact_hash,
        )
        return ExperimentReviewRequest(
            experiment_id=f"{outcome.task_id}_{source_hash[-12:]}",
            proposer_id=outcome.specialist_id,
            reviewer_id=self.reviewer_id,
            source_text=source_text,
            code_identity=code,
            data_identity=self.data_identity,
            amendment=self.amendment,
            policy=self.policy,
        )

    def process_request(
        self,
        *,
        manifest: ResearchRunManifest,
        review_request: ExperimentReviewRequest,
        review_decision: ExperimentReviewDecision,
        source_text: str,
    ) -> ResearchProcessRequest | DockerResearchRequest:
        self._validate_manifest(manifest)
        self._validate_review_chain(review_request, review_decision, source_text)
        spec = self.execution_specs.get(review_request.proposer_id)
        if spec is None:
            raise ValueError("specialist has no frozen execution spec")
        base = ResearchProcessRequest(
            run_id=manifest.run_id,
            experiment_id=review_request.experiment_id,
            code_text=source_text,
            code_hash=review_request.code_identity.source_hash,
            code_identity_hash=review_request.code_identity.artifact_hash,
            data_identity_hash=review_request.data_identity.artifact_hash,
            review_decision=review_decision,
            feature_rows=tuple(row.to_runner_row() for row in spec.feature_rows),
            output_kind=spec.output_kind,
            seed=self.evidence_context.seed,
            amendment_hash=review_request.amendment.artifact_hash,
            amendment_version=AMENDMENT_VERSION,
        )
        if not spec.training_rows:
            return base
        return DockerResearchRequest(
            process_request=base,
            training_rows=spec.training_rows,
            training_cutoff=str(spec.training_cutoff),
        )

    def parse_table(
        self, *, review_request: ExperimentReviewRequest, receipt: ResearchProcessReceipt
    ) -> ResearchTable:
        if receipt.experiment_id != review_request.experiment_id:
            raise ValueError("runner receipt experiment identity mismatch")
        if receipt.code_hash != review_request.code_identity.source_hash:
            raise ValueError("runner receipt code identity mismatch")
        if receipt.output is None or not isinstance(receipt.output, Mapping):
            raise ValueError("runner output is missing or malformed")
        table_type = _TABLE_TYPES.get(receipt.output_kind)
        if table_type is None:
            raise ValueError("runner returned an unknown output contract")
        table = table_type.from_payload(receipt.output)
        if (
            table.experiment_id != review_request.experiment_id
            or table.code_identity_hash != review_request.code_identity.artifact_hash
            or table.data_identity_hash != review_request.data_identity.artifact_hash
        ):
            raise ValueError("standard table identity mismatch")
        return table

    def evidence_arguments(
        self,
        *,
        manifest: ResearchRunManifest,
        review_request: ExperimentReviewRequest,
        review_decision: ExperimentReviewDecision,
        receipt: ResearchProcessReceipt,
        table: ResearchTable,
    ) -> Mapping[str, Any]:
        self._validate_manifest(manifest)
        self._validate_review_chain(review_request, review_decision, review_request.source_text)
        if receipt.experiment_id != table.experiment_id:
            raise ValueError("receipt and standard table identity mismatch")
        expected_execution = self.process_request(
            manifest=manifest,
            review_request=review_request,
            review_decision=review_decision,
            source_text=review_request.source_text,
        )
        if receipt.input_hash != expected_execution.input_hash:
            raise ValueError("runner receipt does not bind the frozen execution request")
        if (
            receipt.code_hash != review_request.code_identity.source_hash
            or receipt.amendment_hash != self.amendment.artifact_hash
            or receipt.output_kind != self.execution_specs[review_request.proposer_id].output_kind
        ):
            raise ValueError("runner receipt authority identity mismatch")
        context = self.evidence_context
        return MappingProxyType(
            {
                "table": table,
                "amendment": self.amendment,
                "code_identity": review_request.code_identity,
                "data_identity": self.data_identity,
                "review_decision": review_decision,
                "runner_receipt": receipt,
                "snapshot": context.snapshot,
                "labels": context.labels,
                "split_hash": context.split_hash,
                "cost_model": context.cost_model,
                "seed": context.seed,
                "partition": context.partition,
                "market_regimes": context.market_regimes,
                "registered_factors": context.registered_factors,
                "annualization": context.annualization,
                "quantile_count": context.quantile_count,
                "event_outcomes": context.event_outcomes,
                "field_coverage": context.field_coverage,
                "condition_number": context.condition_number,
                "event_window_start": context.event_window_start,
                "event_window_end": context.event_window_end,
                **(
                    {}
                    if context.authority_ceiling is None
                    else {
                        "authority_ceiling": context.authority_ceiling,
                        "authority_reason": context.authority_reason,
                    }
                ),
            }
        )

    def _validate_manifest(self, manifest: ResearchRunManifest) -> None:
        if manifest.data_snapshot_hash != self.evidence_context.snapshot.artifact_hash:
            raise ValueError("canonical manifest does not bind the frozen MarketSnapshot")
        if manifest.policy_hash != self.policy.artifact_hash:
            raise ValueError("canonical manifest does not bind the review policy")
        if set(manifest.selected_group_ids) != set(self.execution_specs):
            raise ValueError("canonical manifest specialist set does not match execution specs")

    def _validate_review_chain(
        self,
        request: ExperimentReviewRequest,
        decision: ExperimentReviewDecision,
        source_text: str,
    ) -> None:
        if request.amendment.artifact_hash != self.amendment.artifact_hash:
            raise ValueError("review request amendment identity mismatch")
        if request.policy.artifact_hash != self.policy.artifact_hash:
            raise ValueError("review request policy identity mismatch")
        if request.data_identity.artifact_hash != self.data_identity.artifact_hash:
            raise ValueError("review request data identity mismatch")
        if request.code_identity.source_hash != _source_hash(source_text):
            raise ValueError("review request source identity mismatch")
        if (
            decision.experiment_id != request.experiment_id
            or decision.source_hash != request.code_identity.source_hash
            or decision.policy_hash != self.policy.artifact_hash
            or decision.amendment_hash != self.amendment.artifact_hash
        ):
            raise ValueError("review decision identity mismatch")

    def _validate_specs_against_snapshot(self) -> None:
        snapshot_rows = {}
        for row in self.evidence_context.snapshot.rows:
            key = (row.event_time.date(), row.symbol)
            if key in snapshot_rows:
                raise ValueError("snapshot contains duplicate symbol/session rows")
            snapshot_rows[key] = row
        for spec in self.execution_specs.values():
            for row in spec.feature_rows:
                source = snapshot_rows.get((row.decision_time.date(), row.symbol))
                feature_names = set(row.features)
                source_names = set(source.values) if source is not None else set()
                source_features = (
                    {
                        name: _normalize_frozen_feature_value(source.values[name])
                        for name in row.features
                    }
                    if source is not None and feature_names <= source_names
                    else None
                )
                execution_features = {
                    name: _normalize_frozen_feature_value(value)
                    for name, value in row.features.items()
                }
                if (
                    source is None
                    or source.event_time > row.source_available_at
                    or row.source_available_at > row.decision_time
                    or source_features is None
                    or source_features != execution_features
                ):
                    raise ValueError("execution feature row is not an exact frozen snapshot row")


def _normalize_frozen_feature_value(value: Any) -> Any:
    """Use one JSON missing-value representation without imputing data."""

    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _source_hash(source_text: str) -> str:
    import hashlib

    return "sha256:" + hashlib.sha256(source_text.encode("utf-8")).hexdigest()


__all__ = [
    "ExperimentExecutionSpec",
    "FrozenFeatureObservation",
    "ProductionEvidenceContext",
    "ProductionExperimentBinding",
]
