"""Unified deterministic Evidence admission for all standard research outputs."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Mapping

from ..agent_research_sandbox.contracts import ScoreTable
from ..agent_research_sandbox.evaluator import (
    EvidenceAdmissionStatus,
    SandboxEvidenceEvaluator,
    validate_research_table_identities,
)
from ..agent_research_sandbox.process_runner import canonical_hash
from ..research_quality_delivery_harness.evaluators import (
    EvaluationRejected,
    OutputEvaluatorRegistry,
)
from ..research_quality_delivery_harness.contracts import (
    EvidenceAuthority,
    EvidenceSnapshotV2,
    ResearchVerdict,
)


EVALUATED_OUTPUT_KINDS = (
    "ScoreTable",
    "RegressionTable",
    "RegimeComparisonTable",
    "EventTable",
    "DiagnosticTable",
    "DatasetAssessmentTable",
)
INDEPENDENT_OFFICIAL_OUTPUT_KINDS = (
    "ScoreTable",
    "EventTable",
    "DatasetAssessmentTable",
)


@dataclass(frozen=True, slots=True)
class UnifiedResearchEvidenceResult:
    status: EvidenceAdmissionStatus
    table_hash: str
    failure_code: str | None = None
    official_evidence: Any = None
    auxiliary_evidence: Any = None
    evaluation_row_count: int = 0
    diagnostic: str | None = None

    @property
    def artifact_hash(self) -> str:
        return canonical_hash(
            {
                "status": self.status.value,
                "table_hash": self.table_hash,
                "failure_code": self.failure_code,
                "official_evidence_hash": (
                    None
                    if self.official_evidence is None
                    else self.official_evidence.artifact_hash
                ),
                "auxiliary_evidence_hash": (
                    None
                    if self.auxiliary_evidence is None
                    else self.auxiliary_evidence.artifact_hash
                ),
                "evaluation_row_count": self.evaluation_row_count,
                "diagnostic": self.diagnostic,
            }
        )


class UnifiedResearchEvidenceEvaluator:
    """Validate the common identity chain, then dispatch by output contract.

    ScoreTable retains the existing walk-forward EvidenceService and Court path.
    EventTable and DatasetAssessmentTable can also be official because their
    metrics are recomputed from frozen event outcomes or measured coverage.
    Regression, regime and diagnostic summary tables remain auxiliary until a
    primitive-observation contract lets the evaluator independently recompute
    every claimed metric.
    """

    def __init__(
        self,
        *,
        score_evaluator: SandboxEvidenceEvaluator | None = None,
        output_registry: OutputEvaluatorRegistry | None = None,
    ) -> None:
        self.score_evaluator = score_evaluator or SandboxEvidenceEvaluator()
        self.output_registry = output_registry or OutputEvaluatorRegistry()

    def evaluate(self, **arguments: Any) -> Any:
        table = arguments["table"]
        if isinstance(table, ScoreTable):
            score_arguments = {
                key: value
                for key, value in arguments.items()
                if key not in {"authority_ceiling", "authority_reason"}
            }
            result = self.score_evaluator.evaluate(**score_arguments)
            if (
                arguments.get("authority_ceiling") == "AUXILIARY_ONLY"
                and result.status is EvidenceAdmissionStatus.OFFICIAL_EVIDENCE
                and result.official_evidence is not None
            ):
                official = result.official_evidence
                metric_payload = {
                    name: value
                    for name, value in official.metrics.to_dict().items()
                    if name not in {"contract_type", "schema_version"}
                }
                metric_hashes = {
                    name: canonical_hash({
                        "official_evidence_hash": official.artifact_hash,
                        "metric": name,
                        "value": value,
                    })
                    for name, value in metric_payload.items()
                }
                data_identity = arguments["data_identity"]
                auxiliary = EvidenceSnapshotV2(
                    evidence_id=f"aux.{table.experiment_id}",
                    output_kind="ScoreTable",
                    authority=EvidenceAuthority.AUXILIARY_ONLY,
                    verdict=ResearchVerdict.INSUFFICIENT_EVIDENCE,
                    data_scope={
                        "dataset_hash": data_identity.dataset_hash,
                        "snapshot_hash": data_identity.snapshot_hash,
                        "universe_hash": data_identity.universe_hash,
                    },
                    missingness={},
                    split={
                        "split_hash": arguments["split_hash"],
                        "partition": arguments.get("partition", "VALIDATION"),
                    },
                    cost_model=arguments["cost_model"].to_dict(),
                    metrics=metric_payload,
                    metric_hashes=metric_hashes,
                    subperiod_results=(),
                    regime_results=(),
                    layer_results=(),
                    robustness=(),
                    falsification=(),
                    diagnostics=({
                        "code": str(
                            arguments.get("authority_reason")
                            or "RESEARCH_INPUT_NOT_FROZEN"
                        )
                    },),
                    source_artifact_hashes=tuple(sorted({
                        *official.input_hashes,
                        official.artifact_hash,
                        table.artifact_hash,
                    })),
                    evaluator_version="alphaforge.dynamic-research-authority-ceiling/v1",
                )
                return UnifiedResearchEvidenceResult(
                    status=EvidenceAdmissionStatus.AUXILIARY_ONLY,
                    table_hash=table.artifact_hash,
                    auxiliary_evidence=auxiliary,
                    evaluation_row_count=result.evaluation_row_count,
                    diagnostic=str(
                        arguments.get("authority_reason")
                        or "research input is not bound into the frozen official data identity"
                    ),
                )
            return result

        failure = validate_research_table_identities(
            table=table,
            amendment=arguments["amendment"],
            code_identity=arguments["code_identity"],
            data_identity=arguments["data_identity"],
            review_decision=arguments["review_decision"],
            receipt=arguments["runner_receipt"],
            snapshot=arguments["snapshot"],
            # Summary tables are intentionally AUXILIARY_ONLY. They still need
            # the immutable identity chain, but do not need the official bit.
            require_official_eligibility=(
                type(table).__name__ in INDEPENDENT_OFFICIAL_OUTPUT_KINDS
            ),
        )
        if failure is not None:
            return UnifiedResearchEvidenceResult(
                status=EvidenceAdmissionStatus.BLOCKED,
                table_hash=table.artifact_hash,
                failure_code=failure.value,
                diagnostic="standard table failed immutable review/runner/data identity checks",
            )

        context = _evaluation_context(arguments)
        try:
            evidence = self.output_registry.evaluate(
                table=table.to_dict(),
                context=context,
                evidence_id=f"evidence.{table.experiment_id}",
            )
        except EvaluationRejected as exc:
            return UnifiedResearchEvidenceResult(
                status=EvidenceAdmissionStatus.BLOCKED,
                table_hash=table.artifact_hash,
                failure_code=exc.code,
                evaluation_row_count=len(table.rows),
                diagnostic=str(exc),
            )
        if (
            type(table).__name__ not in INDEPENDENT_OFFICIAL_OUTPUT_KINDS
            or arguments.get("authority_ceiling") == "AUXILIARY_ONLY"
        ):
            auxiliary = replace(evidence, authority=type(evidence.authority).AUXILIARY_ONLY)
            return UnifiedResearchEvidenceResult(
                status=EvidenceAdmissionStatus.AUXILIARY_ONLY,
                table_hash=table.artifact_hash,
                auxiliary_evidence=auxiliary,
                evaluation_row_count=len(table.rows),
                diagnostic=(
                    str(arguments.get("authority_reason"))
                    if arguments.get("authority_ceiling") == "AUXILIARY_ONLY"
                    else "deterministic research diagnostics computed from an "
                    "agent-produced summary; not independent official metrics"
                ),
            )
        return UnifiedResearchEvidenceResult(
            status=EvidenceAdmissionStatus.OFFICIAL_EVIDENCE,
            table_hash=table.artifact_hash,
            official_evidence=evidence,
            evaluation_row_count=len(table.rows),
        )


def supported_official_output_kinds() -> tuple[str, ...]:
    return INDEPENDENT_OFFICIAL_OUTPUT_KINDS


def supported_evaluated_output_kinds() -> tuple[str, ...]:
    return EVALUATED_OUTPUT_KINDS


def _evaluation_context(arguments: Mapping[str, Any]) -> dict[str, Any]:
    snapshot = arguments["snapshot"]
    data_identity = arguments["data_identity"]
    cost_model = arguments["cost_model"]
    labels = arguments["labels"]
    field_counts = {
        field_name: sum(row.values.get(field_name) is not None for row in snapshot.rows)
        for field_name in snapshot.fields
    }
    total_rows = len(snapshot.rows)
    measured_coverage = {
        field_name: count / total_rows if total_rows else 0.0
        for field_name, count in field_counts.items()
    }
    supplied_coverage = dict(arguments.get("field_coverage") or {})
    if supplied_coverage and supplied_coverage != measured_coverage:
        # Dataset assessment is independent: claimed context cannot override
        # the coverage recomputed from the frozen snapshot.
        measured_coverage = {
            field_name: measured_coverage.get(field_name, 0.0)
            for field_name in sorted(set(measured_coverage) | set(supplied_coverage))
        }
    context: dict[str, Any] = {
        "data_scope": {
            "dataset_hash": data_identity.dataset_hash,
            "snapshot_hash": snapshot.artifact_hash,
            "universe_hash": data_identity.universe_hash,
            "decision_start": data_identity.decision_start.isoformat(),
            "decision_end": data_identity.decision_end.isoformat(),
            "row_count": total_rows,
            "symbol_count": len({row.symbol for row in snapshot.rows}),
        },
        "missingness": {
            field_name: 1.0 - coverage
            for field_name, coverage in sorted(measured_coverage.items())
        },
        "split": {
            "split_hash": arguments["split_hash"],
            "partition": arguments.get("partition", "VALIDATION"),
        },
        "cost_model": cost_model.to_dict(),
        "labels": [
            {
                "symbol": row.symbol,
                "decision_time": row.event_time.isoformat(),
                "value": row.value,
            }
            for row in labels
        ],
        "event_outcomes": {
            key: dict(value) for key, value in dict(arguments.get("event_outcomes") or {}).items()
        },
        "field_coverage": measured_coverage,
        "event_window_start": arguments.get("event_window_start", 0),
        "event_window_end": arguments.get("event_window_end", 5),
        "source_artifact_hashes": [
            data_identity.artifact_hash,
            arguments["code_identity"].artifact_hash,
            arguments["review_decision"].artifact_hash,
            arguments["runner_receipt"].artifact_hash,
        ],
    }
    if arguments.get("condition_number") is not None:
        context["condition_number"] = float(arguments["condition_number"])
    return context


__all__ = [
    "EVALUATED_OUTPUT_KINDS",
    "INDEPENDENT_OFFICIAL_OUTPUT_KINDS",
    "UnifiedResearchEvidenceEvaluator",
    "UnifiedResearchEvidenceResult",
    "supported_official_output_kinds",
    "supported_evaluated_output_kinds",
]
