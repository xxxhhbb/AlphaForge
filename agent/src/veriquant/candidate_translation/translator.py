"""Transparent repair and fail-closed FactorDAG translation.

This layer makes harmless syntax repairs but never invents official metrics,
executes Python, or silently changes a requested factor horizon.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from enum import Enum
from typing import ClassVar, Mapping

from ..adapters import compile_proposal_expression
from ..contracts import CanonicalContract, ContractValidationError, FailureCode
from ..contracts.base import fail, require_hash, require_identifier, require_text
from ..data import FieldRole


SCHEMA_VERSION = "alphaforge.candidate-translation/v1"
_ALIASES = {"rank": "rank_cross_section", "zscore": "zscore_cross_section", "ts_mean": "rolling_mean", "ts_std": "rolling_std"}
_FORBIDDEN = re.compile(r"(?i)(?:```|\b(?:import|exec|eval|lambda|open|compile|subprocess|os\.|sys\.)\b|factor\.py|__)")


class CandidateTranslationStatus(str, Enum):
    ACCEPTED = "ACCEPTED"
    REVISE = "REVISE"
    UNSUPPORTED = "UNSUPPORTED"


@dataclass(frozen=True)
class CandidateDraftEnvelope(CanonicalContract):
    campaign_hash: str
    group_id: str
    candidate_id: str
    expression: str
    requested_lookback_sessions: int | None
    declared_lookback_sessions: int | None
    allowed_fields: tuple[str, ...]
    source_draft_hash: str
    schema_version: ClassVar[str] = SCHEMA_VERSION

    def __post_init__(self) -> None:
        require_hash(self.campaign_hash, "campaign_hash")
        require_identifier(self.group_id, "group_id")
        require_identifier(self.candidate_id, "candidate_id")
        require_text(self.expression, "expression")
        require_hash(self.source_draft_hash, "source_draft_hash")
        if not self.allowed_fields:
            fail(FailureCode.MISSING_REQUIRED_FIELD, "allowed_fields are required")
        for name in ("requested_lookback_sessions", "declared_lookback_sessions"):
            value = getattr(self, name)
            if value is not None and (not isinstance(value, int) or value <= 0):
                fail(FailureCode.INVALID_VALUE, f"{name} must be positive", field=name)


@dataclass(frozen=True)
class CandidateTranslationResult(CanonicalContract):
    draft_hash: str
    status: CandidateTranslationStatus
    normalized_expression: str | None
    factor_dag_hash: str | None
    factor_dag_json: str | None
    repairs: tuple[str, ...] = ()
    failure_code: str | None = None
    message: str = ""
    schema_version: ClassVar[str] = SCHEMA_VERSION

    def __post_init__(self) -> None:
        require_hash(self.draft_hash, "draft_hash")
        if not isinstance(self.status, CandidateTranslationStatus):
            fail(FailureCode.INVALID_VALUE, "status is invalid")
        if self.status is CandidateTranslationStatus.ACCEPTED:
            if not self.factor_dag_hash or not self.factor_dag_json or not self.normalized_expression:
                fail(FailureCode.MISSING_REQUIRED_FIELD, "accepted translation requires FactorDAG")
            require_hash(self.factor_dag_hash, "factor_dag_hash")
            if self.failure_code is not None:
                fail(FailureCode.INVALID_STATE, "accepted translation cannot carry failure")
        elif not self.failure_code:
            fail(FailureCode.MISSING_REQUIRED_FIELD, "non-accepted translation requires failure_code")


class SafeCandidateTranslator:
    def translate(self, draft: CandidateDraftEnvelope) -> CandidateTranslationResult:
        if _FORBIDDEN.search(draft.expression):
            return self._rejected(draft, CandidateTranslationStatus.UNSUPPORTED, "FORBIDDEN_EXECUTABLE_PAYLOAD", "可执行代码不进入 FactorDAG 路径")
        expression, repairs = _normalize_expression(draft.expression)
        try:
            tree = ast.parse(expression, mode="eval")
        except SyntaxError:
            return self._rejected(draft, CandidateTranslationStatus.REVISE, "INVALID_EXPRESSION_SYNTAX", "表达式语法需要修订", expression, repairs)
        roles: Mapping[str, FieldRole] = {field: FieldRole.FEATURE for field in draft.allowed_fields}
        try:
            compiled = compile_proposal_expression(expression, field_roles=roles)
        except ContractValidationError as exc:
            status = CandidateTranslationStatus.UNSUPPORTED if exc.code in {FailureCode.UNKNOWN_FIELD, FailureCode.INVALID_FACTOR_DAG} else CandidateTranslationStatus.REVISE
            return self._rejected(draft, status, exc.code.value, str(exc), expression, repairs)
        observed_windows = _windows(tree)
        requested = draft.requested_lookback_sessions
        if requested is not None and requested not in observed_windows:
            return self._rejected(
                draft, CandidateTranslationStatus.REVISE, "REQUESTED_LOOKBACK_NOT_REPRESENTED",
                f"用户要求 {requested} 日窗口，但候选表达式未包含该窗口", expression, repairs,
            )
        if draft.declared_lookback_sessions is not None and observed_windows and max(observed_windows) > draft.declared_lookback_sessions:
            return self._rejected(draft, CandidateTranslationStatus.REVISE, "DECLARED_LOOKBACK_TOO_SHORT", "候选声明的预热长度不足", expression, repairs)
        return CandidateTranslationResult(
            draft_hash=draft.artifact_hash,
            status=CandidateTranslationStatus.ACCEPTED,
            normalized_expression=expression,
            factor_dag_hash=compiled.artifact_hash,
            factor_dag_json=compiled.canonical_json(),
            repairs=repairs,
            message="候选已安全转换为受限 FactorDAG；尚未完成 PIT 或官方评价",
        )

    @staticmethod
    def _rejected(
        draft: CandidateDraftEnvelope,
        status: CandidateTranslationStatus,
        code: str,
        message: str,
        expression: str | None = None,
        repairs: tuple[str, ...] = (),
    ) -> CandidateTranslationResult:
        return CandidateTranslationResult(
            draft_hash=draft.artifact_hash,
            status=status,
            normalized_expression=expression,
            factor_dag_hash=None,
            factor_dag_json=None,
            repairs=repairs,
            failure_code=code,
            message=message,
        )


def _normalize_expression(expression: str) -> tuple[str, tuple[str, ...]]:
    normalized = expression.strip()
    repairs: list[str] = []
    for source, target in _ALIASES.items():
        replaced = re.sub(rf"\b{re.escape(source)}\s*\(", f"{target}(", normalized)
        if replaced != normalized:
            repairs.append(f"operator_alias:{source}->{target}")
            normalized = replaced
    return normalized, tuple(repairs)


def _windows(tree: ast.AST) -> frozenset[int]:
    values: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
            continue
        if node.func.id not in {"pct_change", "lag", "rolling_mean", "rolling_std", "ts_mean", "ts_std"}:
            continue
        if len(node.args) >= 2 and isinstance(node.args[1], ast.Constant) and isinstance(node.args[1].value, int):
            values.add(node.args[1].value)
    return frozenset(values)
