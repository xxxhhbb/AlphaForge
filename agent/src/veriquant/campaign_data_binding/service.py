"""Deterministic Campaign data binding services."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from ..contracts import FailureCode
from ..contracts.base import canonicalize
from .models import (
    CampaignDataBindingFailure,
    CampaignDataBindingRequest,
    CampaignDataBindingResult,
    CampaignDataBindingStatus,
    CampaignDatasetBundle,
)


class CampaignDataBindingService:
    """Bind a confirmed Campaign to a frozen dataset bundle."""

    def bind(self, request: CampaignDataBindingRequest) -> CampaignDataBindingResult:
        if not request.provider_available:
            return CampaignDataBindingResult(
                request_hash=request.artifact_hash,
                status=CampaignDataBindingStatus.UNAVAILABLE,
                failure=CampaignDataBindingFailure.DATA_PROVIDER_UNAVAILABLE,
                failure_code=FailureCode.ADAPTER_UNAVAILABLE,
                message="data provider unavailable",
            )
        campaign = request.campaign
        manifest_hash = _stable_hash(
            {
                "campaign_hash": campaign.artifact_hash,
                "vendor_class": request.vendor_class.value,
                "source_label": request.provider_source_label,
                "universe": campaign.subject.universe,
                "horizon": campaign.subject.horizon,
                "decision_time": campaign.subject.decision_time,
                "allowed_data": campaign.subject.allowed_data,
                "calendar_id": request.calendar_id,
                "content_refs": request.content_refs,
            }
        )
        bundle = CampaignDatasetBundle(
            bundle_id=_stable_id("dataset_bundle", campaign.artifact_hash, manifest_hash),
            campaign_hash=campaign.artifact_hash,
            vendor_class=request.vendor_class,
            provider_source_label=request.provider_source_label,
            official_tushare_source=False,
            universe=campaign.subject.universe,
            horizon=campaign.subject.horizon,
            decision_time=campaign.subject.decision_time,
            allowed_data=campaign.subject.allowed_data,
            calendar_id=request.calendar_id,
            manifest_hash=manifest_hash,
            research_visible_summary_hash=_stable_hash(
                {
                    "manifest_hash": manifest_hash,
                    "visibility": "RESEARCH_VISIBLE_SUMMARY_ONLY",
                }
            ),
            content_refs=request.content_refs,
        )
        return CampaignDataBindingResult(
            request_hash=request.artifact_hash,
            status=CampaignDataBindingStatus.BOUND,
            bundle=bundle,
            message="campaign data bound",
        )


class FakeCampaignDataBinder(CampaignDataBindingService):
    """Explicit deterministic test binder."""


def _stable_hash(value: Any) -> str:
    rendered = json.dumps(canonicalize(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(rendered.encode("utf-8")).hexdigest()


def _stable_id(prefix: str, *parts: Any) -> str:
    return f"{prefix}.{_stable_hash(parts).removeprefix('sha256:')[:16]}"


__all__ = ["CampaignDataBindingService", "FakeCampaignDataBinder"]
