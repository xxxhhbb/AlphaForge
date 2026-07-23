"""Single review and execution gateway for all research specialists.

Specialists may use different prompts, tools and research methods, but every
generated Python draft crosses this same authority boundary.  The gateway
reuses the existing static AST review, optional non-authoritative reviewer
suggestions, isolated runner, standard-table parser and Evidence evaluator.
It contains no specialist-specific field or qualification rules.
"""

from __future__ import annotations

import hashlib
from dataclasses import replace
from collections.abc import Callable
from typing import Any, Protocol

from ..agent_research_sandbox.contracts import (
    ExperimentReviewRequest,
    MechanicalReviewStatus,
    ReviewerSuggestion,
)
from ..agent_research_sandbox.evaluator import EvidenceAdmissionStatus
from ..agent_research_sandbox.process_runner import ProcessRunStatus
from ..agent_research_sandbox.review import ExperimentReviewGate
from ..evidence import EvidenceCourt
from ..formula_semantics import FormulaSemanticReviewer
from ..research_toolbox.registry import BROAD_READONLY_RESEARCH_TOOLS
from .contracts import (
    DraftSourceRepository,
    ExperimentBindingProtocol,
    IsolatedResearchRunnerProtocol,
    ProductExperimentResult,
    ProductExperimentStatus,
    ProductFailureCode,
)


ArtifactWriter = Callable[[str, str, dict[str, Any]], str]


class ResearchReviewAdvisorProtocol(Protocol):
    """Optional semantic reviewer; suggestions never override mechanical errors."""

    def suggest(self, request: ExperimentReviewRequest) -> tuple[ReviewerSuggestion, ...]: ...


