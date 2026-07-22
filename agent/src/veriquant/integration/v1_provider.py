"""Authority-preserving adapters for frozen A-share research inputs.

The adapter deliberately separates integrity verification (Agent A) from bundle
materialization (the existing real-research bridge).  It never downloads data,
selects a fallback provider, or synthesizes missing market-state metadata.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Mapping, Protocol

from veriquant.contracts import ContractValidationError, FailureCode
from veriquant.contracts.base import require_hash, require_identifier, require_text
from veriquant.real_ashare_data.reader import FrozenSnapshotHandle
from veriquant.real_research.api import ResearchDatasetBundle


class FrozenBundleStatus(str, Enum):
    AVAILABLE = "AVAILABLE"
    UNAVAILABLE = "UNAVAILABLE"
    FAILED = "FAILED"


@dataclass(frozen=True, kw_only=True)
class FrozenBundleAvailability:
    status: FrozenBundleStatus
    snapshot_id: str
    failure_code: str | None
    message: str
    source: str = "tushare"

    def __post_init__(self) -> None:
        require_identifier(self.snapshot_id, "snapshot_id")
        require_text(self.message, "message")
        if self.source != "tushare":
            raise ContractValidationError(
                FailureCode.UNAUTHORIZED_CAPABILITY,
                "trusted frozen-bundle adapter accepts Tushare snapshots only",
            )
        if self.status is FrozenBundleStatus.AVAILABLE and self.failure_code is not None:
            raise ContractValidationError(FailureCode.INVALID_VALUE, "available bundle cannot carry failure_code")
        if self.status is not FrozenBundleStatus.AVAILABLE and not self.failure_code:
            raise ContractValidationError(FailureCode.MISSING_REQUIRED_FIELD, "unavailable bundle requires failure_code")


@dataclass(frozen=True, kw_only=True)
class FrozenBundleRegistration:
    snapshot_id: str
    manifest_path: str
    source: str = "tushare"

    def __post_init__(self) -> None:
        require_identifier(self.snapshot_id, "snapshot_id")
        require_text(self.manifest_path, "manifest_path")
        if self.source != "tushare":
            raise ContractValidationError(
                FailureCode.UNAUTHORIZED_CAPABILITY,
                "trusted registration cannot use a fallback provider",
            )


class VerifiedSnapshotReader(Protocol):
    def open(self, manifest_path: str | Path) -> FrozenSnapshotHandle: ...

    def read_snapshot(self, handle: FrozenSnapshotHandle): ...


class ResearchBundleMaterializer(Protocol):
    """Existing bridge port; implementations own no Evidence/Promotion authority."""

    def build_bundle(
        self,
        *,
        snapshot_id: str,
        handle: FrozenSnapshotHandle,
        reader: VerifiedSnapshotReader,
    ) -> ResearchDatasetBundle: ...


class FrozenAShareBundleProvider:
    """Agent-A reader to Agent-B ``FrozenSnapshotProvider`` adapter.

    Missing or unregistered Tushare snapshots return ``None`` as required by
    Agent B.  The typed reason remains available through ``availability``.
    """

    def __init__(
        self,
        *,
        reader: VerifiedSnapshotReader,
        materializer: ResearchBundleMaterializer,
        registrations: Mapping[str, FrozenBundleRegistration],
    ) -> None:
        if not isinstance(registrations, Mapping):
            raise ContractValidationError(FailureCode.INVALID_VALUE, "registrations must be a mapping")
        normalized: dict[str, FrozenBundleRegistration] = {}
        for snapshot_id, registration in registrations.items():
            require_identifier(snapshot_id, "registrations.snapshot_id")
            if not isinstance(registration, FrozenBundleRegistration) or registration.snapshot_id != snapshot_id:
                raise ContractValidationError(FailureCode.INVALID_VALUE, "invalid frozen-bundle registration")
            normalized[snapshot_id] = registration
        self._reader = reader
        self._materializer = materializer
        self._registrations = MappingProxyType(dict(sorted(normalized.items())))
        self._last: dict[str, FrozenBundleAvailability] = {}

    def availability(self, *, snapshot_id: str) -> FrozenBundleAvailability:
        require_identifier(snapshot_id, "snapshot_id")
        if snapshot_id in self._last:
            return self._last[snapshot_id]
        if snapshot_id not in self._registrations:
            return FrozenBundleAvailability(
                status=FrozenBundleStatus.UNAVAILABLE,
                snapshot_id=snapshot_id,
                failure_code=FailureCode.ADAPTER_UNAVAILABLE.value,
                message="No verified Tushare frozen snapshot is registered",
            )
        return FrozenBundleAvailability(
            status=FrozenBundleStatus.UNAVAILABLE,
            snapshot_id=snapshot_id,
            failure_code=FailureCode.INCOMPLETE_RESULT.value,
            message="Registered snapshot has not been materialized",
        )

    def load_bundle(self, *, snapshot_id: str) -> ResearchDatasetBundle | None:
        require_identifier(snapshot_id, "snapshot_id")
        registration = self._registrations.get(snapshot_id)
        if registration is None:
            self._last[snapshot_id] = self.availability(snapshot_id=snapshot_id)
            return None
        try:
            handle = self._reader.open(registration.manifest_path)
            self._reader.read_snapshot(handle)  # revalidate bytes immediately before bridge use
            bundle = self._materializer.build_bundle(
                snapshot_id=snapshot_id,
                handle=handle,
                reader=self._reader,
            )
            if not isinstance(bundle, ResearchDatasetBundle):
                raise ContractValidationError(FailureCode.INVALID_VALUE, "materializer returned invalid bundle")
            if bundle.bridge_run_manifest.snapshot_id != snapshot_id:
                raise ContractValidationError(FailureCode.INVALID_IDENTIFIER, "bundle snapshot_id mismatch")
            for bound_hash in (
                bundle.bridge_run_manifest.snapshot_hash,
                bundle.universe_manifest.source_snapshot_hash,
            ):
                require_hash(bound_hash, "bundle.source_snapshot_hash")
                if bound_hash != handle.snapshot_hash:
                    raise ContractValidationError(FailureCode.INVALID_HASH, "bundle is not bound to verified snapshot")
        except ContractValidationError as exc:
            self._last[snapshot_id] = FrozenBundleAvailability(
                status=FrozenBundleStatus.FAILED,
                snapshot_id=snapshot_id,
                failure_code=exc.code.value,
                message="Frozen Tushare bundle failed closed during verification",
            )
            return None
        except (OSError, ValueError) as exc:
            self._last[snapshot_id] = FrozenBundleAvailability(
                status=FrozenBundleStatus.UNAVAILABLE,
                snapshot_id=snapshot_id,
                failure_code=FailureCode.ADAPTER_UNAVAILABLE.value,
                message=f"Frozen Tushare snapshot is unavailable ({type(exc).__name__})",
            )
            return None
        self._last[snapshot_id] = FrozenBundleAvailability(
            status=FrozenBundleStatus.AVAILABLE,
            snapshot_id=snapshot_id,
            failure_code=None,
            message="Verified Tushare snapshot was materialized through the research bridge",
        )
        return bundle


class OptionalTencentBundleProvider(Protocol):
    """Optional rehearsal hook.  Its output must never satisfy Tushare Gate 2."""

    def load_auxiliary_bundle(self, *, snapshot_id: str) -> ResearchDatasetBundle | None: ...

