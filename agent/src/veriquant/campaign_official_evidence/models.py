"""Bridge approved-proxy FactorDAG attempts into formal research outcomes."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import ClassVar

from ..contracts import CanonicalContract, Component, FailureCode
from ..contracts.base import fail, require_hash, require_identifier, require_text
from ..evidence.critics import EvidenceCourt
from ..evidence.models import CourtOutcome, OfficialEvidence
from ..integration.v1_real_official import RealOfficialEvidenceAdapter, default_factor_candidates
from ..integration.v1_real_official.models import FactorAttemptStatus
from ..research_closure_contracts import (
    CandidateAttemptReceipt,
    CandidateAttemptStatus,
    CandidateResearchOutcome,
    CampaignResearchOutcome,
    MetricDirection,
    OfficialMetricRecord,
    ResearchOutcomeStatus,
    ResearchRunStatus,
    VerifiedDatasetReceipt,
)


CAMPAIGN_OFFICIAL_EVIDENCE_SCHEMA_VERSION = "alphaforge.campaign-official-evidence/v1"


class CampaignOfficialEvidenceStatus(str, Enum):
    COMPLETED = "COMPLETED"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"


@dataclass(frozen=True, kw_only=True)
class CampaignOfficialEvidenceRequest(CanonicalContract):
    SCHEMA_VERSION: ClassVar[str] = CAMPAIGN_OFFICIAL_EVIDENCE_SCHEMA_VERSION

    request_id: str
    campaign_id: str
    run_id: str
    campaign_hash: str
    dataset_receipt: VerifiedDatasetReceipt
    output_root: str
    seed: int = 7
    allow_smoke_metrics: bool = False
    sealed_passed_override: bool | None = None

    def __post_init__(self) -> None:
        for name in ("request_id", "campaign_id", "run_id"):
            require_identifier(getattr(self, name), name)
        require_hash(self.campaign_hash, "campaign_hash")
        if not isinstance(self.dataset_receipt, VerifiedDatasetReceipt):
            fail(FailureCode.INVALID_VALUE, "dataset_receipt must be VerifiedDatasetReceipt")
        require_text(self.output_root, "output_root")
        if isinstance(self.seed, bool) or not isinstance(self.seed, int) or self.seed < 0:
            fail(FailureCode.INVALID_VALUE, "seed must be a non-negative integer")
        if self.allow_smoke_metrics:
            fail(FailureCode.AUXILIARY_MASQUERADE, "smoke metrics cannot enter official evidence bridge")
        if self.sealed_passed_override is not None:
            fail(FailureCode.UNAUTHORIZED_CAPABILITY, "sealed verdict cannot be caller supplied")


@dataclass(frozen=True, kw_only=True)
class CampaignOfficialEvidenceResult(CanonicalContract):
    SCHEMA_VERSION: ClassVar[str] = CAMPAIGN_OFFICIAL_EVIDENCE_SCHEMA_VERSION

    request_hash: str
    status: CampaignOfficialEvidenceStatus
    campaign_outcome: CampaignResearchOutcome
    official_run_hash: str
    trial_ledger_tip_hash: str
    message: str

    def __post_init__(self) -> None:
        require_hash(self.request_hash, "request_hash")
        if isinstance(self.status, str):
            object.__setattr__(self, "status", CampaignOfficialEvidenceStatus(self.status))
        if not isinstance(self.campaign_outcome, CampaignResearchOutcome):
            fail(FailureCode.INVALID_VALUE, "campaign_outcome must be CampaignResearchOutcome")
        require_hash(self.official_run_hash, "official_run_hash")
        require_hash(self.trial_ledger_tip_hash, "trial_ledger_tip_hash")
        require_text(self.message, "message")


class CampaignOfficialEvidenceBridge:
    """Run visible official evidence and project it to closure contracts."""

    def __init__(self, *, adapter: RealOfficialEvidenceAdapter | None = None) -> None:
        self.adapter = adapter or RealOfficialEvidenceAdapter()
        self.court = EvidenceCourt()

    def run(self, request: CampaignOfficialEvidenceRequest) -> CampaignOfficialEvidenceResult:
        root = Path(request.output_root).resolve()
        root.mkdir(parents=True, exist_ok=True)
        official_run = self.adapter.run(
            output_root=root,
            run_id=request.run_id,
            seed=request.seed,
            factors=default_factor_candidates(),
        )
        outcomes = tuple(_candidate_outcome(item, request=request, court=self.court) for item in official_run.factors)
        research_outcome = _campaign_outcome(outcomes)
        run_status = (
            ResearchRunStatus.COMPLETED
            if official_run.status == "COMPLETED_OFFICIAL_VISIBLE"
            else ResearchRunStatus.PARTIAL
            if official_run.status == "PARTIAL_OFFICIAL_VISIBLE"
            else ResearchRunStatus.FAILED
        )
        campaign_outcome = CampaignResearchOutcome(
            campaign_id=request.campaign_id,
            run_id=request.run_id,
            run_status=run_status,
            research_outcome=research_outcome,
            dataset_receipt_hash=request.dataset_receipt.artifact_hash
            if research_outcome is not ResearchOutcomeStatus.NO_OFFICIAL_EVIDENCE
            else None,
            selected_research_groups=("市场结构研究小组", "元阿尔法研究小组"),
            candidate_outcomes=outcomes,
            artifact_hashes=(official_run.artifact_hash, official_run.trial_ledger_tip_hash),
            summary=_summary_for(research_outcome),
        )
        status = (
            CampaignOfficialEvidenceStatus.COMPLETED
            if run_status is ResearchRunStatus.COMPLETED
            else CampaignOfficialEvidenceStatus.PARTIAL
            if run_status is ResearchRunStatus.PARTIAL
            else CampaignOfficialEvidenceStatus.FAILED
        )
        return CampaignOfficialEvidenceResult(
            request_hash=request.artifact_hash,
            status=status,
            campaign_outcome=campaign_outcome,
            official_run_hash=official_run.artifact_hash,
            trial_ledger_tip_hash=official_run.trial_ledger_tip_hash,
            message="官方机器证据桥运行完成",
        )


def _candidate_outcome(item: object, *, request: CampaignOfficialEvidenceRequest, court: EvidenceCourt) -> CandidateResearchOutcome:
    factor_id = item.factor_id
    attempt = CandidateAttemptReceipt(
        attempt_id=item.trial_id,
        candidate_id=factor_id,
        family_id=f"{request.run_id}.family",
        status=CandidateAttemptStatus.OFFICIAL_EVIDENCE_READY
        if item.status is FactorAttemptStatus.OFFICIAL_VISIBLE_EVIDENCE
        else CandidateAttemptStatus.CONSUMED_FAILED,
        draft_hash=item.candidate_payload_hash,
        trial_identity_hash=item.identity_hash,
        budget_consumed=1,
        terminal_event_hash=item.ledger_terminal_event_hash,
    )
    if item.status is not FactorAttemptStatus.OFFICIAL_VISIBLE_EVIDENCE:
        return CandidateResearchOutcome(
            candidate_id=factor_id,
            proposer_component=Component.LLM_RESEARCHER,
            attempt=attempt,
            outcome=ResearchOutcomeStatus.NO_OFFICIAL_EVIDENCE,
            reason="候选未形成可复现的官方机器证据",
            next_actions=("修订表达式或补齐字段后重新验证",),
        )
    evidence: OfficialEvidence = item.validation_evidence
    decision = court.decide(
        evidence=evidence,
        identity_hash=item.identity_hash,
        proposer_id=f"proposer.{factor_id}",
        approver_id="evidence_court.visible",
        decided_at=request.dataset_receipt.decision_time,
        pit_valid=True,
        sealed_passed=None,
    )
    metrics = _official_metric_records(factor_id, evidence)
    outcome = {
        CourtOutcome.REJECT: ResearchOutcomeStatus.REJECT,
        CourtOutcome.REVISE: ResearchOutcomeStatus.REVISE,
        CourtOutcome.VALIDATED_RESEARCH: ResearchOutcomeStatus.VALIDATED_RESEARCH,
        CourtOutcome.PROMOTE_CHALLENGER: ResearchOutcomeStatus.VALIDATED_RESEARCH,
    }[decision.outcome]
    return CandidateResearchOutcome(
        candidate_id=factor_id,
        proposer_component=Component.LLM_RESEARCHER,
        attempt=attempt,
        outcome=outcome,
        official_metrics=metrics,
        reason="; ".join(decision.reason_codes),
        next_actions=_next_actions(outcome),
    )


def _official_metric_records(factor_id: str, evidence: OfficialEvidence) -> tuple[OfficialMetricRecord, ...]:
    records: list[OfficialMetricRecord] = []
    for name, value in evidence.metrics.flat().items():
        records.append(
            OfficialMetricRecord(
                metric_id=f"metric.{factor_id}.{name}",
                candidate_id=factor_id,
                metric_name=name,
                value=float(value),
                direction=_direction_for(name),
                evidence_hash=evidence.artifact_hash,
                split=evidence.partition,
                policy_version="evidence.visible.v1",
            )
        )
    return tuple(records)


def _direction_for(name: str) -> MetricDirection:
    if name in {"turnover", "spread_cost", "linear_impact_cost", "max_drawdown", "max_registered_abs_correlation"}:
        return MetricDirection.LOWER_IS_BETTER
    if name in {"observation_count", "period_count"}:
        return MetricDirection.INFORMATIONAL
    return MetricDirection.HIGHER_IS_BETTER


def _campaign_outcome(outcomes: tuple[CandidateResearchOutcome, ...]) -> ResearchOutcomeStatus:
    if any(item.outcome is ResearchOutcomeStatus.VALIDATED_RESEARCH for item in outcomes):
        return ResearchOutcomeStatus.VALIDATED_RESEARCH
    if any(item.outcome is ResearchOutcomeStatus.REVISE for item in outcomes):
        return ResearchOutcomeStatus.REVISE
    if any(item.outcome is ResearchOutcomeStatus.REJECT for item in outcomes):
        return ResearchOutcomeStatus.REJECT
    return ResearchOutcomeStatus.NO_OFFICIAL_EVIDENCE


def _next_actions(outcome: ResearchOutcomeStatus) -> tuple[str, ...]:
    if outcome is ResearchOutcomeStatus.REVISE:
        return ("修订候选后重新运行机器证据", "检查成本、换手与稳健性")
    if outcome is ResearchOutcomeStatus.REJECT:
        return ("归档失败原因", "停止同一候选的无成本重试")
    if outcome is ResearchOutcomeStatus.VALIDATED_RESEARCH:
        return ("进入人工检查点", "如需挑战池必须接入独立封闭评审")
    return ("补齐数据或表达式后重新提交",)


def _summary_for(outcome: ResearchOutcomeStatus) -> str:
    if outcome is ResearchOutcomeStatus.VALIDATED_RESEARCH:
        return "候选形成可见官方机器证据，但尚未取得独立封闭评审。"
    if outcome is ResearchOutcomeStatus.REVISE:
        return "候选需要修订后复验。"
    if outcome is ResearchOutcomeStatus.REJECT:
        return "候选未通过机器评审。"
    return "未形成可复现官方机器证据。"
