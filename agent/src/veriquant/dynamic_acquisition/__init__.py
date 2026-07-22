"""Dynamic, governed acquisition over the approved Tushare-compatible proxy."""

from .acquisition import ApprovedProxyTransport, DynamicAcquirer
from .artifacts import ContentAddressedRowStore
from .checkpoint import CheckpointCorruptionError, CheckpointStore
from .models import (
    AcquisitionRunResult,
    AcquisitionScope,
    PartitionResult,
    PartitionStatus,
    PlanEstimate,
    QueryResponse,
    QueryTask,
    ResourceBudget,
    canonical_rows_hash,
    canonical_rows_size,
    stable_content_hash,
)
from .planner import AcquisitionPlan, build_plan
from .registry import (
    ApiCapability,
    CAPABILITIES,
    CapabilityGroup,
    CapabilityRegistry,
    DEFAULT_REGISTRY,
    PaginationStrategy,
    QueryRejected,
    REGISTRY_VERSION,
    TimeSemantics,
)

__all__ = [
    "AcquisitionPlan",
    "AcquisitionRunResult",
    "AcquisitionScope",
    "ApiCapability",
    "ApprovedProxyTransport",
    "CAPABILITIES",
    "CapabilityGroup",
    "CapabilityRegistry",
    "CheckpointStore",
    "CheckpointCorruptionError",
    "ContentAddressedRowStore",
    "DEFAULT_REGISTRY",
    "DynamicAcquirer",
    "PaginationStrategy",
    "PartitionResult",
    "PartitionStatus",
    "PlanEstimate",
    "QueryRejected",
    "QueryResponse",
    "QueryTask",
    "REGISTRY_VERSION",
    "ResourceBudget",
    "TimeSemantics",
    "build_plan",
    "canonical_rows_hash",
    "canonical_rows_size",
    "stable_content_hash",
]
