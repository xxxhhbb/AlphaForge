"""Deterministic calibration of validated factor scores into expected returns."""

from __future__ import annotations

import math
import re

from ..contracts import AlphaForecast, Component, FailureCode, ResearchState
from ..contracts.base import fail
from ..data import canonical_content_hash
from .models import AlphaCalibrationRequest


_HORIZON_NUMBER = re.compile(r"(\d+)")


class DeterministicAlphaService:
    """The only Prompt 06 authority that can create calibrated expected returns."""

    service_id = "deterministic-alpha-service-v1"

    @staticmethod
    def _horizon_days(horizon: str) -> int:
        match = _HORIZON_NUMBER.search(horizon)
        if match is None:
            fail(FailureCode.INVALID_VALUE, "horizon must contain a positive day count")
        days = int(match.group(1))
        if days <= 0:
            fail(FailureCode.INVALID_VALUE, "horizon day count must be positive")
        return days

    def calibrate(self, request: AlphaCalibrationRequest) -> AlphaForecast:
        if not isinstance(request, AlphaCalibrationRequest):
            fail(FailureCode.INVALID_ALPHA_SOURCE, "typed AlphaCalibrationRequest is required")
        if request.state is not ResearchState.VALIDATED_RESEARCH:
            fail(FailureCode.INVALID_ALPHA_SOURCE, "candidate is not VALIDATED_RESEARCH")

        scores = [request.factor_scores[symbol] for symbol in request.universe]
        mean = sum(scores) / len(scores)
        variance = sum((value - mean) ** 2 for value in scores) / len(scores)
        std = math.sqrt(variance)
        standardized = [0.0 for _ in scores]
        if std > 0.0:
            standardized = [(value - mean) / std for value in scores]

        metrics = request.evidence.metrics
        correlation = min(1.0, max(0.0, abs(metrics.max_registered_abs_correlation)))
        sample_confidence = metrics.observation_count / (metrics.observation_count + 20.0)
        stability = max(
            0.0,
            min(1.0, 0.5 * (metrics.subperiod_positive_fraction + metrics.regime_positive_fraction)),
        )
        confidence = max(0.0, min(1.0, sample_confidence * stability * (1.0 - correlation)))
        horizon_days = self._horizon_days(request.horizon)
        decay = 0.5 ** (horizon_days / request.decay_half_life_days)
        strength = min(
            0.25,
            max(0.0, abs(metrics.incremental_ic_mean) + abs(metrics.net_return)),
        )
        direction = -1.0 if metrics.ic_mean < 0.0 else 1.0
        slope = direction * strength * confidence * decay
        expected_returns = {
            symbol: standardized[index] * slope
            for index, symbol in enumerate(request.universe)
        }
        forecast_hash = canonical_content_hash(
            {
                "service_id": self.service_id,
                "candidate_hash": request.candidate_hash,
                "evidence_hash": request.evidence.artifact_hash,
                "identity_hash": request.evidence.identity_hash,
                "horizon": request.horizon,
                "decision_time": request.decision_time,
                "decay_half_life_days": request.decay_half_life_days,
                "scores": request.factor_scores,
            }
        )
        return AlphaForecast(
            forecast_id="alpha-" + forecast_hash.removeprefix("sha256:")[:24],
            candidate_hash=request.candidate_hash,
            evidence_hash=request.evidence.artifact_hash,
            universe=request.universe,
            horizon=request.horizon,
            decision_time=request.decision_time,
            expected_returns=expected_returns,
            confidence=confidence,
            decay_half_life_days=request.decay_half_life_days,
            factor_correlation=correlation,
            evidence_identity_hash=request.evidence.identity_hash,
            produced_by=Component.ALPHA_SERVICE,
            state=ResearchState.CHALLENGER,
        )
