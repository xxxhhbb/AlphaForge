"""Contracts for mechanical 24-scenario product acceptance."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import ClassVar

from ..contracts import CanonicalContract, FailureCode
from ..contracts.base import fail, require_hash, require_identifier


SCHEMA_VERSION = "alphaforge.product-acceptance/v1"


class SemanticCompileStatus(str, Enum):
    COMPILED = "COMPILED"
    NEEDS_CLARIFICATION = "NEEDS_CLARIFICATION"
    UNSUPPORTED = "UNSUPPORTED"
    FAILED = "FAILED"


class DataCapabilityStatus(str, Enum):
    READY = "READY"
    PARTIAL = "PARTIAL"
    AUXILIARY_ONLY = "AUXILIARY_ONLY"
    UNAVAILABLE = "UNAVAILABLE"


class EvidenceProvenance(str, Enum):
    REAL_FROZEN = "REAL_FROZEN"
    REGISTERED_EVIDENCE = "REGISTERED_EVIDENCE"
    SYNTHETIC = "SYNTHETIC"
    FIXTURE = "FIXTURE"
    NONE = "NONE"


class ProductTerminal(str, Enum):
    OFFICIAL_EVALUATED = "OFFICIAL_EVALUATED"
    NEGATIVE_RESEARCH_RESULT = "NEGATIVE_RESEARCH_RESULT"
    DATA_GAP_REPORT = "DATA_GAP_REPORT"
    PARTIAL_RESEARCH_RESULT = "PARTIAL_RESEARCH_RESULT"
    DIAGNOSTIC_REPORT = "DIAGNOSTIC_REPORT"
    UNSUPPORTED_SAFELY = "UNSUPPORTED_SAFELY"
    SYSTEM_FAILED = "SYSTEM_FAILED"
    CANCELLED = "CANCELLED"


class ScenarioAcceptanceStatus(str, Enum):
    PASSED = "PASSED"
    FAILED = "FAILED"


class ProductFailureCode(str, Enum):
    MISSING_SCENARIO = "MISSING_SCENARIO"
    DUPLICATE_SCENARIO = "DUPLICATE_SCENARIO"
    MISSING_CRITICAL_HASH = "MISSING_CRITICAL_HASH"
    ILLEGAL_TERMINAL = "ILLEGAL_TERMINAL"
    SEMANTIC_COMPILE_FAILED = "SEMANTIC_COMPILE_FAILED"
    AMBIGUITY_EXECUTED = "AMBIGUITY_EXECUTED"
    SYNTHETIC_AS_REAL = "SYNTHETIC_AS_REAL"
    OFFICIAL_WITHOUT_DATA_CAPABILITY = "OFFICIAL_WITHOUT_DATA_CAPABILITY"
    OFFICIAL_WITHOUT_EVIDENCE = "OFFICIAL_WITHOUT_EVIDENCE"
    OFFICIAL_WITHOUT_SELECTION = "OFFICIAL_WITHOUT_SELECTION"
    SPECIALIST_EVIDENCE_MISMATCH = "SPECIALIST_EVIDENCE_MISMATCH"
    CANDIDATE_TERMINAL_MISMATCH = "CANDIDATE_TERMINAL_MISMATCH"
    TRIAL_COUNT_MISMATCH = "TRIAL_COUNT_MISMATCH"
    MANIFEST_STATE_MISMATCH = "MANIFEST_STATE_MISMATCH"
    TEST_EVIDENCE_MISSING = "TEST_EVIDENCE_MISSING"
    REAL_OR_REGISTERED_COVERAGE_LOW = "REAL_OR_REGISTERED_COVERAGE_LOW"
    SPECIALIST_COVERAGE_INCOMPLETE = "SPECIALIST_COVERAGE_INCOMPLETE"


def _hashes(values: tuple[str, ...], name: str) -> None:
    if not isinstance(values, tuple):
        fail(FailureCode.INVALID_VALUE, f"{name} must be tuple")
    for value in values:
        require_hash(value, name)
    if len(values) != len(set(values)):
        fail(FailureCode.INVALID_VALUE, f"{name} contains duplicates")


@dataclass(frozen=True, kw_only=True)
class ScenarioEvidence(CanonicalContract):
    scenario_id: str
    semantic_status: SemanticCompileStatus
    semantic_compile_hash: str | None
    data_status: DataCapabilityStatus
    data_capability_hash: str | None
    provenance: EvidenceProvenance
    terminal: ProductTerminal
    selected_specialists: tuple[str, ...]
    specialist_result_hashes: tuple[str, ...]
    candidate_terminal_statuses: tuple[str, ...]
    candidate_terminal_hashes: tuple[str, ...]
    official_evidence_hashes: tuple[str, ...]
    selection_hash: str | None
    report_hash: str | None
    canonical_manifest_hash: str | None
    api_manifest_hash: str | None
    workbench_manifest_hash: str | None
    report_manifest_hash: str | None
    test_evidence_hashes: tuple[str, ...]
    trial_count: int
    claims_official: bool = False
    schema_version: ClassVar[str] = SCHEMA_VERSION

    def __post_init__(self) -> None:
        require_identifier(self.scenario_id, "scenario_id")
        for name, enum_type in (
            ("semantic_status", SemanticCompileStatus), ("data_status", DataCapabilityStatus),
            ("provenance", EvidenceProvenance), ("terminal", ProductTerminal),
        ):
            if not isinstance(getattr(self, name), enum_type):
                fail(FailureCode.INVALID_VALUE, f"{name} has invalid enum")
        for name in (
            "semantic_compile_hash", "data_capability_hash", "selection_hash", "report_hash",
            "canonical_manifest_hash", "api_manifest_hash", "workbench_manifest_hash", "report_manifest_hash",
        ):
            value = getattr(self, name)
            if value is not None:
                require_hash(value, name)
        if not isinstance(self.selected_specialists, tuple):
            fail(FailureCode.INVALID_VALUE, "selected_specialists must be tuple")
        for specialist in self.selected_specialists:
            require_identifier(specialist, "selected_specialists")
        if len(self.selected_specialists) != len(set(self.selected_specialists)):
            fail(FailureCode.INVALID_VALUE, "selected_specialists contains duplicates")
        for name in (
            "specialist_result_hashes", "candidate_terminal_hashes", "official_evidence_hashes",
            "test_evidence_hashes",
        ):
            _hashes(getattr(self, name), name)
        if not isinstance(self.candidate_terminal_statuses, tuple):
            fail(FailureCode.INVALID_VALUE, "candidate_terminal_statuses must be tuple")
        for status in self.candidate_terminal_statuses:
            require_identifier(status, "candidate_terminal_statuses")
        if isinstance(self.trial_count, bool) or not isinstance(self.trial_count, int) or self.trial_count < 0:
            fail(FailureCode.INVALID_VALUE, "trial_count must be non-negative")
        if not isinstance(self.claims_official, bool):
            fail(FailureCode.INVALID_VALUE, "claims_official must be boolean")


@dataclass(frozen=True, kw_only=True)
class ScenarioVerdict(CanonicalContract):
    scenario_id: str
    status: ScenarioAcceptanceStatus
    terminal: ProductTerminal
    failure_codes: tuple[ProductFailureCode, ...]
    evidence_hash: str
    schema_version: ClassVar[str] = SCHEMA_VERSION

    def __post_init__(self) -> None:
        require_identifier(self.scenario_id, "scenario_id")
        if not isinstance(self.status, ScenarioAcceptanceStatus):
            fail(FailureCode.INVALID_VALUE, "invalid scenario acceptance status")
        if not isinstance(self.terminal, ProductTerminal):
            fail(FailureCode.INVALID_VALUE, "invalid product terminal")
        if not isinstance(self.failure_codes, tuple) or any(not isinstance(item, ProductFailureCode) for item in self.failure_codes):
            fail(FailureCode.INVALID_VALUE, "invalid failure codes")
        require_hash(self.evidence_hash, "evidence_hash")
        if (self.status is ScenarioAcceptanceStatus.PASSED) == bool(self.failure_codes):
            fail(FailureCode.INVALID_STATE, "status and failure codes disagree")


@dataclass(frozen=True, kw_only=True)
class ProductAcceptanceReport(CanonicalContract):
    matrix_hash: str
    status: ScenarioAcceptanceStatus
    verdicts: tuple[ScenarioVerdict, ...]
    scenario_count: int
    passed_count: int
    real_or_registered_count: int
    covered_specialists: tuple[str, ...]
    global_failure_codes: tuple[ProductFailureCode, ...]
    schema_version: ClassVar[str] = SCHEMA_VERSION

    def __post_init__(self) -> None:
        require_hash(self.matrix_hash, "matrix_hash")
        if not isinstance(self.status, ScenarioAcceptanceStatus):
            fail(FailureCode.INVALID_VALUE, "invalid report status")
        if tuple(sorted(self.verdicts, key=lambda item: item.scenario_id)) != self.verdicts:
            fail(FailureCode.INVALID_STATE, "verdicts must be stable-sorted")
        if len({item.scenario_id for item in self.verdicts}) != len(self.verdicts):
            fail(FailureCode.INVALID_STATE, "duplicate scenario verdict")
        if self.scenario_count != len(self.verdicts):
            fail(FailureCode.INVALID_STATE, "scenario_count mismatch")
        if self.passed_count != sum(item.status is ScenarioAcceptanceStatus.PASSED for item in self.verdicts):
            fail(FailureCode.INVALID_STATE, "passed_count mismatch")
        if tuple(sorted(set(self.covered_specialists))) != self.covered_specialists:
            fail(FailureCode.INVALID_STATE, "covered_specialists must be stable unique")
        if not isinstance(self.global_failure_codes, tuple) or any(not isinstance(item, ProductFailureCode) for item in self.global_failure_codes):
            fail(FailureCode.INVALID_VALUE, "invalid global failures")
        if (self.status is ScenarioAcceptanceStatus.PASSED) == bool(self.global_failure_codes or self.passed_count != self.scenario_count):
            fail(FailureCode.INVALID_STATE, "report status is inconsistent")


__all__ = [
    "DataCapabilityStatus", "EvidenceProvenance", "ProductAcceptanceReport", "ProductFailureCode",
    "ProductTerminal", "ScenarioAcceptanceStatus", "ScenarioEvidence", "ScenarioVerdict",
    "SemanticCompileStatus",
]
