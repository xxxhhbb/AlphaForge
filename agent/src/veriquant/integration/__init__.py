"""VeriQuant integration acceptance gates and optional thin hooks.

This package is deliberately outside the financial truth path.  It discovers
parallel capability packages, validates their handoff metadata, and provides
small offline adapters for READY_FOR_INTEGRATION packages without granting LLM,
document, or auxiliary components official metric, promotion, risk, order, or
ledger authority.
"""

from .acceptance import (
    ACCEPTANCE_STATUS_VALUES,
    CAPABILITY_PROMPTS,
    IntegrationAcceptanceError,
    build_acceptance_result,
    probe_optional_capabilities,
    redacted_for_workbench,
    safe_artifact_relative_path,
    write_acceptance_result,
)
from .llm_desks import (
    FactorDAGCompilePITAdapter,
    InMemoryOfficialEvaluationAdapter,
    run_bounded_llm_desks_offline,
)
from .research_memory import (
    SourceBackedDraftBridgeResult,
    adapt_source_backed_draft,
    source_backed_hypothesis_to_proposal_payload,
)
from .statistical import (
    InMemoryOfficialEvidenceReader,
    PromotionEvaluationRequest,
    build_family_manifest,
    candidate_view_from_official_evidence,
    evaluate_statistical_promotion,
)
__all__ = [
    "ACCEPTANCE_STATUS_VALUES",
    "CAPABILITY_PROMPTS",
    "FactorDAGCompilePITAdapter",
    "InMemoryOfficialEvaluationAdapter",
    "InMemoryOfficialEvidenceReader",
    "IntegrationAcceptanceError",
    "PromotionEvaluationRequest",
    "SourceBackedDraftBridgeResult",
    "WorkbenchPublicationError",
    "adapt_source_backed_draft",
    "build_acceptance_result",
    "build_family_manifest",
    "candidate_view_from_official_evidence",
    "evaluate_statistical_promotion",
    "probe_optional_capabilities",
    "publish_workbench_run",
    "redacted_for_workbench",
    "run_bounded_llm_desks_offline",
    "safe_artifact_relative_path",
    "source_backed_hypothesis_to_proposal_payload",
    "write_acceptance_result",
]


def __getattr__(name: str):
    """Load API-coupled Workbench hooks only when they are requested.

    Importing a financial integration submodule must remain possible with the
    product's fixed ``PYTHONPATH=agent/src``.  The Workbench publisher depends
    on the host API package (``src.api``), which is intentionally absent from
    that narrow runtime.  Lazy loading keeps that optional presentation edge
    out of data/evidence CLI collection without changing its public exports.
    """

    if name in {"WorkbenchPublicationError", "publish_workbench_run"}:
        from .workbench import WorkbenchPublicationError, publish_workbench_run

        return {
            "WorkbenchPublicationError": WorkbenchPublicationError,
            "publish_workbench_run": publish_workbench_run,
        }[name]
    raise AttributeError(name)
