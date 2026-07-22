"""Fail-closed mechanical Level-1 coverage harness.

Level 1 proves only that every catalog row has a typed, authorized static route.
It never upgrades a question to COVERED_AND_TESTED and never treats fixture or
synthetic receipts as real operational evidence.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import tempfile
from typing import Any, Iterable, Mapping

from ..research_toolbox.contracts import (
    ExternalApprovalNeed,
    RegisteredContextReceipt,
    canonical_hash,
)
from ..research_toolbox.coverage import DataCoverageResolver
from ..research_toolbox.registry import (
    REQUIRED_DATA_TO_TOOL,
    SPECIALIST_TOOL_ALLOWLISTS,
)
from .catalog import CatalogQuestion, Research500Catalog, load_catalog


SCHEMA_VERSION = "alphaforge.research500-level1/v1"
QUESTION_STATUS = "COVERED_NOT_RUN"
NOT_COVERED = "NOT_COVERED"

_OUTPUT_BY_EXPERIMENT: Mapping[str, str] = {
    "ADVERSARIAL_FALSIFICATION": "SCORE_TABLE",
    "CAPACITY_STRESS": "SCORE_TABLE",
    "COST_STRESS": "SCORE_TABLE",
    "CROSS_SECTIONAL_VALIDATION": "SCORE_TABLE",
    "DIAGNOSTIC_MONITORING": "DIAGNOSTIC_TABLE",
    "LIQUIDITY_CONDITIONING": "REGIME_COMPARISON_TABLE",
    "NEUTRALIZED_VALIDATION": "SCORE_TABLE",
    "NONLINEAR_DIAGNOSTIC": "REGRESSION_TABLE",
    "OUTLIER_ROBUSTNESS": "SCORE_TABLE",
    "PARAMETER_ROBUSTNESS": "REGIME_COMPARISON_TABLE",
    "PIT_DELAY_TEST": "SCORE_TABLE",
    "QUANTILE_MONOTONICITY": "SCORE_TABLE",
    "REGIME_CONDITIONING": "REGIME_COMPARISON_TABLE",
    "REGISTERED_CORRELATION": "REGRESSION_TABLE",
    "RESIDUAL_INCREMENTAL": "REGRESSION_TABLE",
    "SUBPERIOD_STABILITY": "REGIME_COMPARISON_TABLE",
    "TURNOVER_OPTIMIZATION": "SCORE_TABLE",
    "UNIVERSE_ROBUSTNESS": "REGIME_COMPARISON_TABLE",
}

_REPORT_BY_RESULT: Mapping[str, str] = {
    "DIAGNOSTIC_REPORT": "CUSTOMER_DIAGNOSTIC_REPORT",
    "PORTFOLIO_GAP_OR_INCREMENTAL_REPORT": "CUSTOMER_INCREMENTAL_REPORT",
    "VALIDATION_CONCLUSION": "CUSTOMER_VALIDATION_REPORT",
}


@dataclass(frozen=True, slots=True)
class CoverageInputs:
    capability_report: object | None = None
    gateway_manifest: object | None = None
    auxiliary_receipts: tuple[object, ...] = ()
    registered_context_receipts: tuple[RegisteredContextReceipt, ...] = ()
    external_approval_needs: tuple[ExternalApprovalNeed, ...] = ()

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "CoverageInputs":
        registered = tuple(
            RegisteredContextReceipt(
                data_id=str(item["data_id"]),
                content_hash=str(item["content_hash"]),
                status=str(item.get("status", "READY")),
            )
            for item in raw.get("registered_context_receipts", ())
        )
        approval = tuple(
            ExternalApprovalNeed(data_id=str(item["data_id"]), reason_code=str(item["reason_code"]))
            for item in raw.get("external_approval_needs", ())
        )
        return cls(
            capability_report=raw.get("capability_report"),
            gateway_manifest=raw.get("gateway_manifest"),
            auxiliary_receipts=tuple(raw.get("auxiliary_receipts", ())),
            registered_context_receipts=registered,
            external_approval_needs=approval,
        )


class Research500Level1Harness:
    def __init__(self, *, resolver: DataCoverageResolver | None = None) -> None:
        self._resolver = resolver or DataCoverageResolver()

    def run(
        self,
        *,
        catalog_path: str | Path,
        output_dir: str | Path,
        coverage_inputs: CoverageInputs | None = None,
    ) -> dict[str, Any]:
        catalog = load_catalog(catalog_path)
        inputs = coverage_inputs or CoverageInputs()
        capabilities = [self._capability(item) for item in catalog.questions]
        coverage = [self._coverage(item, inputs) for item in catalog.questions]
        coverage_by_id = {item["question_id"]: item for item in coverage}
        results = [self._question_result(item, capability, coverage_by_id[item.question_id])
                   for item, capability in zip(catalog.questions, capabilities)]
        family_canaries = self._canaries(catalog, key="family_id")
        dimension_canaries = self._canaries(catalog, key="test_id")

        root = Path(output_dir)
        root.mkdir(parents=True, exist_ok=True)
        documents: dict[str, Any] = {
            "capability_catalog.json": self._document(
                "capability-catalog", catalog, capabilities
            ),
            "data_coverage_matrix.json": self._document(
                "data-coverage-matrix", catalog, coverage
            ),
            "family_canaries.json": self._document(
                "family-canaries", catalog, family_canaries
            ),
            "dimension_canaries.json": self._document(
                "dimension-canaries", catalog, dimension_canaries
            ),
        }
        artifact_hashes: dict[str, str] = {}
        for name, document in documents.items():
            _atomic_json(root / name, document)
            artifact_hashes[name] = document["artifact_hash"]
        jsonl_hash = _atomic_jsonl(root / "question_results.jsonl", results)
        artifact_hashes["question_results.jsonl"] = jsonl_hash

        all_static = all(item["level1_status"] == QUESTION_STATUS for item in results)
        all_families_run = all(item["status"] == "REAL_DATA_TESTED" for item in family_canaries)
        all_dimensions_run = all(item["status"] == "REAL_DATA_TESTED" for item in dimension_canaries)
        reasons = []
        if not all_static:
            reasons.append("STATIC_COVERAGE_INCOMPLETE")
        if not all_families_run:
            reasons.append("REAL_FAMILY_CANARIES_NOT_RUN")
        if not all_dimensions_run:
            reasons.append("REAL_DIMENSION_CANARIES_NOT_RUN")
        # Level 1 is intentionally unable to claim end-to-end acceptance.
        verdict = {
            "schema_version": SCHEMA_VERSION,
            "artifact_type": "final-verdict",
            "catalog_hash": catalog.catalog_hash,
            "status": "NOT_PASSED",
            "level1_static_status": "PASSED" if all_static else "FAILED",
            "question_status_counts": _counts(item["level1_status"] for item in results),
            "data_status_counts": _counts(item["data_coverage_status"] for item in results),
            "family_canary_status_counts": _counts(item["status"] for item in family_canaries),
            "dimension_canary_status_counts": _counts(item["status"] for item in dimension_canaries),
            "failure_reasons": sorted(set(reasons or ["LEVEL1_CANNOT_ASSERT_FULL_ACCEPTANCE"])),
            "artifact_hashes": dict(sorted(artifact_hashes.items())),
            "forbidden_claim": "COVERED_AND_TESTED",
        }
        verdict["artifact_hash"] = canonical_hash(verdict)
        _atomic_json(root / "final_verdict.json", verdict)
        return verdict

    def _capability(self, question: CatalogQuestion) -> dict[str, Any]:
        if question.experiment_type not in _OUTPUT_BY_EXPERIMENT:
            return self._invalid_capability(question, "EXPERIMENT_TYPE_UNMAPPED")
        if question.expected_result_kind not in _REPORT_BY_RESULT:
            return self._invalid_capability(question, "REPORT_TYPE_UNMAPPED")
        required_tools = tuple(sorted({REQUIRED_DATA_TO_TOOL[item] for item in question.required_data}))
        route = _route(question.lead_specialist, required_tools)
        if route is None:
            return self._invalid_capability(question, "AUTHORIZED_TOOL_ROUTE_UNAVAILABLE")
        assignments = {
            specialist: sorted(set(required_tools) & set(SPECIALIST_TOOL_ALLOWLISTS[specialist]))
            for specialist in route
        }
        payload = {
            "question_id": question.question_id,
            "family_id": question.family_id,
            "test_id": question.test_id,
            "required_data": list(question.required_data),
            "required_tools": list(required_tools),
            "lead_specialist": question.lead_specialist,
            "specialist_route": list(route),
            "tool_assignments": assignments,
            "experiment_type": question.experiment_type,
            "output_kind": _OUTPUT_BY_EXPERIMENT[question.experiment_type],
            "result_kind": question.expected_result_kind,
            "report_type": _REPORT_BY_RESULT[question.expected_result_kind],
            "static_status": "STATIC_COVERED",
            "failure_codes": [],
        }
        payload["capability_hash"] = canonical_hash(payload)
        return payload

    @staticmethod
    def _invalid_capability(question: CatalogQuestion, code: str) -> dict[str, Any]:
        payload = {
            "question_id": question.question_id,
            "family_id": question.family_id,
            "test_id": question.test_id,
            "static_status": "STATIC_NOT_COVERED",
            "failure_codes": [code],
        }
        payload["capability_hash"] = canonical_hash(payload)
        return payload

    def _coverage(self, question: CatalogQuestion, inputs: CoverageInputs) -> dict[str, Any]:
        decision = self._resolver.resolve(
            required_data=question.required_data,
            capability_report=inputs.capability_report,
            gateway_manifest=inputs.gateway_manifest,
            auxiliary_receipts=inputs.auxiliary_receipts,
            registered_context_receipts=inputs.registered_context_receipts,
            external_approval_needs=inputs.external_approval_needs,
        )
        return {
            "question_id": question.question_id,
            "family_id": question.family_id,
            "test_id": question.test_id,
            **decision.to_dict(),
            "coverage_hash": decision.artifact_hash,
        }

    @staticmethod
    def _question_result(
        question: CatalogQuestion, capability: Mapping[str, Any], coverage: Mapping[str, Any]
    ) -> dict[str, Any]:
        static_covered = capability.get("static_status") == "STATIC_COVERED"
        row = {
            "schema_version": SCHEMA_VERSION,
            "question_id": question.question_id,
            "family_id": question.family_id,
            "test_id": question.test_id,
            "level1_status": QUESTION_STATUS if static_covered else NOT_COVERED,
            "data_coverage_status": coverage["status"],
            "capability_hash": capability["capability_hash"],
            "coverage_hash": coverage["coverage_hash"],
            "experiment_run_status": "NOT_RUN",
            "report_run_status": "NOT_RUN",
            "is_real_data_tested": False,
        }
        row["result_hash"] = canonical_hash(row)
        return row

    @staticmethod
    def _canaries(catalog: Research500Catalog, *, key: str) -> list[dict[str, Any]]:
        values = catalog.families if key == "family_id" else catalog.dimensions
        output = []
        for value in values:
            question_ids = sorted(
                item.question_id for item in catalog.questions if getattr(item, key) == value
            )
            row = {
                key: value,
                "question_ids": question_ids,
                "status": "NOT_RUN",
                "data_provenance": "NONE",
                "evidence_hashes": [],
                "test_hashes": [],
            }
            row["canary_hash"] = canonical_hash(row)
            output.append(row)
        return output

    @staticmethod
    def _document(kind: str, catalog: Research500Catalog, rows: list[dict[str, Any]]) -> dict[str, Any]:
        value = {
            "schema_version": SCHEMA_VERSION,
            "artifact_type": kind,
            "catalog_hash": catalog.catalog_hash,
            "row_count": len(rows),
            "rows": rows,
        }
        value["artifact_hash"] = canonical_hash(value)
        return value


def _route(lead: str, required_tools: tuple[str, ...]) -> tuple[str, ...] | None:
    route = [lead]
    missing = set(required_tools) - set(SPECIALIST_TOOL_ALLOWLISTS[lead])
    while missing:
        candidates = []
        for specialist, tools in SPECIALIST_TOOL_ALLOWLISTS.items():
            if specialist in route:
                continue
            covered = missing & set(tools)
            if covered:
                candidates.append((-len(covered), specialist, covered))
        if not candidates:
            return None
        _, specialist, covered = sorted(candidates, key=lambda item: (item[0], item[1]))[0]
        route.append(specialist)
        missing -= covered
    return tuple(route)


def _counts(values: Iterable[str]) -> dict[str, int]:
    output: dict[str, int] = {}
    for value in values:
        output[value] = output.get(value, 0) + 1
    return dict(sorted(output.items()))


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    _atomic_bytes(path, (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8"))


def _atomic_jsonl(path: Path, rows: list[dict[str, Any]]) -> str:
    data = "".join(json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n" for row in rows).encode("utf-8")
    _atomic_bytes(path, data)
    return canonical_hash(rows)


def _atomic_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


__all__ = ["CoverageInputs", "Research500Level1Harness"]
