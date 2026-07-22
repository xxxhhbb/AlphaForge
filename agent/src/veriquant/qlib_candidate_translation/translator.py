"""Safe Campaign-bound certification of independently proposed Qlib candidates."""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Iterable

from ..data import canonical_content_hash
from ..qlib_expression.api import (
    STATIC_QLIB_OPERATOR_MANIFEST,
    QlibExpressionError,
    QlibExpressionFailureCode,
    QlibExpressionPolicy,
    QlibFieldRole,
    QlibOperatorManifest,
    QlibOperatorTrust,
    certify_qlib_expression,
)
from .models import (
    QlibCandidateDraftEnvelope,
    QlibCandidateFailureCode,
    QlibCandidateTranslationResult,
    QlibCandidateTranslationStatus,
)


_FORBIDDEN_EXPRESSION = re.compile(
    r"(?i)(?:```|\b(?:import|exec|eval|lambda|open|compile|subprocess)\b|factor\.py|__|\bos\.|\bsys\.)"
)
_FIELD_REFERENCE = re.compile(r"\$([A-Za-z][A-Za-z0-9_]*)")
_REVISE_CODES = frozenset(
    {
        QlibExpressionFailureCode.EMPTY_EXPRESSION,
        QlibExpressionFailureCode.EXPRESSION_TOO_LONG,
        QlibExpressionFailureCode.INVALID_TOKEN,
        QlibExpressionFailureCode.INVALID_SYNTAX,
        QlibExpressionFailureCode.INVALID_ARGUMENT_COUNT,
        QlibExpressionFailureCode.INVALID_ARGUMENT,
        QlibExpressionFailureCode.INVALID_WINDOW,
        QlibExpressionFailureCode.SIZE_LIMIT_EXCEEDED,
        QlibExpressionFailureCode.DEPTH_LIMIT_EXCEEDED,
    }
)


