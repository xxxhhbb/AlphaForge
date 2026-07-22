"""Operator-approved proxy data-vendor policy for the VeriQuant V1 data gate.

This module does not rename a compatible proxy as official Tushare.  It records
the project operator's explicit source-risk acceptance and permits only frozen,
content-addressed inputs from the pinned HTTPS endpoint to enter the existing
deterministic Evidence Service.  Evidence, promotion, risk, order, and ledger
authority remain with their existing independent services.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from importlib import metadata
from pathlib import Path
from typing import ClassVar

from veriquant.contracts import CanonicalContract, FailureCode
from veriquant.contracts.base import (
    fail,
    require_aware_datetime,
    require_hash,
    require_identifier,
    require_nonempty_tuple,
    require_text,
)
from veriquant.data import canonical_content_hash
from veriquant.provider_provenance import (
    ProviderEligibility,
    ProviderProvenanceDecision,
    ProviderProvenanceManifest,
    ProviderProvenancePolicy,
    ProviderSourceClass,
    evaluate_provider_provenance,
)

from .v1_real_data_chain import PROXY_HOST, PROXY_PATH, RealAuxiliaryChainResult


SCHEMA_VERSION = "veriquant.approved-proxy-policy/v1"
POLICY_ID = "veriquant-v1-approved-proxy-20260718"
POLICY_VERSION = "1.0.0"
APPROVED_PROXY_POLICY_HASH = "sha256:96629003a1cddbd10db4b47e65054ff7e7da1a203a18085eb6c19dd3ab56f9ee"
APPROVED_API_NAMES = (
    "adj_factor",
    "daily",
    "index_weight",
    "stk_limit",
    "stock_basic",
    "suspend_d",
    "trade_cal",
)


class ApprovedProviderClass(str, Enum):
    APPROVED_PROXY_DATA_VENDOR = "APPROVED_PROXY_DATA_VENDOR"


@dataclass(frozen=True, kw_only=True)
class OperatorProxyApproval(CanonicalContract):
    """Versioned operator decision; it is policy, not source attestation."""

    SCHEMA_VERSION: ClassVar[str] = SCHEMA_VERSION

    policy_id: str
    policy_version: str
    approved_by_component: str
    provider_class: ApprovedProviderClass
    approval_basis: str
    endpoint_scheme: str
    endpoint_host: str
    endpoint_path: str
    sdk_name: str
    approved_api_names: tuple[str, ...]
    effective_at: datetime
    operator_accepts_non_official_source_risk: bool
    approved_for_official_evidence_inputs: bool
    official_tushare_claim_allowed: bool
    frozen_content_addressing_required: bool
    deterministic_evidence_required: bool
    promotion_authority_granted: bool
    risk_authority_granted: bool
    order_authority_granted: bool
    ledger_authority_granted: bool

    def __post_init__(self) -> None:
        require_identifier(self.policy_id, "policy_id")
        require_text(self.policy_version, "policy_version")
        if self.approved_by_component != "OPERATOR":
            fail(FailureCode.INVALID_ARTIFACT_PRODUCER, "proxy approval must be an operator decision")
        if self.provider_class is not ApprovedProviderClass.APPROVED_PROXY_DATA_VENDOR:
            fail(FailureCode.INVALID_VALUE, "provider class must remain APPROVED_PROXY_DATA_VENDOR")
        if self.approval_basis != "EXPLICIT_OPERATOR_RISK_ACCEPTANCE":
            fail(FailureCode.INVALID_VALUE, "approval basis is not recognized")
        if self.endpoint_scheme != "https":
            fail(FailureCode.UNAUTHORIZED_CAPABILITY, "approved proxy must use HTTPS")
        if self.endpoint_host != PROXY_HOST or self.endpoint_path != PROXY_PATH:
            fail(FailureCode.UNAUTHORIZED_CAPABILITY, "approval endpoint differs from the pinned proxy")
        if self.sdk_name != "tushare":
            fail(FailureCode.INVALID_VALUE, "approved proxy must use the reviewed tushare SDK adapter")
        require_nonempty_tuple(self.approved_api_names, "approved_api_names")
        normalized = tuple(sorted(set(self.approved_api_names)))
        if normalized != APPROVED_API_NAMES:
            fail(FailureCode.UNAUTHORIZED_CAPABILITY, "approved provider API set changed")
        object.__setattr__(self, "approved_api_names", normalized)
        require_aware_datetime(self.effective_at, "effective_at")
        for name in (
            "operator_accepts_non_official_source_risk",
            "approved_for_official_evidence_inputs",
            "official_tushare_claim_allowed",
            "frozen_content_addressing_required",
            "deterministic_evidence_required",
            "promotion_authority_granted",
            "risk_authority_granted",
            "order_authority_granted",
            "ledger_authority_granted",
        ):
            if type(getattr(self, name)) is not bool:
                fail(FailureCode.INVALID_VALUE, f"{name} must be boolean")
        if not self.operator_accepts_non_official_source_risk:
            fail(FailureCode.UNAUTHORIZED_CAPABILITY, "operator source-risk acceptance is required")
        if not self.approved_for_official_evidence_inputs:
            fail(FailureCode.INVALID_STATE, "approval does not permit deterministic evidence inputs")
        if self.official_tushare_claim_allowed:
            fail(FailureCode.UNAUTHORIZED_CAPABILITY, "compatible proxy cannot claim OFFICIAL_TUSHARE")
        if not self.frozen_content_addressing_required or not self.deterministic_evidence_required:
            fail(FailureCode.UNAUTHORIZED_CAPABILITY, "frozen deterministic evidence controls are mandatory")
        if any(
            (
                self.promotion_authority_granted,
                self.risk_authority_granted,
                self.order_authority_granted,
                self.ledger_authority_granted,
            )
        ):
            fail(FailureCode.UNAUTHORIZED_CAPABILITY, "data-vendor approval cannot grant financial authority")


@dataclass(frozen=True, kw_only=True)
class ApprovedProxyQualification(CanonicalContract):
    """Qualification of frozen inputs, not official evidence or promotion."""

    SCHEMA_VERSION: ClassVar[str] = SCHEMA_VERSION

    status: str
    provider_class: ApprovedProviderClass
    source_class: ProviderSourceClass
    provider_eligibility: ProviderEligibility
    approval_hash: str
    provider_manifest_hash: str
    provider_policy_hash: str
    provider_decision_hash: str
    auxiliary_result_hash: str
    snapshot_hash: str
    calendar_hash: str
    universe_hash: str
    eligible_for_official_evidence_inputs: bool
    official_tushare_source: bool
    upstream_attestation_present: bool
    operator_risk_accepted: bool
    official_evidence_run: bool
    promotion_run: bool
    challenger_created: bool

    def __post_init__(self) -> None:
        if self.status != "APPROVED_INPUT_SOURCE":
            fail(FailureCode.INVALID_STATE, "approved proxy qualification status is invalid")
        if self.provider_class is not ApprovedProviderClass.APPROVED_PROXY_DATA_VENDOR:
            fail(FailureCode.INVALID_VALUE, "provider class changed")
        if self.source_class is not ProviderSourceClass.TUSHARE_COMPATIBLE_PROXY:
            fail(FailureCode.INVALID_VALUE, "approved provider must remain a compatible proxy")
        if self.provider_eligibility is not ProviderEligibility.ELIGIBLE_FOR_OFFICIAL_EVIDENCE:
            fail(FailureCode.UNAUTHORIZED_CAPABILITY, "provider is not eligible for official evidence inputs")
        for name in (
            "approval_hash",
            "provider_manifest_hash",
            "provider_policy_hash",
            "provider_decision_hash",
            "auxiliary_result_hash",
            "snapshot_hash",
            "calendar_hash",
            "universe_hash",
        ):
            require_hash(getattr(self, name), name)
        if not self.eligible_for_official_evidence_inputs or not self.operator_risk_accepted:
            fail(FailureCode.UNAUTHORIZED_CAPABILITY, "operator-approved input eligibility is required")
        if self.official_tushare_source or self.upstream_attestation_present:
            fail(FailureCode.AUXILIARY_MASQUERADE, "proxy approval cannot masquerade as official or attested")
        if any((self.official_evidence_run, self.promotion_run, self.challenger_created)):
            fail(FailureCode.UNAUTHORIZED_CAPABILITY, "source qualification cannot perform financial authority actions")


def default_policy_path() -> Path:
    return (
        Path(__file__).resolve().parents[4]
        / ".autobuild/veriquant/integration/v1_real_data/approved_proxy_policy.json"
    )


def load_operator_proxy_approval(path: str | Path | None = None) -> OperatorProxyApproval:
    selected = default_policy_path() if path is None else Path(path)
    if selected.resolve() != default_policy_path().resolve():
        fail(FailureCode.UNAUTHORIZED_CAPABILITY, "operator policy path is not the pinned artifact")
    try:
        payload = json.loads(selected.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        fail(FailureCode.INCOMPLETE_RESULT, "operator proxy policy is unavailable or invalid", error=type(exc).__name__)
    approval = OperatorProxyApproval.from_payload(payload)
    if approval.artifact_hash != APPROVED_PROXY_POLICY_HASH:
        fail(FailureCode.INVALID_HASH, "operator proxy policy hash does not match the pinned approval")
    return approval


def _provider_manifest_from_auxiliary_result(
    result: RealAuxiliaryChainResult,
) -> ProviderProvenanceManifest:
    if not isinstance(result, RealAuxiliaryChainResult):
        fail(FailureCode.INVALID_VALUE, "typed real auxiliary result is required")
    if any((result.official_evidence_run, result.promotion_run, result.challenger_created)):
        fail(FailureCode.UNAUTHORIZED_CAPABILITY, "source qualification requires a pre-authority auxiliary run")
    combined_capture_hash = canonical_content_hash(
        {
            "bar_capture_hash": result.bar_capture_hash,
            "market_state_capture_hash": result.market_state_capture_hash,
        }
    )
    try:
        sdk_version = metadata.version("tushare")
    except metadata.PackageNotFoundError:
        fail(FailureCode.ADAPTER_UNAVAILABLE, "tushare SDK version is unavailable")
    manifest = ProviderProvenanceManifest(
        provider_id="tushare-compatible-proxy-sdk",
        source_class=ProviderSourceClass.TUSHARE_COMPATIBLE_PROXY,
        endpoint_host=PROXY_HOST,
        endpoint_path=PROXY_PATH,
        sdk_name="tushare",
        sdk_version=sdk_version,
        acquired_at=result.snapshot_fetched_at,
        api_names=APPROVED_API_NAMES,
        snapshot_hash=result.snapshot_hash,
        capture_hash=combined_capture_hash,
        calendar_hash=result.calendar_hash,
        universe_hash=result.universe_hash,
        upstream_attestation_hash=None,
    )
    if manifest.artifact_hash != result.provenance_manifest_hash:
        fail(FailureCode.INVALID_HASH, "reconstructed provider provenance differs from the frozen run")
    return manifest


def qualify_approved_proxy_inputs(
    *, result: RealAuxiliaryChainResult, approval: OperatorProxyApproval
) -> ApprovedProxyQualification:
    if not isinstance(approval, OperatorProxyApproval):
        fail(FailureCode.INVALID_VALUE, "typed operator approval is required")
    if approval.artifact_hash != APPROVED_PROXY_POLICY_HASH:
        fail(FailureCode.INVALID_HASH, "unrecognized operator approval artifact")
    manifest = _provider_manifest_from_auxiliary_result(result)
    policy = ProviderProvenancePolicy(
        policy_id=approval.policy_id,
        official_tushare_hosts=("api.tushare.pro",),
        approved_proxy_hosts=(approval.endpoint_host,),
        approved_proxy_attestation_hashes=(),
        require_proxy_attestation=False,
    )
    decision: ProviderProvenanceDecision = evaluate_provider_provenance(manifest, policy)
    if decision.eligibility is not ProviderEligibility.ELIGIBLE_FOR_OFFICIAL_EVIDENCE:
        fail(FailureCode.UNAUTHORIZED_CAPABILITY, "operator-approved proxy did not qualify")
    return ApprovedProxyQualification(
        status="APPROVED_INPUT_SOURCE",
        provider_class=ApprovedProviderClass.APPROVED_PROXY_DATA_VENDOR,
        source_class=manifest.source_class,
        provider_eligibility=decision.eligibility,
        approval_hash=approval.artifact_hash,
        provider_manifest_hash=manifest.artifact_hash,
        provider_policy_hash=policy.artifact_hash,
        provider_decision_hash=decision.artifact_hash,
        auxiliary_result_hash=result.artifact_hash,
        snapshot_hash=result.snapshot_hash,
        calendar_hash=result.calendar_hash,
        universe_hash=result.universe_hash,
        eligible_for_official_evidence_inputs=True,
        official_tushare_source=False,
        upstream_attestation_present=False,
        operator_risk_accepted=True,
        official_evidence_run=False,
        promotion_run=False,
        challenger_created=False,
    )


__all__ = [
    "APPROVED_PROXY_POLICY_HASH",
    "ApprovedProviderClass",
    "ApprovedProxyQualification",
    "OperatorProxyApproval",
    "default_policy_path",
    "load_operator_proxy_approval",
    "qualify_approved_proxy_inputs",
]
