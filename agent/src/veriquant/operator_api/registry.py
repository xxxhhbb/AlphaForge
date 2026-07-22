"""Registered offline snapshot/profile registry for operator jobs."""

from __future__ import annotations

from types import MappingProxyType
from typing import Mapping

from veriquant.contracts import FailureCode

from .models import OperatorApiError, OperatorBudget, RegisteredProfile, RegisteredSnapshot


class OperatorRegistry:
    """Immutable allowlist of snapshots and profiles accepted by the API."""

    def __init__(
        self,
        *,
        snapshots: Mapping[str, RegisteredSnapshot],
        profiles: Mapping[str, RegisteredProfile],
    ) -> None:
        if not snapshots:
            raise OperatorApiError(FailureCode.INVALID_VALUE, "at least one registered snapshot is required")
        if not profiles:
            raise OperatorApiError(FailureCode.INVALID_VALUE, "at least one registered profile is required")
        self._snapshots = MappingProxyType(dict(snapshots))
        self._profiles = MappingProxyType(dict(profiles))

    @classmethod
    def fixture_default(cls) -> "OperatorRegistry":
        """Tiny deterministic fixture registry for offline smoke tests."""

        return cls(
            snapshots={
                "fixture-snapshot-a": RegisteredSnapshot(
                    snapshot_id="fixture-snapshot-a",
                    content_hash="sha256:" + ("1" * 64),
                    description="offline frozen test snapshot",
                )
            },
            profiles={
                "offline-smoke": RegisteredProfile(
                    profile_id="offline-smoke",
                    max_budget=OperatorBudget(max_steps=10, max_events=100, max_wall_clock_seconds=60),
                    description="loopback deterministic smoke profile",
                )
            },
        )

    @property
    def snapshots(self) -> Mapping[str, RegisteredSnapshot]:
        return self._snapshots

    @property
    def profiles(self) -> Mapping[str, RegisteredProfile]:
        return self._profiles

    def require_snapshot(self, snapshot_id: str) -> RegisteredSnapshot:
        try:
            return self._snapshots[snapshot_id]
        except KeyError as exc:
            raise OperatorApiError(FailureCode.INVALID_IDENTIFIER, "snapshot_id is not registered") from exc

    def require_profile(self, profile_id: str) -> RegisteredProfile:
        try:
            return self._profiles[profile_id]
        except KeyError as exc:
            raise OperatorApiError(FailureCode.INVALID_IDENTIFIER, "profile_id is not registered") from exc

    def require_budget_within_profile(self, *, budget: OperatorBudget, profile: RegisteredProfile) -> None:
        if budget.max_steps > profile.max_budget.max_steps:
            raise OperatorApiError(FailureCode.PROVIDER_BUDGET_EXCEEDED, "budget.max_steps exceeds registered profile")
        if budget.max_events > profile.max_budget.max_events:
            raise OperatorApiError(FailureCode.PROVIDER_BUDGET_EXCEEDED, "budget.max_events exceeds registered profile")
        if budget.max_wall_clock_seconds > profile.max_budget.max_wall_clock_seconds:
            raise OperatorApiError(
                FailureCode.PROVIDER_BUDGET_EXCEEDED,
                "budget.max_wall_clock_seconds exceeds registered profile",
            )

