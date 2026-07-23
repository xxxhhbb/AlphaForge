"""Deterministic research-answer semantics for the unique product path."""

from .contracts import (
    AnswerResearchVerdict,
    AnswerStatus,
    CandidateDecision,
    CandidateDefinition,
    CandidateDisposition,
    EvidenceAuthority,
    EvidenceBoundFact,
    QuestionPartAnswer,
    QuestionRequirement,
    ResearchAnswer,
    ResearchAnswerSpec,
)
from .source_summary import ReviewedSourceSummary, summarize_reviewed_source
from .compiler import ResearchAnswerContractCompiler
from .coverage import CoverageStatus, EvidenceCoverageGate, RequirementCoverage


def __getattr__(name: str):
    # Keep the pure source-summary module usable by the pre-execution formula
    # reviewer without importing the product service back through assembler.
    if name == "ResearchAnswerAssembler":
        from .assembler import ResearchAnswerAssembler

        return ResearchAnswerAssembler
    raise AttributeError(name)

__all__ = [
    "AnswerResearchVerdict", "AnswerStatus", "CandidateDecision",
    "CandidateDefinition", "CandidateDisposition", "EvidenceAuthority",
    "EvidenceBoundFact", "QuestionPartAnswer", "QuestionRequirement",
    "ResearchAnswer", "ResearchAnswerAssembler", "ResearchAnswerSpec",
    "ResearchAnswerContractCompiler", "CoverageStatus", "EvidenceCoverageGate",
    "RequirementCoverage",
    "ReviewedSourceSummary", "summarize_reviewed_source",
]
