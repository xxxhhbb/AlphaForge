"""Mode-specific Campaign aggregation and decision artifacts.

Wave C6 turns a completed Campaign runtime manifest and visible
train/validation evidence summaries into product-facing outputs.  It does not
calculate official Evidence and does not issue Promotion, CHALLENGER, Alpha,
Portfolio, Risk, Execution, or Ledger decisions.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from types import MappingProxyType
from typing import ClassVar, Mapping

from ..contracts import CanonicalContract, Component, FailureCode
from ..contracts.base import (
    fail,
    freeze_mapping,
    require_aware_datetime,
    require_finite_number,
    require_hash,
    require_identifier,
    require_text,
)
from ..evidence.models import VISIBLE_SUMMARY_METRICS
from ..llm_desks.safety import canonical_hash
from .models import ResearchCampaign, ResearchDepth, ResearchMode, require_campaign_executable
from .runtime import CampaignRuntimeManifest, CampaignRuntimeStatus


CAMPAIGN_AGGREGATION_RULE_VERSION = "veriquant.campaign-aggregation/v1"
DEFAULT_AGGREGATED_AT = datetime(2026, 7, 19, 12, 30, tzinfo=timezone.utc)

REQUIRED_SELECTION_METRICS = frozenset(
    {
        "icir",
        "net_return",
        "turnover",
        "max_drawdown",
        "subperiod_positive_fraction",
        "regime_positive_fraction",
        "max_registered_abs_correlation",
        "incremental_ic_mean",
    }
)

_SELECTION_SOURCE_COMPONENTS = frozenset(
    {
        Component.LLM_RESEARCHER,
        Component.RESEARCH_DESK,
        Component.QUANTAALPHA,
        Component.QLIB,
        Component.OPERATOR,
    }
)

_FORBIDDEN_REPORT_TEXT = (
    "sealed",
    "holdout",
    "api_key",
    "apikey",
    "authorization",
    "bearer ",
    "private_key",
    "secret",
    "risk_approval",
    "target_weights",
    "broker",
    "factor.py",
    "eval(",
    "exec(",
)


class CampaignDecisionArtifactKind(str, Enum):
    CANDIDATE_SET = "CANDIDATE_SET"
    VALIDATION_CONCLUSION = "VALIDATION_CONCLUSION"
    FACTOR_COMPARISON_REPORT = "FACTOR_COMPARISON_REPORT"
    PORTFOLIO_GAP_REPORT = "PORTFOLIO_GAP_REPORT"
    DIAGNOSTIC_REPORT = "DIAGNOSTIC_REPORT"
    EVENT_STUDY_REPORT = "EVENT_STUDY_REPORT"
    DATASET_ASSESSMENT = "DATASET_ASSESSMENT"
    REPLICATION_REPORT = "REPLICATION_REPORT"
    PRELAUNCH_REVIEW = "PRELAUNCH_REVIEW"
    MONITORING_RECOMMENDATION = "MONITORING_RECOMMENDATION"


class CandidateDraftSource(str, Enum):
    RESEARCH_DESK = "RESEARCH_DESK"
    DOCUMENT = "DOCUMENT"
    NEWS = "NEWS"
    QLIB_AUXILIARY = "QLIB_AUXILIARY"
    OPERATOR = "OPERATOR"


class CandidateSelectionBasis(str, Enum):
    DETERMINISTIC_VISIBLE_EVIDENCE = "DETERMINISTIC_VISIBLE_EVIDENCE"
    SOURCE_CITATION_ONLY = "SOURCE_CITATION_ONLY"
    LLM_RANKING = "LLM_RANKING"
    RANK_IC_ONLY = "RANK_IC_ONLY"


class CandidateSelectionVerdict(str, Enum):
    SELECTED_FOR_PROMOTION_REVIEW = "SELECTED_FOR_PROMOTION_REVIEW"
    NOT_ELIGIBLE = "NOT_ELIGIBLE"
    REVISE = "REVISE"
    REJECTED = "REJECTED"


class ValidationConclusionVerdict(str, Enum):
    SUPPORTED = "SUPPORTED"
    CONDITIONALLY_SUPPORTED = "CONDITIONALLY_SUPPORTED"
    REJECTED = "REJECTED"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"


class ReplicationStatus(str, Enum):
    REPLICATED = "REPLICATED"
    APPROXIMATE = "APPROXIMATE"
    DATA_UNAVAILABLE = "DATA_UNAVAILABLE"
    TIME_SEMANTICS_UNCLEAR = "TIME_SEMANTICS_UNCLEAR"
    NOT_REPLICATED = "NOT_REPLICATED"


class ReviewStatus(str, Enum):
    PASS = "PASS"
    REVISE = "REVISE"
    REJECT = "REJECT"
    BLOCKED = "BLOCKED"


class MonitoringAction(str, Enum):
    KEEP = "KEEP"
    SCALE_DOWN = "SCALE_DOWN"
    PAUSE = "PAUSE"
    RETIRE = "RETIRE"


@dataclass(frozen=True, kw_only=True)
class SelectionMetricBundle(CanonicalContract):
    SCHEMA_VERSION: ClassVar[str] = CAMPAIGN_AGGREGATION_RULE_VERSION

    net_icir: float
    stability: float
    cost_capacity: float
    novelty: float
    incrementality: float
    family_budget_remaining: int
    family_budget_consumed: int
    visible_train_evidence_hash: str
    visible_validation_evidence_hash: str
    visible_train_metrics: Mapping[str, float]
    visible_validation_metrics: Mapping[str, float]

    def __post_init__(self) -> None:
        for name in ("net_icir", "stability", "cost_capacity", "novelty", "incrementality"):
            value = getattr(self, name)
            require_finite_number(value, name)
        for name in ("stability", "cost_capacity", "novelty", "incrementality"):
            value = float(getattr(self, name))
            if not 0.0 <= value <= 1.0:
                fail(FailureCode.INVALID_VALUE, f"{name} must be between zero and one")
        for name in ("family_budget_remaining", "family_budget_consumed"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                fail(FailureCode.INVALID_VALUE, f"{name} must be a non-negative integer")
        if self.family_budget_remaining + self.family_budget_consumed < 1:
            fail(FailureCode.MISSING_REQUIRED_FIELD, "selection requires family budget context")
        require_hash(self.visible_train_evidence_hash, "visible_train_evidence_hash")
        require_hash(self.visible_validation_evidence_hash, "visible_validation_evidence_hash")
        for field_name in ("visible_train_metrics", "visible_validation_metrics"):
            values = freeze_mapping(getattr(self, field_name), field_name, numeric_values=True)
            keys = set(values)
            if keys - VISIBLE_SUMMARY_METRICS:
                fail(FailureCode.UNAUTHORIZED_CAPABILITY, "visible evidence summary contains non-allowlisted metrics")
            if keys <= {"rank_ic"}:
                fail(FailureCode.UNAUTHORIZED_CAPABILITY, "RankIC-only evidence cannot drive selection")
            missing = REQUIRED_SELECTION_METRICS - keys
            if missing:
                fail(
                    FailureCode.MISSING_REQUIRED_FIELD,
                    "selection metrics must include net/cost/stability/incrementality evidence",
                    field=field_name,
                    missing=tuple(sorted(missing)),
                )
            object.__setattr__(self, field_name, values)


@dataclass(frozen=True, kw_only=True)
class CandidateSelectionPolicy(CanonicalContract):
    SCHEMA_VERSION: ClassVar[str] = CAMPAIGN_AGGREGATION_RULE_VERSION

    policy_id: str = "candidate_selection.visible_train_validation.v1"
    min_net_icir: float = 0.0
    min_stability: float = 0.5
    min_cost_capacity: float = 0.5
    min_novelty: float = 0.05
    min_incrementality: float = 0.0
    max_selected: int = 5
    net_icir_weight: float = 0.30
    stability_weight: float = 0.20
    cost_capacity_weight: float = 0.20
    novelty_weight: float = 0.15
    incrementality_weight: float = 0.10
    family_budget_weight: float = 0.05

    def __post_init__(self) -> None:
        require_identifier(self.policy_id, "policy_id")
        for name in (
            "min_net_icir",
            "min_stability",
            "min_cost_capacity",
            "min_novelty",
            "min_incrementality",
            "net_icir_weight",
            "stability_weight",
            "cost_capacity_weight",
            "novelty_weight",
            "incrementality_weight",
            "family_budget_weight",
        ):
            require_finite_number(getattr(self, name), name)
        if isinstance(self.max_selected, bool) or not isinstance(self.max_selected, int) or self.max_selected < 1:
            fail(FailureCode.INVALID_VALUE, "max_selected must be a positive integer")
        weights = (
            self.net_icir_weight,
            self.stability_weight,
            self.cost_capacity_weight,
            self.novelty_weight,
            self.incrementality_weight,
            self.family_budget_weight,
        )
        if any(weight <= 0 for weight in weights):
            fail(FailureCode.UNAUTHORIZED_CAPABILITY, "deterministic selection must use every required factor")
        total = sum(weights)
        if abs(total - 1.0) > 1e-9:
            fail(FailureCode.INVALID_VALUE, "selection weights must sum to one")

    def score(self, metrics: SelectionMetricBundle) -> float:
        budget_total = metrics.family_budget_remaining + metrics.family_budget_consumed
        budget_score = metrics.family_budget_remaining / budget_total
        return (
            self.net_icir_weight * metrics.net_icir
            + self.stability_weight * metrics.stability
            + self.cost_capacity_weight * metrics.cost_capacity
            + self.novelty_weight * metrics.novelty
            + self.incrementality_weight * metrics.incrementality
            + self.family_budget_weight * budget_score
        )


@dataclass(frozen=True, kw_only=True)
class CandidateSelectionInput(CanonicalContract):
    SCHEMA_VERSION: ClassVar[str] = CAMPAIGN_AGGREGATION_RULE_VERSION

    candidate_id: str
    candidate_hash: str
    family_id: str
    proposer_id: str
    draft_source: CandidateDraftSource
    source_component: Component
    selection_basis: CandidateSelectionBasis = CandidateSelectionBasis.DETERMINISTIC_VISIBLE_EVIDENCE
    pit_validated: bool = False
    official_evidence_hash: str | None = None
    critic_passed: bool = False
    critic_finding_hashes: tuple[str, ...] = ()
    critic_reason_codes: tuple[str, ...] = ()
    selection_metrics: SelectionMetricBundle | None = None

    def __post_init__(self) -> None:
        require_identifier(self.candidate_id, "candidate_id")
        require_hash(self.candidate_hash, "candidate_hash")
        require_identifier(self.family_id, "family_id")
        require_identifier(self.proposer_id, "proposer_id")
        if not isinstance(self.draft_source, CandidateDraftSource):
            object.__setattr__(self, "draft_source", CandidateDraftSource(self.draft_source))
        if not isinstance(self.source_component, Component):
            object.__setattr__(self, "source_component", Component(self.source_component))
        if self.source_component not in _SELECTION_SOURCE_COMPONENTS:
            fail(FailureCode.INVALID_ARTIFACT_PRODUCER, "candidate source cannot be a downstream authority")
        if not isinstance(self.selection_basis, CandidateSelectionBasis):
            object.__setattr__(self, "selection_basis", CandidateSelectionBasis(self.selection_basis))
        if self.selection_basis in {CandidateSelectionBasis.LLM_RANKING, CandidateSelectionBasis.RANK_IC_ONLY}:
            fail(FailureCode.UNAUTHORIZED_CAPABILITY, "LLM ranking or RankIC-only cannot drive selection")
        if not isinstance(self.pit_validated, bool) or not isinstance(self.critic_passed, bool):
            fail(FailureCode.INVALID_VALUE, "candidate validation flags must be boolean")
        if self.official_evidence_hash is not None:
            require_hash(self.official_evidence_hash, "official_evidence_hash")
            if not self.pit_validated:
                fail(FailureCode.INVALID_STATE, "official evidence candidate must pass PIT first")
            if self.selection_metrics is None:
                fail(FailureCode.MISSING_REQUIRED_FIELD, "official evidence selection requires visible metrics")
        _validate_hash_tuple(self.critic_finding_hashes, "critic_finding_hashes", nonempty=False)
        _validate_string_tuple(self.critic_reason_codes, "critic_reason_codes", nonempty=False)
        if self.critic_passed:
            if self.official_evidence_hash is None:
                fail(FailureCode.INVALID_STATE, "critic-passed candidate requires official evidence hash")
            if not self.critic_finding_hashes or not self.critic_reason_codes:
                fail(FailureCode.MISSING_REQUIRED_FIELD, "critic-passed candidate requires critic findings")
        if self.selection_basis is CandidateSelectionBasis.SOURCE_CITATION_ONLY and (
            self.pit_validated or self.official_evidence_hash is not None or self.critic_passed
        ):
            fail(FailureCode.UNAUTHORIZED_CAPABILITY, "document/news citation alone cannot claim PIT/Evidence/Critic")


@dataclass(frozen=True, kw_only=True)
class CandidateSelectionRecord(CanonicalContract):
    SCHEMA_VERSION: ClassVar[str] = CAMPAIGN_AGGREGATION_RULE_VERSION

    candidate_id: str
    candidate_hash: str
    family_id: str
    verdict: CandidateSelectionVerdict
    score: float | None
    reason_codes: tuple[str, ...]
    official_evidence_hash: str | None = None

    def __post_init__(self) -> None:
        require_identifier(self.candidate_id, "candidate_id")
        require_hash(self.candidate_hash, "candidate_hash")
        require_identifier(self.family_id, "family_id")
        if not isinstance(self.verdict, CandidateSelectionVerdict):
            object.__setattr__(self, "verdict", CandidateSelectionVerdict(self.verdict))
        if self.score is not None:
            require_finite_number(self.score, "score")
        _validate_string_tuple(self.reason_codes, "reason_codes")
        if self.official_evidence_hash is not None:
            require_hash(self.official_evidence_hash, "official_evidence_hash")
        if self.verdict is CandidateSelectionVerdict.SELECTED_FOR_PROMOTION_REVIEW:
            if self.score is None or self.official_evidence_hash is None:
                fail(FailureCode.INVALID_STATE, "selected candidates require score and official evidence hash")


@dataclass(frozen=True, kw_only=True)
class CandidateSet(CanonicalContract):
    SCHEMA_VERSION: ClassVar[str] = CAMPAIGN_AGGREGATION_RULE_VERSION

    candidate_set_id: str
    campaign_hash: str
    run_manifest_hash: str
    route_hash: str
    campaign_mode: ResearchMode
    research_depth: ResearchDepth
    selected_candidates: tuple[CandidateSelectionRecord, ...]
    rejected_candidates: tuple[CandidateSelectionRecord, ...]
    promotion_candidate_hashes: tuple[str, ...]
    selection_policy_hash: str
    generated_at: datetime
    produced_by: Component = Component.CRITIC_SERVICE
    artifact_kind: CampaignDecisionArtifactKind = CampaignDecisionArtifactKind.CANDIDATE_SET

    def __post_init__(self) -> None:
        _validate_common_artifact(
            artifact_id=self.candidate_set_id,
            campaign_hash=self.campaign_hash,
            run_manifest_hash=self.run_manifest_hash,
            route_hash=self.route_hash,
            campaign_mode=self.campaign_mode,
            research_depth=self.research_depth,
            generated_at=self.generated_at,
            produced_by=self.produced_by,
            expected_mode=ResearchMode.NEW_ALPHA_EXPLORATION,
        )
        object.__setattr__(
            self,
            "artifact_kind",
            _validate_artifact_kind(self.artifact_kind, CampaignDecisionArtifactKind.CANDIDATE_SET),
        )
        _validate_record_tuple(self.selected_candidates, "selected_candidates")
        _validate_record_tuple(self.rejected_candidates, "rejected_candidates")
        _validate_hash_tuple(self.promotion_candidate_hashes, "promotion_candidate_hashes", nonempty=False)
        require_hash(self.selection_policy_hash, "selection_policy_hash")
        selected_hashes = {record.candidate_hash for record in self.selected_candidates}
        if set(self.promotion_candidate_hashes) - selected_hashes:
            fail(FailureCode.INVALID_STATE, "only selected CandidateSet records may enter promotion review")
        if self.research_depth is ResearchDepth.QUICK_EXPLORATION and self.promotion_candidate_hashes:
            fail(FailureCode.UNAUTHORIZED_CAPABILITY, "QUICK_EXPLORATION cannot enter promotion review")


@dataclass(frozen=True, kw_only=True)
class ValidationConclusion(CanonicalContract):
    SCHEMA_VERSION: ClassVar[str] = CAMPAIGN_AGGREGATION_RULE_VERSION

    conclusion_id: str
    campaign_hash: str
    run_manifest_hash: str
    route_hash: str
    campaign_mode: ResearchMode
    research_depth: ResearchDepth
    verdict: ValidationConclusionVerdict
    supporting_candidate_hashes: tuple[str, ...]
    evidence_hashes: tuple[str, ...]
    reason_codes: tuple[str, ...]
    generated_at: datetime
    produced_by: Component = Component.CRITIC_SERVICE
    artifact_kind: CampaignDecisionArtifactKind = CampaignDecisionArtifactKind.VALIDATION_CONCLUSION

    def __post_init__(self) -> None:
        _validate_common_artifact(
            artifact_id=self.conclusion_id,
            campaign_hash=self.campaign_hash,
            run_manifest_hash=self.run_manifest_hash,
            route_hash=self.route_hash,
            campaign_mode=self.campaign_mode,
            research_depth=self.research_depth,
            generated_at=self.generated_at,
            produced_by=self.produced_by,
            expected_mode=ResearchMode.VALIDATE_IDEA,
        )
        object.__setattr__(self, "verdict", _coerce_enum(self.verdict, ValidationConclusionVerdict))
        object.__setattr__(
            self,
            "artifact_kind",
            _validate_artifact_kind(self.artifact_kind, CampaignDecisionArtifactKind.VALIDATION_CONCLUSION),
        )
        _validate_hash_tuple(self.supporting_candidate_hashes, "supporting_candidate_hashes", nonempty=False)
        _validate_hash_tuple(self.evidence_hashes, "evidence_hashes", nonempty=False)
        _validate_string_tuple(self.reason_codes, "reason_codes")


@dataclass(frozen=True, kw_only=True)
class FactorComparisonReport(CanonicalContract):
    SCHEMA_VERSION: ClassVar[str] = CAMPAIGN_AGGREGATION_RULE_VERSION

    report_id: str
    campaign_hash: str
    run_manifest_hash: str
    route_hash: str
    campaign_mode: ResearchMode
    research_depth: ResearchDepth
    parent_factor_id: str
    child_candidate_hashes: tuple[str, ...]
    comparison_hashes: tuple[str, ...]
    reason_codes: tuple[str, ...]
    generated_at: datetime
    produced_by: Component = Component.CRITIC_SERVICE
    artifact_kind: CampaignDecisionArtifactKind = CampaignDecisionArtifactKind.FACTOR_COMPARISON_REPORT

    def __post_init__(self) -> None:
        _validate_common_artifact(
            artifact_id=self.report_id,
            campaign_hash=self.campaign_hash,
            run_manifest_hash=self.run_manifest_hash,
            route_hash=self.route_hash,
            campaign_mode=self.campaign_mode,
            research_depth=self.research_depth,
            generated_at=self.generated_at,
            produced_by=self.produced_by,
            expected_mode=ResearchMode.IMPROVE_EXISTING_FACTOR,
        )
        require_identifier(self.parent_factor_id, "parent_factor_id")
        _validate_hash_tuple(self.child_candidate_hashes, "child_candidate_hashes", nonempty=False)
        _validate_hash_tuple(self.comparison_hashes, "comparison_hashes", nonempty=False)
        _validate_string_tuple(self.reason_codes, "reason_codes")
        object.__setattr__(
            self,
            "artifact_kind",
            _validate_artifact_kind(self.artifact_kind, CampaignDecisionArtifactKind.FACTOR_COMPARISON_REPORT),
        )


@dataclass(frozen=True, kw_only=True)
class PortfolioGapReport(CanonicalContract):
    SCHEMA_VERSION: ClassVar[str] = CAMPAIGN_AGGREGATION_RULE_VERSION

    report_id: str
    campaign_hash: str
    run_manifest_hash: str
    route_hash: str
    campaign_mode: ResearchMode
    research_depth: ResearchDepth
    portfolio_id: str
    gap_summary_hash: str
    candidate_hashes: tuple[str, ...]
    incremental_candidate_hashes: tuple[str, ...]
    generated_at: datetime
    produced_by: Component = Component.CRITIC_SERVICE
    artifact_kind: CampaignDecisionArtifactKind = CampaignDecisionArtifactKind.PORTFOLIO_GAP_REPORT

    def __post_init__(self) -> None:
        _validate_common_artifact(
            artifact_id=self.report_id,
            campaign_hash=self.campaign_hash,
            run_manifest_hash=self.run_manifest_hash,
            route_hash=self.route_hash,
            campaign_mode=self.campaign_mode,
            research_depth=self.research_depth,
            generated_at=self.generated_at,
            produced_by=self.produced_by,
            expected_mode=ResearchMode.PORTFOLIO_GAP_SEARCH,
        )
        require_identifier(self.portfolio_id, "portfolio_id")
        require_hash(self.gap_summary_hash, "gap_summary_hash")
        _validate_hash_tuple(self.candidate_hashes, "candidate_hashes", nonempty=False)
        _validate_hash_tuple(self.incremental_candidate_hashes, "incremental_candidate_hashes", nonempty=False)
        object.__setattr__(
            self,
            "artifact_kind",
            _validate_artifact_kind(self.artifact_kind, CampaignDecisionArtifactKind.PORTFOLIO_GAP_REPORT),
        )


@dataclass(frozen=True, kw_only=True)
class DiagnosticReport(CanonicalContract):
    SCHEMA_VERSION: ClassVar[str] = CAMPAIGN_AGGREGATION_RULE_VERSION

    report_id: str
    campaign_hash: str
    run_manifest_hash: str
    route_hash: str
    campaign_mode: ResearchMode
    research_depth: ResearchDepth
    root_cause_codes: tuple[str, ...]
    evidence_hashes: tuple[str, ...]
    recommendation: str
    generated_at: datetime
    produced_by: Component = Component.CRITIC_SERVICE
    artifact_kind: CampaignDecisionArtifactKind = CampaignDecisionArtifactKind.DIAGNOSTIC_REPORT

    def __post_init__(self) -> None:
        _validate_common_artifact(
            artifact_id=self.report_id,
            campaign_hash=self.campaign_hash,
            run_manifest_hash=self.run_manifest_hash,
            route_hash=self.route_hash,
            campaign_mode=self.campaign_mode,
            research_depth=self.research_depth,
            generated_at=self.generated_at,
            produced_by=self.produced_by,
            expected_mode=ResearchMode.DIAGNOSE_FACTOR_PORTFOLIO,
        )
        _validate_string_tuple(self.root_cause_codes, "root_cause_codes")
        _validate_hash_tuple(self.evidence_hashes, "evidence_hashes", nonempty=False)
        _validate_report_text(self.recommendation, "recommendation")
        if "challenger" in self.recommendation.lower():
            fail(FailureCode.UNAUTHORIZED_CAPABILITY, "diagnosis cannot directly create CHALLENGER")
        object.__setattr__(
            self,
            "artifact_kind",
            _validate_artifact_kind(self.artifact_kind, CampaignDecisionArtifactKind.DIAGNOSTIC_REPORT),
        )


@dataclass(frozen=True, kw_only=True)
class EventStudyReport(CanonicalContract):
    SCHEMA_VERSION: ClassVar[str] = CAMPAIGN_AGGREGATION_RULE_VERSION

    report_id: str
    campaign_hash: str
    run_manifest_hash: str
    route_hash: str
    campaign_mode: ResearchMode
    research_depth: ResearchDepth
    event_definition_hash: str
    conditional_finding_hashes: tuple[str, ...]
    generated_at: datetime
    produced_by: Component = Component.CRITIC_SERVICE
    artifact_kind: CampaignDecisionArtifactKind = CampaignDecisionArtifactKind.EVENT_STUDY_REPORT

    def __post_init__(self) -> None:
        _validate_common_artifact(
            artifact_id=self.report_id,
            campaign_hash=self.campaign_hash,
            run_manifest_hash=self.run_manifest_hash,
            route_hash=self.route_hash,
            campaign_mode=self.campaign_mode,
            research_depth=self.research_depth,
            generated_at=self.generated_at,
            produced_by=self.produced_by,
            expected_mode=ResearchMode.EVENT_MARKET_TOPIC,
        )
        require_hash(self.event_definition_hash, "event_definition_hash")
        _validate_hash_tuple(self.conditional_finding_hashes, "conditional_finding_hashes", nonempty=False)
        object.__setattr__(
            self,
            "artifact_kind",
            _validate_artifact_kind(self.artifact_kind, CampaignDecisionArtifactKind.EVENT_STUDY_REPORT),
        )


@dataclass(frozen=True, kw_only=True)
class DatasetAssessment(CanonicalContract):
    SCHEMA_VERSION: ClassVar[str] = CAMPAIGN_AGGREGATION_RULE_VERSION

    assessment_id: str
    campaign_hash: str
    run_manifest_hash: str
    route_hash: str
    campaign_mode: ResearchMode
    research_depth: ResearchDepth
    dataset_hash: str
    pit_assessment_hash: str
    coverage_bias_hash: str
    generated_at: datetime
    produced_by: Component = Component.CRITIC_SERVICE
    artifact_kind: CampaignDecisionArtifactKind = CampaignDecisionArtifactKind.DATASET_ASSESSMENT

    def __post_init__(self) -> None:
        _validate_common_artifact(
            artifact_id=self.assessment_id,
            campaign_hash=self.campaign_hash,
            run_manifest_hash=self.run_manifest_hash,
            route_hash=self.route_hash,
            campaign_mode=self.campaign_mode,
            research_depth=self.research_depth,
            generated_at=self.generated_at,
            produced_by=self.produced_by,
            expected_mode=ResearchMode.NEW_DATA_SOURCE_EVALUATION,
        )
        for name in ("dataset_hash", "pit_assessment_hash", "coverage_bias_hash"):
            require_hash(getattr(self, name), name)
        object.__setattr__(
            self,
            "artifact_kind",
            _validate_artifact_kind(self.artifact_kind, CampaignDecisionArtifactKind.DATASET_ASSESSMENT),
        )


@dataclass(frozen=True, kw_only=True)
class ReplicationReport(CanonicalContract):
    SCHEMA_VERSION: ClassVar[str] = CAMPAIGN_AGGREGATION_RULE_VERSION

    report_id: str
    campaign_hash: str
    run_manifest_hash: str
    route_hash: str
    campaign_mode: ResearchMode
    research_depth: ResearchDepth
    source_document_hash: str
    replication_status: ReplicationStatus
    discrepancy_hashes: tuple[str, ...]
    generated_at: datetime
    produced_by: Component = Component.CRITIC_SERVICE
    artifact_kind: CampaignDecisionArtifactKind = CampaignDecisionArtifactKind.REPLICATION_REPORT

    def __post_init__(self) -> None:
        _validate_common_artifact(
            artifact_id=self.report_id,
            campaign_hash=self.campaign_hash,
            run_manifest_hash=self.run_manifest_hash,
            route_hash=self.route_hash,
            campaign_mode=self.campaign_mode,
            research_depth=self.research_depth,
            generated_at=self.generated_at,
            produced_by=self.produced_by,
            expected_mode=ResearchMode.PAPER_STRATEGY_REPLICATION,
        )
        require_hash(self.source_document_hash, "source_document_hash")
        object.__setattr__(self, "replication_status", _coerce_enum(self.replication_status, ReplicationStatus))
        _validate_hash_tuple(self.discrepancy_hashes, "discrepancy_hashes", nonempty=False)
        object.__setattr__(
            self,
            "artifact_kind",
            _validate_artifact_kind(self.artifact_kind, CampaignDecisionArtifactKind.REPLICATION_REPORT),
        )


@dataclass(frozen=True, kw_only=True)
class PrelaunchReview(CanonicalContract):
    SCHEMA_VERSION: ClassVar[str] = CAMPAIGN_AGGREGATION_RULE_VERSION

    review_id: str
    campaign_hash: str
    run_manifest_hash: str
    route_hash: str
    campaign_mode: ResearchMode
    research_depth: ResearchDepth
    review_status: ReviewStatus
    cost_capacity_hash: str
    robustness_hash: str
    unresolved_risk_codes: tuple[str, ...]
    generated_at: datetime
    produced_by: Component = Component.CRITIC_SERVICE
    artifact_kind: CampaignDecisionArtifactKind = CampaignDecisionArtifactKind.PRELAUNCH_REVIEW

    def __post_init__(self) -> None:
        _validate_common_artifact(
            artifact_id=self.review_id,
            campaign_hash=self.campaign_hash,
            run_manifest_hash=self.run_manifest_hash,
            route_hash=self.route_hash,
            campaign_mode=self.campaign_mode,
            research_depth=self.research_depth,
            generated_at=self.generated_at,
            produced_by=self.produced_by,
            expected_mode=ResearchMode.PRELAUNCH_REVIEW,
        )
        object.__setattr__(self, "review_status", _coerce_enum(self.review_status, ReviewStatus))
        require_hash(self.cost_capacity_hash, "cost_capacity_hash")
        require_hash(self.robustness_hash, "robustness_hash")
        _validate_string_tuple(self.unresolved_risk_codes, "unresolved_risk_codes", nonempty=False)
        object.__setattr__(
            self,
            "artifact_kind",
            _validate_artifact_kind(self.artifact_kind, CampaignDecisionArtifactKind.PRELAUNCH_REVIEW),
        )


@dataclass(frozen=True, kw_only=True)
class MonitoringRecommendation(CanonicalContract):
    SCHEMA_VERSION: ClassVar[str] = CAMPAIGN_AGGREGATION_RULE_VERSION

    recommendation_id: str
    campaign_hash: str
    run_manifest_hash: str
    route_hash: str
    campaign_mode: ResearchMode
    research_depth: ResearchDepth
    action: MonitoringAction
    baseline_hash: str
    drift_evidence_hashes: tuple[str, ...]
    human_decision_required: bool
    generated_at: datetime
    produced_by: Component = Component.CRITIC_SERVICE
    artifact_kind: CampaignDecisionArtifactKind = CampaignDecisionArtifactKind.MONITORING_RECOMMENDATION

    def __post_init__(self) -> None:
        _validate_common_artifact(
            artifact_id=self.recommendation_id,
            campaign_hash=self.campaign_hash,
            run_manifest_hash=self.run_manifest_hash,
            route_hash=self.route_hash,
            campaign_mode=self.campaign_mode,
            research_depth=self.research_depth,
            generated_at=self.generated_at,
            produced_by=self.produced_by,
            expected_mode=ResearchMode.MONITOR_DECAY_RETIREMENT,
        )
        object.__setattr__(self, "action", _coerce_enum(self.action, MonitoringAction))
        require_hash(self.baseline_hash, "baseline_hash")
        _validate_hash_tuple(self.drift_evidence_hashes, "drift_evidence_hashes", nonempty=False)
        if not isinstance(self.human_decision_required, bool) or not self.human_decision_required:
            fail(FailureCode.UNAUTHORIZED_CAPABILITY, "monitoring recommendations require a human decision")
        object.__setattr__(
            self,
            "artifact_kind",
            _validate_artifact_kind(self.artifact_kind, CampaignDecisionArtifactKind.MONITORING_RECOMMENDATION),
        )


CampaignDecisionArtifact = (
    CandidateSet
    | ValidationConclusion
    | FactorComparisonReport
    | PortfolioGapReport
    | DiagnosticReport
    | EventStudyReport
    | DatasetAssessment
    | ReplicationReport
    | PrelaunchReview
    | MonitoringRecommendation
)


class CampaignDecisionAggregator:
    """Deterministically aggregate Campaign runtime output into mode products."""

    def __init__(self, *, selection_policy: CandidateSelectionPolicy | None = None) -> None:
        self.selection_policy = selection_policy or CandidateSelectionPolicy()

    def aggregate(
        self,
        *,
        campaign: ResearchCampaign,
        runtime_manifest: CampaignRuntimeManifest,
        candidates: tuple[CandidateSelectionInput, ...] = (),
        generated_at: datetime = DEFAULT_AGGREGATED_AT,
    ) -> CampaignDecisionArtifact:
        require_campaign_executable(campaign)
        _validate_runtime_binding(campaign, runtime_manifest)
        require_aware_datetime(generated_at, "generated_at")
        if not isinstance(candidates, tuple) or not all(isinstance(item, CandidateSelectionInput) for item in candidates):
            fail(FailureCode.INVALID_VALUE, "candidates must be CandidateSelectionInput tuple")
        sorted_candidates = tuple(sorted(candidates, key=lambda item: item.candidate_id))
        if campaign.research_depth is ResearchDepth.QUICK_EXPLORATION:
            _reject_quick_official_evidence(sorted_candidates)

        match campaign.mode:
            case ResearchMode.NEW_ALPHA_EXPLORATION:
                return self._candidate_set(campaign, runtime_manifest, sorted_candidates, generated_at)
            case ResearchMode.VALIDATE_IDEA:
                return self._validation_conclusion(campaign, runtime_manifest, sorted_candidates, generated_at)
            case ResearchMode.IMPROVE_EXISTING_FACTOR:
                return self._factor_comparison(campaign, runtime_manifest, sorted_candidates, generated_at)
            case ResearchMode.PORTFOLIO_GAP_SEARCH:
                return self._portfolio_gap(campaign, runtime_manifest, sorted_candidates, generated_at)
            case ResearchMode.DIAGNOSE_FACTOR_PORTFOLIO:
                return self._diagnostic(campaign, runtime_manifest, sorted_candidates, generated_at)
            case ResearchMode.EVENT_MARKET_TOPIC:
                return self._event_study(campaign, runtime_manifest, generated_at)
            case ResearchMode.NEW_DATA_SOURCE_EVALUATION:
                return self._dataset_assessment(campaign, runtime_manifest, generated_at)
            case ResearchMode.PAPER_STRATEGY_REPLICATION:
                return self._replication(campaign, runtime_manifest, generated_at)
            case ResearchMode.PRELAUNCH_REVIEW:
                return self._prelaunch(campaign, runtime_manifest, sorted_candidates, generated_at)
            case ResearchMode.MONITOR_DECAY_RETIREMENT:
                return self._monitoring(campaign, runtime_manifest, generated_at)
        fail(FailureCode.INVALID_VALUE, "unsupported research mode")

    def _candidate_set(
        self,
        campaign: ResearchCampaign,
        runtime_manifest: CampaignRuntimeManifest,
        candidates: tuple[CandidateSelectionInput, ...],
        generated_at: datetime,
    ) -> CandidateSet:
        selected, rejected = self._select(candidates)
        selected_hashes = tuple(record.candidate_hash for record in selected)
        return CandidateSet(
            candidate_set_id=_artifact_id("candidate_set", campaign, runtime_manifest, generated_at),
            campaign_hash=campaign.artifact_hash,
            run_manifest_hash=runtime_manifest.artifact_hash,
            route_hash=runtime_manifest.route_hash,
            campaign_mode=campaign.mode,
            research_depth=campaign.research_depth,
            selected_candidates=selected,
            rejected_candidates=rejected,
            promotion_candidate_hashes=selected_hashes,
            selection_policy_hash=self.selection_policy.artifact_hash,
            generated_at=generated_at,
        )

    def _validation_conclusion(
        self,
        campaign: ResearchCampaign,
        runtime_manifest: CampaignRuntimeManifest,
        candidates: tuple[CandidateSelectionInput, ...],
        generated_at: datetime,
    ) -> ValidationConclusion:
        eligible = tuple(candidate for candidate in candidates if _eligible(candidate, self.selection_policy))
        evidence_hashes = _official_evidence_hashes(candidates)
        if eligible:
            verdict = ValidationConclusionVerdict.SUPPORTED
            reason_codes = ("VISIBLE_EVIDENCE_SUPPORTS_HYPOTHESIS",)
        elif evidence_hashes:
            verdict = ValidationConclusionVerdict.REJECTED
            reason_codes = ("VISIBLE_EVIDENCE_FAILS_SELECTION_POLICY",)
        else:
            verdict = ValidationConclusionVerdict.INSUFFICIENT_EVIDENCE
            reason_codes = ("NO_OFFICIAL_VISIBLE_EVIDENCE",)
        return ValidationConclusion(
            conclusion_id=_artifact_id("validation_conclusion", campaign, runtime_manifest, generated_at),
            campaign_hash=campaign.artifact_hash,
            run_manifest_hash=runtime_manifest.artifact_hash,
            route_hash=runtime_manifest.route_hash,
            campaign_mode=campaign.mode,
            research_depth=campaign.research_depth,
            verdict=verdict,
            supporting_candidate_hashes=tuple(candidate.candidate_hash for candidate in eligible),
            evidence_hashes=evidence_hashes,
            reason_codes=reason_codes,
            generated_at=generated_at,
        )

    def _factor_comparison(
        self,
        campaign: ResearchCampaign,
        runtime_manifest: CampaignRuntimeManifest,
        candidates: tuple[CandidateSelectionInput, ...],
        generated_at: datetime,
    ) -> FactorComparisonReport:
        parent = campaign.subject.existing_factor_ids[0] if campaign.subject.existing_factor_ids else "missing.parent"
        return FactorComparisonReport(
            report_id=_artifact_id("factor_comparison", campaign, runtime_manifest, generated_at),
            campaign_hash=campaign.artifact_hash,
            run_manifest_hash=runtime_manifest.artifact_hash,
            route_hash=runtime_manifest.route_hash,
            campaign_mode=campaign.mode,
            research_depth=campaign.research_depth,
            parent_factor_id=parent,
            child_candidate_hashes=_candidate_hashes(candidates),
            comparison_hashes=_official_evidence_hashes(candidates),
            reason_codes=("PARENT_CHILD_VISIBLE_COMPARISON_ONLY",),
            generated_at=generated_at,
        )

    def _portfolio_gap(
        self,
        campaign: ResearchCampaign,
        runtime_manifest: CampaignRuntimeManifest,
        candidates: tuple[CandidateSelectionInput, ...],
        generated_at: datetime,
    ) -> PortfolioGapReport:
        incremental = tuple(
            candidate.candidate_hash
            for candidate in candidates
            if candidate.selection_metrics is not None
            and candidate.selection_metrics.incrementality >= self.selection_policy.min_incrementality
            and candidate.selection_metrics.family_budget_remaining > 0
        )
        portfolio_id = campaign.subject.portfolio_id or "registered.portfolio.scope"
        return PortfolioGapReport(
            report_id=_artifact_id("portfolio_gap", campaign, runtime_manifest, generated_at),
            campaign_hash=campaign.artifact_hash,
            run_manifest_hash=runtime_manifest.artifact_hash,
            route_hash=runtime_manifest.route_hash,
            campaign_mode=campaign.mode,
            research_depth=campaign.research_depth,
            portfolio_id=portfolio_id,
            gap_summary_hash=canonical_hash(
                {
                    "campaign_hash": campaign.artifact_hash,
                    "portfolio_id": portfolio_id,
                    "candidate_hashes": _candidate_hashes(candidates),
                }
            ),
            candidate_hashes=_candidate_hashes(candidates),
            incremental_candidate_hashes=tuple(sorted(incremental)),
            generated_at=generated_at,
        )

    def _diagnostic(
        self,
        campaign: ResearchCampaign,
        runtime_manifest: CampaignRuntimeManifest,
        candidates: tuple[CandidateSelectionInput, ...],
        generated_at: datetime,
    ) -> DiagnosticReport:
        evidence_hashes = _official_evidence_hashes(candidates) or tuple(
            sorted(runtime_manifest.artifact_hashes.values())
        )[:3]
        return DiagnosticReport(
            report_id=_artifact_id("diagnostic", campaign, runtime_manifest, generated_at),
            campaign_hash=campaign.artifact_hash,
            run_manifest_hash=runtime_manifest.artifact_hash,
            route_hash=runtime_manifest.route_hash,
            campaign_mode=campaign.mode,
            research_depth=campaign.research_depth,
            root_cause_codes=("ATTRIBUTION_REVIEW_REQUIRED", "REGIME_AND_COST_SPLIT_REQUIRED"),
            evidence_hashes=evidence_hashes,
            recommendation="Return a ranked diagnosis and human review actions; do not promote or trade.",
            generated_at=generated_at,
        )

    def _event_study(
        self,
        campaign: ResearchCampaign,
        runtime_manifest: CampaignRuntimeManifest,
        generated_at: datetime,
    ) -> EventStudyReport:
        return EventStudyReport(
            report_id=_artifact_id("event_study", campaign, runtime_manifest, generated_at),
            campaign_hash=campaign.artifact_hash,
            run_manifest_hash=runtime_manifest.artifact_hash,
            route_hash=runtime_manifest.route_hash,
            campaign_mode=campaign.mode,
            research_depth=campaign.research_depth,
            event_definition_hash=canonical_hash({"question": campaign.question, "subject": campaign.subject.artifact_hash}),
            conditional_finding_hashes=tuple(sorted(runtime_manifest.artifact_hashes.values()))[:3],
            generated_at=generated_at,
        )

    def _dataset_assessment(
        self,
        campaign: ResearchCampaign,
        runtime_manifest: CampaignRuntimeManifest,
        generated_at: datetime,
    ) -> DatasetAssessment:
        return DatasetAssessment(
            assessment_id=_artifact_id("dataset_assessment", campaign, runtime_manifest, generated_at),
            campaign_hash=campaign.artifact_hash,
            run_manifest_hash=runtime_manifest.artifact_hash,
            route_hash=runtime_manifest.route_hash,
            campaign_mode=campaign.mode,
            research_depth=campaign.research_depth,
            dataset_hash=campaign.data_manifest_hash,
            pit_assessment_hash=canonical_hash({"runtime": runtime_manifest.artifact_hash, "stage": "availability_pit"}),
            coverage_bias_hash=canonical_hash({"runtime": runtime_manifest.artifact_hash, "stage": "coverage_and_bias"}),
            generated_at=generated_at,
        )

    def _replication(
        self,
        campaign: ResearchCampaign,
        runtime_manifest: CampaignRuntimeManifest,
        generated_at: datetime,
    ) -> ReplicationReport:
        return ReplicationReport(
            report_id=_artifact_id("replication", campaign, runtime_manifest, generated_at),
            campaign_hash=campaign.artifact_hash,
            run_manifest_hash=runtime_manifest.artifact_hash,
            route_hash=runtime_manifest.route_hash,
            campaign_mode=campaign.mode,
            research_depth=campaign.research_depth,
            source_document_hash=canonical_hash({"question": campaign.question, "mode": campaign.mode.value}),
            replication_status=ReplicationStatus.APPROXIMATE,
            discrepancy_hashes=tuple(sorted(runtime_manifest.artifact_hashes.values()))[:3],
            generated_at=generated_at,
        )

    def _prelaunch(
        self,
        campaign: ResearchCampaign,
        runtime_manifest: CampaignRuntimeManifest,
        candidates: tuple[CandidateSelectionInput, ...],
        generated_at: datetime,
    ) -> PrelaunchReview:
        all_eligible = all(_eligible(candidate, self.selection_policy) for candidate in candidates) if candidates else False
        return PrelaunchReview(
            review_id=_artifact_id("prelaunch", campaign, runtime_manifest, generated_at),
            campaign_hash=campaign.artifact_hash,
            run_manifest_hash=runtime_manifest.artifact_hash,
            route_hash=runtime_manifest.route_hash,
            campaign_mode=campaign.mode,
            research_depth=campaign.research_depth,
            review_status=ReviewStatus.PASS if all_eligible else ReviewStatus.REVISE,
            cost_capacity_hash=canonical_hash({"candidate_hashes": _candidate_hashes(candidates), "aspect": "cost_capacity"}),
            robustness_hash=canonical_hash({"candidate_hashes": _candidate_hashes(candidates), "aspect": "robustness"}),
            unresolved_risk_codes=() if all_eligible else ("PRELAUNCH_REVIEW_NOT_FULLY_SATISFIED",),
            generated_at=generated_at,
        )

    def _monitoring(
        self,
        campaign: ResearchCampaign,
        runtime_manifest: CampaignRuntimeManifest,
        generated_at: datetime,
    ) -> MonitoringRecommendation:
        return MonitoringRecommendation(
            recommendation_id=_artifact_id("monitoring", campaign, runtime_manifest, generated_at),
            campaign_hash=campaign.artifact_hash,
            run_manifest_hash=runtime_manifest.artifact_hash,
            route_hash=runtime_manifest.route_hash,
            campaign_mode=campaign.mode,
            research_depth=campaign.research_depth,
            action=MonitoringAction.KEEP,
            baseline_hash=canonical_hash({"campaign_hash": campaign.artifact_hash, "baseline": "registered_visible"}),
            drift_evidence_hashes=tuple(sorted(runtime_manifest.artifact_hashes.values()))[:3],
            human_decision_required=True,
            generated_at=generated_at,
        )

    def _select(
        self,
        candidates: tuple[CandidateSelectionInput, ...],
    ) -> tuple[tuple[CandidateSelectionRecord, ...], tuple[CandidateSelectionRecord, ...]]:
        eligible_records: list[CandidateSelectionRecord] = []
        rejected_records: list[CandidateSelectionRecord] = []
        for candidate in candidates:
            reasons = _eligibility_failures(candidate, self.selection_policy)
            if reasons:
                rejected_records.append(
                    CandidateSelectionRecord(
                        candidate_id=candidate.candidate_id,
                        candidate_hash=candidate.candidate_hash,
                        family_id=candidate.family_id,
                        verdict=CandidateSelectionVerdict.NOT_ELIGIBLE,
                        score=None,
                        reason_codes=reasons,
                        official_evidence_hash=candidate.official_evidence_hash,
                    )
                )
                continue
            assert candidate.selection_metrics is not None
            eligible_records.append(
                CandidateSelectionRecord(
                    candidate_id=candidate.candidate_id,
                    candidate_hash=candidate.candidate_hash,
                    family_id=candidate.family_id,
                    verdict=CandidateSelectionVerdict.SELECTED_FOR_PROMOTION_REVIEW,
                    score=self.selection_policy.score(candidate.selection_metrics),
                    reason_codes=("PIT_EVIDENCE_CRITIC_VISIBLE_SELECTION_PASS",),
                    official_evidence_hash=candidate.official_evidence_hash,
                )
            )
        eligible_records.sort(key=lambda item: (-(item.score or 0.0), item.candidate_id))
        selected = tuple(eligible_records[: self.selection_policy.max_selected])
        overflow = tuple(
            CandidateSelectionRecord(
                candidate_id=item.candidate_id,
                candidate_hash=item.candidate_hash,
                family_id=item.family_id,
                verdict=CandidateSelectionVerdict.REVISE,
                score=item.score,
                reason_codes=("CANDIDATE_SELECTION_BUDGET_LIMIT",),
                official_evidence_hash=item.official_evidence_hash,
            )
            for item in eligible_records[self.selection_policy.max_selected :]
        )
        rejected_records.sort(key=lambda item: item.candidate_id)
        return selected, tuple(rejected_records) + overflow


def _eligible(candidate: CandidateSelectionInput, policy: CandidateSelectionPolicy) -> bool:
    return not _eligibility_failures(candidate, policy)


def _eligibility_failures(
    candidate: CandidateSelectionInput,
    policy: CandidateSelectionPolicy,
) -> tuple[str, ...]:
    reasons: list[str] = []
    if candidate.selection_basis is not CandidateSelectionBasis.DETERMINISTIC_VISIBLE_EVIDENCE:
        reasons.append("SELECTION_BASIS_NOT_DETERMINISTIC_VISIBLE_EVIDENCE")
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


def _reject_quick_official_evidence(candidates: tuple[CandidateSelectionInput, ...]) -> None:
    for candidate in candidates:
        if candidate.pit_validated or candidate.official_evidence_hash is not None or candidate.critic_passed:
            fail(FailureCode.UNAUTHORIZED_CAPABILITY, "QUICK_EXPLORATION cannot enter official Evidence or Promotion")


def _validate_runtime_binding(campaign: ResearchCampaign, runtime_manifest: CampaignRuntimeManifest) -> None:
    if not isinstance(runtime_manifest, CampaignRuntimeManifest):
        fail(FailureCode.INVALID_VALUE, "runtime_manifest must be CampaignRuntimeManifest")
    if runtime_manifest.campaign_hash != campaign.artifact_hash:
        fail(FailureCode.INVALID_HASH, "runtime manifest does not bind Campaign hash")
    if runtime_manifest.status not in {
        CampaignRuntimeStatus.COMPLETED,
        CampaignRuntimeStatus.PARTIAL,
        CampaignRuntimeStatus.AWAITING_CHECKPOINT,
        CampaignRuntimeStatus.BLOCKED,
    }:
        fail(FailureCode.INVALID_STATE, "runtime manifest is not aggregatable")


def _artifact_id(prefix: str, campaign: ResearchCampaign, runtime_manifest: CampaignRuntimeManifest, generated_at: datetime) -> str:
    material = {
        "prefix": prefix,
        "campaign_hash": campaign.artifact_hash,
        "run_manifest_hash": runtime_manifest.artifact_hash,
        "mode": campaign.mode.value,
        "generated_at": generated_at,
    }
    return prefix + "." + canonical_hash(material).removeprefix("sha256:")[:24]


def _candidate_hashes(candidates: tuple[CandidateSelectionInput, ...]) -> tuple[str, ...]:
    return tuple(sorted(candidate.candidate_hash for candidate in candidates))


def _official_evidence_hashes(candidates: tuple[CandidateSelectionInput, ...]) -> tuple[str, ...]:
    return tuple(sorted(candidate.official_evidence_hash for candidate in candidates if candidate.official_evidence_hash))


def _validate_common_artifact(
    *,
    artifact_id: str,
    campaign_hash: str,
    run_manifest_hash: str,
    route_hash: str,
    campaign_mode: ResearchMode,
    research_depth: ResearchDepth,
    generated_at: datetime,
    produced_by: Component,
    expected_mode: ResearchMode,
) -> None:
    require_identifier(artifact_id, "artifact_id")
    require_hash(campaign_hash, "campaign_hash")
    require_hash(run_manifest_hash, "run_manifest_hash")
    require_hash(route_hash, "route_hash")
    campaign_mode = _coerce_enum(campaign_mode, ResearchMode)
    research_depth = _coerce_enum(research_depth, ResearchDepth)
    if campaign_mode is not expected_mode:
        fail(FailureCode.INVALID_VALUE, f"artifact mode must be {expected_mode.value}")
    if not isinstance(produced_by, Component):
        produced_by = Component(produced_by)
    if produced_by is not Component.CRITIC_SERVICE:
        fail(FailureCode.INVALID_ARTIFACT_PRODUCER, "Campaign decision artifacts belong to deterministic aggregation")
    require_aware_datetime(generated_at, "generated_at")


def _validate_record_tuple(value: tuple[CandidateSelectionRecord, ...], field_name: str) -> None:
    if not isinstance(value, tuple):
        fail(FailureCode.INVALID_VALUE, f"{field_name} must be a tuple")
    if not all(isinstance(item, CandidateSelectionRecord) for item in value):
        fail(FailureCode.INVALID_VALUE, f"{field_name} must contain CandidateSelectionRecord")
    ids = tuple(item.candidate_id for item in value)
    if len(set(ids)) != len(ids):
        fail(FailureCode.INVALID_VALUE, f"{field_name} contains duplicate candidates")


def _validate_hash_tuple(value: tuple[str, ...], field_name: str, *, nonempty: bool = True) -> None:
    if not isinstance(value, tuple) or (nonempty and not value):
        fail(FailureCode.INVALID_VALUE, f"{field_name} must be a tuple")
    for item in value:
        require_hash(item, field_name)
    if len(set(value)) != len(value):
        fail(FailureCode.INVALID_VALUE, f"{field_name} contains duplicates")


def _validate_string_tuple(value: tuple[str, ...], field_name: str, *, nonempty: bool = True) -> None:
    if not isinstance(value, tuple) or (nonempty and not value):
        fail(FailureCode.INVALID_VALUE, f"{field_name} must be a tuple")
    for item in value:
        require_identifier(item, field_name)
        _validate_report_text(item, field_name)
    if len(set(value)) != len(value):
        fail(FailureCode.INVALID_VALUE, f"{field_name} contains duplicates")


def _validate_report_text(value: str, field_name: str) -> None:
    require_text(value, field_name)
    lowered = value.lower()
    if any(token in lowered for token in _FORBIDDEN_REPORT_TEXT):
        fail(FailureCode.UNAUTHORIZED_CAPABILITY, f"{field_name} contains forbidden privileged text")


def _coerce_enum(value: object, enum_type: type[Enum]) -> Enum:
    if isinstance(value, enum_type):
        return value
    try:
        return enum_type(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        fail(FailureCode.INVALID_VALUE, f"invalid {enum_type.__name__}", value=value)


def _validate_artifact_kind(
    value: CampaignDecisionArtifactKind,
    expected: CampaignDecisionArtifactKind,
) -> CampaignDecisionArtifactKind:
    coerced = _coerce_enum(value, CampaignDecisionArtifactKind)
    if coerced is not expected:
        fail(FailureCode.INVALID_VALUE, f"artifact_kind must be {expected.value}")
    return coerced


__all__ = [
    "CAMPAIGN_AGGREGATION_RULE_VERSION",
    "CampaignDecisionArtifactKind",
    "CandidateDraftSource",
    "CandidateSelectionBasis",
    "CandidateSelectionVerdict",
    "ValidationConclusionVerdict",
    "ReplicationStatus",
    "ReviewStatus",
    "MonitoringAction",
    "SelectionMetricBundle",
    "CandidateSelectionPolicy",
    "CandidateSelectionInput",
    "CandidateSelectionRecord",
    "CandidateSet",
    "ValidationConclusion",
    "FactorComparisonReport",
    "PortfolioGapReport",
    "DiagnosticReport",
    "EventStudyReport",
    "DatasetAssessment",
    "ReplicationReport",
    "PrelaunchReview",
    "MonitoringRecommendation",
    "CampaignDecisionArtifact",
    "CampaignDecisionAggregator",
]
