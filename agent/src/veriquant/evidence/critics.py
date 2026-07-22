"""Mechanical evidence critics and non-LLM Evidence Court."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from ..contracts import ContractValidationError, FailureCode
from ..data import canonical_content_hash
from .models import (
    CourtOutcome,
    MechanicalDecision,
    MetricSet,
    OfficialEvidence,
)


class CriticVerdict(str, Enum):
    PASS = "PASS"
    REVISE = "REVISE"
    REJECT = "REJECT"


@dataclass(frozen=True, kw_only=True)
class CriticFinding:
    critic: str
    verdict: CriticVerdict
    code: str
    message: str


@dataclass(frozen=True, kw_only=True)
class EvidenceCourtPolicy:
    min_observations: int = 20
    min_rank_ic: float = 0.0
    min_icir: float = -1.0
    max_turnover: float = 1.0
    max_drawdown: float = 0.5
    min_net_return: float = 0.0
    max_registered_abs_correlation: float = 0.95
    min_incremental_ic: float = -0.05


class PITCritic:
    name = "PITCritic"

    def review(self, evidence: OfficialEvidence, *, pit_valid: bool) -> CriticFinding:
        if not pit_valid:
            return CriticFinding(
                critic=self.name,
                verdict=CriticVerdict.REJECT,
                code="PIT_INVALID",
                message="frozen inputs did not pass point-in-time validation",
            )
        return CriticFinding(
            critic=self.name,
            verdict=CriticVerdict.PASS,
            code="PIT_VALID",
            message="point-in-time validation passed",
        )


class StatisticalCritic:
    name = "StatisticalCritic"

    def review(self, metrics: MetricSet, policy: EvidenceCourtPolicy) -> CriticFinding:
        if metrics.observation_count < policy.min_observations:
            return CriticFinding(
                critic=self.name,
                verdict=CriticVerdict.REJECT,
                code="INSUFFICIENT_OBSERVATIONS",
                message="official evidence has too few observations",
            )
        if metrics.rank_ic < policy.min_rank_ic or metrics.icir < policy.min_icir:
            return CriticFinding(
                critic=self.name,
                verdict=CriticVerdict.REVISE,
                code="WEAK_STATISTICAL_EVIDENCE",
                message="rank IC or ICIR misses the mechanical threshold",
            )
        if metrics.subperiod_positive_fraction < 0.5 or metrics.regime_positive_fraction < 0.5:
            return CriticFinding(
                critic=self.name,
                verdict=CriticVerdict.REVISE,
                code="UNSTABLE_EVIDENCE",
                message="subperiod or market-regime stability is insufficient",
            )
        return CriticFinding(
            critic=self.name,
            verdict=CriticVerdict.PASS,
            code="STATISTICS_PASS",
            message="statistical thresholds passed",
        )


class CostCapacityCritic:
    name = "CostCapacityCritic"

    def review(self, metrics: MetricSet, policy: EvidenceCourtPolicy) -> CriticFinding:
        if metrics.gross_return > 0.0 and metrics.net_return < policy.min_net_return:
            return CriticFinding(
                critic=self.name,
                verdict=CriticVerdict.REJECT,
                code="COSTS_ERASE_ALPHA",
                message="gross return is positive but spread and impact make net return negative",
            )
        if metrics.turnover > policy.max_turnover or metrics.max_drawdown > policy.max_drawdown:
            return CriticFinding(
                critic=self.name,
                verdict=CriticVerdict.REVISE,
                code="CAPACITY_OR_DRAWDOWN",
                message="turnover or drawdown exceeds the policy",
            )
        if metrics.net_return < policy.min_net_return:
            return CriticFinding(
                critic=self.name,
                verdict=CriticVerdict.REJECT,
                code="NET_RETURN_FAIL",
                message="net return misses the mechanical threshold",
            )
        return CriticFinding(
            critic=self.name,
            verdict=CriticVerdict.PASS,
            code="COST_CAPACITY_PASS",
            message="net cost and capacity thresholds passed",
        )


class IncrementalityCritic:
    name = "IncrementalityCritic"

    def review(self, metrics: MetricSet, policy: EvidenceCourtPolicy) -> CriticFinding:
        if metrics.max_registered_abs_correlation > policy.max_registered_abs_correlation:
            return CriticFinding(
                critic=self.name,
                verdict=CriticVerdict.REVISE,
                code="REGISTERED_FACTOR_DUPLICATE",
                message="candidate is too correlated with a registered factor",
            )
        if metrics.incremental_ic_mean < policy.min_incremental_ic:
            return CriticFinding(
                critic=self.name,
                verdict=CriticVerdict.REVISE,
                code="NO_INCREMENTAL_EVIDENCE",
                message="residualized IC misses the incrementality threshold",
            )
        return CriticFinding(
            critic=self.name,
            verdict=CriticVerdict.PASS,
            code="INCREMENTALITY_PASS",
            message="correlation and residualized IC thresholds passed",
        )


class EvidenceCourt:
    """Fixed policy aggregation; no LLM or proposer judgment is accepted."""

    def __init__(self, policy: EvidenceCourtPolicy | None = None) -> None:
        self.policy = policy or EvidenceCourtPolicy()
        self.pit = PITCritic()
        self.statistical = StatisticalCritic()
        self.cost_capacity = CostCapacityCritic()
        self.incrementality = IncrementalityCritic()

    def findings(
        self, evidence: OfficialEvidence, *, pit_valid: bool
    ) -> tuple[CriticFinding, ...]:
        return (
            self.pit.review(evidence, pit_valid=pit_valid),
            self.statistical.review(evidence.metrics, self.policy),
            self.cost_capacity.review(evidence.metrics, self.policy),
            self.incrementality.review(evidence.metrics, self.policy),
        )

    def decide(
        self,
        *,
        evidence: OfficialEvidence,
        identity_hash: str,
        proposer_id: str,
        approver_id: str,
        decided_at: datetime,
        pit_valid: bool,
        sealed_passed: bool | None,
    ) -> MechanicalDecision:
        if proposer_id == approver_id:
            raise ContractValidationError(
                FailureCode.SELF_APPROVAL,
                "the same proposer cannot sit as Evidence Court approver",
            )
        findings = self.findings(evidence, pit_valid=pit_valid)
        if any(item.verdict is CriticVerdict.REJECT for item in findings):
            outcome = CourtOutcome.REJECT
        elif any(item.verdict is CriticVerdict.REVISE for item in findings):
            outcome = CourtOutcome.REVISE
        elif sealed_passed is None:
            outcome = CourtOutcome.VALIDATED_RESEARCH
        elif sealed_passed:
            outcome = CourtOutcome.PROMOTE_CHALLENGER
        else:
            outcome = CourtOutcome.REJECT
        reason_codes = tuple(item.code for item in findings)
        if sealed_passed is not None:
            reason_codes += ("SEALED_PASS" if sealed_passed else "SEALED_REJECT",)
        decision_material = {
            "identity_hash": identity_hash,
            "evidence_hash": evidence.artifact_hash,
            "proposer_id": proposer_id,
            "approver_id": approver_id,
            "outcome": outcome.value,
            "reason_codes": reason_codes,
            "decided_at": decided_at,
        }
        decision_hash = canonical_content_hash(decision_material)
        return MechanicalDecision(
            decision_id="decision-" + decision_hash.removeprefix("sha256:")[:24],
            identity_hash=identity_hash,
            evidence_hash=evidence.artifact_hash,
            proposer_id=proposer_id,
            approver_id=approver_id,
            outcome=outcome,
            reason_codes=reason_codes,
            decided_at=decided_at,
        )

