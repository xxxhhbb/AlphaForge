"""Approved proxy to frozen A-share research-scale input adapter."""

from .adapter import APPROVED_PROVIDER_CLASS, APPROVED_SOURCE_CLASS, ApprovedProxyScaleAdapterV2
from .client import API_FIELD_ALLOWLIST, AllowlistedProxyScaleClient, ProxyScaleQueryClient
from .models import (
    ProxyScaleAcquisitionRequest,
    ProxyScaleAdapterResult,
    ProxyScaleAdapterStatus,
    ProxyScaleFailureCode,
)

__all__ = [
    "APPROVED_PROVIDER_CLASS",
    "APPROVED_SOURCE_CLASS",
    "API_FIELD_ALLOWLIST",
    "AllowlistedProxyScaleClient",
    "ApprovedProxyScaleAdapterV2",
    "ProxyScaleAcquisitionRequest",
    "ProxyScaleAdapterResult",
    "ProxyScaleAdapterStatus",
    "ProxyScaleFailureCode",
    "ProxyScaleQueryClient",
]