class ResearchReviewGateway:
    """One fail-closed gateway shared by all six research groups."""

    def __init__(
        self,
        *,
        source_repository: DraftSourceRepository,
        review_gate: ExperimentReviewGate,
        runner: IsolatedResearchRunnerProtocol,
        binding: ExperimentBindingProtocol,
        evidence_evaluator: Any,
        artifact_writer: ArtifactWriter,
        review_advisor: ResearchReviewAdvisorProtocol | None = None,
        formula_reviewer: FormulaSemanticReviewer | None = None,
    ) -> None:
        self.sources = source_repository
        self.review_gate = review_gate
        self.runner = runner
        self.binding = binding
        self.evaluator = evidence_evaluator
        self.write_artifact = artifact_writer
        self.review_advisor = review_advisor
        self.formula_reviewer = formula_reviewer or FormulaSemanticReviewer()

    def evaluate_draft(
        self, *, manifest, outcome, source_hash: str,
        prior_review_hash: str | None = None,
    ) -> ProductExperimentResult:
        experiment_id = f"{outcome.task_id}_{source_hash[-12:]}"
        source = self.sources.get(source_hash)
        if source is None:
            return _blocked(experiment_id, outcome.specialist_id, source_hash, ProductFailureCode.SOURCE_ARTIFACT_MISSING)
        if _source_hash(source) != source_hash:
            return _blocked(experiment_id, outcome.specialist_id, source_hash, ProductFailureCode.SOURCE_HASH_MISMATCH)
        try:
            review_request = self.binding.review_request(
                manifest=manifest, outcome=outcome, source_text=source
            )
            if prior_review_hash is not None:
                review_request = replace(
                    review_request,
                    revision_number=1,
                    prior_review_hash=prior_review_hash,
                )
        except (TypeError, ValueError):
            return _blocked(
                experiment_id, outcome.specialist_id, source_hash,
                ProductFailureCode.IDENTITY_BINDING_FAILED,
            )

        suggestions: tuple[ReviewerSuggestion, ...] = ()
        if self.review_advisor is not None:
            try:
                suggestions = self.review_advisor.suggest(review_request)
            except Exception:
                # Semantic advice is useful but non-authoritative.  Its outage
                # cannot grant authority and must not erase a mechanically
                # reviewable experiment.
                suggestions = (ReviewerSuggestion(
                    suggestion_id="reviewer.unavailable",
                    message="独立语义审查建议暂不可用；机械安全审查仍按固定政策执行。",
                ),)
        decision = self.review_gate.review(review_request, reviewer_suggestions=suggestions)
        self.write_artifact(manifest.run_id, f"experiments/{experiment_id}/review.json", decision.to_dict())
        if decision.status is not MechanicalReviewStatus.ACCEPTED:
            # A rejected experiment is a useful negative research result, not
            # a platform failure.  It never reaches the runner.
            return _result(
                experiment_id, outcome.specialist_id, source_hash,
                ProductExperimentStatus.NEGATIVE,
                ProductFailureCode.REVIEW_REJECTED,
                review_hash=decision.artifact_hash,
                review_feedback_codes=tuple(sorted({item.code.value for item in decision.findings})),
            )

        semantic_arguments = getattr(self.binding, "formula_semantic_arguments", None)
        formula_review = None
        if callable(semantic_arguments):
            try:
                formula_review = self.formula_reviewer.review(
                    source_text=source,
                    **dict(semantic_arguments(proposer_id=outcome.specialist_id)),
                )
            except (TypeError, ValueError):
                return _blocked(
                    experiment_id, outcome.specialist_id, source_hash,
                    ProductFailureCode.IDENTITY_BINDING_FAILED,
                    review_hash=decision.artifact_hash,
                )
            self.write_artifact(
                manifest.run_id,
                f"experiments/{experiment_id}/formula_semantic_review.json",
                formula_review.to_dict(include_hash=False),
            )
            if formula_review.blocked or formula_review.requires_revision:
                return _result(
                    experiment_id, outcome.specialist_id, source_hash,
                    ProductExperimentStatus.NEGATIVE,
                    ProductFailureCode.FORMULA_SEMANTIC_REVIEW_REQUIRED,
                    review_hash=decision.artifact_hash,
                    formula_review_hash=formula_review.artifact_hash,
                    review_feedback_codes=tuple(sorted({
                        item.code for item in formula_review.findings
                        if item.severity.value in {"BLOCKING", "REVISION_REQUIRED"}
                    })),
                )

        try:
            process_request = self.binding.process_request(
                manifest=manifest,
                review_request=review_request,
                review_decision=decision,
                source_text=source,
            )
        except (TypeError, ValueError):
            return _blocked(
                experiment_id, outcome.specialist_id, source_hash,
                ProductFailureCode.IDENTITY_BINDING_FAILED,
                review_hash=decision.artifact_hash,
            )
        receipt = self.runner.execute(process_request)
        self.write_artifact(manifest.run_id, f"experiments/{experiment_id}/runner.json", receipt_dict(receipt))
        if receipt.status is not ProcessRunStatus.COMPLETED_AUXILIARY or receipt.output is None:
            feedback_codes = _runner_feedback_codes(receipt)
            return _blocked(
                experiment_id, outcome.specialist_id, source_hash,
                ProductFailureCode.RUNNER_BLOCKED,
                review_hash=decision.artifact_hash,
                runner_receipt_hash=receipt.artifact_hash,
                review_feedback_codes=feedback_codes,
            )

        try:
            table = self.binding.parse_table(review_request=review_request, receipt=receipt)
        except Exception:
            # Custom or malformed output still yields a PARTIAL report with the
            # review and runner receipts.  It is not mistaken for official data.
            return _result(
                experiment_id, outcome.specialist_id, source_hash,
                ProductExperimentStatus.PARTIAL,
                ProductFailureCode.OUTPUT_CONTRACT_INVALID,
                review_hash=decision.artifact_hash,
                runner_receipt_hash=receipt.artifact_hash,
            )

        self.write_artifact(
            manifest.run_id,
            f"experiments/{experiment_id}/standard_table.json",
            table.to_dict(),
        )
        try:
            arguments = self.binding.evidence_arguments(
                manifest=manifest,
                review_request=review_request,
                review_decision=decision,
                receipt=receipt,
                table=table,
            )
            dynamic_tools = tuple(sorted({
                item.tool_name
                for item in outcome.investigation_receipts
                if item.tool_name not in BROAD_READONLY_RESEARCH_TOOLS
            }))
            if dynamic_tools:
                # Vibe's external read-only tools may guide the research plan,
                # but their returned payloads are not automatically part of
                # the immutable runner data identity.  Keep the experiment and
                # deterministic metrics useful while preventing an authority
                # upgrade until those results are explicitly frozen and PIT
                # bound into a later snapshot.
                arguments = dict(arguments)
                if arguments.get("authority_ceiling") is None:
                    arguments["authority_ceiling"] = "AUXILIARY_ONLY"
                    arguments["authority_reason"] = "UNFROZEN_DYNAMIC_TOOL_INPUT"
            evidence = self.evaluator.evaluate(**dict(arguments))
        except Exception as exc:
            self.write_artifact(
                manifest.run_id,
                f"experiments/{experiment_id}/evidence_failure.json",
                {
                    "failure_code": ProductFailureCode.EVIDENCE_BLOCKED.value,
                    "exception_type": type(exc).__name__,
                },
            )
            return _result(
                experiment_id, outcome.specialist_id, source_hash,
                ProductExperimentStatus.PARTIAL,
                ProductFailureCode.EVIDENCE_BLOCKED,
                review_hash=decision.artifact_hash,
                runner_receipt_hash=receipt.artifact_hash,
            )

        self.write_artifact(manifest.run_id, f"experiments/{experiment_id}/evidence.json", evidence_dict(evidence))
        if evidence.official_evidence is not None and hasattr(evidence.official_evidence, "to_dict"):
            self.write_artifact(
                manifest.run_id,
                f"experiments/{experiment_id}/official_evidence.json",
                evidence.official_evidence.to_dict(),
            )
        if getattr(evidence, "auxiliary_evidence", None) is not None and hasattr(evidence.auxiliary_evidence, "to_dict"):
            self.write_artifact(
                manifest.run_id,
                f"experiments/{experiment_id}/auxiliary_evidence.json",
                evidence.auxiliary_evidence.to_dict(),
            )

        evidence_failure = (
            None
            if evidence.failure_code is None
            else str(getattr(evidence.failure_code, "value", evidence.failure_code))
        )
        if evidence.status is EvidenceAdmissionStatus.OFFICIAL_EVIDENCE:
            status = ProductExperimentStatus.OFFICIAL_EVIDENCE
            failure = None
        elif evidence.status is EvidenceAdmissionStatus.AUXILIARY_ONLY:
            status = ProductExperimentStatus.AUXILIARY_ONLY
            failure = None
        elif evidence_failure in {"LABEL_MISSING", "CAPACITY_FIELD_INVALID"}:
            status = ProductExperimentStatus.DATA_GAP
            failure = ProductFailureCode.EVIDENCE_BLOCKED
        else:
            status = ProductExperimentStatus.PARTIAL
            failure = ProductFailureCode.EVIDENCE_BLOCKED
        official_metrics: tuple[tuple[str, str], ...] = ()
        research_metrics: tuple[tuple[str, str], ...] = ()
        research_verdict = None
        court_outcome = None
        court_reason_codes: tuple[str, ...] = ()
        court_decision_hash = None
        official = evidence.official_evidence
        auxiliary = getattr(evidence, "auxiliary_evidence", None)
        if official is not None and hasattr(official, "metrics"):
            metric_source = official.metrics
            if hasattr(metric_source, "to_dict"):
                metric_payload = metric_source.to_dict()
            elif isinstance(metric_source, dict) or hasattr(metric_source, "items"):
                metric_payload = dict(metric_source)
            else:
                metric_payload = {}
            official_metrics = tuple(sorted(
                (name, _display_metric(value))
                for name, value in metric_payload.items()
                if name not in {"contract_type", "schema_version"}
            ))
            verdict_value = getattr(getattr(official, "verdict", None), "value", None)
            research_verdict = None if verdict_value is None else str(verdict_value)
        if auxiliary is not None and hasattr(auxiliary, "metrics"):
            metric_source = auxiliary.metrics
            metric_payload = (
                metric_source.to_dict()
                if hasattr(metric_source, "to_dict")
                else dict(metric_source) if hasattr(metric_source, "items") else {}
            )
            research_metrics = tuple(sorted(
                (name, _display_metric(value))
                for name, value in metric_payload.items()
                if name not in {"contract_type", "schema_version"}
            ))
            verdict_value = getattr(getattr(auxiliary, "verdict", None), "value", None)
            research_verdict = None if verdict_value is None else str(verdict_value)
        if official is not None and hasattr(official, "identity_hash"):
            court_decision = EvidenceCourt().decide(
                evidence=official,
                identity_hash=official.identity_hash,
                proposer_id=outcome.specialist_id,
                approver_id="evidence_court",
                decided_at=manifest.created_at,
                pit_valid=True,
                sealed_passed=None,
            )
            self.write_artifact(
                manifest.run_id,
                f"experiments/{experiment_id}/court_decision.json",
                court_decision.to_dict(),
            )
            court_outcome = court_decision.outcome.value
            court_reason_codes = court_decision.reason_codes
            court_decision_hash = court_decision.artifact_hash
        return _result(
            experiment_id,
            outcome.specialist_id,
            source_hash,
            status,
            failure,
            review_hash=decision.artifact_hash,
            formula_review_hash=(
                None if not callable(semantic_arguments) else formula_review.artifact_hash
            ),
            formula_advisory_codes=(
                () if formula_review is None else tuple(sorted({
                    item.code for item in formula_review.findings
                    if item.severity.value == "ADVISORY"
                }))
            ),
            runner_receipt_hash=receipt.artifact_hash,
            evidence_result_hash=evidence.artifact_hash,
            official_evidence_hash=(
                None if evidence.official_evidence is None else evidence.official_evidence.artifact_hash
            ),
            research_evidence_hash=(
                None if auxiliary is None else auxiliary.artifact_hash
            ),
            official_metrics=official_metrics,
            research_metrics=research_metrics,
            research_verdict=research_verdict,
            court_outcome=court_outcome,
            court_reason_codes=court_reason_codes,
            court_decision_hash=court_decision_hash,
            output_kind=receipt.output_kind,
            review_feedback_codes=(
                ("UNFROZEN_DYNAMIC_TOOL_INPUT",) if dynamic_tools else ()
            ),
        )


