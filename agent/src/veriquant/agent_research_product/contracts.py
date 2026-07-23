"""Contracts for the single Agent-research product orchestration path."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping, Protocol

from ..agent_research_sandbox.contracts import ExperimentReviewDecision, ExperimentReviewRequest
from ..agent_research_sandbox.evaluator import ResearchTable
from ..agent_research_sandbox.docker_runner import DockerResearchRequest
from ..agent_research_sandbox.process_runner import ResearchProcessReceipt, ResearchProcessRequest
from ..agent_research_sandbox.report import CustomerResearchReport
from ..research_agent_runtime import (
    ResearchAgentOutcome,
    ResearchAgentRuntimeProtocol,
    ResearchAgentTask,
    ResearchSwarmResult,
    ResearchToolboxProtocol,
)
from ..research_run_controller import ResearchRunManifest
from ..research_results import ResearchResultKind
from ..agent_research_sandbox.process_runner import canonical_hash


class ProductFailureCode(str, Enum):
    BRANCH_FAILED = "BRANCH_FAILED"
    CANCELLED = "CANCELLED"
    SOURCE_ARTIFACT_MISSING = "SOURCE_ARTIFACT_MISSING"
    SOURCE_HASH_MISMATCH = "SOURCE_HASH_MISMATCH"
    IDENTITY_BINDING_FAILED = "IDENTITY_BINDING_FAILED"
    REVIEW_REJECTED = "REVIEW_REJECTED"
    FORMULA_SEMANTIC_REVIEW_REQUIRED = "FORMULA_SEMANTIC_REVIEW_REQUIRED"
    RUNNER_BLOCKED = "RUNNER_BLOCKED"
    OUTPUT_CONTRACT_INVALID = "OUTPUT_CONTRACT_INVALID"
    EVIDENCE_BLOCKED = "EVIDENCE_BLOCKED"
    INFRASTRUCTURE_FAILED = "INFRASTRUCTURE_FAILED"


class ProductExperimentStatus(str, Enum):
    OFFICIAL_EVIDENCE = "OFFICIAL_EVIDENCE"
    AUXILIARY_ONLY = "AUXILIARY_ONLY"
    NEGATIVE = "NEGATIVE"
    DATA_GAP = "DATA_GAP"
    PARTIAL = "PARTIAL"
    BLOCKED = "BLOCKED"


class DraftSourceRepository(Protocol):
    """Content-addressed source store; hashes without source fail closed."""

    def get(self, content_hash: str) -> str | None: ...


class IsolatedResearchRunnerProtocol(Protocol):
    def execute(
        self, request: ResearchProcessRequest | DockerResearchRequest
    ) -> ResearchProcessReceipt: ...


class ExperimentBindingProtocol(Protocol):
    """Bind a DRAFT source to frozen identities without calculating metrics."""

    def review_request(
        self, *, manifest: ResearchRunManifest, outcome: ResearchAgentOutcome, source_text: str
    ) -> ExperimentReviewRequest: ...

    def process_request(
        self, *, manifest: ResearchRunManifest, review_request: ExperimentReviewRequest,
        review_decision: ExperimentReviewDecision, source_text: str
    ) -> ResearchProcessRequest | DockerResearchRequest: ...

    def parse_table(
        self, *, review_request: ExperimentReviewRequest, receipt: ResearchProcessReceipt
    ) -> ResearchTable: ...

    def evidence_arguments(
        self, *, manifest: ResearchRunManifest, review_request: ExperimentReviewRequest,
        review_decision: ExperimentReviewDecision, receipt: ResearchProcessReceipt,
        table: ResearchTable
    ) -> Mapping[str, Any]: ...


@dataclass(frozen=True, slots=True)
class ProductRunRequest:
    manifest: ResearchRunManifest
    question_id: str
    question: str
    tasks: tuple[ResearchAgentTask, ...]
    runtimes: Mapping[str, ResearchAgentRuntimeProtocol]
    toolboxes: Mapping[str, ResearchToolboxProtocol]

    def __post_init__(self) -> None:
        if not self.question_id or not self.question.strip():
            raise ValueError("question identity and text are required")
        if not self.tasks:
            raise ValueError("at least one specialist task is required")
        if any(task.run_id != self.manifest.run_id for task in self.tasks):
            raise ValueError("all tasks must bind the canonical manifest")


@dataclass(frozen=True, slots=True)
class ProductExperimentResult:
    experiment_id: str
    specialist_id: str
    source_hash: str
    status: ProductExperimentStatus
    review_hash: str | None = None
    formula_review_hash: str | None = None
    formula_advisory_codes: tuple[str, ...] = ()
    runner_receipt_hash: str | None = None
    evidence_result_hash: str | None = None
    official_evidence_hash: str | None = None
    research_evidence_hash: str | None = None
    failure_code: str | None = None
    review_feedback_codes: tuple[str, ...] = ()
    official_metrics: tuple[tuple[str, str], ...] = ()
    research_metrics: tuple[tuple[str, str], ...] = ()
    research_verdict: str | None = None
    court_outcome: str | None = None
    court_reason_codes: tuple[str, ...] = ()
    court_decision_hash: str | None = None
    output_kind: str | None = None
    revision_of_source_hash: str | None = None
    revision_reason_codes: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "experiment_id": self.experiment_id,
            "specialist_id": self.specialist_id,
            "source_hash": self.source_hash,
            "status": self.status.value,
            "review_hash": self.review_hash,
            "formula_review_hash": self.formula_review_hash,
            "formula_advisory_codes": list(self.formula_advisory_codes),
            "runner_receipt_hash": self.runner_receipt_hash,
            "evidence_result_hash": self.evidence_result_hash,
            "official_evidence_hash": self.official_evidence_hash,
            "research_evidence_hash": self.research_evidence_hash,
            "failure_code": self.failure_code,
            "review_feedback_codes": list(self.review_feedback_codes),
            "official_metrics": [
                {"name": name, "display_value": display_value}
                for name, display_value in self.official_metrics
            ],
            "research_metrics": [
                {"name": name, "display_value": display_value}
                for name, display_value in self.research_metrics
            ],
            "research_verdict": self.research_verdict,
            "court_outcome": self.court_outcome,
            "court_reason_codes": list(self.court_reason_codes),
            "court_decision_hash": self.court_decision_hash,
            "output_kind": self.output_kind,
            "revision_of_source_hash": self.revision_of_source_hash,
            "revision_reason_codes": list(self.revision_reason_codes),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ProductExperimentResult":
        raw = dict(value)
        raw["status"] = ProductExperimentStatus(raw["status"])
        raw["review_feedback_codes"] = tuple(raw.get("review_feedback_codes", ()))
        raw["formula_advisory_codes"] = tuple(raw.get("formula_advisory_codes", ()))
        raw["official_metrics"] = tuple(
            (str(item["name"]), str(item["display_value"]))
            for item in raw.get("official_metrics", ())
        )
        raw["research_metrics"] = tuple(
            (str(item["name"]), str(item["display_value"]))
            for item in raw.get("research_metrics", ())
        )
        raw["court_reason_codes"] = tuple(raw.get("court_reason_codes", ()))
        raw["revision_reason_codes"] = tuple(raw.get("revision_reason_codes", ()))
        return cls(**raw)


@dataclass(frozen=True, slots=True)
class ResearchResultBundle:
    """One machine-readable terminal summary for every user research run.

    The bundle does not calculate metrics or upgrade evidence authority.  It
    only classifies already-recorded branch, review, runner and Evidence facts
    into a product outcome that must receive a report.
    """

    run_id: str
    result_kind: ResearchResultKind
    experiments: tuple[ProductExperimentResult, ...]
    failure_codes: tuple[str, ...]
    branch_statuses: tuple[str, ...]
    official_evidence_hashes: tuple[str, ...]
    report_required: bool = True

    def __post_init__(self) -> None:
        if not self.run_id or not isinstance(self.result_kind, ResearchResultKind):
            raise ValueError("result bundle requires a run identity and typed result kind")
        if tuple(sorted(set(self.failure_codes))) != self.failure_codes:
            raise ValueError("failure_codes must be unique and stable-sorted")
        if tuple(sorted(self.branch_statuses)) != self.branch_statuses:
            raise ValueError("branch_statuses must be stable-sorted")
        if tuple(sorted(set(self.official_evidence_hashes))) != self.official_evidence_hashes:
            raise ValueError("official Evidence hashes must be unique and stable-sorted")
        if self.report_required is not True:
            raise ValueError("every legitimate product outcome requires a report")
        claimed = tuple(sorted({
            item.official_evidence_hash for item in self.experiments if item.official_evidence_hash
        }))
        if claimed != self.official_evidence_hashes:
            raise ValueError("bundle Evidence hashes do not match experiment results")

    def to_dict(self, *, include_hash: bool = True) -> dict[str, Any]:
        value = {
            "schema_version": "alphaforge.research-result-bundle/v1",
            "run_id": self.run_id,
            "result_kind": self.result_kind.value,
            "experiments": [item.to_dict() for item in self.experiments],
            "failure_codes": list(self.failure_codes),
            "branch_statuses": list(self.branch_statuses),
            "official_evidence_hashes": list(self.official_evidence_hashes),
            "report_required": self.report_required,
        }
        if include_hash:
            value["artifact_hash"] = canonical_hash(value)
        return value

    @property
    def artifact_hash(self) -> str:
        return str(self.to_dict()["artifact_hash"])

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ResearchResultBundle":
        raw = dict(value)
        raw.pop("schema_version", None)
        raw.pop("artifact_hash", None)
        raw["result_kind"] = ResearchResultKind(raw["result_kind"])
        raw["experiments"] = tuple(ProductExperimentResult.from_dict(item) for item in raw["experiments"])
        raw["failure_codes"] = tuple(raw["failure_codes"])
        raw["branch_statuses"] = tuple(raw["branch_statuses"])
        raw["official_evidence_hashes"] = tuple(raw["official_evidence_hashes"])
        return cls(**raw)


class ProductReportBuilderProtocol(Protocol):
    def build(
        self, *, request: ProductRunRequest, swarm: ResearchSwarmResult | None,
        experiments: tuple[ProductExperimentResult, ...], failures: tuple[str, ...],
        artifact_hashes: tuple[str, ...], result_bundle: ResearchResultBundle,
        artifact_repository: DraftSourceRepository | None = None,
        research_answer: Mapping[str, Any] | None = None,
    ) -> CustomerResearchReport: ...


@dataclass(frozen=True, slots=True)
class ProductRunResult:
    run_id: str
    manifest_hash: str
    lifecycle: str
    swarm_hash: str | None
    experiments: tuple[ProductExperimentResult, ...]
    failures: tuple[str, ...]
    report_hash: str
    report_path: str
    result_kind: ResearchResultKind = ResearchResultKind.PARTIAL_RESEARCH_RESULT
    result_bundle_hash: str | None = None
    answer_hash: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "manifest_hash": self.manifest_hash,
            "lifecycle": self.lifecycle,
            "swarm_hash": self.swarm_hash,
            "experiments": [item.to_dict() for item in self.experiments],
            "failures": list(self.failures),
            "report_hash": self.report_hash,
            "report_path": self.report_path,
            "result_kind": self.result_kind.value,
            "result_bundle_hash": self.result_bundle_hash,
            "answer_hash": self.answer_hash,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ProductRunResult":
        raw = dict(value)
        raw["experiments"] = tuple(ProductExperimentResult.from_dict(item) for item in raw["experiments"])
        raw["failures"] = tuple(raw["failures"])
        raw["result_kind"] = ResearchResultKind(raw.get("result_kind", ResearchResultKind.PARTIAL_RESEARCH_RESULT.value))
        return cls(**raw)


__all__ = [
    "DraftSourceRepository", "ExperimentBindingProtocol", "IsolatedResearchRunnerProtocol",
    "ProductExperimentResult", "ProductExperimentStatus", "ProductFailureCode", "ResearchResultBundle",
    "ProductReportBuilderProtocol", "ProductRunRequest", "ProductRunResult",
]
