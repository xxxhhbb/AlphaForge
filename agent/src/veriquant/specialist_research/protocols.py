"""Program-owned specialist protocol checks beyond generic schema validation."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import ClassVar, Mapping

from ..contracts import CanonicalContract, FailureCode
from ..contracts.base import fail, require_hash, require_identifier, require_text
from .models import SCHEMA_VERSION, ResearchExplorationPlan, SpecialistCandidateDraft
from .profiles import SPECIALIST_PROFILES


class DatasetAssessmentStatus(str, Enum):
    ELIGIBLE = "ELIGIBLE"
    AUXILIARY_ONLY = "AUXILIARY_ONLY"
    REJECTED = "REJECTED"


@dataclass(frozen=True)
class DatasetAssessment(CanonicalContract):
    dataset_id: str
    provenance_hash: str
    status: DatasetAssessmentStatus
    published_at_known: bool
    coverage_sufficient: bool
    entity_mapping_validated: bool
    pit_validated: bool
    findings: tuple[str, ...]
    SCHEMA_VERSION: ClassVar[str] = SCHEMA_VERSION

    def __post_init__(self) -> None:
        require_identifier(self.dataset_id, "dataset_id")
        require_hash(self.provenance_hash, "provenance_hash")
        if not isinstance(self.status, DatasetAssessmentStatus):
            fail(FailureCode.INVALID_VALUE, "dataset assessment status is invalid")
        for name in ("published_at_known", "coverage_sufficient", "entity_mapping_validated", "pit_validated"):
            if not isinstance(getattr(self, name), bool):
                fail(FailureCode.INVALID_VALUE, f"{name} must be boolean")
        if not self.findings:
            fail(FailureCode.MISSING_REQUIRED_FIELD, "dataset assessment requires findings")
        for item in self.findings:
            require_text(item, "findings")
        eligible = all((self.published_at_known, self.coverage_sufficient, self.entity_mapping_validated, self.pit_validated))
        if self.status is DatasetAssessmentStatus.ELIGIBLE and not eligible:
            fail(FailureCode.INVALID_STATE, "incomplete dataset assessment cannot be eligible")


class SpecialistProtocol:
    def __init__(self, specialist_id: str) -> None:
        if specialist_id not in SPECIALIST_PROFILES:
            fail(FailureCode.INVALID_IDENTIFIER, "unknown specialist protocol")
        self.specialist_id = specialist_id

    def validate_plan(self, plan: ResearchExplorationPlan) -> None:
        if plan.specialist_id != self.specialist_id:
            fail(FailureCode.INVALID_FIELD_OWNER, "plan belongs to another specialist protocol")
        if len(plan.directions) < 2:
            fail(FailureCode.INCOMPLETE_RESULT, "specialist plan must cover multiple directions")
        if plan.mechanism_ideas and len({item.family_id for item in plan.mechanism_ideas}) < 2:
            fail(FailureCode.INCOMPLETE_RESULT, "specialist mechanisms must cover multiple candidate families")
        if plan.candidate_family_plans and len(plan.candidate_family_plans) < 2:
            fail(FailureCode.INCOMPLETE_RESULT, "specialist plan must construct multiple candidate families")
        # The request-specific loop owns the actual family/combinations budget.
        # Do not impose a second hidden four-family ceiling here: it prevented
        # standard/strict campaigns from using their already-reserved budget.
        if len(plan.candidate_family_plans) > 64:
            fail(FailureCode.PROVIDER_BUDGET_EXCEEDED, "specialist plan exceeds the global family safety ceiling")

    def validate_candidate(self, candidate: SpecialistCandidateDraft) -> None:
        material = " ".join(candidate.availability_assumptions).lower()
        if self.specialist_id == "expectations_events":
            required = ("event_time", "published_at", "available_at", "first_tradable_time")
            missing = tuple(item for item in required if item not in material)
            if missing:
                fail(FailureCode.MISSING_REQUIRED_FIELD, "event candidate lacks four-time semantics", fields=missing)
        elif self.specialist_id == "fundamental":
            if "available_at" not in material:
                fail(FailureCode.MISSING_REQUIRED_FIELD, "fundamental candidate must state financial available_at")
            fundamental_fields = {
                "roe", "roa", "revenue", "net_income", "book_value", "cashflow",
                "operating_cashflow", "total_assets", "total_liabilities", "earnings",
                "eps", "pe", "pb", "book_to_market",
            }
            price_fields = {"open", "high", "low", "close", "volume", "vwap", "amount", "turnover"}
            candidate_fields = set(candidate.required_fields)
            fundamental_families = set(SPECIALIST_PROFILES["fundamental"].candidate_families)
            if (
                candidate.family_id in fundamental_families
                and candidate_fields
                and candidate_fields <= price_fields
                and not (candidate_fields & fundamental_fields)
            ):
                disclosure = " ".join((candidate.economic_mechanism, candidate.novelty_explanation)).lower()
                if not any(token in disclosure for token in ("cross-domain", "cross domain", "跨域", "price proxy", "价格代理")):
                    fail(
                        FailureCode.INVALID_FIELD_OWNER,
                        "fundamental price-only candidate must explicitly declare a cross-domain proxy",
                    )
        elif self.specialist_id == "macro_regime":
            forbidden = ("事后牛熊", "ex_post_bull_bear", "future_regime")
            if any(value in material for value in forbidden):
                fail(FailureCode.INVALID_TIME_ORDER, "macro regime uses an ex-post state definition")
        elif self.specialist_id == "meta_alpha" and any(value in material for value in ("sealed", "holdout", "test_rank")):
            fail(FailureCode.UNAUTHORIZED_CAPABILITY, "meta-alpha candidate references sealed evidence")

    def assess_alternative_dataset(
        self,
        *,
        dataset_id: str,
        provenance_hash: str,
        published_at_known: bool,
        coverage_sufficient: bool,
        entity_mapping_validated: bool,
        pit_validated: bool,
        findings: tuple[str, ...],
    ) -> DatasetAssessment:
        if self.specialist_id != "alternative_data":
            fail(FailureCode.UNAUTHORIZED_CAPABILITY, "only alternative-data protocol performs DatasetAssessment")
        complete = all((published_at_known, coverage_sufficient, entity_mapping_validated, pit_validated))
        status = DatasetAssessmentStatus.ELIGIBLE if complete else DatasetAssessmentStatus.AUXILIARY_ONLY
        return DatasetAssessment(
            dataset_id=dataset_id,
            provenance_hash=provenance_hash,
            status=status,
            published_at_known=published_at_known,
            coverage_sufficient=coverage_sufficient,
            entity_mapping_validated=entity_mapping_validated,
            pit_validated=pit_validated,
            findings=findings,
        )


SPECIALIST_PROTOCOLS: Mapping[str, SpecialistProtocol] = MappingProxyType(
    {specialist_id: SpecialistProtocol(specialist_id) for specialist_id in SPECIALIST_PROFILES}
)


def get_specialist_protocol(specialist_id: str) -> SpecialistProtocol:
    protocol = SPECIALIST_PROTOCOLS.get(specialist_id)
    if protocol is None:
        fail(FailureCode.INVALID_IDENTIFIER, "unknown specialist protocol")
    return protocol
