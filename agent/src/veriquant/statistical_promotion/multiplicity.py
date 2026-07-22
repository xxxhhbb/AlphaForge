"""Deterministic multiple-testing corrections."""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping

from .contracts import (
    MultipleTestingMethod,
    MultipleTestingPolicy,
    PromotionContract,
    StatisticalPromotionFailureCode,
    _fail,
    _require_p_value,
)


@dataclass(frozen=True, kw_only=True)
class MultipleTestingAssessment(PromotionContract):
    method: MultipleTestingMethod
    alpha: float
    passed: Mapping[str, bool]
    adjusted_p_values: Mapping[str, float]
    rejection_boundary: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "passed", MappingProxyType(dict(self.passed)))
        object.__setattr__(
            self,
            "adjusted_p_values",
            MappingProxyType(dict(self.adjusted_p_values)),
        )


def _validated_p_values(p_values: Mapping[str, float]) -> dict[str, float]:
    if not p_values:
        _fail(
            StatisticalPromotionFailureCode.MISSING_EVIDENCE,
            "multiple testing requires family p-values",
        )
    validated: dict[str, float] = {}
    for key, value in p_values.items():
        if not isinstance(key, str) or not key:
            _fail(
                StatisticalPromotionFailureCode.INVALID_IDENTIFIER,
                "p-value keys must be stable candidate ids",
            )
        _require_p_value(value, f"p_values.{key}")
        if key in validated:
            _fail(
                StatisticalPromotionFailureCode.DUPLICATE_CANDIDATE,
                "duplicate candidate key in p-value collection",
            )
        validated[key] = float(value)
    return validated


def _ranked_items(p_values: Mapping[str, float]) -> list[tuple[str, float]]:
    return sorted(p_values.items(), key=lambda item: (item[1], item[0]))


def benjamini_hochberg(
    p_values: Mapping[str, float],
    *,
    alpha: float,
    method: MultipleTestingMethod = MultipleTestingMethod.BENJAMINI_HOCHBERG,
) -> MultipleTestingAssessment:
    values = _validated_p_values(p_values)
    _require_p_value(alpha, "alpha")
    ranked = _ranked_items(values)
    count = len(ranked)
    raw_adjusted: dict[str, float] = {}
    running_min = 1.0
    for rank_from_end, (key, p_value) in enumerate(reversed(ranked), start=1):
        rank = count - rank_from_end + 1
        running_min = min(running_min, p_value * count / rank)
        raw_adjusted[key] = min(1.0, running_min)
    passed = {key: raw_adjusted[key] <= alpha for key in values}
    max_rank = 0
    for rank, (key, p_value) in enumerate(ranked, start=1):
        if p_value <= alpha * rank / count:
            max_rank = rank
    boundary = alpha * max_rank / count if max_rank else 0.0
    return MultipleTestingAssessment(
        method=method,
        alpha=alpha,
        passed=passed,
        adjusted_p_values=raw_adjusted,
        rejection_boundary=boundary,
    )


def bonferroni(
    p_values: Mapping[str, float],
    *,
    alpha: float,
) -> MultipleTestingAssessment:
    values = _validated_p_values(p_values)
    _require_p_value(alpha, "alpha")
    count = len(values)
    boundary = alpha / count
    adjusted = {key: min(1.0, value * count) for key, value in values.items()}
    return MultipleTestingAssessment(
        method=MultipleTestingMethod.BONFERRONI,
        alpha=alpha,
        passed={key: value <= boundary for key, value in values.items()},
        adjusted_p_values=adjusted,
        rejection_boundary=boundary,
    )


def holm(
    p_values: Mapping[str, float],
    *,
    alpha: float,
) -> MultipleTestingAssessment:
    values = _validated_p_values(p_values)
    _require_p_value(alpha, "alpha")
    ranked = _ranked_items(values)
    count = len(ranked)
    adjusted_by_rank: dict[str, float] = {}
    running_max = 0.0
    failed = False
    passed: dict[str, bool] = {}
    boundary = 0.0
    for rank, (key, p_value) in enumerate(ranked, start=1):
        divisor = count - rank + 1
        threshold = alpha / divisor
        running_max = max(running_max, min(1.0, p_value * divisor))
        adjusted_by_rank[key] = running_max
        if failed or p_value > threshold:
            failed = True
            passed[key] = False
        else:
            passed[key] = True
            boundary = threshold
    return MultipleTestingAssessment(
        method=MultipleTestingMethod.HOLM,
        alpha=alpha,
        passed={key: passed.get(key, False) for key in values},
        adjusted_p_values=adjusted_by_rank,
        rejection_boundary=boundary,
    )


def apply_multiple_testing(
    p_values: Mapping[str, float],
    policy: MultipleTestingPolicy,
) -> MultipleTestingAssessment:
    if policy.method is MultipleTestingMethod.BENJAMINI_HOCHBERG:
        return benjamini_hochberg(p_values, alpha=policy.alpha)
    if policy.method is MultipleTestingMethod.FAMILY_BH_FDR:
        return benjamini_hochberg(
            p_values,
            alpha=policy.alpha,
            method=MultipleTestingMethod.FAMILY_BH_FDR,
        )
    if policy.method is MultipleTestingMethod.BONFERRONI:
        return bonferroni(p_values, alpha=policy.alpha)
    if policy.method is MultipleTestingMethod.HOLM:
        return holm(p_values, alpha=policy.alpha)
    _fail(
        StatisticalPromotionFailureCode.INVALID_CONTRACT,
        "unknown multiple-testing method",
    )

