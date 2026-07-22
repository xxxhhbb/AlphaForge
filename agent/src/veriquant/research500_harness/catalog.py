"""Strict parser for the 25-family by 20-dimension research catalog."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any, Mapping

from ..research_toolbox.contracts import canonical_hash
from ..research_toolbox.registry import SPECIALIST_TOOL_ALLOWLISTS, validate_required_data


CATALOG_SCHEMA = "alphaforge.factor-research-question-catalog/v1"
QUESTION_COUNT = 500
FAMILY_COUNT = 25
DIMENSION_COUNT = 20


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be non-empty text")
    return value


@dataclass(frozen=True, slots=True)
class CatalogQuestion:
    question_id: str
    family_id: str
    family_name: str
    test_id: str
    research_dimension: str
    question: str
    required_data: tuple[str, ...]
    lead_specialist: str
    experiment_type: str
    expected_result_kind: str
    current_repository_readiness: str

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "CatalogQuestion":
        required_data = validate_required_data(list(value.get("required_data", ())))
        if not required_data:
            raise ValueError("required_data cannot be empty")
        lead = _text(value.get("lead_specialist"), "lead_specialist")
        if lead not in SPECIALIST_TOOL_ALLOWLISTS:
            raise ValueError("lead_specialist is not registered")
        return cls(
            question_id=_text(value.get("question_id"), "question_id"),
            family_id=_text(value.get("family_id"), "family_id"),
            family_name=_text(value.get("family_name"), "family_name"),
            test_id=_text(value.get("test_id"), "test_id"),
            research_dimension=_text(value.get("research_dimension"), "research_dimension"),
            question=_text(value.get("question"), "question"),
            required_data=required_data,
            lead_specialist=lead,
            experiment_type=_text(value.get("experiment_type"), "experiment_type"),
            expected_result_kind=_text(value.get("expected_result_kind"), "expected_result_kind"),
            current_repository_readiness=_text(
                value.get("current_repository_readiness"), "current_repository_readiness"
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "question_id": self.question_id,
            "family_id": self.family_id,
            "family_name": self.family_name,
            "test_id": self.test_id,
            "research_dimension": self.research_dimension,
            "question": self.question,
            "required_data": list(self.required_data),
            "lead_specialist": self.lead_specialist,
            "experiment_type": self.experiment_type,
            "expected_result_kind": self.expected_result_kind,
            "current_repository_readiness": self.current_repository_readiness,
        }


@dataclass(frozen=True, slots=True)
class Research500Catalog:
    questions: tuple[CatalogQuestion, ...]
    catalog_hash: str

    @property
    def families(self) -> tuple[str, ...]:
        return tuple(sorted({item.family_id for item in self.questions}))

    @property
    def dimensions(self) -> tuple[str, ...]:
        return tuple(sorted({item.test_id for item in self.questions}))


def load_catalog(path: str | Path) -> Research500Catalog:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, Mapping) or raw.get("schema_version") != CATALOG_SCHEMA:
        raise ValueError("unsupported 500-question catalog schema")
    rows = raw.get("questions")
    if not isinstance(rows, list):
        raise ValueError("catalog questions must be an array")
    # The source catalog hash covers every source field, including descriptive
    # readiness metadata that is not copied into the executable contract.
    actual_hash = canonical_hash(rows)
    if raw.get("catalog_hash") != actual_hash:
        raise ValueError("catalog_hash does not match canonical question content")
    questions = tuple(CatalogQuestion.from_mapping(item) for item in rows if isinstance(item, Mapping))
    if len(questions) != len(rows):
        raise ValueError("every catalog question must be an object")
    if len(questions) != QUESTION_COUNT or raw.get("question_count") != QUESTION_COUNT:
        raise ValueError("catalog must contain exactly 500 questions")
    ids = tuple(item.question_id for item in questions)
    texts = tuple(item.question for item in questions)
    if len(set(ids)) != QUESTION_COUNT or len(set(texts)) != QUESTION_COUNT:
        raise ValueError("question ids and texts must be unique")
    families = {item.family_id for item in questions}
    dimensions = {item.test_id for item in questions}
    if len(families) != FAMILY_COUNT or raw.get("family_count") != FAMILY_COUNT:
        raise ValueError("catalog must contain exactly 25 families")
    if len(dimensions) != DIMENSION_COUNT or raw.get("dimension_count") != DIMENSION_COUNT:
        raise ValueError("catalog must contain exactly 20 dimensions")
    pairs = {(item.family_id, item.test_id) for item in questions}
    expected = {(family, dimension) for family in families for dimension in dimensions}
    if pairs != expected or len(pairs) != QUESTION_COUNT:
        raise ValueError("catalog must be a complete 25x20 family/dimension matrix")
    family_names: dict[str, str] = {}
    dimension_names: dict[str, str] = {}
    for item in questions:
        if family_names.setdefault(item.family_id, item.family_name) != item.family_name:
            raise ValueError("family name is inconsistent")
        if dimension_names.setdefault(item.test_id, item.research_dimension) != item.research_dimension:
            raise ValueError("research dimension name is inconsistent")
    return Research500Catalog(questions=questions, catalog_hash=actual_hash)


__all__ = ["CatalogQuestion", "Research500Catalog", "load_catalog"]
