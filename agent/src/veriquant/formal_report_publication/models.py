"""Projection from formal reports to Workbench artifacts."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from ..contracts import CanonicalContract, FailureCode
from ..contracts.base import fail, require_hash, require_identifier, require_text
from ..formal_report import FormalResearchReport, render_markdown
from ..integration.workbench import publish_workbench_run
from ..research_closure_contracts import ResearchOutcomeStatus


FORMAL_REPORT_PUBLICATION_SCHEMA_VERSION = "alphaforge.formal-report-publication/v1"


@dataclass(frozen=True, kw_only=True)
class FormalWorkbenchPublication(CanonicalContract):
    SCHEMA_VERSION: ClassVar[str] = FORMAL_REPORT_PUBLICATION_SCHEMA_VERSION

    run_id: str
    workbench_status: str
    formal_report_hash: str
    publication_hash: str
    artifact_count: int
    detail_hash: str

    def __post_init__(self) -> None:
        require_identifier(self.run_id, "run_id")
        require_text(self.workbench_status, "workbench_status")
        for name in ("formal_report_hash", "publication_hash", "detail_hash"):
            require_hash(getattr(self, name), name)
        if isinstance(self.artifact_count, bool) or not isinstance(self.artifact_count, int) or self.artifact_count < 1:
            fail(FailureCode.INVALID_VALUE, "artifact_count must be positive")


class FormalWorkbenchPublisher:
    """Publish a redacted formal report through the existing Workbench writer."""

    def publish(
        self,
        *,
        root: str | Path,
        report: FormalResearchReport,
        updated_at: str,
    ) -> FormalWorkbenchPublication:
        if not isinstance(report, FormalResearchReport):
            fail(FailureCode.INVALID_VALUE, "report must be FormalResearchReport")
        require_text(updated_at, "updated_at")
        artifacts = _artifacts(report)
        publication = publish_workbench_run(
            root=root,
            run_id=report.run_id,
            status=_workbench_status(report.conclusion.verdict),
            updated_at=updated_at,
            artifacts=artifacts,
        )
        return FormalWorkbenchPublication(
            run_id=report.run_id,
            workbench_status=publication["status"],
            formal_report_hash=report.artifact_hash,
            publication_hash=publication["index_hash"],
            artifact_count=int(publication["artifact_count"]),
            detail_hash=publication["detail_hash"],
        )


def _artifacts(report: FormalResearchReport) -> dict[str, object]:
    return {
        "overview": {
            "schema_version": "alphaforge.formal-workbench-overview/v1",
            "run_id": report.run_id,
            "campaign_id": report.campaign_id,
            "title": "AlphaForge 正式研究报告",
            "question": report.question,
            "status": report.conclusion.verdict.value,
            "run_status": report.conclusion.run_status,
            "headline": report.conclusion.headline,
            "next_step": report.conclusion.next_step,
            "formal_report_hash": report.artifact_hash,
            "markdown": render_markdown(report),
        },
        "lineage": {
            "schema_version": "alphaforge.formal-lineage/v1",
            "candidates": [
                {
                    "candidate_id": item.candidate_id,
                    "attempt_hash": item.attempt_hash,
                    "factor_dag_hash": item.factor_dag_hash,
                    "pit_receipt_hash": item.pit_receipt_hash,
                    "outcome": item.outcome.value,
                }
                for item in report.candidates
            ],
        },
        "evidence": {
            "schema_version": "alphaforge.formal-evidence/v1",
            "data": report.data_coverage.to_dict(),
            "metrics": [
                metric.to_dict()
                for candidate in report.candidates
                for metric in candidate.metrics
            ],
        },
        "critics": {
            "schema_version": "alphaforge.formal-critics/v1",
            "candidate_reasons": [
                {
                    "candidate_id": item.candidate_id,
                    "outcome": item.outcome.value,
                    "reason": item.reason,
                    "next_actions": item.next_actions,
                }
                for item in report.candidates
            ],
        },
        "promotion": {
            "schema_version": "alphaforge.formal-promotion/v1",
            "verdict": report.conclusion.verdict.value,
            "note": "挑战池需要独立封闭评审收据；本发布层不读取封闭明细。",
        },
        "citations": {},
        "shadow_portfolio": {},
        "nav_attribution": {},
        "drift_alerts": {},
    }


def _workbench_status(outcome: ResearchOutcomeStatus) -> str:
    if outcome is ResearchOutcomeStatus.CHALLENGER:
        return "CHALLENGER"
    if outcome is ResearchOutcomeStatus.VALIDATED_RESEARCH:
        return "VALIDATED_RESEARCH"
    if outcome is ResearchOutcomeStatus.REJECT:
        return "REJECTED"
    if outcome is ResearchOutcomeStatus.NO_OFFICIAL_EVIDENCE:
        return "BLOCKED"
    return "INCOMPLETE"