def _source_hash(source: str) -> str:
    return "sha256:" + hashlib.sha256(source.encode("utf-8")).hexdigest()


def _display_metric(value: Any) -> str:
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return format(value, ".6g")
    return str(value)


def _runner_feedback_codes(receipt: Any) -> tuple[str, ...]:
    """Expose only typed, already-redacted runner diagnostics for one revision."""

    failure = getattr(getattr(receipt, "failure_code", None), "value", None)
    diagnostic = str(getattr(receipt, "diagnostic", "") or "")
    if failure != "PROTOCOL_FAILED" or not diagnostic.startswith("PROTOCOL_FAILED"):
        return ()
    safe = "".join(character if character.isalnum() else "_" for character in diagnostic.upper())
    safe = "_".join(part for part in safe.split("_") if part)
    return (f"RUNNER_{safe}",) if safe else ()


def _blocked(experiment_id, specialist_id, source_hash, failure, **hashes):
    return _result(
        experiment_id, specialist_id, source_hash,
        ProductExperimentStatus.BLOCKED, failure, **hashes,
    )


def _result(experiment_id, specialist_id, source_hash, status, failure=None, **hashes):
    return ProductExperimentResult(
        experiment_id=experiment_id,
        specialist_id=specialist_id,
        source_hash=source_hash,
        status=status,
        failure_code=None if failure is None else failure.value,
        **hashes,
    )


