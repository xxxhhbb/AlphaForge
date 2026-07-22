"""Static FactorDAG lint adapter; this module has no validation authority."""

from __future__ import annotations

import hashlib
from collections import Counter
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from ..contracts import CanonicalContract, ContractValidationError, FailureCode, ResearchState
from ..contracts.base import require_hash, require_identifier
from ..data import FieldRole
from ..factors import CompiledFactorDAG, parse_compile_factor_dag


def _fail(code: FailureCode, message: str, **details: Any) -> None:
    raise ContractValidationError(code, message, details=details)


@dataclass(frozen=True, kw_only=True)
class FactorLintPolicy(CanonicalContract):
    max_ast_nodes: int = 64
    max_symbol_length: int = 4096
    max_base_features: int = 8
    max_duplicate_subtrees: int = 0
    max_factor_zoo_similarity: float = 0.95

    def __post_init__(self) -> None:
        for field_name in (
            "max_ast_nodes",
            "max_symbol_length",
            "max_base_features",
            "max_duplicate_subtrees",
        ):
            value = getattr(self, field_name)
            minimum = 0 if field_name == "max_duplicate_subtrees" else 1
            if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
                _fail(FailureCode.INVALID_VALUE, f"{field_name} has an invalid limit")
        if not isinstance(self.max_factor_zoo_similarity, float) or not (
            0.0 <= self.max_factor_zoo_similarity <= 1.0
        ):
            _fail(FailureCode.INVALID_VALUE, "max_factor_zoo_similarity must be in [0, 1]")


@dataclass(frozen=True, kw_only=True)
class FactorLintReport(CanonicalContract):
    lint_id: str
    candidate_hash: str
    parser_accepted: bool
    lint_passed: bool
    ast_node_count: int
    symbol_length: int
    base_features: tuple[str, ...]
    duplicate_subtrees: int
    factor_zoo_similarity: float
    findings: tuple[str, ...]
    failure_code: FailureCode | None
    report_kind: str = "lint"
    state: ResearchState = ResearchState.DRAFT

    def __post_init__(self) -> None:
        require_identifier(self.lint_id, "lint_id")
        require_hash(self.candidate_hash, "candidate_hash")
        if type(self.parser_accepted) is not bool or type(self.lint_passed) is not bool:
            _fail(FailureCode.INVALID_VALUE, "lint flags must be boolean")
        for name in ("ast_node_count", "symbol_length", "duplicate_subtrees"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                _fail(FailureCode.INVALID_VALUE, f"{name} must be a non-negative integer")
        if not 0.0 <= self.factor_zoo_similarity <= 1.0:
            _fail(FailureCode.INVALID_VALUE, "factor_zoo_similarity must be in [0, 1]")
        if self.report_kind != "lint":
            _fail(FailureCode.INVALID_VALUE, "regulator output must be named lint")
        if self.state is not ResearchState.DRAFT:
            _fail(FailureCode.INVALID_STATE, "lint cannot advance research state")
        if self.lint_passed and (not self.parser_accepted or self.findings or self.failure_code):
            _fail(FailureCode.INVALID_VALUE, "passing lint cannot contain failures")


def _subtree_signatures(dag: CompiledFactorDAG) -> tuple[str, ...]:
    by_id: dict[str, str] = {}
    signatures: list[str] = []
    for node in dag.nodes:
        inputs = tuple(by_id[item] for item in node.inputs)
        parameters = tuple(sorted((key, repr(value)) for key, value in node.parameters.items()))
        signature = repr((node.operator.value, parameters, inputs))
        by_id[node.node_id] = signature
        signatures.append(signature)
    return tuple(signatures)


def _similarity(left: tuple[str, ...], right: tuple[str, ...]) -> float:
    left_set, right_set = set(left), set(right)
    union = left_set | right_set
    return 0.0 if not union else len(left_set & right_set) / len(union)


class FactorRegulatorAdapter:
    """Return parser/complexity/redundancy lint only, never PIT or alpha claims."""

    def __init__(self, *, policy: FactorLintPolicy | None = None) -> None:
        self.policy = policy or FactorLintPolicy()

    def lint(
        self,
        payload: Mapping[str, Any],
        *,
        field_roles: Mapping[str, FieldRole],
        factor_zoo: Sequence[CompiledFactorDAG] = (),
    ) -> FactorLintReport:
        try:
            dag = parse_compile_factor_dag(payload, field_roles=field_roles)
        except ContractValidationError as exc:
            digest = "sha256:" + hashlib.sha256(
                (type(payload).__name__ + ":parse-rejected").encode("utf-8")
            ).hexdigest()
            return FactorLintReport(
                lint_id=f"lint-{digest[7:27]}",
                candidate_hash=digest,
                parser_accepted=False,
                lint_passed=False,
                ast_node_count=0,
                symbol_length=0,
                base_features=(),
                duplicate_subtrees=0,
                factor_zoo_similarity=0.0,
                findings=("PARSER_REJECTED",),
                failure_code=exc.code,
            )
        signatures = _subtree_signatures(dag)
        counts = Counter(signatures)
        duplicates = sum(count - 1 for count in counts.values() if count > 1)
        similarities = tuple(
            _similarity(signatures, _subtree_signatures(existing)) for existing in factor_zoo
        )
        zoo_similarity = max(similarities, default=0.0)
        node_count = len(dag.nodes)
        symbol_length = len(dag.canonical_json())
        findings: list[str] = []
        if node_count > self.policy.max_ast_nodes:
            findings.append("AST_NODE_LIMIT")
        if symbol_length > self.policy.max_symbol_length:
            findings.append("SYMBOL_LENGTH_LIMIT")
        if len(dag.required_fields) > self.policy.max_base_features:
            findings.append("BASE_FEATURE_LIMIT")
        if duplicates > self.policy.max_duplicate_subtrees:
            findings.append("DUPLICATE_SUBTREE")
        if zoo_similarity > self.policy.max_factor_zoo_similarity:
            findings.append("FACTOR_ZOO_SIMILARITY")
        passed = not findings
        return FactorLintReport(
            lint_id=f"lint-{dag.artifact_hash[7:27]}",
            candidate_hash=dag.artifact_hash,
            parser_accepted=True,
            lint_passed=passed,
            ast_node_count=node_count,
            symbol_length=symbol_length,
            base_features=dag.required_fields,
            duplicate_subtrees=duplicates,
            factor_zoo_similarity=zoo_similarity,
            findings=tuple(findings),
            failure_code=None if passed else FailureCode.INVALID_FACTOR_DAG,
        )
