"""Versioned in-memory alpha registry with immutable trace events."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from ..contracts import AlphaForecast, CanonicalContract, Component, FailureCode, ResearchState
from ..contracts.base import (
    fail,
    require_aware_datetime,
    require_hash,
    require_identifier,
    require_time_order,
)


class AlphaRegistryAction(str, Enum):
    REGISTER = "REGISTER"
    RETIRE = "RETIRE"


@dataclass(frozen=True, kw_only=True)
class AlphaRegistryEvent(CanonicalContract):
    sequence: int
    alpha_id: str
    version: int
    action: AlphaRegistryAction
    forecast_hash: str
    evidence_hash: str
    identity_hash: str
    candidate_hash: str
    event_time: datetime
    previous_event_hash: str | None
    produced_by: Component = Component.ALPHA_SERVICE

    def __post_init__(self) -> None:
        if isinstance(self.sequence, bool) or not isinstance(self.sequence, int) or self.sequence < 1:
            fail(FailureCode.INVALID_VALUE, "registry sequence must be positive")
        require_identifier(self.alpha_id, "alpha_id")
        if isinstance(self.version, bool) or not isinstance(self.version, int) or self.version < 1:
            fail(FailureCode.INVALID_VALUE, "alpha version must be positive")
        if not isinstance(self.action, AlphaRegistryAction):
            fail(FailureCode.INVALID_VALUE, "unknown registry action")
        for name in ("forecast_hash", "evidence_hash", "identity_hash", "candidate_hash"):
            require_hash(getattr(self, name), name)
        require_aware_datetime(self.event_time, "event_time")
        if self.previous_event_hash is not None:
            require_hash(self.previous_event_hash, "previous_event_hash")
        if self.produced_by is not Component.ALPHA_SERVICE:
            fail(FailureCode.INVALID_ARTIFACT_PRODUCER, "Alpha Registry belongs to Alpha Service")


@dataclass(frozen=True, kw_only=True)
class AlphaRegistryRecord(CanonicalContract):
    alpha_id: str
    version: int
    status: ResearchState
    forecast_hash: str
    evidence_hash: str
    identity_hash: str
    candidate_hash: str
    registered_at: datetime
    registration_event_hash: str
    retired_at: datetime | None = None
    retirement_event_hash: str | None = None

    def __post_init__(self) -> None:
        require_identifier(self.alpha_id, "alpha_id")
        if isinstance(self.version, bool) or not isinstance(self.version, int) or self.version < 1:
            fail(FailureCode.INVALID_VALUE, "alpha version must be positive")
        if self.status not in {ResearchState.CHALLENGER, ResearchState.RETIRED}:
            fail(FailureCode.INVALID_STATE, "registry status must be CHALLENGER or RETIRED")
        for name in (
            "forecast_hash",
            "evidence_hash",
            "identity_hash",
            "candidate_hash",
            "registration_event_hash",
        ):
            require_hash(getattr(self, name), name)
        require_aware_datetime(self.registered_at, "registered_at")
        if self.status is ResearchState.CHALLENGER:
            if self.retired_at is not None or self.retirement_event_hash is not None:
                fail(FailureCode.INVALID_STATE, "challenger cannot carry retirement fields")
        else:
            if self.retired_at is None or self.retirement_event_hash is None:
                fail(FailureCode.INVALID_STATE, "retired alpha requires retirement provenance")
            require_time_order(("registered_at", self.registered_at), ("retired_at", self.retired_at))
            require_hash(self.retirement_event_hash, "retirement_event_hash")


class AlphaRegistry:
    """Thread-safe materialized registry; events preserve version/status provenance."""

    def __init__(self) -> None:
        self._records: dict[tuple[str, int], AlphaRegistryRecord] = {}
        self._by_forecast: dict[str, tuple[str, int]] = {}
        self._events: list[AlphaRegistryEvent] = []
        self._lock = threading.RLock()

    @property
    def events(self) -> tuple[AlphaRegistryEvent, ...]:
        with self._lock:
            return tuple(self._events)

    def history(self, alpha_id: str) -> tuple[AlphaRegistryRecord, ...]:
        require_identifier(alpha_id, "alpha_id")
        with self._lock:
            return tuple(
                self._records[key]
                for key in sorted(self._records)
                if key[0] == alpha_id
            )

    def register(
        self,
        *,
        alpha_id: str,
        forecast: AlphaForecast,
        identity_hash: str,
        registered_at: datetime,
    ) -> AlphaRegistryRecord:
        require_identifier(alpha_id, "alpha_id")
        require_hash(identity_hash, "identity_hash")
        require_aware_datetime(registered_at, "registered_at")
        if not isinstance(forecast, AlphaForecast):
            fail(FailureCode.INVALID_ALPHA_SOURCE, "registry requires AlphaForecast")
        if forecast.produced_by is not Component.ALPHA_SERVICE:
            fail(FailureCode.INVALID_ALPHA_SOURCE, "forecast was not calibrated by Alpha Service")
        if forecast.state is not ResearchState.CHALLENGER:
            fail(FailureCode.INVALID_STATE, "only challenger forecasts can be registered")
        if forecast.evidence_identity_hash is None:
            fail(
                FailureCode.INVALID_ALPHA_SOURCE,
                "registry requires forecast-bound evidence identity",
            )
        if forecast.evidence_identity_hash != identity_hash:
            fail(FailureCode.ALPHA_REGISTRY_CONFLICT, "forecast/registry identity mismatch")
        with self._lock:
            existing_key = self._by_forecast.get(forecast.artifact_hash)
            if existing_key is not None:
                if existing_key[0] != alpha_id:
                    fail(
                        FailureCode.ALPHA_REGISTRY_CONFLICT,
                        "same forecast hash cannot be registered under another alpha_id",
                    )
                existing = self._records[existing_key]
                if existing.identity_hash != identity_hash:
                    fail(FailureCode.ALPHA_REGISTRY_CONFLICT, "identity mismatch on idempotent register")
                return existing
            versions = [version for key_alpha, version in self._records if key_alpha == alpha_id]
            version = max(versions, default=0) + 1
            event = AlphaRegistryEvent(
                sequence=len(self._events) + 1,
                alpha_id=alpha_id,
                version=version,
                action=AlphaRegistryAction.REGISTER,
                forecast_hash=forecast.artifact_hash,
                evidence_hash=forecast.evidence_hash,
                identity_hash=identity_hash,
                candidate_hash=forecast.candidate_hash,
                event_time=registered_at,
                previous_event_hash=self._events[-1].artifact_hash if self._events else None,
            )
            record = AlphaRegistryRecord(
                alpha_id=alpha_id,
                version=version,
                status=ResearchState.CHALLENGER,
                forecast_hash=forecast.artifact_hash,
                evidence_hash=forecast.evidence_hash,
                identity_hash=identity_hash,
                candidate_hash=forecast.candidate_hash,
                registered_at=registered_at,
                registration_event_hash=event.artifact_hash,
            )
            self._events.append(event)
            self._records[(alpha_id, version)] = record
            self._by_forecast[forecast.artifact_hash] = (alpha_id, version)
            return record

    def retire(self, *, alpha_id: str, version: int, retired_at: datetime) -> AlphaRegistryRecord:
        require_identifier(alpha_id, "alpha_id")
        require_aware_datetime(retired_at, "retired_at")
        key = (alpha_id, version)
        with self._lock:
            record = self._records.get(key)
            if record is None:
                fail(FailureCode.INVALID_VALUE, "unknown alpha registry version")
            if record.status is ResearchState.RETIRED:
                return record
            if retired_at < record.registered_at:
                fail(FailureCode.INVALID_TIME_ORDER, "retired_at precedes registered_at")
            event = AlphaRegistryEvent(
                sequence=len(self._events) + 1,
                alpha_id=alpha_id,
                version=version,
                action=AlphaRegistryAction.RETIRE,
                forecast_hash=record.forecast_hash,
                evidence_hash=record.evidence_hash,
                identity_hash=record.identity_hash,
                candidate_hash=record.candidate_hash,
                event_time=retired_at,
                previous_event_hash=self._events[-1].artifact_hash if self._events else None,
            )
            retired = AlphaRegistryRecord(
                alpha_id=record.alpha_id,
                version=record.version,
                status=ResearchState.RETIRED,
                forecast_hash=record.forecast_hash,
                evidence_hash=record.evidence_hash,
                identity_hash=record.identity_hash,
                candidate_hash=record.candidate_hash,
                registered_at=record.registered_at,
                registration_event_hash=record.registration_event_hash,
                retired_at=retired_at,
                retirement_event_hash=event.artifact_hash,
            )
            self._events.append(event)
            self._records[key] = retired
            return retired
