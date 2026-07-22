"""Explicit import surface for the parallel Real A-Share Research Bridge package."""

from .bridge import AShareBridgeRequest, AShareResearchBridge, build_default_bridge_request
from .fixture import OfflineRealResearchFixture, build_offline_real_research_fixture
from .models import (
    BridgeRunManifest,
    DataQualityManifest,
    LabelSpecification,
    PITRejection,
    QlibBridgeRequest,
    QlibBridgeUnavailable,
    ResearchDatasetBundle,
    ResearchFeatureRow,
    ResearchLabelRow,
    ResearchSplitManifest,
    UniverseManifest,
)
from .qlib_bridge import QlibBridgeAdapter, QlibBridgeMaterialization, visible_rows_by_partition
from .reader import (
    FrozenSnapshotEnvelope,
    FrozenSnapshotReader,
    FrozenSnapshotValidationManifest,
    InMemoryFrozenSnapshotReader,
    RowMarketState,
)


__all__ = [
    "AShareBridgeRequest",
    "AShareResearchBridge",
    "BridgeRunManifest",
    "DataQualityManifest",
    "FrozenSnapshotEnvelope",
    "FrozenSnapshotReader",
    "FrozenSnapshotValidationManifest",
    "InMemoryFrozenSnapshotReader",
    "LabelSpecification",
    "OfflineRealResearchFixture",
    "PITRejection",
    "QlibBridgeAdapter",
    "QlibBridgeMaterialization",
    "QlibBridgeRequest",
    "QlibBridgeUnavailable",
    "ResearchDatasetBundle",
    "ResearchFeatureRow",
    "ResearchLabelRow",
    "ResearchSplitManifest",
    "RowMarketState",
    "UniverseManifest",
    "build_default_bridge_request",
    "build_offline_real_research_fixture",
    "visible_rows_by_partition",
]
