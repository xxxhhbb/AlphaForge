"""Durable Campaign run store.

The store persists Campaign runtime materializations and operational events.
It is not a Trial Ledger and must not be used as Evidence, Promotion, Portfolio,
Risk, Execution, or accounting truth.
"""

from .store import (
    CampaignCheckpoint,
    CampaignRunEvent,
    CampaignRunEventType,
    CampaignRunRecord,
    CampaignRunStoreFailure,
    CampaignRunStoreStatus,
    FileCampaignRunStore,
    InMemoryCampaignRunStore,
)

__all__ = [
    "CampaignCheckpoint",
    "CampaignRunEvent",
    "CampaignRunEventType",
    "CampaignRunRecord",
    "CampaignRunStoreFailure",
    "CampaignRunStoreStatus",
    "FileCampaignRunStore",
    "InMemoryCampaignRunStore",
]
