"""Campaign automation integration contracts for AlphaForge."""

from .models import (
    CAMPAIGN_AUTOMATION_SCHEMA_VERSION,
    ArtifactRef,
    AutomationFailureCode,
    AutomationProviderMode,
    AutomationRunStatus,
    AutomationStageStatus,
    CampaignAutomationRequest,
    CampaignAutomationSnapshot,
    CampaignDataBindingRef,
    CampaignPublicationManifest,
    CampaignStageCommit,
    DataVendorKind,
    freeze_automation_payload,
    require_safe_artifact_locator,
)
from .protocols import CampaignDataBinder, CampaignReportPublisher, CampaignRunStore, CampaignStageRunner

__all__ = [
    "CAMPAIGN_AUTOMATION_SCHEMA_VERSION",
    "ArtifactRef",
    "AutomationFailureCode",
    "AutomationProviderMode",
    "AutomationRunStatus",
    "AutomationStageStatus",
    "CampaignAutomationRequest",
    "CampaignAutomationSnapshot",
    "CampaignDataBinder",
    "CampaignDataBindingRef",
    "CampaignPublicationManifest",
    "CampaignReportPublisher",
    "CampaignRunStore",
    "CampaignStageCommit",
    "CampaignStageRunner",
    "DataVendorKind",
    "freeze_automation_payload",
    "require_safe_artifact_locator",
]

