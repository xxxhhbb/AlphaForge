"""Contracts for Research OS mechanical acceptance evidence."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Any, ClassVar, Mapping

from veriquant.contracts import CanonicalContract, FailureCode
from veriquant.contracts.base import fail, freeze_mapping, require_hash, require_identifier, require_text


RESEARCH_OS_ACCEPTANCE_SCHEMA_VERSION = "alphaforge.research-os-acceptance/v1"


class StrEnum(str, Enum):
    """Python 3.10-compatible stable string enum."""


class AcceptanceStatus(StrEnum):
    IMPLEMENTED_AND_TESTED = "IMPLEMENTED_AND_TESTED"
    IMPLEMENTED_NOT_RUN = "IMPLEMENTED_NOT_RUN"
    UNAVAILABLE = "UNAVAILABLE"
    DEFERRED = "DEFERRED"
    FAILED = "FAILED"


class AcceptanceGate(StrEnum):
    CONTRACTS = "CONTRACTS"
    CAPABILITY_DATA_PLANNING = "CAPABILITY_DATA_PLANNING"
    SELECTED_GROUP_ISOLATION = "SELECTED_GROUP_ISOLATION"
    PER_GROUP_DRAFTS = "PER_GROUP_DRAFTS"
    TERMINAL_ACCOUNTING_BUDGET = "TERMINAL_ACCOUNTING_BUDGET"
    FACTOR_DAG_ONLY = "FACTOR_DAG_ONLY"
    DYNAMIC_LOOKBACK = "DYNAMIC_LOOKBACK"
    PIT_OFFICIAL_AUTHORITY = "PIT_OFFICIAL_AUTHORITY"
    TOURNAMENT = "TOURNAMENT"
    PARTIAL_USEFUL_REPORT = "PARTIAL_USEFUL_REPORT"
    IDEMPOTENT_RESUME = "IDEMPOTENT_RESUME"


class AcceptanceFailureCode(StrEnum):
    MISSING_PACKAGE = "MISSING_PACKAGE"
    PACKAGE_NOT_READY = "PACKAGE_NOT_READY"
    TESTS_NOT_RUN = "TESTS_NOT_RUN"
    EMPTY_OR_CLAIMED_TEST_LOG = "EMPTY_OR_CLAIMED_TEST_LOG"
    MISSING_SYMBOL = "MISSING_SYMBOL"
    MISSING_ARTIFACT_HASH = "MISSING_ARTIFACT_HASH"
    CONTRACT_EVIDENCE_MISSING = "CONTRACT_EVIDENCE_MISSING"
    DATA_PLANNING_INCOMPLETE = "DATA_PLANNING_INCOMPLETE"
    GROUP_ISOLATION_BROKEN = "GROUP_ISOLATION_BROKEN"
    DRAFT_COUNT_INVALID = "DRAFT_COUNT_INVALID"
    CANDIDATE_NOT_TERMINAL = "CANDIDATE_NOT_TERMINAL"
    BUDGET_NOT_CONSUMED = "BUDGET_NOT_CONSUMED"
    UNSAFE_FACTOR_PATH = "UNSAFE_FACTOR_PATH"
    LOOKBACK_NOT_PRESERVED = "LOOKBACK_NOT_PRESERVED"
    WRONG_AUTHORITY = "WRONG_AUTHORITY"
    TOURNAMENT_INCOMPLETE = "TOURNAMENT_INCOMPLETE"
    RANKIC_ONLY = "RANKIC_ONLY"
    SEALED_LEAKAGE = "SEALED_LEAKAGE"
    REPORT_NOT_USEFUL = "REPORT_NOT_USEFUL"
    RESUME_NOT_IDEMPOTENT = "RESUME_NOT_IDEMPOTENT"


_RAW_READY_STATUSES = frozenset({"READY_FOR_INTEGRATION", "IMPLEMENTED_AND_TESTED"})
_TEST_SUCCESS_TOKENS = (" passed", "passed in", "py_compile passed")
_TEST_BAD_TOKENS = ("not_run", "claimed", "fake", "empty", "skipped", "xfail")


def _coerce_enum(value: Any, enum_type: type[Enum], field_name: str) -> Enum:
    if isinstance(value, enum_type):
        return value
    try:
        return enum_type(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} has invalid enum value") from exc


def _text_tuple(value: tuple[str, ...], field_name: str, *, required: bool = False) -> None:
    if not isinstance(value, tuple) or (required and not value):
        fail(FailureCode.INVALID_VALUE, f"{field_name} must be a tuple", field=field_name)
    for item in value:
        require_text(item, field_name)
    if len(value) != len(set(value)):
        fail(FailureCode.INVALID_VALUE, f"{field_name} contains duplicates", field=field_name)


def _hash_tuple(value: tuple[str, ...], field_name: str, *, required: bool = False) -> None:
    _text_tuple(value, field_name, required=required)
    for item in value:
        require_hash(item, field_name)


@dataclass(frozen=True, kw_only=True)
class PackageEvidence(CanonicalContract):
    SCHEMA_VERSION: ClassVar[str] = RESEARCH_OS_ACCEPTANCE_SCHEMA_VERSION

    package_id: str
    raw_status: str
    tests_run: tuple[str, ...]
    test_result: str
    exported_symbols: tuple[str, ...] = ()
    operational_smoke_status: str = ""
    artifact_hashes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        require_identifier(self.package_id, "package_id")
        require_text(self.raw_status, "raw_status")
        _text_tuple(self.tests_run, "tests_run")
        require_text(self.test_result, "test_result")
        _text_tuple(self.exported_symbols, "exported_symbols")
        if self.operational_smoke_status:
            require_text(self.operational_smoke_status, "operational_smoke_status")
        _hash_tuple(self.artifact_hashes, "artifact_hashes")

    @classmethod
    def from_result_payload(cls, package_id: str, payload: Mapping[str, Any]) -> "PackageEvidence":
        if not isinstance(payload, Mapping):
            fail(FailureCode.INVALID_VALUE, "package result payload must be a mapping")
        hashes: list[str] = []
        artifact_map = payload.get("artifact_hashes")
        if isinstance(artifact_map, Mapping):
            hashes.extend(str(value) for value in artifact_map.values())
        return cls(
            package_id=package_id,
            raw_status=str(payload.get("status", payload.get("raw_status", ""))),
            tests_run=tuple(str(item) for item in payload.get("tests_run", ()) if str(item).strip()),
            test_result=str(payload.get("test_result", "")),
            exported_symbols=tuple(str(item) for item in payload.get("exported_symbols", ()) if str(item).strip()),
            operational_smoke_status=str(payload.get("operational_smoke_status", "")),
            artifact_hashes=tuple(item for item in hashes if item.startswith("sha256:")),
        )

    @property
    def has_real_tests(self) -> bool:
        result = self.test_result.strip().lower()
        if not self.tests_run or not result:
            return False
        if any(token in result for token in _TEST_BAD_TOKENS):
            return False
        return any(token in result for token in _TEST_SUCCESS_TOKENS)

    @property
    def ready_and_tested(self) -> bool:
        return self.raw_status in _RAW_READY_STATUSES and self.has_real_tests


@dataclass(frozen=True, kw_only=True)
class ResearchRunAcceptanceEvidence(CanonicalContract):
    SCHEMA_VERSION: ClassVar[str] = RESEARCH_OS_ACCEPTANCE_SCHEMA_VERSION

    run_id: str
    campaign_hash: str
    intent_hash: str
    data_plan_hash: str
    route_plan_hash: str
    selected_group_ids: tuple[str, ...]
    executed_group_ids: tuple[str, ...]
    group_draft_counts: Mapping[str, int] = field(default_factory=dict)
    candidate_request_hashes: tuple[str, ...] = ()
    terminal_candidate_hashes: tuple[str, ...] = ()
    budget_consumed_by_candidate: Mapping[str, int] = field(default_factory=dict)
    factor_dag_only: bool = False
    unsafe_factor_path_detected: bool = False
    requested_lookback_sessions: int | None = None
    translated_lookback_sessions: tuple[int, ...] = ()
    pit_validated_by: str = ""
    official_evidence_by: str = ""
    official_evidence_hashes: tuple[str, ...] = ()
    tournament_candidate_hashes: tuple[str, ...] = ()
    tournament_uses_full_coverage: bool = False
    tournament_uses_rankic_only: bool = False
    tournament_sealed_material_exposed: bool = False
    report_section_levels: tuple[str, ...] = ()
    report_has_partial_limitations: bool = False
    report_hash: str | None = None
    committed_stage_hashes: tuple[str, ...] = ()
    skipped_stage_hashes: tuple[str, ...] = ()
    committed_stage_rerun_count: int = 0
    same_input_same_hash: bool = False
    provider_unavailable_deterministic_complete: bool = False

    def __post_init__(self) -> None:
        require_identifier(self.run_id, "run_id")
        for name in ("campaign_hash", "intent_hash", "data_plan_hash", "route_plan_hash"):
            require_hash(getattr(self, name), name)
        _text_tuple(self.selected_group_ids, "selected_group_ids", required=True)
        _text_tuple(self.executed_group_ids, "executed_group_ids")
        _hash_tuple(self.candidate_request_hashes, "candidate_request_hashes")
        _hash_tuple(self.terminal_candidate_hashes, "terminal_candidate_hashes")
        for name in ("factor_dag_only", "unsafe_factor_path_detected", "tournament_uses_full_coverage", "tournament_uses_rankic_only", "tournament_sealed_material_exposed", "report_has_partial_limitations", "same_input_same_hash", "provider_unavailable_deterministic_complete"):
            if type(getattr(self, name)) is not bool:
                fail(FailureCode.INVALID_VALUE, f"{name} must be boolean")
        if self.requested_lookback_sessions is not None:
            value = self.requested_lookback_sessions
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                fail(FailureCode.INVALID_VALUE, "requested_lookback_sessions must be positive")
        if not isinstance(self.translated_lookback_sessions, tuple):
            fail(FailureCode.INVALID_VALUE, "translated_lookback_sessions must be a tuple")
        for value in self.translated_lookback_sessions:
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                fail(FailureCode.INVALID_VALUE, "translated_lookback_sessions must be positive integers")
        _text_tuple(self.report_section_levels, "report_section_levels")
        _hash_tuple(self.official_evidence_hashes, "official_evidence_hashes")
        _hash_tuple(self.tournament_candidate_hashes, "tournament_candidate_hashes")
        _hash_tuple(self.committed_stage_hashes, "committed_stage_hashes")
        _hash_tuple(self.skipped_stage_hashes, "skipped_stage_hashes")
        if self.report_hash is not None:
            require_hash(self.report_hash, "report_hash")
        if isinstance(self.committed_stage_rerun_count, bool) or self.committed_stage_rerun_count < 0:
            fail(FailureCode.INVALID_VALUE, "committed_stage_rerun_count must be non-negative")
        budgets = freeze_mapping(self.budget_consumed_by_candidate, "budget_consumed_by_candidate", numeric_values=True)
        draft_counts = freeze_mapping(self.group_draft_counts, "group_draft_counts", numeric_values=True)
        object.__setattr__(self, "budget_consumed_by_candidate", MappingProxyType(dict(sorted(budgets.items()))))
        object.__setattr__(self, "group_draft_counts", MappingProxyType(dict(sorted(draft_counts.items()))))


@dataclass(frozen=True, kw_only=True)
class AcceptanceGateResult(CanonicalContract):
    SCHEMA_VERSION: ClassVar[str] = RESEARCH_OS_ACCEPTANCE_SCHEMA_VERSION

    gate: AcceptanceGate
    status: AcceptanceStatus
    failure_codes: tuple[AcceptanceFailureCode, ...] = ()
    evidence_hashes: tuple[str, ...] = ()
    message: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "gate", _coerce_enum(self.gate, AcceptanceGate, "gate"))
        object.__setattr__(self, "status", _coerce_enum(self.status, AcceptanceStatus, "status"))
        if not isinstance(self.failure_codes, tuple):
            fail(FailureCode.INVALID_VALUE, "failure_codes must be a tuple")
        for code in self.failure_codes:
            if not isinstance(code, AcceptanceFailureCode):
                object.__setattr__(
                    self,
                    "failure_codes",
                    tuple(_coerce_enum(item, AcceptanceFailureCode, "failure_codes") for item in self.failure_codes),
                )
                break
        _hash_tuple(self.evidence_hashes, "evidence_hashes")
        if self.status is AcceptanceStatus.IMPLEMENTED_AND_TESTED and self.failure_codes:
            fail(FailureCode.INVALID_STATE, "passing gate cannot carry failure codes")
        if self.status is not AcceptanceStatus.IMPLEMENTED_AND_TESTED and not self.failure_codes:
            fail(FailureCode.INVALID_STATE, "non-passing gate must carry failure codes")
        if self.message:
            require_text(self.message, "message")


@dataclass(frozen=True, kw_only=True)
class ResearchOSAcceptanceReport(CanonicalContract):
    SCHEMA_VERSION: ClassVar[str] = RESEARCH_OS_ACCEPTANCE_SCHEMA_VERSION

    report_id: str
    status: AcceptanceStatus
    gates: tuple[AcceptanceGateResult, ...]
    package_hashes: tuple[str, ...] = ()
    run_evidence_hash: str | None = None

    def __post_init__(self) -> None:
        require_identifier(self.report_id, "report_id")
        object.__setattr__(self, "status", _coerce_enum(self.status, AcceptanceStatus, "status"))
        if not isinstance(self.gates, tuple) or len(self.gates) != len(AcceptanceGate):
            fail(FailureCode.INVALID_VALUE, "report must contain exactly one result for every gate")
        gate_ids = tuple(gate.gate for gate in self.gates)
        if len(set(gate_ids)) != len(gate_ids):
            fail(FailureCode.INVALID_VALUE, "duplicate acceptance gate")
        _hash_tuple(self.package_hashes, "package_hashes")
        if self.run_evidence_hash is not None:
            require_hash(self.run_evidence_hash, "run_evidence_hash")
        expected = AcceptanceStatus.IMPLEMENTED_AND_TESTED if all(gate.status is AcceptanceStatus.IMPLEMENTED_AND_TESTED for gate in self.gates) else AcceptanceStatus.FAILED
        if self.status is not expected:
            fail(FailureCode.INVALID_STATE, "overall status must mechanically match gate statuses")
