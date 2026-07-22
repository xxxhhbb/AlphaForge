from __future__ import annotations

from dataclasses import dataclass

from ..qlib_expression.models import stable_hash


@dataclass(frozen=True, slots=True)
class SemanticTemplateResult:
    template_id: str
    operator: str
    expression: str
    status: str
    expression_hash: str | None
    failure_code: str | None

    def to_dict(self) -> dict[str, object]:
        return {
            "template_id": self.template_id,
            "operator": self.operator,
            "expression": self.expression,
            "status": self.status,
            "expression_hash": self.expression_hash,
            "failure_code": self.failure_code,
        }


@dataclass(frozen=True, slots=True)
class CapabilityHarnessReport:
    pinned_operator_count: int
    classified_operator_count: int
    trusted_operator_count: int
    excluded_operator_count: int
    semantic_template_count: int
    certified_template_count: int
    known_difference_template_count: int
    failed_template_count: int
    catalog_record_count: int | None
    catalog_classification_complete: bool | None
    templates: tuple[SemanticTemplateResult, ...]
    schema_version: str = "alphaforge.qlib-product-capability/v1"

    @property
    def report_hash(self) -> str:
        return stable_hash(self.to_dict(include_hash=False))

    @property
    def passed(self) -> bool:
        return (
            self.pinned_operator_count == self.classified_operator_count
            and self.semantic_template_count >= 150
            and self.failed_template_count == 0
            and self.catalog_classification_complete is True
        )

    def to_dict(self, *, include_hash: bool = True) -> dict[str, object]:
        payload: dict[str, object] = {
            "schema_version": self.schema_version,
            "status": "PASSED" if self.passed else "FAILED",
            "pinned_operator_count": self.pinned_operator_count,
            "classified_operator_count": self.classified_operator_count,
            "trusted_operator_count": self.trusted_operator_count,
            "excluded_operator_count": self.excluded_operator_count,
            "semantic_template_count": self.semantic_template_count,
            "certified_template_count": self.certified_template_count,
            "known_difference_template_count": self.known_difference_template_count,
            "failed_template_count": self.failed_template_count,
            "catalog_record_count": self.catalog_record_count,
            "catalog_classification_complete": self.catalog_classification_complete,
            "templates": [item.to_dict() for item in self.templates],
        }
        if include_hash:
            payload["report_hash"] = self.report_hash
        return payload
