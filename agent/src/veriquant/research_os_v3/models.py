"""Canonical Research OS V3 views over the reused ResearchRunManifest."""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from ..contracts import CanonicalContract, FailureCode
from ..contracts.base import fail, require_hash, require_identifier
from ..research_results import ResearchResultKind


SCHEMA_VERSION = "alphaforge.research-os-v3/v1"


@dataclass(frozen=True)
class ResearchOSV3RunView(CanonicalContract):
    run_id: str
    manifest_hash: str
    current_stage: str
    result_kind: ResearchResultKind
    selected_specialists: tuple[str, ...]
    candidate_count: int
    accepted_preflight_count: int
    trial_count: int
    typed_failures: tuple[str, ...]
    official_evidence_hash: str | None
    report_hash: str | None
    specialist_outcome_hash: str | None
    SCHEMA_VERSION: ClassVar[str] = SCHEMA_VERSION

    def __post_init__(self) -> None:
        require_identifier(self.run_id, "run_id")
        require_hash(self.manifest_hash, "manifest_hash")
        require_identifier(self.current_stage, "current_stage")
        if not isinstance(self.result_kind, ResearchResultKind):
            fail(FailureCode.INVALID_VALUE, "result_kind is invalid")
        for item in self.selected_specialists:
            require_identifier(item, "selected_specialists")
        for name in ("candidate_count", "accepted_preflight_count", "trial_count"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                fail(FailureCode.INVALID_VALUE, f"{name} must be non-negative")
        for item in self.typed_failures:
            require_identifier(item, "typed_failures")
        for name in ("official_evidence_hash", "report_hash", "specialist_outcome_hash"):
            value = getattr(self, name)
            if value is not None:
                require_hash(value, name)


@dataclass(frozen=True)
class ResearchOSV3MigrationMap(CanonicalContract):
    campaign_compiler: str = "REUSE"
    campaign_router: str = "REUSE"
    specialist_runtime: str = "BOUNDED_RESEARCH_AGENT_LOOP_ONLY"
    concurrent_runtime: str = "SCHEDULER_ONLY"
    research_os_v2: str = "DISABLED_FALLBACK"
    legacy_group_runtime: str = "EXCLUDED_FROM_TRUSTED_PATH"
    SCHEMA_VERSION: ClassVar[str] = SCHEMA_VERSION
