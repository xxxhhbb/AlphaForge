"""AlphaForge Campaign API product facade."""

from .models import (
    CAMPAIGN_API_SCHEMA_VERSION,
    CampaignApiFailureCode,
    CampaignApiResponse,
    CampaignApiStatus,
    CampaignCatalogView,
    CampaignConfirmationRequest,
    CampaignDraftRequest,
    CampaignRunIntent,
)
from .service import CAMPAIGN_API_RULE_VERSION, CampaignApiService

__all__ = [
    "CAMPAIGN_API_RULE_VERSION",
    "CAMPAIGN_API_SCHEMA_VERSION",
    "CampaignApiFailureCode",
    "CampaignApiResponse",
    "CampaignApiService",
    "CampaignApiStatus",
    "CampaignCatalogView",
    "CampaignConfirmationRequest",
    "CampaignDraftRequest",
    "CampaignRunIntent",
]

