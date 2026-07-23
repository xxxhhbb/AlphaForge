"""Versioned, deterministic answer contracts for AlphaForge research runs.

These contracts organize already-recorded research and Evidence facts.  They
do not calculate financial metrics and own no Evidence or Promotion authority.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import re
from typing import Any

from ..agent_research_sandbox.process_runner import canonical_hash


SCHEMA_VERSION = "alphaforge.research-answer/v3"
_HASH = re.compile(r"sha256:[0-9a-f]{64}")
_FORBIDDEN = re.compile(
    r"(?i)(?:bearer\s+\S+|authorization\s*:|api[_-]?key\s*[:=]|token\s*[:=]|"
    r"sealed(?:_|\s)*(?:metric|data|value)|\btarget_weights?\b|\borders?\b|"
    r"\bbroker_credentials?\b)"
)


class AnswerStatus(str, Enum):
    ANSWERED = "ANSWERED"
    CONDITIONALLY_ANSWERED = "CONDITIONALLY_ANSWERED"
    NEGATIVE_ANSWER = "NEGATIVE_ANSWER"
    PARTIALLY_ANSWERED = "PARTIALLY_ANSWERED"
    UNANSWERABLE_WITH_REASON = "UNANSWERABLE_WITH_REASON"


class AnswerResearchVerdict(str, Enum):
    SUPPORTED = "SUPPORTED"
    CONDITIONALLY_SUPPORTED = "CONDITIONALLY_SUPPORTED"
    REJECTED = "REJECTED"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
    DATA_UNAVAILABLE = "DATA_UNAVAILABLE"


class CandidateDisposition(str, Enum):
    RETAIN = "RETAIN"
    RETAIN_WITH_CONDITIONS = "RETAIN_WITH_CONDITIONS"
    REVISE = "REVISE"
    REJECT = "REJECT"
    NOT_EVALUATED = "NOT_EVALUATED"


class EvidenceAuthority(str, Enum):
    NONE = "NONE"
    AUXILIARY_ONLY = "AUXILIARY_ONLY"
    OFFICIAL_EVIDENCE = "OFFICIAL_EVIDENCE"
    MIXED = "MIXED"


def _text(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be non-empty text")
    value = value.strip()
    if _FORBIDDEN.search(value):
        raise ValueError(f"{name} contains privileged or secret material")
    return value


def _hash(value: str, name: str) -> str:
    if not isinstance(value, str) or _HASH.fullmatch(value) is None:
        raise ValueError(f"{name} must be sha256")
    return value


def _stable(values: tuple[str, ...], name: str, *, hashes: bool = False) -> None:
    if tuple(sorted(set(values))) != values:
        raise ValueError(f"{name} must be unique and stable-sorted")
    for value in values:
        _hash(value, name) if hashes else _text(value, name)


@dataclass(frozen=True, slots=True)
class QuestionRequirement:
    requirement_id: str
    question_text: str
    # Compatibility hint for older receipts only.  Question requirements are
    # defined by semantics, never by one mandatory result-table type.
    output_kind: str | None
    dimensions: tuple[str, ...]
    optional: bool = False
    required_metrics: tuple[str, ...] = ()
    depends_on: tuple[str, ...] = ()
    comparison_required: bool = False
    minimum_distinct_candidates: int = 1
    semantic_match: str = "ALL"

    def __post_init__(self) -> None:
        _text(self.requirement_id, "requirement_id")
        _text(self.question_text, "question_text")
        if self.output_kind is not None:
            _text(self.output_kind, "output_kind")
        if not self.dimensions:
            raise ValueError("dimensions cannot be empty")
        _stable(self.dimensions, "dimensions")
        _stable(self.required_metrics, "required_metrics")
        _stable(self.depends_on, "depends_on")
        if isinstance(self.minimum_distinct_candidates, bool) or not 1 <= self.minimum_distinct_candidates <= 50:
            raise ValueError("minimum_distinct_candidates must be 1..50")
        if self.comparison_required and self.minimum_distinct_candidates < 2:
            raise ValueError("comparison requires at least two distinct candidates")
        if self.semantic_match not in {"ALL", "ANY"}:
            raise ValueError("semantic_match must be ALL or ANY")

    def to_dict(self) -> dict[str, Any]:
        return {
            "requirement_id": self.requirement_id,
            "question_text": self.question_text,
            "output_kind": self.output_kind,
            "dimensions": list(self.dimensions),
            "optional": self.optional,
            "required_metrics": list(self.required_metrics),
            "depends_on": list(self.depends_on),
            "comparison_required": self.comparison_required,
            "minimum_distinct_candidates": self.minimum_distinct_candidates,
            "semantic_match": self.semantic_match,
        }


@dataclass(frozen=True, slots=True)
class ResearchAnswerSpec:
    question_id: str
    question: str
    question_hash: str
    requirements: tuple[QuestionRequirement, ...]
    task_hashes: tuple[str, ...]
    rule_version: str = "alphaforge.answer-spec/v2"

    def __post_init__(self) -> None:
        _text(self.question_id, "question_id")
        _text(self.question, "question")
        _hash(self.question_hash, "question_hash")
        if canonical_hash({"question": self.question}) != self.question_hash:
            raise ValueError("question_hash does not bind question")
        if not self.requirements:
            raise ValueError("answer spec requires at least one requirement")
        ids = tuple(item.requirement_id for item in self.requirements)
        if ids != tuple(sorted(set(ids))):
            raise ValueError("requirements must be unique and stable-sorted")
        known = set(ids)
        for item in self.requirements:
            if not set(item.depends_on).issubset(known):
                raise ValueError("requirement dependency is absent from answer spec")
            if item.requirement_id in item.depends_on:
                raise ValueError("requirement cannot depend on itself")
        _stable(self.task_hashes, "task_hashes", hashes=True)

    def to_dict(self, *, include_hash: bool = True) -> dict[str, Any]:
        value = {
            "schema_version": self.rule_version,
            "question_id": self.question_id,
            "question": self.question,
            "question_hash": self.question_hash,
            "requirements": [item.to_dict() for item in self.requirements],
            "task_hashes": list(self.task_hashes),
        }
        if include_hash:
            value["artifact_hash"] = canonical_hash(value)
        return value

    @property
    def artifact_hash(self) -> str:
        return str(self.to_dict()["artifact_hash"])


@dataclass(frozen=True, slots=True)
class CandidateDefinition:
    candidate_id: str
    specialist_id: str
    source_hash: str
    display_name: str
    hypothesis: str
    method_summary: str
    output_kind: str
    data_snapshot_hash: str
    result_role: str = "ALPHA_SIGNAL"
    implementation_summary: str | None = None
    input_fields: tuple[str, ...] = ()
    design_warnings: tuple[str, ...] = ()
    design_advisories: tuple[str, ...] = ()
    revision_of_source_hash: str | None = None
    revision_reason_codes: tuple[str, ...] = ()
    plan_hash: str | None = None
    evidence_hashes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name in (
            "candidate_id", "specialist_id", "display_name", "hypothesis",
            "method_summary", "output_kind", "result_role",
        ):
            _text(getattr(self, name), name)
        if self.result_role not in {
            "ALPHA_SIGNAL", "STATISTICAL_TEST", "CONDITION_ANALYSIS",
            "EVENT_STUDY", "DIAGNOSTIC", "DATA_ASSESSMENT",
        }:
            raise ValueError("unsupported candidate result_role")
        if self.implementation_summary is not None:
            _text(self.implementation_summary, "implementation_summary")
        _stable(self.input_fields, "input_fields")
        _stable(self.design_warnings, "design_warnings")
        _stable(self.design_advisories, "design_advisories")
        _stable(self.revision_reason_codes, "revision_reason_codes")
        _hash(self.source_hash, "source_hash")
        _hash(self.data_snapshot_hash, "data_snapshot_hash")
        if self.plan_hash is not None:
            _hash(self.plan_hash, "plan_hash")
        if self.revision_of_source_hash is not None:
            _hash(self.revision_of_source_hash, "revision_of_source_hash")
        _stable(self.evidence_hashes, "evidence_hashes", hashes=True)

    def to_dict(self, *, include_hash: bool = True) -> dict[str, Any]:
        value = {
            "candidate_id": self.candidate_id,
            "specialist_id": self.specialist_id,
            "source_hash": self.source_hash,
            "display_name": self.display_name,
            "hypothesis": self.hypothesis,
            "method_summary": self.method_summary,
            "output_kind": self.output_kind,
            "result_role": self.result_role,
            "implementation_summary": self.implementation_summary,
            "input_fields": list(self.input_fields),
            "design_warnings": list(self.design_warnings),
            "design_advisories": list(self.design_advisories),
            "revision_of_source_hash": self.revision_of_source_hash,
            "revision_reason_codes": list(self.revision_reason_codes),
            "data_snapshot_hash": self.data_snapshot_hash,
            "plan_hash": self.plan_hash,
            "evidence_hashes": list(self.evidence_hashes),
        }
        if include_hash:
            value["artifact_hash"] = canonical_hash(value)
        return value

    @property
    def artifact_hash(self) -> str:
        return str(self.to_dict()["artifact_hash"])


@dataclass(frozen=True, slots=True)
class EvidenceBoundFact:
    text: str
    evidence_hash: str
    authority: EvidenceAuthority

    def __post_init__(self) -> None:
        _text(self.text, "fact text")
        _hash(self.evidence_hash, "fact evidence_hash")
        if self.authority is EvidenceAuthority.NONE:
            raise ValueError("an evidence-bound fact cannot have NONE authority")

    def to_dict(self) -> dict[str, str]:
        return {
            "text": self.text,
            "evidence_hash": self.evidence_hash,
            "authority": self.authority.value,
        }


@dataclass(frozen=True, slots=True)
class QuestionPartAnswer:
    requirement_id: str
    answer_status: AnswerStatus
    direct_answer: str
    judgment: str
    evidence_authority: EvidenceAuthority
    candidate_ids: tuple[str, ...]
    supporting_facts: tuple[EvidenceBoundFact, ...] = ()
    opposing_facts: tuple[EvidenceBoundFact, ...] = ()
    limitations: tuple[str, ...] = ()
    unanswered_reason: str | None = None
    recovery_requirement: str | None = None
    question_text: str | None = None

    def __post_init__(self) -> None:
        _text(self.requirement_id, "requirement_id")
        _text(self.direct_answer, "direct_answer")
        _text(self.judgment, "judgment")
        if self.question_text is not None:
            _text(self.question_text, "question_text")
        _stable(self.candidate_ids, "candidate_ids")
        _stable(self.limitations, "limitations")
        has_evidence = bool(self.supporting_facts or self.opposing_facts)
        if self.answer_status in {
            AnswerStatus.ANSWERED,
            AnswerStatus.CONDITIONALLY_ANSWERED,
            AnswerStatus.NEGATIVE_ANSWER,
        } and not has_evidence:
            raise ValueError("answered question part requires Evidence-bound facts")
        if self.evidence_authority is EvidenceAuthority.NONE and has_evidence:
            raise ValueError("Evidence facts require a non-NONE authority")
        if self.answer_status is AnswerStatus.UNANSWERABLE_WITH_REASON:
            _text(self.unanswered_reason or "", "unanswered_reason")
            _text(self.recovery_requirement or "", "recovery_requirement")

    def to_dict(self, *, include_hash: bool = True) -> dict[str, Any]:
        value = {
            "requirement_id": self.requirement_id,
            "answer_status": self.answer_status.value,
            "direct_answer": self.direct_answer,
            "judgment": self.judgment,
            "evidence_authority": self.evidence_authority.value,
            "candidate_ids": list(self.candidate_ids),
            "supporting_facts": [item.to_dict() for item in self.supporting_facts],
            "opposing_facts": [item.to_dict() for item in self.opposing_facts],
            "limitations": list(self.limitations),
            "unanswered_reason": self.unanswered_reason,
            "recovery_requirement": self.recovery_requirement,
            "question_text": self.question_text,
        }
        if include_hash:
            value["artifact_hash"] = canonical_hash(value)
        return value


@dataclass(frozen=True, slots=True)
class CandidateDecision:
    candidate_id: str
    disposition: CandidateDisposition
    reason_codes: tuple[str, ...]
    evidence_hashes: tuple[str, ...]
    unresolved_requirements: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _text(self.candidate_id, "candidate_id")
        if not self.reason_codes:
            raise ValueError("candidate decision requires reason codes")
        _stable(self.reason_codes, "reason_codes")
        _stable(self.evidence_hashes, "evidence_hashes", hashes=True)
        _stable(self.unresolved_requirements, "unresolved_requirements")

    def to_dict(self, *, include_hash: bool = True) -> dict[str, Any]:
        value = {
            "candidate_id": self.candidate_id,
            "disposition": self.disposition.value,
            "reason_codes": list(self.reason_codes),
            "evidence_hashes": list(self.evidence_hashes),
            "unresolved_requirements": list(self.unresolved_requirements),
        }
        if include_hash:
            value["artifact_hash"] = canonical_hash(value)
        return value


@dataclass(frozen=True, slots=True)
class ResearchAnswer:
    run_id: str
    question_id: str
    question_hash: str
    answer_spec_hash: str
    answer_status: AnswerStatus
    research_verdict: AnswerResearchVerdict
    direct_answer: str
    evidence_authority: EvidenceAuthority
    question_parts: tuple[QuestionPartAnswer, ...]
    candidate_definitions: tuple[CandidateDefinition, ...]
    candidate_decisions: tuple[CandidateDecision, ...]
    conflicts: tuple[str, ...]
    tested_scope: tuple[str, ...]
    untested_scope: tuple[str, ...]
    limitations: tuple[str, ...]
    next_experiment: str
    source_artifact_hashes: tuple[str, ...]
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        _text(self.run_id, "run_id")
        _text(self.question_id, "question_id")
        _hash(self.question_hash, "question_hash")
        _hash(self.answer_spec_hash, "answer_spec_hash")
        _text(self.direct_answer, "direct_answer")
        _text(self.next_experiment, "next_experiment")
        ids = tuple(item.requirement_id for item in self.question_parts)
        if not ids or ids != tuple(sorted(set(ids))):
            raise ValueError("question_parts must be non-empty, unique, and stable-sorted")
        candidate_ids = tuple(item.candidate_id for item in self.candidate_definitions)
        if candidate_ids != tuple(sorted(set(candidate_ids))):
            raise ValueError("candidate_definitions must be unique and stable-sorted")
        decision_ids = tuple(item.candidate_id for item in self.candidate_decisions)
        if decision_ids != candidate_ids:
            raise ValueError("candidate decisions must cover every candidate")
        for name in ("conflicts", "tested_scope", "untested_scope", "limitations"):
            _stable(getattr(self, name), name)
        _stable(self.source_artifact_hashes, "source_artifact_hashes", hashes=True)
        if self.answer_status is AnswerStatus.UNANSWERABLE_WITH_REASON and not any(
            item.answer_status is AnswerStatus.UNANSWERABLE_WITH_REASON for item in self.question_parts
        ):
            raise ValueError("unanswerable result requires an unanswerable question part")

    def to_dict(self, *, include_hash: bool = True) -> dict[str, Any]:
        value = {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "question_id": self.question_id,
            "question_hash": self.question_hash,
            "answer_spec_hash": self.answer_spec_hash,
            "answer_status": self.answer_status.value,
            "research_verdict": self.research_verdict.value,
            "direct_answer": self.direct_answer,
            "evidence_authority": self.evidence_authority.value,
            "question_parts": [item.to_dict() for item in self.question_parts],
            "candidate_definitions": [item.to_dict() for item in self.candidate_definitions],
            "candidate_decisions": [item.to_dict() for item in self.candidate_decisions],
            "conflicts": list(self.conflicts),
            "tested_scope": list(self.tested_scope),
            "untested_scope": list(self.untested_scope),
            "limitations": list(self.limitations),
            "next_experiment": self.next_experiment,
            "source_artifact_hashes": list(self.source_artifact_hashes),
        }
        if include_hash:
            value["artifact_hash"] = canonical_hash(value)
        return value

    @property
    def artifact_hash(self) -> str:
        return str(self.to_dict()["artifact_hash"])


__all__ = [
    "AnswerResearchVerdict", "AnswerStatus", "CandidateDecision",
    "CandidateDefinition", "CandidateDisposition", "EvidenceAuthority",
    "EvidenceBoundFact", "QuestionPartAnswer", "QuestionRequirement",
    "ResearchAnswer", "ResearchAnswerSpec", "SCHEMA_VERSION",
]
