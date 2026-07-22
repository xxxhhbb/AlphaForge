"""Protocol surfaces for Campaign automation packages."""

from __future__ import annotations

from typing import Protocol

from ..research_campaign import CampaignProtocolPlan, CampaignRoute, ResearchCampaign
from .models import (
    CampaignAutomationRequest,
    CampaignAutomationSnapshot,
    CampaignDataBindingRef,
    CampaignPublicationManifest,
    CampaignStageCommit,
)


class CampaignDataBinder(Protocol):
    def bind(self, request: CampaignAutomationRequest, campaign: ResearchCampaign) -> CampaignDataBindingRef: ...


class CampaignStageRunner(Protocol):
    def run_stage(
        self,
        request: CampaignAutomationRequest,
        campaign: ResearchCampaign,
        route: CampaignRoute,
        protocol_plan: CampaignProtocolPlan,
        stage_key: str,
    ) -> CampaignStageCommit: ...


class CampaignRunStore(Protocol):
    def load(self, run_id: str) -> CampaignAutomationSnapshot: ...

    def commit_stage(self, commit: CampaignStageCommit) -> CampaignAutomationSnapshot: ...

    def cancel(self, run_id: str) -> CampaignAutomationSnapshot: ...


class CampaignReportPublisher(Protocol):
    def publish(self, snapshot: CampaignAutomationSnapshot) -> CampaignPublicationManifest: ...

