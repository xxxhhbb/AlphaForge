"""Campaign data binding boundary.

This package binds a confirmed ResearchCampaign to a frozen, research-visible
dataset bundle. It does not calculate PIT factors, official Evidence,
Promotion, Portfolio, Risk, Execution, or Ledger state.
"""

from .models import (
    CAMPAIGN_DATA_BINDING_SCHEMA_VERSION,
    CampaignDataBindingFailure,
    CampaignDataBindingRequest,
    CampaignDataBindingResult,
    CampaignDataBindingStatus,
    CampaignDatasetBundle,
    DataVendorClass,
)
from .service import CampaignDataBindingService, FakeCampaignDataBinder

__all__ = [
    "CAMPAIGN_DATA_BINDING_SCHEMA_VERSION",
    "CampaignDataBindingFailure",
    "CampaignDataBindingRequest",
    "CampaignDataBindingResult",
    "CampaignDataBindingService",
    "CampaignDataBindingStatus",
    "CampaignDatasetBundle",
    "DataVendorClass",
    "FakeCampaignDataBinder",
]