class SafeQlibCandidateTranslator:
    """Certify candidates one by one; no failure aborts sibling candidates."""

    def __init__(
        self,
        *,
        manifest: QlibOperatorManifest = STATIC_QLIB_OPERATOR_MANIFEST,
        max_window: int = 2_520,
        max_ref_periods: int = 2_520,
    ) -> None:
        self.manifest = manifest
        self.max_window = max_window
        self.max_ref_periods = max_ref_periods

    def translate(self, draft: QlibCandidateDraftEnvelope) -> QlibCandidateTranslationResult:
        policy = self._policy(draft)
        if _FORBIDDEN_EXPRESSION.search(draft.expression):
            return self._failure(
                draft,
                policy,
                status=QlibCandidateTranslationStatus.UNSUPPORTED,
                failure_code=QlibCandidateFailureCode.FORBIDDEN_CANDIDATE_PAYLOAD,
                expression_failure_code=None,
                message="可执行代码、factor.py 和动态 Python 入口禁止进入 Qlib 认证路径",
            )
        try:
            certified = certify_qlib_expression(
                draft.expression,
                policy=policy,
                manifest=self.manifest,
            )
        except QlibExpressionError as exc:
            status = (
                QlibCandidateTranslationStatus.REVISE
                if exc.code in _REVISE_CODES
                else QlibCandidateTranslationStatus.UNSUPPORTED
            )
            return self._failure(
                draft,
                policy,
                status=status,
                failure_code=QlibCandidateFailureCode.EXPRESSION_REJECTED,
                expression_failure_code=exc.code,
                message=str(exc),
            )

        requested = draft.requested_lookback_sessions
        observed_windows = _observed_window_literals(certified.root, self.manifest)
        if requested is not None and requested not in observed_windows:
            return self._failure(
                draft,
                policy,
                status=QlibCandidateTranslationStatus.REVISE,
                failure_code=QlibCandidateFailureCode.REQUESTED_LOOKBACK_NOT_REPRESENTED,
                expression_failure_code=None,
                message=f"用户要求 {requested} 日窗口，但候选表达式未显式包含该窗口",
            )

        program_hash = canonical_content_hash(
            {
                "schema": "veriquant.certified-qlib-program/v1",
                "expression_hash": certified.expression_hash,
                "policy_hash": certified.policy_hash,
                "manifest_hash": certified.manifest_hash,
                "required_fields": certified.required_fields,
                "maximum_lookback": certified.maximum_lookback,
                "requested_lookback_sessions": requested,
            }
        )
        return QlibCandidateTranslationResult(
            draft_hash=draft.artifact_hash,
            candidate_id=draft.candidate_id,
            confirmed_field_whitelist=draft.confirmed_field_whitelist,
            status=QlibCandidateTranslationStatus.CERTIFIED,
            policy_hash=certified.policy_hash,
            manifest_hash=certified.manifest_hash,
            canonical_expression=certified.canonical_expression,
            certified_expression=certified,
            expression_hash=certified.expression_hash,
            program_hash=program_hash,
            required_fields=certified.required_fields,
            maximum_lookback=certified.maximum_lookback,
            operators=certified.operators,
            message="候选已通过 Qlib 表达式认证；尚未获得 PIT、官方指标或晋级结论",
        )

    def translate_many(
        self,
        drafts: Iterable[QlibCandidateDraftEnvelope],
    ) -> tuple[QlibCandidateTranslationResult, ...]:
        return tuple(self.translate(draft) for draft in drafts)

    def _policy(self, draft: QlibCandidateDraftEnvelope) -> QlibExpressionPolicy:
        roles = {
            field_name: QlibFieldRole.FEATURE
            for field_name in draft.confirmed_field_whitelist
        }
        # Give privilege-shaped references an explicit forbidden role so a
        # sealed/label attempt is distinct from an ordinary unavailable field.
        for field_name in _FIELD_REFERENCE.findall(draft.expression):
            lowered = field_name.lower()
            if "sealed" in lowered:
                roles.setdefault(field_name, QlibFieldRole.SEALED)
            elif "label" in lowered or lowered.startswith("future_"):
                roles.setdefault(field_name, QlibFieldRole.LABEL)
        trusted_operators = tuple(
            item.name
            for item in self.manifest.operators
            if item.trust is not QlibOperatorTrust.EXCLUDED
        )
        return QlibExpressionPolicy(
            field_roles=roles,
            allowed_operators=trusted_operators,
            max_window=self.max_window,
            max_ref_periods=self.max_ref_periods,
        )

    def _failure(
        self,
        draft: QlibCandidateDraftEnvelope,
        policy: QlibExpressionPolicy,
        *,
        status: QlibCandidateTranslationStatus,
        failure_code: QlibCandidateFailureCode,
        expression_failure_code: QlibExpressionFailureCode | None,
        message: str,
    ) -> QlibCandidateTranslationResult:
        return QlibCandidateTranslationResult(
            draft_hash=draft.artifact_hash,
            candidate_id=draft.candidate_id,
            confirmed_field_whitelist=draft.confirmed_field_whitelist,
            status=status,
            policy_hash=policy.policy_hash,
            manifest_hash=self.manifest.manifest_hash,
            failure_code=failure_code,
            expression_failure_code=expression_failure_code,
            message=message,
        )


def _observed_window_literals(root: object, manifest: QlibOperatorManifest) -> frozenset[int]:
    values: set[int] = set()

    def visit(node: object) -> None:
        if type(node).__name__ != "CallNode":
            for attribute in ("operand", "left", "right"):
                child = getattr(node, attribute, None)
                if child is not None:
                    visit(child)
            return
        name = getattr(node, "name")
        arguments = getattr(node, "arguments")
        spec = manifest.by_name.get(name)
        if spec is not None:
            index: int | None = None
            if spec.category == "PAIR_ROLLING":
                index = 2
            elif spec.constraint in {
                "PAST_ONLY_REF",
                "BOUNDED_INTEGER_WINDOW",
                "WINDOW_AND_QUANTILE",
                "WINDOW_OR_ALPHA",
            }:
                index = 1
            if index is not None and index < len(arguments):
                literal = _positive_integer_literal(arguments[index])
                if literal is not None:
                    values.add(literal)
        for argument in arguments:
            visit(argument)

    visit(root)
    return frozenset(values)


def _positive_integer_literal(node: object) -> int | None:
    if type(node).__name__ != "NumberNode":
        return None
    try:
        value = Decimal(getattr(node, "canonical_value"))
    except (InvalidOperation, TypeError, ValueError):
        return None
    if value < 1 or value != value.to_integral_value():
        return None
    return int(value)


__all__ = ["SafeQlibCandidateTranslator"]
