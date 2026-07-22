"""Typed contracts for full-accounting candidate validation and comparison."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import ClassVar, Mapping

from ..candidate_tournament import CandidateTournamentResult
from ..candidate_translation import CandidateTranslationResult
from ..contracts import CanonicalContract, FailureCode
from ..contracts.base import (
    fail,
    freeze_mapping,
    require_aware_datetime,
    require_hash,
    require_identifier,
    require_text,
    require_time_order,
)
from ..data import MarketSnapshot, canonical_content_hash
from ..research_evidence_bridge import CandidateEvidenceValidationResult
from ..research_group_runtime import GroupRuntimeResult
from ..research_os import ValidationDisposition


SCHEMA_VERSION = "alphaforge.research-validation-pipeline-v2/v1"


class CandidateTerminalStatus(str, Enum):
    OFFICIAL_VISIBLE_EVALUATED = "OFFICIAL_VISIBLE_EVALUATED"
    TRANSLATION_REJECTED = "TRANSLATION_REJECTED"
    PIT_REJECTED = "PIT_REJECTED"
    EVALUATION_REJECTED = "EVALUATION_REJECTED"
    BUDGET_BLOCKED = "BUDGET_BLOCKED"
    LEDGER_BLOCKED = "LEDGER_BLOCKED"


class PipelineFailureCode(str, Enum):
    DRAFT_TRANSLATION_REJECTED = "DRAFT_TRANSLATION_REJECTED"
    PIT_INPUT_REJECTED = "PIT_INPUT_REJECTED"
    OFFICIAL_EVALUATION_REJECTED = "OFFICIAL_EVALUATION_REJECTED"
    FAMILY_BUDGET_EXHAUSTED = "FAMILY_BUDGET_EXHAUSTED"
    TRIAL_LEDGER_BLOCKED = "TRIAL_LEDGER_BLOCKED"
    NO_COMPLETED_RESEARCH_GROUP = "NO_COMPLETED_RESEARCH_GROUP"


@dataclass(frozen=True, kw_only=True)
class FrozenRowProvenance(CanonicalContract):
    """Immutable knowledge clocks and source lineage for one evaluated row.

    ``published_at`` may equal ``available_at`` only when the capture adapter
    explicitly records the conservative proxy policy below.  Legacy snapshots
    are never backfilled with invented clocks; they simply have no provenance
    rows and therefore cannot satisfy strict data capability checks.
    """

    symbol: str
    event_time: datetime
    published_at: datetime
    available_at: datetime
    revision_id: str
    source_kind: str
    source_label: str
    provider_class: str
    source_class: str
    input_origin: str
    captured_at: datetime
    source_record_hash: str
    adapter_manifest_hash: str
    calendar_hash: str
    universe_hash: str
    availability_policy_hash: str
    publication_time_policy: str
    schema_version: ClassVar[str] = SCHEMA_VERSION

    def __post_init__(self) -> None:
        require_identifier(self.symbol, "symbol")
        require_time_order(
            ("event_time", self.event_time),
            ("published_at", self.published_at),
            ("available_at", self.available_at),
            ("captured_at", self.captured_at),
        )
        require_identifier(self.revision_id, "revision_id")
        require_identifier(self.source_kind, "source_kind")
        for name in (
            "source_label",
            "provider_class",
            "source_class",
            "input_origin",
            "publication_time_policy",
        ):
            require_text(getattr(self, name), name)
        for name in (
            "source_record_hash",
            "adapter_manifest_hash",
            "calendar_hash",
            "universe_hash",
            "availability_policy_hash",
        ):
            require_hash(getattr(self, name), name)
        if self.source_label == "OFFICIAL_TUSHARE":
            fail(FailureCode.AUXILIARY_MASQUERADE, "proxy data cannot claim OFFICIAL_TUSHARE")
        if self.published_at == self.available_at and self.publication_time_policy != "AVAILABLE_AT_CONSERVATIVE_PROXY":
            fail(
                FailureCode.INVALID_VALUE,
                "equal publication and availability clocks require explicit conservative proxy policy",
            )

    @property
    def revision_key(self) -> tuple[str, datetime, str]:
        return (self.symbol, self.event_time, self.revision_id)


@dataclass(frozen=True, kw_only=True)
class FrozenValidationData(CanonicalContract):
    snapshot: MarketSnapshot
    labels: Mapping[str, float]
    qualification_hash: str
    source_label: str
    row_provenance: tuple[FrozenRowProvenance, ...] = ()
    schema_version: ClassVar[str] = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if not isinstance(self.snapshot, MarketSnapshot):
            fail(FailureCode.INVALID_VALUE, "snapshot must be a frozen MarketSnapshot")
        require_hash(self.qualification_hash, "qualification_hash")
        require_identifier(self.source_label, "source_label")
        if self.source_label == "OFFICIAL_TUSHARE":
            fail(FailureCode.AUXILIARY_MASQUERADE, "proxy data cannot claim OFFICIAL_TUSHARE")
        frozen = freeze_mapping(self.labels, "labels", numeric_values=True)
        object.__setattr__(self, "labels", frozen)
        if not isinstance(self.row_provenance, tuple):
            fail(FailureCode.INVALID_VALUE, "row_provenance must be a tuple")
        if any(not isinstance(item, FrozenRowProvenance) for item in self.row_provenance):
            fail(FailureCode.INVALID_VALUE, "row_provenance contains invalid items")
        ordered = tuple(sorted(
            self.row_provenance,
            key=lambda item: (item.event_time, item.symbol, item.available_at, item.revision_id),
        ))
        if ordered != self.row_provenance:
            fail(FailureCode.INVALID_VALUE, "row_provenance must be stable-sorted")
        if len({item.revision_key for item in ordered}) != len(ordered):
            fail(FailureCode.INVALID_VALUE, "row_provenance contains duplicate revisions")
        for item in ordered:
            if item.source_label != self.source_label:
                fail(FailureCode.INVALID_VALUE, "row source lineage does not match frozen source")
            if item.available_at > self.snapshot.decision_time:
                fail(FailureCode.INVALID_TIME_ORDER, "row is unavailable at snapshot decision_time")
        if ordered:
            expected = {(row.symbol, row.event_time) for row in self.snapshot.rows}
            observed = {(item.symbol, item.event_time) for item in ordered}
            if expected != observed:
                fail(FailureCode.MISSING_REQUIRED_FIELD, "row provenance must cover snapshot rows exactly")

    @property
    def labels_hash(self) -> str:
        return canonical_content_hash(dict(sorted(self.labels.items())))

    @property
    def strict_provenance_available(self) -> bool:
        """Whether this snapshot carries complete persisted source clocks."""
        return bool(self.row_provenance)


@dataclass(frozen=True, kw_only=True)
class CandidateTerminalRecord(CanonicalContract):
    group_id: str
    candidate_id: str
    candidate_draft_hash: str
    translation: CandidateTranslationResult
    validation: CandidateEvidenceValidationResult
    status: CandidateTerminalStatus
    disposition: ValidationDisposition
    failure_codes: tuple[str, ...]
    composite_score: float | None
    schema_version: ClassVar[str] = SCHEMA_VERSION

    def __post_init__(self) -> None:
        require_identifier(self.group_id, "group_id")
        require_identifier(self.candidate_id, "candidate_id")
        require_hash(self.candidate_draft_hash, "candidate_draft_hash")
        if not isinstance(self.status, CandidateTerminalStatus):
            fail(FailureCode.INVALID_VALUE, "candidate status must be terminal")
        if self.disposition is ValidationDisposition.CHALLENGER:
            fail(FailureCode.UNAUTHORIZED_CAPABILITY, "visible validation cannot create CHALLENGER")
        if self.composite_score is not None and not 0.0 <= self.composite_score <= 1.0:
            fail(FailureCode.INVALID_VALUE, "composite_score must lie in [0,1]")
        if self.disposition is ValidationDisposition.NOT_EVALUATED and not self.failure_codes:
            fail(FailureCode.MISSING_REQUIRED_FIELD, "not-evaluated terminal record needs typed failure")


@dataclass(frozen=True, kw_only=True)
class BestObservedResearchClue(CanonicalContract):
    candidate_draft_hash: str
    group_id: str
    composite_score: float
    eligible_for_selection: bool
    authority: str = "VISIBLE_RESEARCH_CLUE_ONLY"
    schema_version: ClassVar[str] = SCHEMA_VERSION

    def __post_init__(self) -> None:
        require_hash(self.candidate_draft_hash, "candidate_draft_hash")
        require_identifier(self.group_id, "group_id")
        if not 0.0 <= self.composite_score <= 1.0:
            fail(FailureCode.INVALID_VALUE, "composite_score must lie in [0,1]")
        if self.authority != "VISIBLE_RESEARCH_CLUE_ONLY":
            fail(FailureCode.UNAUTHORIZED_CAPABILITY, "best observed is not promotion")


@dataclass(frozen=True, kw_only=True)
class ResearchValidationPipelineResult(CanonicalContract):
    runtime_hash: str
    frozen_data_hash: str
    terminal_records: tuple[CandidateTerminalRecord, ...]
    tournament: CandidateTournamentResult
    best_observed: BestObservedResearchClue | None
    schema_version: ClassVar[str] = SCHEMA_VERSION

    def __post_init__(self) -> None:
        require_hash(self.runtime_hash, "runtime_hash")
        require_hash(self.frozen_data_hash, "frozen_data_hash")
        if not self.terminal_records:
            fail(FailureCode.INCOMPLETE_RESULT, "pipeline needs at least one submitted candidate")
        keys = {(item.group_id, item.candidate_draft_hash) for item in self.terminal_records}
        if len(keys) != len(self.terminal_records):
            fail(FailureCode.INVALID_VALUE, "duplicate candidate terminal record")
        if self.tournament.unvalidated_candidate_hashes:
            fail(FailureCode.INCOMPLETE_RESULT, "all submitted candidates must be terminal")

    @property
    def reproducibility_hash(self) -> str:
        """Stable financial-result identity, excluding replay transport status."""
        return canonical_content_hash({
            "runtime_hash": self.runtime_hash,
            "frozen_data_hash": self.frozen_data_hash,
            "terminal_records": tuple({
                "group_id": item.group_id,
                "candidate_id": item.candidate_id,
                "candidate_draft_hash": item.candidate_draft_hash,
                "translation_hash": item.translation.artifact_hash,
                "identity_hash": item.validation.result_ref.identity_hash,
                "evidence_hashes": item.validation.result_ref.evidence_hashes,
                "summary_hash": item.validation.result_ref.summary_hash,
                "disposition_hash": item.validation.disposition.artifact_hash if item.validation.disposition else None,
                "disposition": item.disposition.value,
                "failure_codes": item.failure_codes,
                "composite_score": item.composite_score,
            } for item in self.terminal_records),
            "tournament_hash": self.tournament.artifact_hash,
            "best_observed_hash": self.best_observed.artifact_hash if self.best_observed else None,
        })


@dataclass(frozen=True, kw_only=True)
class ResearchValidationPipelineRequest(CanonicalContract):
    campaign_hash: str
    runtime: GroupRuntimeResult
    frozen_data: FrozenValidationData
    requested_lookback_sessions: int | None
    family_budget_per_candidate: int = 1
    seed: int = 0
    schema_version: ClassVar[str] = SCHEMA_VERSION

    def __post_init__(self) -> None:
        require_hash(self.campaign_hash, "campaign_hash")
        if not isinstance(self.runtime, GroupRuntimeResult):
            fail(FailureCode.INVALID_VALUE, "runtime must be GroupRuntimeResult")
        if not isinstance(self.frozen_data, FrozenValidationData):
            fail(FailureCode.INVALID_VALUE, "frozen_data is required")
        if self.requested_lookback_sessions is not None and self.requested_lookback_sessions < 1:
            fail(FailureCode.INVALID_VALUE, "requested lookback must be positive")
        if self.family_budget_per_candidate < 1:
            fail(FailureCode.INVALID_VALUE, "family budget must be positive")
        if self.seed < 0:
            fail(FailureCode.INVALID_VALUE, "seed must be non-negative")
