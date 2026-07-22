"""Deterministic confidence-interval precondition for statistical promotion."""

from __future__ import annotations

import math
import hashlib
import json
from statistics import NormalDist

from .contracts import (
    ConfidenceGateError,
    ConfidenceGateFailureCode,
    ConfidenceGateReceipt,
    ConfidenceGateRequest,
    ConfidenceGateStatus,
    ConfidenceIntervalPolicy,
)


class StatisticalConfidenceGateV2:
    """Assess visible period-IC uncertainty; never emits CHALLENGER."""

    def __init__(self, *, policy: ConfidenceIntervalPolicy | None = None) -> None:
        self.policy = policy or ConfidenceIntervalPolicy()

    def evaluate(self, request: ConfidenceGateRequest) -> ConfidenceGateReceipt:
        if not isinstance(request, ConfidenceGateRequest):
            raise ConfidenceGateError(
                ConfidenceGateFailureCode.INVALID_CONTRACT,
                "request must be ConfidenceGateRequest",
            )
        statistic = request.statistic
        if (
            statistic.identity_hash != request.expected_identity_hash
            or statistic.evidence_hash != request.expected_evidence_hash
        ):
            raise ConfidenceGateError(
                ConfidenceGateFailureCode.INVALID_HASH_BINDING,
                "CI request hashes do not match official evidence",
            )

        if statistic.sample_size < self.policy.min_sample_size:
            return self._receipt(
                request,
                status=ConfidenceGateStatus.UNAVAILABLE,
                failure=ConfidenceGateFailureCode.INSUFFICIENT_SAMPLE,
                lower=None,
                upper=None,
            )
        if statistic.sample_std <= 0.0:
            return self._receipt(
                request,
                status=ConfidenceGateStatus.UNAVAILABLE,
                failure=ConfidenceGateFailureCode.DEGENERATE_VARIANCE,
                lower=None,
                upper=None,
            )

        z_value = NormalDist().inv_cdf((1.0 + self.policy.confidence_level) / 2.0)
        margin = z_value * statistic.sample_std / math.sqrt(statistic.sample_size)
        lower = statistic.estimate - margin
        upper = statistic.estimate + margin
        if lower <= self.policy.min_lower_bound:
            return self._receipt(
                request,
                status=ConfidenceGateStatus.REJECT,
                failure=ConfidenceGateFailureCode.INTERVAL_CROSSES_THRESHOLD,
                lower=lower,
                upper=upper,
            )
        if upper - lower > self.policy.max_interval_width:
            return self._receipt(
                request,
                status=ConfidenceGateStatus.REJECT,
                failure=ConfidenceGateFailureCode.INTERVAL_TOO_WIDE,
                lower=lower,
                upper=upper,
            )
        return self._receipt(
            request,
            status=ConfidenceGateStatus.PASS,
            failure=None,
            lower=lower,
            upper=upper,
        )

    def _receipt(
        self,
        request: ConfidenceGateRequest,
        *,
        status: ConfidenceGateStatus,
        failure: ConfidenceGateFailureCode | None,
        lower: float | None,
        upper: float | None,
    ) -> ConfidenceGateReceipt:
        statistic = request.statistic
        seed = {
            "request_hash": request.artifact_hash,
            "policy_hash": self.policy.artifact_hash,
            "status": status.value,
            "failure": failure.value if failure else None,
            "lower": lower,
            "upper": upper,
        }
        receipt_seed = "sha256:" + hashlib.sha256(
            json.dumps(seed, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
        ).hexdigest()
        return ConfidenceGateReceipt(
            receipt_id="confidence-receipt-" + receipt_seed.removeprefix("sha256:")[:24],
            candidate_id=statistic.candidate_id,
            status=status,
            failure_code=failure,
            confidence_level=self.policy.confidence_level,
            lower_bound=lower,
            upper_bound=upper,
            estimate=statistic.estimate,
            sample_size=statistic.sample_size,
            identity_hash=statistic.identity_hash,
            evidence_hash=statistic.evidence_hash,
            policy_hash=self.policy.artifact_hash,
            promotion_precondition_satisfied=status is ConfidenceGateStatus.PASS,
        )
