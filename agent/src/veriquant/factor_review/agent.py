"""Deterministic independent review before formula certification."""

from __future__ import annotations

import re

from ..specialist_research import PreflightStatus
from .models import (
    FactorReviewDecision,
    FactorReviewFinding,
    FactorReviewFindingCode,
    FactorReviewRequest,
    FactorReviewVerdict,
    FactorRevisionAdvice,
)


_TOKEN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*|\d+")


class FactorReviewAgent:
    """Review DRAFT semantics only; never calculate or approve financial truth."""

    def review(self, request: FactorReviewRequest) -> FactorReviewDecision:
        candidate = request.candidate
        expression = request.preflight.normalized_expression or candidate.expression
        findings: list[FactorReviewFinding] = []

        missing = sorted(set(candidate.required_fields) - set(request.available_fields))
        if missing:
            findings.append(self._finding(
                FactorReviewFindingCode.MISSING_APPROVED_FIELD,
                "ERROR",
                "candidate requires unavailable approved fields: " + ",".join(missing),
            ))

        forbidden = sorted(set(candidate.required_fields) & set(request.policy.forbidden_proxy_fields))
        if forbidden:
            findings.append(self._finding(
                FactorReviewFindingCode.UNTRADEABLE_PROXY,
                "ERROR",
                "candidate uses prohibited label, sealed, or authority-bearing proxies: " + ",".join(forbidden),
            ))

        lower_expression = expression.lower()
        absent_from_expression = sorted(field for field in candidate.required_fields if field.lower() not in lower_expression)
        if absent_from_expression:
            findings.append(self._finding(
                FactorReviewFindingCode.EXPRESSION_HYPOTHESIS_MISMATCH,
                "ERROR",
                "declared hypothesis fields are absent from the expression: " + ",".join(absent_from_expression),
            ))

        if candidate.lookback_sessions > request.policy.max_lookback_sessions:
            findings.append(self._finding(
                FactorReviewFindingCode.WINDOW_OUT_OF_POLICY,
                "ERROR",
                f"lookback {candidate.lookback_sessions} exceeds review policy maximum",
            ))
        if candidate.forecast_horizon_sessions > request.policy.max_horizon_sessions:
            findings.append(self._finding(
                FactorReviewFindingCode.HORIZON_OUT_OF_POLICY,
                "ERROR",
                f"forecast horizon {candidate.forecast_horizon_sessions} exceeds review policy maximum",
            ))

        current_tokens = self._tokens(expression)
        for previous in request.registered_drafts:
            if previous.candidate_hash == candidate.artifact_hash or self._normalized(previous.normalized_expression) == self._normalized(expression):
                findings.append(self._finding(
                    FactorReviewFindingCode.DUPLICATE_DRAFT,
                    "ERROR",
                    "candidate duplicates a registered draft",
                    previous.candidate_id,
                ))
                break
            similarity = self._jaccard(current_tokens, self._tokens(previous.normalized_expression))
            if similarity >= request.policy.similarity_revision_threshold:
                findings.append(self._finding(
                    FactorReviewFindingCode.HIGH_SIMILARITY,
                    "WARNING",
                    f"candidate token similarity {similarity:.3f} requires explicit differentiation",
                    previous.candidate_id,
                ))
                break

        if request.preflight.status is not PreflightStatus.ACCEPTED:
            findings.append(self._finding(
                FactorReviewFindingCode.CERTIFICATION_REVISION_REQUIRED
                if candidate.revision_number == 0
                else FactorReviewFindingCode.CERTIFICATION_REJECTED_AFTER_REVISION,
                "ERROR",
                "formula certification preflight failed: " + str(request.preflight.failure_code),
            ))

        fatal = any(item.code in {
            FactorReviewFindingCode.UNTRADEABLE_PROXY,
            FactorReviewFindingCode.DUPLICATE_DRAFT,
            FactorReviewFindingCode.CERTIFICATION_REJECTED_AFTER_REVISION,
        } for item in findings)
        if not findings:
            verdict = FactorReviewVerdict.ACCEPT_FOR_CERTIFICATION
            advice = None
        elif fatal or candidate.revision_number > 0:
            verdict = FactorReviewVerdict.REJECT_DRAFT
            advice = None
        else:
            verdict = FactorReviewVerdict.REVISE
            reason_codes = tuple(sorted({item.code.value for item in findings}))
            advice = FactorRevisionAdvice(
                source_candidate_id=candidate.candidate_id,
                source_candidate_hash=candidate.artifact_hash,
                revision_number=1,
                reason_codes=reason_codes,
                instructions=tuple(self._instruction(code) for code in reason_codes),
            )

        return FactorReviewDecision(
            candidate_id=candidate.candidate_id,
            candidate_hash=candidate.artifact_hash,
            proposer_id=request.proposer_id,
            reviewer_id=request.reviewer_id,
            verdict=verdict,
            policy_hash=request.policy.artifact_hash,
            preflight_hash=request.preflight.artifact_hash,
            findings=tuple(findings),
            revision_advice=advice,
            parent_candidate_id=candidate.parent_candidate_id,
            revision_number=candidate.revision_number,
            previous_review_hash=request.previous_review_hash,
        )

    @staticmethod
    def _finding(code, severity, message, related_candidate_id=None):
        return FactorReviewFinding(code, severity, message, related_candidate_id)

    @staticmethod
    def _normalized(expression: str) -> str:
        return "".join(expression.lower().split())

    @staticmethod
    def _tokens(expression: str) -> frozenset[str]:
        return frozenset(token.lower() for token in _TOKEN.findall(expression))

    @staticmethod
    def _jaccard(left: frozenset[str], right: frozenset[str]) -> float:
        union = left | right
        return len(left & right) / len(union) if union else 1.0

    @staticmethod
    def _instruction(code: str) -> str:
        instructions = {
            FactorReviewFindingCode.MISSING_APPROVED_FIELD.value: "Replace unavailable fields with approved PIT-safe observables or declare a data capability gap.",
            FactorReviewFindingCode.EXPRESSION_HYPOTHESIS_MISMATCH.value: "Align declared required fields and the inert expression with the stated economic mechanism.",
            FactorReviewFindingCode.WINDOW_OUT_OF_POLICY.value: "Use a lookback within the confirmed Campaign and review policy.",
            FactorReviewFindingCode.HORIZON_OUT_OF_POLICY.value: "Use the forecast horizon frozen by the confirmed Campaign.",
            FactorReviewFindingCode.HIGH_SIMILARITY.value: "Explain and encode a material, testable distinction from the registered candidate.",
            FactorReviewFindingCode.CERTIFICATION_REVISION_REQUIRED.value: "Repair only the typed Qlib certification error; do not introduce Python or executable code.",
        }
        return instructions.get(code, "Address the typed review finding without changing authority or using sealed information.")

