"""Versioned contracts for a deterministic visible confidence-interval gate."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import ClassVar

from ..contracts import CanonicalContract, Component, EvidenceClass
from ..contracts.base import require_finite_number, require_hash, require_identifier
from ..evidence.models import OfficialEvidence


SCHEMA_VERSION = "statistical-confidence-gate/v2"


class ConfidenceGateFailureCode(str, Enum):
    INVALID_CONTRACT = "INVALID_CONTRACT"
    INVALID_HASH_BINDING = "INVALID_HASH_BINDING"
    UNAUTHORIZED_PRODUCER = "UNAUTHORIZED_PRODUCER"
    AUXILIARY_EVIDENCE_REJECTED = "AUXILIARY_EVIDENCE_REJECTED"
    SEALED_EVIDENCE_REJECTED = "SEALED_EVIDENCE_REJECTED"
    UNSUPPORTED_STATISTIC = "UNSUPPORTED_STATISTIC"
    INSUFFICIENT_SAMPLE = "INSUFFICIENT_SAMPLE"
    DEGENERATE_VARIANCE = "DEGENERATE_VARIANCE"
    INTERVAL_CROSSES_THRESHOLD = "INTERVAL_CROSSES_THRESHOLD"
    INTERVAL_TOO_WIDE = "INTERVAL_TOO_WIDE"


class ConfidenceGateError(ValueError):
    def __init__(self, code: ConfidenceGateFailureCode, message: str) -> None:
        super().__init__(message)
        self.code = code


class ConfidenceEstimationMethod(str, Enum):
    NORMAL_APPROXIMATION_PERIOD_IC = "NORMAL_APPROXIMATION_PERIOD_IC"


class ConfidenceGateStatus(str, Enum):
    PASS = "PASS"
    REJECT = "REJECT"
    UNAVAILABLE = "UNAVAILABLE"


@dataclass(frozen=True, kw_only=True)
class ConfidenceIntervalPolicy(CanonicalContract):
    """Versioned visible-validation CI policy, not a promotion policy."""

    SCHEMA_VERSION: ClassVar[str] = SCHEMA_VERSION

    policy_id: str = "visible-period-ic-confidence"
    policy_version: str = "2.0.0"
    method: ConfidenceEstimationMethod = ConfidenceEstimationMethod.NORMAL_APPROXIMATION_PERIOD_IC
    confidence_level: float = 0.95
    min_sample_size: int = 30
    min_lower_bound: float = 0.0
    max_interval_width: float = 0.20

    def __post_init__(self) -> None:
        require_identifier(self.policy_id, "policy_id")
        require_identifier(self.policy_version, "policy_version")
        if self.method is not ConfidenceEstimationMethod.NORMAL_APPROXIMATION_PERIOD_IC:
            raise ConfidenceGateError(
                ConfidenceGateFailureCode.INVALID_CONTRACT,
                "unsupported confidence estimation method",
            )
        for name in ("confidence_level", "min_lower_bound", "max_interval_width"):
            require_finite_number(getattr(self, name), name)
        if not 0.80 <= self.confidence_level < 1.0:
            raise ConfidenceGateError(
                ConfidenceGateFailureCode.INVALID_CONTRACT,
                "confidence_level must be in [0.80, 1.0)",
            )
        if isinstance(self.min_sample_size, bool) or self.min_sample_size < 2:
            raise ConfidenceGateError(
                ConfidenceGateFailureCode.INVALID_CONTRACT,
                "min_sample_size must be at least two",
            )
        if self.max_interval_width <= 0.0:
            raise ConfidenceGateError(
                ConfidenceGateFailureCode.INVALID_CONTRACT,
                "max_interval_width must be positive",
            )


@dataclass(frozen=True, kw_only=True)
class OfficialConfidenceStatistic(CanonicalContract):
    """Hash-bound sufficient statistics projected from official validation evidence."""

    SCHEMA_VERSION: ClassVar[str] = SCHEMA_VERSION

    candidate_id: str
    identity_hash: str
    evidence_hash: str
    metric_name: str
    estimate: float
    sample_std: float
    sample_size: int
    split: str = "VALIDATION"
    produced_by: Component = Component.EVIDENCE_SERVICE
    evidence_class: EvidenceClass = EvidenceClass.OFFICIAL_EVIDENCE

    def __post_init__(self) -> None:
        require_identifier(self.candidate_id, "candidate_id")
        require_hash(self.identity_hash, "identity_hash")
        require_hash(self.evidence_hash, "evidence_hash")
        require_identifier(self.metric_name, "metric_name")
        require_finite_number(self.estimate, "estimate")
        require_finite_number(self.sample_std, "sample_std")
        if isinstance(self.sample_size, bool) or not isinstance(self.sample_size, int) or self.sample_size < 0:
            raise ConfidenceGateError(
                ConfidenceGateFailureCode.INVALID_CONTRACT,
                "sample_size must be a non-negative integer",
            )
        if self.split != "VALIDATION":
            raise ConfidenceGateError(
                ConfidenceGateFailureCode.SEALED_EVIDENCE_REJECTED,
                "confidence gate accepts only visible VALIDATION evidence",
            )
        if self.produced_by is not Component.EVIDENCE_SERVICE:
            raise ConfidenceGateError(
                ConfidenceGateFailureCode.UNAUTHORIZED_PRODUCER,
                "confidence statistics must come from Evidence Service",
            )
        if self.evidence_class is not EvidenceClass.OFFICIAL_EVIDENCE:
            raise ConfidenceGateError(
                ConfidenceGateFailureCode.AUXILIARY_EVIDENCE_REJECTED,
                "auxiliary evidence cannot enter the confidence gate",
            )
        if self.metric_name != "period_ic_mean":
            raise ConfidenceGateError(
                ConfidenceGateFailureCode.UNSUPPORTED_STATISTIC,
                "only period_ic_mean can be assessed; RankIC-only is insufficient",
            )

    @classmethod
    def from_official_evidence(
        cls,
        *,
        candidate_id: str,
        evidence: OfficialEvidence,
        expected_identity_hash: str,
    ) -> "OfficialConfidenceStatistic":
        if not isinstance(evidence, OfficialEvidence):
            raise ConfidenceGateError(
                ConfidenceGateFailureCode.AUXILIARY_EVIDENCE_REJECTED,
                "input must be an OfficialEvidence object",
            )
        if evidence.partition != "VALIDATION":
            raise ConfidenceGateError(
                ConfidenceGateFailureCode.SEALED_EVIDENCE_REJECTED,
                "sealed/train evidence cannot be projected into the visible CI gate",
            )
        require_hash(expected_identity_hash, "expected_identity_hash")
        if evidence.identity_hash != expected_identity_hash:
            raise ConfidenceGateError(
                ConfidenceGateFailureCode.INVALID_HASH_BINDING,
                "official evidence identity does not match the frozen identity",
            )
        return cls(
            candidate_id=candidate_id,
            identity_hash=evidence.identity_hash,
            evidence_hash=evidence.artifact_hash,
            metric_name="period_ic_mean",
            estimate=evidence.metrics.ic_mean,
            sample_std=evidence.metrics.ic_std,
            sample_size=evidence.metrics.period_count,
        )


@dataclass(frozen=True, kw_only=True)
class ConfidenceGateRequest(CanonicalContract):
    SCHEMA_VERSION: ClassVar[str] = SCHEMA_VERSION

    statistic: OfficialConfidenceStatistic
    expected_identity_hash: str
    expected_evidence_hash: str

    def __post_init__(self) -> None:
        if not isinstance(self.statistic, OfficialConfidenceStatistic):
            raise ConfidenceGateError(
                ConfidenceGateFailureCode.INVALID_CONTRACT,
                "statistic must be OfficialConfidenceStatistic",
            )
        require_hash(self.expected_identity_hash, "expected_identity_hash")
        require_hash(self.expected_evidence_hash, "expected_evidence_hash")


@dataclass(frozen=True, kw_only=True)
class ConfidenceGateReceipt(CanonicalContract):
    SCHEMA_VERSION: ClassVar[str] = SCHEMA_VERSION

    receipt_id: str
    candidate_id: str
    status: ConfidenceGateStatus
    failure_code: ConfidenceGateFailureCode | None
    confidence_level: float
    lower_bound: float | None
    upper_bound: float | None
    estimate: float
    sample_size: int
    identity_hash: str
    evidence_hash: str
    policy_hash: str
    promotion_precondition_satisfied: bool
    sealed_detail_disclosed: bool = False

    def __post_init__(self) -> None:
        require_identifier(self.receipt_id, "receipt_id")
        require_identifier(self.candidate_id, "candidate_id")
        require_finite_number(self.confidence_level, "confidence_level")
        require_finite_number(self.estimate, "estimate")
        for name in ("identity_hash", "evidence_hash", "policy_hash"):
            require_hash(getattr(self, name), name)
        if self.lower_bound is not None:
            require_finite_number(self.lower_bound, "lower_bound")
        if self.upper_bound is not None:
            require_finite_number(self.upper_bound, "upper_bound")
        if self.status is ConfidenceGateStatus.PASS:
            if self.failure_code is not None or not self.promotion_precondition_satisfied:
                raise ConfidenceGateError(ConfidenceGateFailureCode.INVALID_CONTRACT, "invalid passing receipt")
        elif self.promotion_precondition_satisfied:
            raise ConfidenceGateError(ConfidenceGateFailureCode.INVALID_CONTRACT, "non-pass cannot satisfy promotion")
        if self.sealed_detail_disclosed is not False:
            raise ConfidenceGateError(
                ConfidenceGateFailureCode.SEALED_EVIDENCE_REJECTED,
                "CI receipt cannot disclose sealed details",
            )

    @property
    def receipt_hash(self) -> str:
        return self.artifact_hash

