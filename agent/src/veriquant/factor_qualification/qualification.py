"""Pure-static qualification of legacy factor-library seeds.

The service reads source bytes and literal catalog metadata only.  It does not
import legacy modules, call compute functions, run factor files, or use dynamic
execution.  Official evaluation remains with the existing FactorDAG/PIT/Evidence
services.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Mapping

from veriquant.data import FieldRole
from veriquant.factor_library import (
    CatalogBuildError,
    FactorLibraryCatalogBuilder,
    FactorLibraryRecord,
    build_factor_library_catalog,
)
from veriquant.factors import FACTOR_DAG_SCHEMA_VERSION, FactorOperator, parse_compile_factor_dag
from veriquant.research_tool_contracts import content_hash


MAX_QUALIFIED_TEMPLATES = 24
QUALIFICATION_SCHEMA_VERSION = "alphaforge.factor-qualification/v1"

_WINDOW_RE = re.compile(r"(\d+)$")
_UNSAFE_SOURCE_MARKERS = ("subprocess", "__import__", "os.system", "factor.py", "importlib")
_UNSAFE_CALL_MARKERS = ("exec(", "eval(")
_CONDITIONAL_MARKERS = ("where(", "if ", "if(", "case_when", "conditional", "np.where")
_MISSING_OPERATOR_HINTS = {
    "ts_argmax": "ts_argmax",
    "ts_argmin": "ts_argmin",
    "signed_power": "signed_power",
    "decay_linear": "decay_linear",
    "correlation": "correlation",
    "covariance": "covariance",
    "ts_rank": "ts_rank",
    "argmax": "argmax",
    "argmin": "argmin",
    "rank(" : "time_series_rank",
}


class FactorCompatibilityStatus(str, Enum):
    DIRECT_DAG = "DIRECT_DAG"
    NEEDS_DETERMINISTIC_OPERATOR = "NEEDS_DETERMINISTIC_OPERATOR"
    AUXILIARY_ONLY = "AUXILIARY_ONLY"
    REJECTED_SOURCE = "REJECTED_SOURCE"


def _read_source(root: Path, record: FactorLibraryRecord) -> str:
    source = (root / record.source_relpath).resolve(strict=True)
    root_resolved = root.resolve(strict=True)
    try:
        source.relative_to(root_resolved)
    except ValueError as exc:
        raise CatalogBuildError(record.factor_id, "source path escapes root") from exc
    return source.read_text(encoding="utf-8")


def _field_roles(fields: tuple[str, ...]) -> Mapping[str, FieldRole]:
    return {field: FieldRole.FEATURE for field in fields}


def _node(node_id: str, op: str, **kwargs: Any) -> dict[str, Any]:
    return {"id": node_id, "op": op, **kwargs}


def _single_field_template(record: FactorLibraryRecord) -> dict[str, Any] | None:
    if len(record.fields) != 1:
        return None
    field = record.fields[0]
    factor_id = record.factor_id
    nodes: list[dict[str, Any]]
    output = "score"
    if factor_id.startswith("fund_"):
        nodes = [
            _node("base", FactorOperator.FIELD.value, field=field),
            _node(output, FactorOperator.ZSCORE_CROSS_SECTION.value, inputs=["base"]),
        ]
    elif factor_id.startswith("qlib158_roc"):
        window = _window(factor_id)
        nodes = [
            _node("base", FactorOperator.FIELD.value, field=field),
            _node(output, FactorOperator.PCT_CHANGE.value, inputs=["base"], periods=window),
        ]
    elif factor_id.startswith("qlib158_std") or factor_id.startswith("qlib158_vstd"):
        window = _window(factor_id)
        nodes = [
            _node("base", FactorOperator.FIELD.value, field=field),
            _node(output, FactorOperator.ROLLING_STD.value, inputs=["base"], window=window),
        ]
    elif factor_id.startswith("qlib158_ma") or factor_id.startswith("qlib158_vma"):
        window = _window(factor_id)
        nodes = [
            _node("base", FactorOperator.FIELD.value, field=field),
            _node("mean", FactorOperator.ROLLING_MEAN.value, inputs=["base"], window=window),
            _node(output, FactorOperator.DIVIDE_SAFE.value, inputs=["mean", "base"], epsilon=1e-12),
        ]
    else:
        return None
    payload = {
        "schema_version": FACTOR_DAG_SCHEMA_VERSION,
        "dag_id": f"library.{factor_id}",
        "nodes": nodes,
        "output_node": output,
    }
    parse_compile_factor_dag(payload, field_roles=_field_roles(record.fields))
    return payload


def _window(factor_id: str) -> int:
    match = _WINDOW_RE.search(factor_id)
    return int(match.group(1)) if match else 1


def _missing_operator_hints(source_text: str) -> tuple[str, ...]:
    lowered = source_text.lower()
    return tuple(sorted({label for marker, label in _MISSING_OPERATOR_HINTS.items() if marker in lowered}))


@dataclass(frozen=True, kw_only=True)
class FactorComplexityLint:
    ast_nodes: int
    source_bytes: int
    required_field_count: int
    window_max: int
    has_conditionals: bool
    has_unsafe_execution_marker: bool

    @property
    def artifact_hash(self) -> str:
        return content_hash(self)


@dataclass(frozen=True, kw_only=True)
class FactorSimilarityLint:
    family: str
    duplicate_family_count: int
    source_token_count: int

    @property
    def artifact_hash(self) -> str:
        return content_hash(self)


@dataclass(frozen=True, kw_only=True)
class QualifiedFactorTemplate:
    factor_id: str
    source_hash: str
    dag_payload: Mapping[str, Any]
    dag_hash: str
    required_fields: tuple[str, ...]
    required_operators: tuple[str, ...]
    schema_version: str = QUALIFICATION_SCHEMA_VERSION

    @property
    def artifact_hash(self) -> str:
        return content_hash(self)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "factor_id": self.factor_id,
            "source_hash": self.source_hash,
            "dag_payload": self.dag_payload,
            "dag_hash": self.dag_hash,
            "required_fields": self.required_fields,
            "required_operators": self.required_operators,
        }


@dataclass(frozen=True, kw_only=True)
class FactorCompatibilityRecord:
    factor_id: str
    zoo: str
    source_relpath: str
    source_hash: str
    metadata_hash: str
    family: str
    status: FactorCompatibilityStatus
    required_fields: tuple[str, ...]
    required_operators: tuple[str, ...]
    missing_operators: tuple[str, ...]
    windows: tuple[int, ...]
    complexity: FactorComplexityLint
    similarity: FactorSimilarityLint
    reason_codes: tuple[str, ...]
    template_hash: str | None = None
    schema_version: str = QUALIFICATION_SCHEMA_VERSION

    @property
    def artifact_hash(self) -> str:
        return content_hash(self)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "factor_id": self.factor_id,
            "zoo": self.zoo,
            "source_relpath": self.source_relpath,
            "source_hash": self.source_hash,
            "metadata_hash": self.metadata_hash,
            "family": self.family,
            "status": self.status.value,
            "required_fields": self.required_fields,
            "required_operators": self.required_operators,
            "missing_operators": self.missing_operators,
            "windows": self.windows,
            "complexity": self.complexity,
            "similarity": self.similarity,
            "reason_codes": self.reason_codes,
            "template_hash": self.template_hash,
        }


@dataclass(frozen=True, kw_only=True)
class FactorCompatibilityReport:
    records: tuple[FactorCompatibilityRecord, ...]
    qualified_templates: tuple[QualifiedFactorTemplate, ...]
    schema_version: str = QUALIFICATION_SCHEMA_VERSION

    def __post_init__(self) -> None:
        ordered_records = tuple(sorted(self.records, key=lambda item: item.factor_id))
        ordered_templates = tuple(sorted(self.qualified_templates, key=lambda item: item.factor_id))
        if len({record.factor_id for record in ordered_records}) != len(ordered_records):
            raise ValueError("duplicate compatibility factor id")
        if len(ordered_templates) > MAX_QUALIFIED_TEMPLATES:
            raise ValueError("too many qualified templates")
        object.__setattr__(self, "records", ordered_records)
        object.__setattr__(self, "qualified_templates", ordered_templates)

    @property
    def total_records(self) -> int:
        return len(self.records)

    @property
    def report_hash(self) -> str:
        return content_hash(self.to_dict())

    @property
    def counts_by_status(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for record in self.records:
            counts[record.status.value] = counts.get(record.status.value, 0) + 1
        return dict(sorted(counts.items()))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "records": tuple(record.to_dict() for record in self.records),
            "qualified_templates": tuple(template.to_dict() for template in self.qualified_templates),
            "counts": {
                "total": self.total_records,
                "by_status": self.counts_by_status,
                "qualified_templates": len(self.qualified_templates),
            },
        }


class FactorSourceScanner:
    def __init__(self, zoo_root: Path | str | None = None) -> None:
        self.zoo_root = Path(zoo_root) if zoo_root is not None else None

    def scan(self, *, paths: Iterable[Path | str] | None = None) -> tuple[FactorLibraryRecord, ...]:
        catalog = FactorLibraryCatalogBuilder(self.zoo_root).build(paths=paths) if self.zoo_root is not None or paths is not None else build_factor_library_catalog()
        return catalog.records


class FactorQualificationService:
    def __init__(self, zoo_root: Path | str | None = None) -> None:
        self.zoo_root = Path(zoo_root) if zoo_root is not None else None

    def qualify(self, *, paths: Iterable[Path | str] | None = None) -> FactorCompatibilityReport:
        builder = FactorLibraryCatalogBuilder(self.zoo_root) if self.zoo_root is not None or paths is not None else None
        catalog = builder.build(paths=paths) if builder is not None else build_factor_library_catalog()
        root = (builder.zoo_root if builder is not None else FactorLibraryCatalogBuilder().zoo_root).resolve(strict=True)
        family_counts: dict[str, int] = {}
        for record in catalog.records:
            family_counts[record.family] = family_counts.get(record.family, 0) + 1

        eligible: list[tuple[FactorLibraryRecord, dict[str, Any], tuple[str, ...]]] = []
        source_texts: dict[str, str] = {}
        for record in catalog.records:
            source = _read_source(root, record)
            source_texts[record.factor_id] = source
            template = _single_field_template(record)
            if template is not None:
                operators = tuple(sorted({node["op"] for node in template["nodes"]}))
                eligible.append((record, template, operators))
        selected_ids = {
            record.factor_id
            for record, _, _ in sorted(
                eligible,
                key=lambda item: (0 if item[0].zoo == "fundamental" else 1, item[0].warmup, item[0].factor_id),
            )[:MAX_QUALIFIED_TEMPLATES]
        }

        templates: dict[str, QualifiedFactorTemplate] = {}
        records: list[FactorCompatibilityRecord] = []
        for record in catalog.records:
            source = source_texts[record.factor_id]
            template_payload = _single_field_template(record) if record.factor_id in selected_ids else None
            template: QualifiedFactorTemplate | None = None
            missing = _missing_operator_hints(source)
            reason_codes: list[str] = []
            unsafe_source = any(marker in source for marker in _UNSAFE_SOURCE_MARKERS)
            unsafe_call = any(marker in source for marker in _UNSAFE_CALL_MARKERS)
            has_conditional = any(marker in source.lower() for marker in _CONDITIONAL_MARKERS)
            if template_payload is not None:
                compiled = parse_compile_factor_dag(template_payload, field_roles=_field_roles(record.fields))
                operators = tuple(sorted(node.operator.value for node in compiled.nodes))
                template = QualifiedFactorTemplate(
                    factor_id=record.factor_id,
                    source_hash=record.source_hash,
                    dag_payload=template_payload,
                    dag_hash=compiled.artifact_hash,
                    required_fields=compiled.required_fields,
                    required_operators=operators,
                )
                templates[record.factor_id] = template
                status = FactorCompatibilityStatus.DIRECT_DAG
                reason_codes.append("CURRENT_FACTOR_DAG_TEMPLATE")
            elif unsafe_call or unsafe_source:
                status = FactorCompatibilityStatus.REJECTED_SOURCE if unsafe_call else FactorCompatibilityStatus.AUXILIARY_ONLY
                reason_codes.append("PYTHON_EXECUTION_SURFACE")
            elif missing or has_conditional:
                status = FactorCompatibilityStatus.NEEDS_DETERMINISTIC_OPERATOR
                if missing:
                    reason_codes.append("NEEDS_OPERATOR:" + ",".join(missing))
                if has_conditional:
                    reason_codes.append("NEEDS_OPERATOR:conditional")
            else:
                status = FactorCompatibilityStatus.AUXILIARY_ONLY
                reason_codes.append("NO_DIRECT_TEMPLATE_THIS_WAVE")
            complexity = self._complexity(source, record)
            similarity = FactorSimilarityLint(
                family=record.family,
                duplicate_family_count=family_counts[record.family],
                source_token_count=len(set(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", source))),
            )
            required_ops = tuple(sorted({node["op"] for node in template_payload["nodes"]})) if template_payload else ()
            records.append(
                FactorCompatibilityRecord(
                    factor_id=record.factor_id,
                    zoo=record.zoo,
                    source_relpath=record.source_relpath,
                    source_hash=record.source_hash,
                    metadata_hash=record.metadata_hash,
                    family=record.family,
                    status=status,
                    required_fields=record.fields,
                    required_operators=required_ops,
                    missing_operators=missing,
                    windows=(record.warmup,) if record.warmup else (),
                    complexity=complexity,
                    similarity=similarity,
                    reason_codes=tuple(sorted(dict.fromkeys(reason_codes))),
                    template_hash=template.artifact_hash if template is not None else None,
                )
            )
        return FactorCompatibilityReport(records=tuple(records), qualified_templates=tuple(templates.values()))

    @staticmethod
    def _complexity(source: str, record: FactorLibraryRecord) -> FactorComplexityLint:
        try:
            tree = ast.parse(source)
            ast_nodes = sum(1 for _ in ast.walk(tree))
        except SyntaxError:
            ast_nodes = 0
        return FactorComplexityLint(
            ast_nodes=ast_nodes,
            source_bytes=len(source.encode("utf-8")),
            required_field_count=len(record.fields),
            window_max=record.warmup,
            has_conditionals=any(marker in source.lower() for marker in _CONDITIONAL_MARKERS),
            has_unsafe_execution_marker=any(marker in source for marker in (*_UNSAFE_SOURCE_MARKERS, *_UNSAFE_CALL_MARKERS)),
        )
