"""Closed provider/profile registry for offline jobs."""

from __future__ import annotations

from types import MappingProxyType
from typing import Mapping, Protocol, runtime_checkable

from .models import OperatorError, OperatorFailureCode, ProviderProfile, StageResult


@runtime_checkable
class OfflineStageProvider(Protocol):
    def execute_stage(
        self,
        *,
        job_id: str,
        stage_id: str,
        seed: int,
        budget: int,
        committed_artifacts: Mapping[str, str],
    ) -> StageResult: ...


class OperatorRegistry:
    def __init__(self, *, profiles: Mapping[str, ProviderProfile], providers: Mapping[str, OfflineStageProvider]) -> None:
        self._profiles = MappingProxyType(dict(profiles))
        self._providers = MappingProxyType(dict(providers))
        for profile_id, profile in self._profiles.items():
            if profile_id != profile.profile_id or not profile.stage_ids:
                raise OperatorError(OperatorFailureCode.INVALID_REQUEST, "invalid operator profile")
            if not profile.offline_only or profile.requires_provider_key:
                raise OperatorError(OperatorFailureCode.INVALID_REQUEST, "operator profiles must be offline and key-free")
            if profile.provider_id not in self._providers:
                raise OperatorError(OperatorFailureCode.PROVIDER_UNAVAILABLE, "profile provider is unavailable")

    def profile(self, profile_id: str) -> ProviderProfile:
        try:
            return self._profiles[profile_id]
        except KeyError as exc:
            raise OperatorError(OperatorFailureCode.UNKNOWN_PROFILE, "unknown operator profile") from exc

    def provider(self, provider_id: str) -> OfflineStageProvider:
        try:
            return self._providers[provider_id]
        except KeyError as exc:
            raise OperatorError(OperatorFailureCode.PROVIDER_UNAVAILABLE, "registered provider unavailable") from exc

    def public_profiles(self) -> tuple[dict[str, object], ...]:
        return tuple(
            {
                "profile_id": item.profile_id,
                "max_budget": item.max_budget,
                "stage_count": len(item.stage_ids),
                "offline_only": item.offline_only,
                "requires_provider_key": item.requires_provider_key,
                "plan_hash": item.plan_hash,
            }
            for item in sorted(self._profiles.values(), key=lambda value: value.profile_id)
        )
