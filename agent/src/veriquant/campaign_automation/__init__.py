"""Campaign automation orchestration for AlphaForge.

This product layer turns a confirmed research question into a routed Campaign
run and a redacted report. It does not own Evidence, Promotion, Risk,
Execution, or Ledger authority.
"""

from .models import (
    CAMPAIGN_AUTOMATION_RULE_VERSION,
    AutomationDataManifest,
    AutomationReport,
    AutomationRequest,
    AutomationResult,
    AutomationRuntimeMode,
    AutomationStatus,
)
from .service import CampaignAutomationService, build_automation_report_markdown

__all__ = [
    "CAMPAIGN_AUTOMATION_RULE_VERSION",
    "AutomationDataManifest",
    "AutomationReport",
    "AutomationRequest",
    "AutomationResult",
    "AutomationRuntimeMode",
    "AutomationStatus",
    "CampaignAutomationService",
    "build_automation_report_markdown",
]
