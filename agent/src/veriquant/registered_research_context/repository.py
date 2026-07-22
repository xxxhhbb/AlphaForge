"""Read-only, content-addressed, point-in-time repository."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .models import RegisteredResearchContext


@dataclass(frozen=True)
class RegisteredResearchContextRepository:
    _context: RegisteredResearchContext

    @property
    def artifact_hash(self) -> str:
        return self._context.artifact_hash

    def snapshot(self, *, decision_time: datetime) -> RegisteredResearchContext:
        if not isinstance(decision_time, datetime) or decision_time.tzinfo is None:
            raise ValueError("decision_time must be timezone-aware")
        factors = tuple(item for item in self._context.factors if item.available_at <= decision_time)
        keys = {(item.factor_id, item.version) for item in factors}
        return RegisteredResearchContext(
            factors=factors,
            score_history=tuple(item for item in self._context.score_history if item.available_at <= decision_time and (item.factor_id, item.version) in keys),
            visible_evidence=tuple(item for item in self._context.visible_evidence if item.available_at <= decision_time and (item.factor_id, item.version) in keys),
            correlations=tuple(item for item in self._context.correlations if item.available_at <= decision_time and (item.left_factor_id, item.left_version) in keys and (item.right_factor_id, item.right_version) in keys),
            exposures=tuple(item for item in self._context.exposures if item.available_at <= decision_time and (item.factor_id, item.version) in keys),
            cost_turnover=tuple(item for item in self._context.cost_turnover if item.available_at <= decision_time and (item.factor_id, item.version) in keys),
            portfolio_membership=tuple(item for item in self._context.portfolio_membership if item.available_at <= decision_time and (item.factor_id, item.factor_version) in keys),
            portfolio_returns=tuple(item for item in self._context.portfolio_returns if item.available_at <= decision_time),
        )

    def query_factor(self, factor_id: str, *, decision_time: datetime) -> RegisteredResearchContext:
        view = self.snapshot(decision_time=decision_time)
        target_versions = {(item.factor_id, item.version) for item in view.factors if item.factor_id == factor_id}
        correlations = tuple(
            item for item in view.correlations
            if (item.left_factor_id, item.left_version) in target_versions
            or (item.right_factor_id, item.right_version) in target_versions
        )
        companion_versions = target_versions | {
            key for item in correlations
            for key in ((item.left_factor_id, item.left_version), (item.right_factor_id, item.right_version))
        }
        memberships = tuple(
            item for item in view.portfolio_membership
            if (item.factor_id, item.factor_version) in target_versions
        )
        portfolio_ids = {(item.portfolio_id, item.portfolio_scope) for item in memberships}
        return RegisteredResearchContext(
            factors=tuple(item for item in view.factors if (item.factor_id, item.version) in companion_versions),
            score_history=tuple(item for item in view.score_history if (item.factor_id, item.version) in target_versions),
            visible_evidence=tuple(item for item in view.visible_evidence if (item.factor_id, item.version) in target_versions),
            correlations=correlations,
            exposures=tuple(item for item in view.exposures if (item.factor_id, item.version) in target_versions),
            cost_turnover=tuple(item for item in view.cost_turnover if (item.factor_id, item.version) in target_versions),
            portfolio_membership=memberships,
            portfolio_returns=tuple(item for item in view.portfolio_returns if (item.portfolio_id, item.portfolio_scope) in portfolio_ids),
        )


__all__ = ["RegisteredResearchContextRepository"]