def receipt_dict(receipt) -> dict[str, Any]:
    payload = {
        "artifact_hash": receipt.artifact_hash,
        "run_id": receipt.run_id,
        "experiment_id": receipt.experiment_id,
        "status": receipt.status.value,
        "isolation_level": receipt.isolation_level.value,
        "input_hash": receipt.input_hash,
        "code_hash": receipt.code_hash,
        "output_hash": receipt.output_hash,
        "output_kind": receipt.output_kind,
        "failure_code": None if receipt.failure_code is None else receipt.failure_code.value,
        "diagnostic": receipt.diagnostic,
        "official_evidence_eligible": receipt.official_evidence_eligible,
    }
    for name in ("training_input_hash", "prediction_input_hash", "image_id", "entrypoint_version"):
        if hasattr(receipt, name):
            payload[name] = getattr(receipt, name)
    return payload


def evidence_dict(evidence) -> dict[str, Any]:
    return {
        "artifact_hash": evidence.artifact_hash,
        "status": evidence.status.value,
        "table_hash": evidence.table_hash,
        "failure_code": (
            None
            if evidence.failure_code is None
            else str(getattr(evidence.failure_code, "value", evidence.failure_code))
        ),
        "official_evidence_hash": (
            None if evidence.official_evidence is None else evidence.official_evidence.artifact_hash
        ),
        "auxiliary_evidence_hash": (
            None
            if getattr(evidence, "auxiliary_evidence", None) is None
            else evidence.auxiliary_evidence.artifact_hash
        ),
        "evaluation_row_count": evidence.evaluation_row_count,
        "diagnostic": getattr(evidence, "diagnostic", None),
    }


__all__ = [
    "ResearchReviewAdvisorProtocol", "ResearchReviewGateway", "evidence_dict", "receipt_dict",
]
