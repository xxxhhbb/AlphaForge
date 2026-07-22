"""Public surface for the unique Agent research product orchestrator."""

from .contracts import *
from .service import (
    AgentResearchProductService, ContentAddressedDraftSourceRepository,
    InMemoryDraftSourceRepository, MinimalChineseReportBuilder,
)
from .production_binding import (
    ExperimentExecutionSpec, FrozenFeatureObservation, ProductionEvidenceContext,
    ProductionExperimentBinding,
)
from .review_gateway import ResearchReviewAdvisorProtocol, ResearchReviewGateway
from .planner import ResearchTaskPlan, ResearchTaskPlanner
from .evidence_gateway import UnifiedResearchEvidenceEvaluator, supported_official_output_kinds

__all__ = [
    "AgentResearchProductService", "ContentAddressedDraftSourceRepository", "DraftSourceRepository", "ExperimentBindingProtocol",
    "InMemoryDraftSourceRepository", "IsolatedResearchRunnerProtocol", "MinimalChineseReportBuilder",
    "ProductExperimentResult", "ProductExperimentStatus", "ProductFailureCode",
    "ProductReportBuilderProtocol", "ProductRunRequest", "ProductRunResult", "ResearchResultBundle",
    "ResearchTaskPlan", "ResearchTaskPlanner",
    "UnifiedResearchEvidenceEvaluator", "supported_official_output_kinds",
    "ResearchReviewAdvisorProtocol", "ResearchReviewGateway",
    "ExperimentExecutionSpec", "FrozenFeatureObservation", "ProductionEvidenceContext",
    "ProductionExperimentBinding",
]
