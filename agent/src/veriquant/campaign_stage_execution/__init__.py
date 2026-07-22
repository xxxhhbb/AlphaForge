"""True stage-by-stage execution boundary for Campaign automation."""

from .executor import DeterministicStageFixtureExecutor
from .models import (
    CAMPAIGN_STAGE_EXECUTION_SCHEMA_VERSION,
    CampaignStageExecutor,
    CampaignStageFailure,
    CampaignStageName,
    CampaignStageRequest,
    CampaignStageResult,
    CampaignStageStatus,
    ProviderMode,
    StageArtifactRef,
    StageExecutionPolicy,
    stage_key_for,
)

__all__ = [
    "CAMPAIGN_STAGE_EXECUTION_SCHEMA_VERSION",
    "CampaignStageExecutor",
    "CampaignStageFailure",
    "CampaignStageName",
    "CampaignStageRequest",
    "CampaignStageResult",
    "CampaignStageStatus",
    "DeterministicStageFixtureExecutor",
    "ProviderMode",
    "StageArtifactRef",
    "StageExecutionPolicy",
    "stage_key_for",
]
