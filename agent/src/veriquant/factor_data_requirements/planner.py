"""Data sizing derived from research semantics, never fixed stock/day caps."""

from __future__ import annotations

import math

from .models import DataAdequacyPlan, DataAdequacyRequest, stable_hash


class DataAdequacyPlanner:
    """Calculate the minimum requested scale from explicit research constraints."""

    def plan(self, request: DataAdequacyRequest) -> DataAdequacyPlan:
        if not isinstance(request, DataAdequacyRequest):
            raise TypeError("request must be DataAdequacyRequest")
        coverage = float(request.minimum_coverage)
        required_symbols = math.ceil(request.minimum_cross_section / coverage)
        fold_sessions = request.walk_forward_folds * (
            request.train_sessions_per_fold + request.validation_sessions_per_fold
        )
        power_sessions = math.ceil(
            request.minimum_effective_observations
            / (required_symbols * coverage)
        )
        analysis_sessions = max(
            fold_sessions,
            power_sessions,
            request.minimum_independent_sessions,
        )
        effective_warmup = max(request.maximum_lookback, request.warmup)
        label_safe_sessions = analysis_sessions + request.label_horizon
        required_sessions = effective_warmup + label_safe_sessions
        return DataAdequacyPlan(
            request_hash=stable_hash(request),
            effective_warmup_sessions=effective_warmup,
            analysis_sessions=analysis_sessions,
            required_sessions=required_sessions,
            required_symbols=required_symbols,
            required_rows=required_sessions * required_symbols,
            label_safe_sessions=label_safe_sessions,
            formula=(
                "symbols=ceil(min_cross_section/coverage); "
                "analysis=max(folds*(train+validation),ceil(power_observations/(symbols*coverage)),independent_sessions); "
                "sessions=max(lookback,warmup)+analysis+label_horizon"
            ),
        )

