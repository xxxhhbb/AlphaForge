"""Versioned contracts for agent-authored, non-authoritative research experiments.

These contracts deliberately stop at the research-sandbox boundary.  They do
not grant execution, Evidence, Promotion, portfolio, risk, order, or ledger
authority.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from types import MappingProxyType
from typing import ClassVar, Mapping

from ..contracts import CanonicalContract, FailureCode
from ..contracts.base import (
    fail,
    freeze_mapping,
    require_aware_datetime,
    require_finite_number,
    require_hash,
    require_identifier,
    require_text,
)


SCHEMA_VERSION = "alphaforge.agent-research-sandbox/v1"


class StrEnum(str, Enum):
    """Python 3.10-compatible stable string enum."""


class ResearchOutputClass(StrEnum):
    DRAFT_RESEARCH_RESULT = "DRAFT_RESEARCH_RESULT"
    AUXILIARY_RESEARCH_EVIDENCE = "AUXILIARY_RESEARCH_EVIDENCE"


class ReviewFailureCode(StrEnum):
    SYNTAX_ERROR = "SYNTAX_ERROR"
    SOURCE_HASH_MISMATCH = "SOURCE_HASH_MISMATCH"
    AMENDMENT_NOT_BOUND = "AMENDMENT_NOT_BOUND"
    SELF_REVIEW = "SELF_REVIEW"
    REVISION_LIMIT_EXCEEDED = "REVISION_LIMIT_EXCEEDED"
    REVISION_BINDING_MISSING = "REVISION_BINDING_MISSING"
    AST_COMPLEXITY_EXCEEDED = "AST_COMPLEXITY_EXCEEDED"
    FORBIDDEN_IMPORT = "FORBIDDEN_IMPORT"
    DYNAMIC_IMPORT_FORBIDDEN = "DYNAMIC_IMPORT_FORBIDDEN"
    DYNAMIC_EXECUTION_FORBIDDEN = "DYNAMIC_EXECUTION_FORBIDDEN"
    FORBIDDEN_CALL = "FORBIDDEN_CALL"
    DUNDER_ACCESS_FORBIDDEN = "DUNDER_ACCESS_FORBIDDEN"
    FILE_IO_FORBIDDEN = "FILE_IO_FORBIDDEN"
    NETWORK_FORBIDDEN = "NETWORK_FORBIDDEN"
    PROCESS_FORBIDDEN = "PROCESS_FORBIDDEN"
    ENVIRONMENT_ACCESS_FORBIDDEN = "ENVIRONMENT_ACCESS_FORBIDDEN"
    PATH_ACCESS_FORBIDDEN = "PATH_ACCESS_FORBIDDEN"
    UNSAFE_SERIALIZATION_FORBIDDEN = "UNSAFE_SERIALIZATION_FORBIDDEN"
    LABEL_ACCESS_FORBIDDEN = "LABEL_ACCESS_FORBIDDEN"
    FUTURE_REFERENCE_FORBIDDEN = "FUTURE_REFERENCE_FORBIDDEN"
    ENTRYPOINT_INVALID = "ENTRYPOINT_INVALID"


class ReviewSeverity(StrEnum):
    ERROR = "ERROR"
    WARNING = "WARNING"
    INFO = "INFO"


class MechanicalReviewStatus(StrEnum):
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"


def _number(value: object, field_name: str) -> float:
    require_finite_number(value, field_name)  # type: ignore[arg-type]
    return float(value)


def _canonicalize_rows(owner: object, rows: tuple[CanonicalContract, ...], *, key, field_name: str) -> None:
    if not isinstance(rows, tuple) or not rows:
        fail(FailureCode.INVALID_VALUE, f"{field_name} must be a non-empty tuple")
    keys = tuple(key(item) for item in rows)
    if len(keys) != len(set(keys)):
        fail(FailureCode.INVALID_VALUE, f"{field_name} contains duplicate rows")
    object.__setattr__(owner, field_name, tuple(sorted(rows, key=key)))


def _validate_table_identity(*, experiment_id: str, code_identity_hash: str, data_identity_hash: str) -> None:
    require_identifier(experiment_id, "experiment_id")
    require_hash(code_identity_hash, "code_identity_hash")
    require_hash(data_identity_hash, "data_identity_hash")


@dataclass(frozen=True, kw_only=True)
class SandboxAmendmentIdentity(CanonicalContract):
    amendment_id: str
    amendment_version: str
    constitution_hash: str
    policy_hash: str
    authorized_at: datetime
    generated_python_scope: str = "ISOLATED_RESEARCH_ONLY"

    SCHEMA_VERSION: ClassVar[str] = SCHEMA_VERSION

    def __post_init__(self) -> None:
        require_identifier(self.amendment_id, "amendment_id")
        require_identifier(self.amendment_version, "amendment_version")
        require_hash(self.constitution_hash, "constitution_hash")
        require_hash(self.policy_hash, "policy_hash")
        require_aware_datetime(self.authorized_at, "authorized_at")
        if self.generated_python_scope != "ISOLATED_RESEARCH_ONLY":
            fail(FailureCode.UNAUTHORIZED_CAPABILITY, "amendment cannot authorize trusted-path Python")


@dataclass(frozen=True, kw_only=True)
class ExperimentCodeIdentity(CanonicalContract):
    source_hash: str
    python_runtime_hash: str
    dependency_manifest_hash: str
    review_policy_hash: str
    amendment_hash: str
    entrypoint: str = "run_research"

    SCHEMA_VERSION: ClassVar[str] = SCHEMA_VERSION

    def __post_init__(self) -> None:
        for name in (
            "source_hash",
            "python_runtime_hash",
            "dependency_manifest_hash",
            "review_policy_hash",
            "amendment_hash",
        ):
            require_hash(getattr(self, name), name)
        require_identifier(self.entrypoint, "entrypoint")


@dataclass(frozen=True, kw_only=True)
class ExperimentDataIdentity(CanonicalContract):
    dataset_hash: str
    snapshot_hash: str
    universe_hash: str
    schema_hash: str
    availability_policy_hash: str
    decision_start: datetime
    decision_end: datetime

    SCHEMA_VERSION: ClassVar[str] = SCHEMA_VERSION

    def __post_init__(self) -> None:
        for name in ("dataset_hash", "snapshot_hash", "universe_hash", "schema_hash", "availability_policy_hash"):
            require_hash(getattr(self, name), name)
        require_aware_datetime(self.decision_start, "decision_start")
        require_aware_datetime(self.decision_end, "decision_end")
        if self.decision_start > self.decision_end:
            fail(FailureCode.INVALID_TIME_ORDER, "decision_start must not be after decision_end")


@dataclass(frozen=True, kw_only=True)
class ScoreRow(CanonicalContract):
    symbol: str
    decision_time: datetime
    source_available_at: datetime
    score: float
    label_horizon_sessions: int

    SCHEMA_VERSION: ClassVar[str] = SCHEMA_VERSION

    def __post_init__(self) -> None:
        require_identifier(self.symbol, "symbol")
        require_aware_datetime(self.decision_time, "decision_time")
        require_aware_datetime(self.source_available_at, "source_available_at")
        if self.source_available_at > self.decision_time:
            fail(FailureCode.INVALID_TIME_ORDER, "score source is unavailable at decision_time")
        object.__setattr__(self, "score", _number(self.score, "score"))
        if isinstance(self.label_horizon_sessions, bool) or not isinstance(self.label_horizon_sessions, int) or self.label_horizon_sessions < 1:
            fail(FailureCode.INVALID_VALUE, "label_horizon_sessions must be positive")


@dataclass(frozen=True, kw_only=True)
class ScoreTable(CanonicalContract):
    experiment_id: str
    code_identity_hash: str
    data_identity_hash: str
    rows: tuple[ScoreRow, ...]
    output_class: ResearchOutputClass = ResearchOutputClass.DRAFT_RESEARCH_RESULT

    SCHEMA_VERSION: ClassVar[str] = SCHEMA_VERSION

    def __post_init__(self) -> None:
        _validate_table_identity(experiment_id=self.experiment_id, code_identity_hash=self.code_identity_hash, data_identity_hash=self.data_identity_hash)
        if not all(isinstance(row, ScoreRow) for row in self.rows):
            fail(FailureCode.INVALID_VALUE, "ScoreTable rows must be ScoreRow")
        _canonicalize_rows(
            self,
            self.rows,
            key=lambda row: (row.decision_time, row.symbol),
            field_name="rows",
        )
        if self.output_class not in set(ResearchOutputClass):
            fail(FailureCode.INVALID_VALUE, "invalid research output class")


@dataclass(frozen=True, kw_only=True)
class EventRow(CanonicalContract):
    event_id: str
    symbol: str
    event_type: str
    event_time: datetime
    published_at: datetime
    available_at: datetime
    decision_time: datetime
    signal: float

    SCHEMA_VERSION: ClassVar[str] = SCHEMA_VERSION

    def __post_init__(self) -> None:
        for value, name in ((self.event_id, "event_id"), (self.symbol, "symbol"), (self.event_type, "event_type")):
            require_identifier(value, name)
        for name in ("event_time", "published_at", "available_at", "decision_time"):
            require_aware_datetime(getattr(self, name), name)
        if self.published_at > self.available_at or self.available_at > self.decision_time:
            fail(FailureCode.INVALID_TIME_ORDER, "event publication/availability/decision clocks are invalid")
        object.__setattr__(self, "signal", _number(self.signal, "signal"))


@dataclass(frozen=True, kw_only=True)
class EventTable(CanonicalContract):
    experiment_id: str
    code_identity_hash: str
    data_identity_hash: str
    rows: tuple[EventRow, ...]
    output_class: ResearchOutputClass = ResearchOutputClass.DRAFT_RESEARCH_RESULT

    SCHEMA_VERSION: ClassVar[str] = SCHEMA_VERSION

    def __post_init__(self) -> None:
        _validate_table_identity(experiment_id=self.experiment_id, code_identity_hash=self.code_identity_hash, data_identity_hash=self.data_identity_hash)
        if not all(isinstance(row, EventRow) for row in self.rows):
            fail(FailureCode.INVALID_VALUE, "EventTable rows must be EventRow")
        _canonicalize_rows(self, self.rows, key=lambda row: (row.decision_time, row.event_id), field_name="rows")


@dataclass(frozen=True, kw_only=True)
class RegressionRow(CanonicalContract):
    term: str
    estimate: float
    std_error: float
    t_statistic: float
    p_value: float
    observation_count: int

    SCHEMA_VERSION: ClassVar[str] = SCHEMA_VERSION

    def __post_init__(self) -> None:
        require_identifier(self.term, "term")
        for name in ("estimate", "std_error", "t_statistic", "p_value"):
            object.__setattr__(self, name, _number(getattr(self, name), name))
        if self.std_error < 0 or not 0 <= self.p_value <= 1:
            fail(FailureCode.INVALID_VALUE, "invalid regression uncertainty")
        if isinstance(self.observation_count, bool) or not isinstance(self.observation_count, int) or self.observation_count < 1:
            fail(FailureCode.INVALID_VALUE, "observation_count must be positive")


@dataclass(frozen=True, kw_only=True)
class RegressionTable(CanonicalContract):
    experiment_id: str
    code_identity_hash: str
    data_identity_hash: str
    specification_id: str
    rows: tuple[RegressionRow, ...]
    output_class: ResearchOutputClass = ResearchOutputClass.AUXILIARY_RESEARCH_EVIDENCE

    SCHEMA_VERSION: ClassVar[str] = SCHEMA_VERSION

    def __post_init__(self) -> None:
        require_identifier(self.specification_id, "specification_id")
        _validate_table_identity(experiment_id=self.experiment_id, code_identity_hash=self.code_identity_hash, data_identity_hash=self.data_identity_hash)
        if not all(isinstance(row, RegressionRow) for row in self.rows):
            fail(FailureCode.INVALID_VALUE, "RegressionTable rows must be RegressionRow")
        _canonicalize_rows(self, self.rows, key=lambda row: row.term, field_name="rows")


@dataclass(frozen=True, kw_only=True)
class RegimeComparisonRow(CanonicalContract):
    regime_id: str
    metric_name: str
    estimate: float
    sample_count: int

    SCHEMA_VERSION: ClassVar[str] = SCHEMA_VERSION

    def __post_init__(self) -> None:
        require_identifier(self.regime_id, "regime_id")
        require_identifier(self.metric_name, "metric_name")
        object.__setattr__(self, "estimate", _number(self.estimate, "estimate"))
        if isinstance(self.sample_count, bool) or not isinstance(self.sample_count, int) or self.sample_count < 1:
            fail(FailureCode.INVALID_VALUE, "sample_count must be positive")


@dataclass(frozen=True, kw_only=True)
class RegimeComparisonTable(CanonicalContract):
    experiment_id: str
    code_identity_hash: str
    data_identity_hash: str
    rows: tuple[RegimeComparisonRow, ...]
    output_class: ResearchOutputClass = ResearchOutputClass.AUXILIARY_RESEARCH_EVIDENCE

    SCHEMA_VERSION: ClassVar[str] = SCHEMA_VERSION

    def __post_init__(self) -> None:
        _validate_table_identity(experiment_id=self.experiment_id, code_identity_hash=self.code_identity_hash, data_identity_hash=self.data_identity_hash)
        if not all(isinstance(row, RegimeComparisonRow) for row in self.rows):
            fail(FailureCode.INVALID_VALUE, "RegimeComparisonTable rows must be RegimeComparisonRow")
        _canonicalize_rows(self, self.rows, key=lambda row: (row.regime_id, row.metric_name), field_name="rows")


@dataclass(frozen=True, kw_only=True)
class DiagnosticRow(CanonicalContract):
    diagnostic_id: str
    metric_name: str
    value: float
    unit: str
    interpretation: str

    SCHEMA_VERSION: ClassVar[str] = SCHEMA_VERSION

    def __post_init__(self) -> None:
        require_identifier(self.diagnostic_id, "diagnostic_id")
        require_identifier(self.metric_name, "metric_name")
        require_identifier(self.unit, "unit")
        require_text(self.interpretation, "interpretation")
        object.__setattr__(self, "value", _number(self.value, "value"))


@dataclass(frozen=True, kw_only=True)
class DiagnosticTable(CanonicalContract):
    experiment_id: str
    code_identity_hash: str
    data_identity_hash: str
    rows: tuple[DiagnosticRow, ...]
    output_class: ResearchOutputClass = ResearchOutputClass.AUXILIARY_RESEARCH_EVIDENCE

    SCHEMA_VERSION: ClassVar[str] = SCHEMA_VERSION

    def __post_init__(self) -> None:
        _validate_table_identity(experiment_id=self.experiment_id, code_identity_hash=self.code_identity_hash, data_identity_hash=self.data_identity_hash)
        if not all(isinstance(row, DiagnosticRow) for row in self.rows):
            fail(FailureCode.INVALID_VALUE, "DiagnosticTable rows must be DiagnosticRow")
        _canonicalize_rows(self, self.rows, key=lambda row: (row.diagnostic_id, row.metric_name), field_name="rows")


@dataclass(frozen=True, kw_only=True)
class DatasetAssessmentRow(CanonicalContract):
    field_name: str
    source_id: str
    coverage: float
    pit_eligible: bool
    observed_count: int
    limitation: str

    SCHEMA_VERSION: ClassVar[str] = SCHEMA_VERSION

    def __post_init__(self) -> None:
        require_identifier(self.field_name, "field_name")
        require_identifier(self.source_id, "source_id")
        object.__setattr__(self, "coverage", _number(self.coverage, "coverage"))
        if not 0 <= self.coverage <= 1:
            fail(FailureCode.INVALID_VALUE, "coverage must lie in [0,1]")
        if type(self.pit_eligible) is not bool:
            fail(FailureCode.INVALID_VALUE, "pit_eligible must be boolean")
        if isinstance(self.observed_count, bool) or not isinstance(self.observed_count, int) or self.observed_count < 0:
            fail(FailureCode.INVALID_VALUE, "observed_count must be non-negative")
        require_text(self.limitation, "limitation")


@dataclass(frozen=True, kw_only=True)
class DatasetAssessmentTable(CanonicalContract):
    experiment_id: str
    code_identity_hash: str
    data_identity_hash: str
    rows: tuple[DatasetAssessmentRow, ...]
    output_class: ResearchOutputClass = ResearchOutputClass.AUXILIARY_RESEARCH_EVIDENCE

    SCHEMA_VERSION: ClassVar[str] = SCHEMA_VERSION

    def __post_init__(self) -> None:
        _validate_table_identity(experiment_id=self.experiment_id, code_identity_hash=self.code_identity_hash, data_identity_hash=self.data_identity_hash)
        if not all(isinstance(row, DatasetAssessmentRow) for row in self.rows):
            fail(FailureCode.INVALID_VALUE, "DatasetAssessmentTable rows must be DatasetAssessmentRow")
        _canonicalize_rows(self, self.rows, key=lambda row: (row.field_name, row.source_id), field_name="rows")


@dataclass(frozen=True, kw_only=True)
class ReviewFinding(CanonicalContract):
    code: ReviewFailureCode
    severity: ReviewSeverity
    message: str
    line: int | None = None
    column: int | None = None
    mechanical: bool = True

    SCHEMA_VERSION: ClassVar[str] = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if not isinstance(self.code, ReviewFailureCode) or not isinstance(self.severity, ReviewSeverity):
            fail(FailureCode.INVALID_VALUE, "review finding enum is invalid")
        require_text(self.message, "message")
        for name in ("line", "column"):
            value = getattr(self, name)
            if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
                fail(FailureCode.INVALID_VALUE, f"{name} must be non-negative")
        if self.mechanical is not True:
            fail(FailureCode.INVALID_VALUE, "ReviewFinding is reserved for mechanical findings")


@dataclass(frozen=True, kw_only=True)
class ReviewerSuggestion(CanonicalContract):
    suggestion_id: str
    message: str

    SCHEMA_VERSION: ClassVar[str] = SCHEMA_VERSION

    def __post_init__(self) -> None:
        require_identifier(self.suggestion_id, "suggestion_id")
        require_text(self.message, "message")


@dataclass(frozen=True, kw_only=True)
class ExperimentReviewPolicy(CanonicalContract):
    policy_id: str = "agent-research-python-static-review"
    policy_version: str = "1.0.0"
    allowed_import_roots: tuple[str, ...] = (
        "math",
        "numpy",
        "pandas",
        "polars",
        "pyarrow",
        "scipy",
        "statistics",
        "statsmodels",
    )
    max_ast_nodes: int = 5000
    max_source_bytes: int = 100_000
    max_revisions: int = 1

    SCHEMA_VERSION: ClassVar[str] = SCHEMA_VERSION

    def __post_init__(self) -> None:
        require_identifier(self.policy_id, "policy_id")
        require_identifier(self.policy_version, "policy_version")
        if tuple(sorted(set(self.allowed_import_roots))) != self.allowed_import_roots:
            fail(FailureCode.INVALID_VALUE, "allowed_import_roots must be sorted and unique")
        for item in self.allowed_import_roots:
            require_identifier(item, "allowed_import_roots")
        for name in ("max_ast_nodes", "max_source_bytes"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                fail(FailureCode.INVALID_VALUE, f"{name} must be positive")
        if self.max_revisions != 1:
            fail(FailureCode.INVALID_VALUE, "review contract permits exactly one revision")


@dataclass(frozen=True, kw_only=True)
class ExperimentReviewRequest(CanonicalContract):
    experiment_id: str
    proposer_id: str
    reviewer_id: str
    source_text: str
    code_identity: ExperimentCodeIdentity
    data_identity: ExperimentDataIdentity
    amendment: SandboxAmendmentIdentity
    policy: ExperimentReviewPolicy
    revision_number: int = 0
    prior_review_hash: str | None = None

    SCHEMA_VERSION: ClassVar[str] = SCHEMA_VERSION

    def __post_init__(self) -> None:
        for value, name in ((self.experiment_id, "experiment_id"), (self.proposer_id, "proposer_id"), (self.reviewer_id, "reviewer_id")):
            require_identifier(value, name)
        if self.proposer_id == self.reviewer_id:
            fail(FailureCode.SELF_APPROVAL, "experiment reviewer must be independent")
        require_text(self.source_text, "source_text")
        if not isinstance(self.code_identity, ExperimentCodeIdentity) or not isinstance(self.data_identity, ExperimentDataIdentity):
            fail(FailureCode.INVALID_VALUE, "typed code and data identities are required")
        if not isinstance(self.amendment, SandboxAmendmentIdentity) or not isinstance(self.policy, ExperimentReviewPolicy):
            fail(FailureCode.INVALID_VALUE, "typed amendment and policy are required")
        if self.code_identity.review_policy_hash != self.policy.artifact_hash:
            fail(FailureCode.INVALID_HASH, "code identity does not bind review policy")
        if self.code_identity.amendment_hash != self.amendment.artifact_hash:
            fail(FailureCode.INVALID_HASH, "code identity does not bind sandbox amendment")
        if self.revision_number not in (0, 1) or self.revision_number > self.policy.max_revisions:
            fail(FailureCode.INVALID_VALUE, "at most one revision is permitted")
        if self.revision_number == 0 and self.prior_review_hash is not None:
            fail(FailureCode.INVALID_STATE, "initial review cannot bind a prior review")
        if self.revision_number == 1:
            if self.prior_review_hash is None:
                fail(FailureCode.MISSING_REQUIRED_FIELD, "revision requires prior_review_hash")
            require_hash(self.prior_review_hash, "prior_review_hash")


@dataclass(frozen=True, kw_only=True)
class ExperimentReviewDecision(CanonicalContract):
    experiment_id: str
    source_hash: str
    policy_hash: str
    amendment_hash: str
    status: MechanicalReviewStatus
    findings: tuple[ReviewFinding, ...]
    reviewer_suggestions: tuple[ReviewerSuggestion, ...]
    revision_number: int
    may_submit_once_more: bool
    grants_execution_authority: bool = False

    SCHEMA_VERSION: ClassVar[str] = SCHEMA_VERSION

    def __post_init__(self) -> None:
        require_identifier(self.experiment_id, "experiment_id")
        for name in ("source_hash", "policy_hash", "amendment_hash"):
            require_hash(getattr(self, name), name)
        if not isinstance(self.status, MechanicalReviewStatus):
            fail(FailureCode.INVALID_VALUE, "invalid mechanical review status")
        if not isinstance(self.findings, tuple) or not all(isinstance(item, ReviewFinding) for item in self.findings):
            fail(FailureCode.INVALID_VALUE, "findings must be ReviewFinding tuple")
        if not isinstance(self.reviewer_suggestions, tuple) or not all(isinstance(item, ReviewerSuggestion) for item in self.reviewer_suggestions):
            fail(FailureCode.INVALID_VALUE, "reviewer_suggestions must be typed")
        has_error = any(item.severity is ReviewSeverity.ERROR for item in self.findings)
        if self.status is MechanicalReviewStatus.ACCEPTED and has_error:
            fail(FailureCode.INVALID_STATE, "reviewer suggestions cannot override mechanical rejection")
        if self.status is MechanicalReviewStatus.REJECTED and not has_error:
            fail(FailureCode.INVALID_STATE, "mechanical rejection requires an error finding")
        if self.revision_number not in (0, 1):
            fail(FailureCode.INVALID_VALUE, "invalid revision_number")
        if self.may_submit_once_more != (self.status is MechanicalReviewStatus.REJECTED and self.revision_number == 0):
            fail(FailureCode.INVALID_STATE, "revision permission does not match review result")
        if self.grants_execution_authority is not False:
            fail(FailureCode.UNAUTHORIZED_CAPABILITY, "static review never grants execution authority")


__all__ = [
    "DatasetAssessmentRow",
    "DatasetAssessmentTable",
    "DiagnosticRow",
    "DiagnosticTable",
    "EventRow",
    "EventTable",
    "ExperimentCodeIdentity",
    "ExperimentDataIdentity",
    "ExperimentReviewDecision",
    "ExperimentReviewPolicy",
    "ExperimentReviewRequest",
    "MechanicalReviewStatus",
    "RegressionRow",
    "RegressionTable",
    "RegimeComparisonRow",
    "RegimeComparisonTable",
    "ResearchOutputClass",
    "ReviewFailureCode",
    "ReviewFinding",
    "ReviewerSuggestion",
    "ReviewSeverity",
    "SandboxAmendmentIdentity",
    "ScoreRow",
    "ScoreTable",
]
