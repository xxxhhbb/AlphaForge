"""Mechanical source qualification, separate from Evidence and Promotion."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import ClassVar

from ..contracts import CanonicalContract, FailureCode
from ..contracts.base import (
    fail,
    require_aware_datetime,
    require_hash,
    require_identifier,
    require_nonempty_tuple,
    require_text,
)


SCHEMA_VERSION = "veriquant.provider-provenance/v1"
_HOST = re.compile(r"^[a-z0-9.-]+$")
_ALLOWED_APIS = frozenset(
    {
        "daily",
        "adj_factor",
        "stock_basic",
        "index_weight",
        "trade_cal",
        "suspend_d",
        "stk_limit",
        "daily_basic",
        "income",
        "balancesheet",
        "cashflow",
        "fina_indicator",
        "forecast",
        "express",
        "moneyflow",
        "index_daily",
        "shibor",
    }
)


class ProviderSourceClass(str, Enum):
    OFFICIAL_TUSHARE = "OFFICIAL_TUSHARE"
    TUSHARE_COMPATIBLE_PROXY = "TUSHARE_COMPATIBLE_PROXY"
    UNKNOWN = "UNKNOWN"


class ProviderEligibility(str, Enum):
    ELIGIBLE_FOR_OFFICIAL_EVIDENCE = "ELIGIBLE_FOR_OFFICIAL_EVIDENCE"
    AUXILIARY_ONLY = "AUXILIARY_ONLY"
    REJECTED = "REJECTED"


@dataclass(frozen=True, kw_only=True)
class ProviderProvenanceManifest(CanonicalContract):
    SCHEMA_VERSION: ClassVar[str] = SCHEMA_VERSION

    provider_id: str
    source_class: ProviderSourceClass
    endpoint_host: str
    endpoint_path: str
    sdk_name: str
    sdk_version: str
    acquired_at: datetime
    api_names: tuple[str, ...]
    snapshot_hash: str
    capture_hash: str
    calendar_hash: str
    universe_hash: str
    upstream_attestation_hash: str | None = None

    def __post_init__(self) -> None:
        require_identifier(self.provider_id, "provider_id")
        if not isinstance(self.source_class, ProviderSourceClass):
            fail(FailureCode.INVALID_VALUE, "source_class is invalid")
        host = self.endpoint_host.strip().lower()
        path = self.endpoint_path.strip()
        if not _HOST.fullmatch(host) or ":" in host or "/" in host:
            fail(FailureCode.INVALID_VALUE, "endpoint_host is invalid")
        if not path.startswith("/") or "?" in path or "#" in path or "token=" in path.lower():
            fail(FailureCode.UNAUTHORIZED_CAPABILITY, "endpoint_path contains forbidden material")
        object.__setattr__(self, "endpoint_host", host)
        object.__setattr__(self, "endpoint_path", path)
        require_text(self.sdk_name, "sdk_name")
        require_text(self.sdk_version, "sdk_version")
        require_aware_datetime(self.acquired_at, "acquired_at")
        require_nonempty_tuple(self.api_names, "api_names")
        normalized = tuple(sorted(set(self.api_names)))
        if len(normalized) != len(self.api_names) or any(name not in _ALLOWED_APIS for name in normalized):
            fail(FailureCode.UNAUTHORIZED_CAPABILITY, "provider API set is invalid")
        object.__setattr__(self, "api_names", normalized)
        for name in ("snapshot_hash", "capture_hash", "calendar_hash", "universe_hash"):
            require_hash(getattr(self, name), name)
        if self.upstream_attestation_hash is not None:
            require_hash(self.upstream_attestation_hash, "upstream_attestation_hash")


@dataclass(frozen=True, kw_only=True)
class ProviderProvenancePolicy(CanonicalContract):
    SCHEMA_VERSION: ClassVar[str] = SCHEMA_VERSION

    policy_id: str = "provider-provenance-default"
    official_tushare_hosts: tuple[str, ...] = ("api.tushare.pro",)
    approved_proxy_hosts: tuple[str, ...] = ()
    approved_proxy_attestation_hashes: tuple[str, ...] = ()
    require_proxy_attestation: bool = True

    def __post_init__(self) -> None:
        require_identifier(self.policy_id, "policy_id")
        for field_name in ("official_tushare_hosts", "approved_proxy_hosts"):
            values = tuple(sorted(set(getattr(self, field_name))))
            if any(not _HOST.fullmatch(value) for value in values):
                fail(FailureCode.INVALID_VALUE, f"{field_name} contains invalid host")
            object.__setattr__(self, field_name, values)
        attestations = tuple(sorted(set(self.approved_proxy_attestation_hashes)))
        for value in attestations:
            require_hash(value, "approved_proxy_attestation_hashes")
        object.__setattr__(self, "approved_proxy_attestation_hashes", attestations)
        if type(self.require_proxy_attestation) is not bool:
            fail(FailureCode.INVALID_VALUE, "require_proxy_attestation must be boolean")


@dataclass(frozen=True, kw_only=True)
class ProviderProvenanceDecision(CanonicalContract):
    SCHEMA_VERSION: ClassVar[str] = SCHEMA_VERSION

    provider_manifest_hash: str
    policy_hash: str
    eligibility: ProviderEligibility
    official_evidence_eligible: bool
    promotion_eligible: bool
    official_v1_tushare_smoke: bool
    reasons: tuple[str, ...]

    def __post_init__(self) -> None:
        require_hash(self.provider_manifest_hash, "provider_manifest_hash")
        require_hash(self.policy_hash, "policy_hash")
        if not isinstance(self.eligibility, ProviderEligibility):
            fail(FailureCode.INVALID_VALUE, "eligibility is invalid")
        if not self.reasons:
            fail(FailureCode.MISSING_REQUIRED_FIELD, "decision reasons are required")
        for name in (
            "official_evidence_eligible",
            "promotion_eligible",
            "official_v1_tushare_smoke",
        ):
            if type(getattr(self, name)) is not bool:
                fail(FailureCode.INVALID_VALUE, f"{name} must be boolean")
        eligible = self.eligibility is ProviderEligibility.ELIGIBLE_FOR_OFFICIAL_EVIDENCE
        if any(
            (self.official_evidence_eligible, self.promotion_eligible, self.official_v1_tushare_smoke)
        ) != eligible:
            fail(FailureCode.INVALID_VALUE, "authority booleans conflict with eligibility")


def evaluate_provider_provenance(
    manifest: ProviderProvenanceManifest,
    policy: ProviderProvenancePolicy | None = None,
) -> ProviderProvenanceDecision:
    """Apply source policy without calculating metrics or promotion verdicts."""

    if not isinstance(manifest, ProviderProvenanceManifest):
        fail(FailureCode.INVALID_VALUE, "typed provider manifest is required")
    selected = policy or ProviderProvenancePolicy()
    if not isinstance(selected, ProviderProvenancePolicy):
        fail(FailureCode.INVALID_VALUE, "typed provider policy is required")
    eligible = False
    if manifest.source_class is ProviderSourceClass.OFFICIAL_TUSHARE:
        eligible = manifest.endpoint_host in selected.official_tushare_hosts
        reasons = (
            "official Tushare host is policy approved"
            if eligible
            else "official Tushare host is not policy approved",
        )
    elif manifest.source_class is ProviderSourceClass.TUSHARE_COMPATIBLE_PROXY:
        host_ok = manifest.endpoint_host in selected.approved_proxy_hosts
        attestation_ok = (
            not selected.require_proxy_attestation
            or (
                manifest.upstream_attestation_hash is not None
                and manifest.upstream_attestation_hash in selected.approved_proxy_attestation_hashes
            )
        )
        eligible = host_ok and attestation_ok
        reasons = (
            "proxy host and upstream attestation are policy approved",
        ) if eligible else (
            "compatible proxy remains auxiliary until host and upstream attestation are approved",
        )
    else:
        reasons = ("unknown provider source is rejected",)
    eligibility = (
        ProviderEligibility.ELIGIBLE_FOR_OFFICIAL_EVIDENCE
        if eligible
        else ProviderEligibility.REJECTED
        if manifest.source_class is ProviderSourceClass.UNKNOWN
        else ProviderEligibility.AUXILIARY_ONLY
    )
    return ProviderProvenanceDecision(
        provider_manifest_hash=manifest.artifact_hash,
        policy_hash=selected.artifact_hash,
        eligibility=eligibility,
        official_evidence_eligible=eligible,
        promotion_eligible=eligible,
        official_v1_tushare_smoke=eligible,
        reasons=reasons,
    )
