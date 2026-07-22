"""Shared product research terminal kinds.

This lightweight module is deliberately independent of any runtime so the
same terminal semantics can be reused by Research OS, Agent-product assembly,
reports and acceptance without importing another orchestration stack.
"""

from enum import Enum


class ResearchResultKind(str, Enum):
    EVALUATED_RESEARCH_RESULT = "EVALUATED_RESEARCH_RESULT"
    NEGATIVE_RESEARCH_RESULT = "NEGATIVE_RESEARCH_RESULT"
    DATA_OR_CAPABILITY_GAP_REPORT = "DATA_OR_CAPABILITY_GAP_REPORT"
    PARTIAL_RESEARCH_RESULT = "PARTIAL_RESEARCH_RESULT"
    SYSTEM_FAILED = "SYSTEM_FAILED"


__all__ = ["ResearchResultKind"]
