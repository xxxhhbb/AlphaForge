"""AlphaForge Research OS V3 single-mainline orchestration."""

from .models import ResearchOSV3MigrationMap, ResearchOSV3RunView, ResearchResultKind
from .multi_models import (
    MultiSpecialistBranchResult,
    MultiSpecialistBranchStatus,
    MultiSpecialistCandidateRef,
    MultiSpecialistRunResult,
    MultiSpecialistRunStatus,
)
from .multi_service import ResearchOSV3MultiSpecialistService
from .control import ResearchOSV3CampaignControl
from .evidence_adapter import (
    CandidateEvidenceTerminal,
    EvidenceAdapterFailureCode,
    EvidenceAdapterStatus,
    EvidenceDispositionRef,
    U4EvidenceAdapterRequest,
    U4EvidenceAdapterResult,
    U4OfficialEvidenceAdapter,
)
from .product_service import ResearchOSV3ProductService
from .reporting import (
    ResearchCandidateView,
    ResearchGroupView,
    ResearchMetricView,
    ResearchOSV3FormalReport,
    ResearchOSV3ReportBackend,
    ResearchOSV3ReportReceipt,
    ResearchReportOutcome,
    render_chinese_report,
)
from .qualification import (
    SpecialistOperationalStatus,
    SpecialistQualification,
    U2U3QualificationGate,
    U2U3QualificationReport,
)
from .service import ResearchOSV3Service
from .tools import (
    APPROVED_SOURCE_LABEL,
    FrozenResearchContext,
    ResearchCapabilityStatus,
    build_frozen_research_registry,
    load_frozen_research_context,
)

__all__ = [
    "ResearchOSV3MigrationMap",
    "ResearchOSV3RunView",
    "ResearchOSV3Service",
    "ResearchResultKind",
    "MultiSpecialistBranchResult",
    "MultiSpecialistBranchStatus",
    "MultiSpecialistCandidateRef",
    "MultiSpecialistRunResult",
    "MultiSpecialistRunStatus",
    "ResearchOSV3MultiSpecialistService",
    "ResearchOSV3CampaignControl",
    "ResearchOSV3ProductService",
    "CandidateEvidenceTerminal",
    "EvidenceAdapterFailureCode",
    "EvidenceAdapterStatus",
    "EvidenceDispositionRef",
    "U4EvidenceAdapterRequest",
    "U4EvidenceAdapterResult",
    "U4OfficialEvidenceAdapter",
    "ResearchCandidateView",
    "ResearchGroupView",
    "ResearchMetricView",
    "ResearchOSV3FormalReport",
    "ResearchOSV3ReportBackend",
    "ResearchOSV3ReportReceipt",
    "ResearchReportOutcome",
    "render_chinese_report",
    "APPROVED_SOURCE_LABEL",
    "FrozenResearchContext",
    "ResearchCapabilityStatus",
    "SpecialistOperationalStatus",
    "SpecialistQualification",
    "U2U3QualificationGate",
    "U2U3QualificationReport",
    "build_frozen_research_registry",
    "load_frozen_research_context",
]
