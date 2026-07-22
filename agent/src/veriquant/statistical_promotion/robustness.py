"""Deterministic robustness, cost, and capacity checks."""

from __future__ import annotations

from dataclasses import dataclass

from .contracts import (
    CandidateEvidenceView,
    PromotionContract,
    RobustnessPolicy,
    StatisticalPromotionFailureCode,
    _fail,
)


@dataclass(frozen=True, kw_only=True)
class RobustnessFinding(PromotionContract):
    check_name: str
    passed: bool
    code: StatisticalPromotionFailureCode | None
    message: str


@dataclass(frozen=True, kw_only=True)
class RobustnessAssessment(PromotionContract):
    passed: bool
    findings: tuple[RobustnessFinding, ...]

    @property
    def failure_codes(self) -> tuple[StatisticalPromotionFailureCode, ...]:
        return tuple(item.code for item in self.findings if item.code is not None)


def _metric(view: CandidateEvidenceView, key: str) -> float:
    if key in view.robustness_metrics:
        return float(view.robustness_metrics[key])
    validation = view.visible_metrics.get("VALIDATION")
    if validation is not None and key in validation:
        return float(validation[key])
    train = view.visible_metrics.get("TRAIN")
    if train is not None and key in train:
        return float(train[key])
    _fail(
        StatisticalPromotionFailureCode.MISSING_EVIDENCE,
        "required robustness evidence is missing",
        metric=key,
    )


def _finding(
    name: str,
    passed: bool,
    code: StatisticalPromotionFailureCode | None,
    message: str,
) -> RobustnessFinding:
    return RobustnessFinding(
        check_name=name,
        passed=passed,
        code=None if passed else code,
        message=message,
    )


def evaluate_robustness(
    view: CandidateEvidenceView,
    policy: RobustnessPolicy,
) -> RobustnessAssessment:
    if "TRAIN" not in view.visible_metrics or "VALIDATION" not in view.visible_metrics:
        _fail(
            StatisticalPromotionFailureCode.MISSING_EVIDENCE,
            "train and validation evidence are both required",
        )

    observations = _metric(view, "observation_count")
    rank_ic = _metric(view, "rank_ic")
    icir = _metric(view, "icir")
    subperiod_fraction = _metric(view, "subperiod_positive_fraction")
    subperiod_min = _metric(view, "subperiod_min_ic")
    regime_fraction = _metric(view, "regime_positive_fraction")
    regime_min = _metric(view, "regime_min_ic")
    industry_exposure = abs(_metric(view, "max_abs_industry_exposure"))
    cap_exposure = abs(_metric(view, "max_abs_market_cap_exposure"))
    turnover = _metric(view, "turnover")
    gross_before_cost = _metric(view, "gross_return_before_cost")
    net_after_cost = _metric(view, "net_return_after_cost")
    cost_sensitivity_loss = _metric(view, "cost_sensitivity_loss")
    capacity_utilization = _metric(view, "capacity_utilization")
    coverage = _metric(view, "coverage")
    parameter_delta = _metric(view, "parameter_sensitivity_max_delta")

    findings = (
        _finding(
            "sample_size",
            observations >= policy.min_observations,
            StatisticalPromotionFailureCode.MISSING_EVIDENCE,
            "validation sample has enough observations",
        ),
        _finding(
            "visible_statistics",
            rank_ic >= policy.min_rank_ic and icir >= policy.min_icir,
            StatisticalPromotionFailureCode.ROBUSTNESS_FAILED,
            "visible RankIC and ICIR meet thresholds",
        ),
        _finding(
            "time_subperiod_stability",
            (
                subperiod_fraction >= policy.min_subperiod_positive_fraction
                and subperiod_min >= policy.min_subperiod_min_ic
            ),
            StatisticalPromotionFailureCode.ROBUSTNESS_FAILED,
            "time subperiod stability meets thresholds",
        ),
        _finding(
            "market_regime_stability",
            (
                regime_fraction >= policy.min_regime_positive_fraction
                and regime_min >= policy.min_regime_min_ic
            ),
            StatisticalPromotionFailureCode.ROBUSTNESS_FAILED,
            "market regime stability meets thresholds",
        ),
        _finding(
            "industry_exposure",
            industry_exposure <= policy.max_abs_industry_exposure,
            StatisticalPromotionFailureCode.ROBUSTNESS_FAILED,
            "industry exposure is within policy",
        ),
        _finding(
            "market_cap_exposure",
            cap_exposure <= policy.max_abs_market_cap_exposure,
            StatisticalPromotionFailureCode.ROBUSTNESS_FAILED,
            "market-cap exposure is within policy",
        ),
        _finding(
            "turnover",
            turnover <= policy.max_turnover,
            StatisticalPromotionFailureCode.COST_FAILED,
            "turnover is within policy",
        ),
        _finding(
            "cost_sensitivity",
            (
                net_after_cost >= policy.min_net_return_after_cost
                and not (gross_before_cost > 0.0 and net_after_cost < 0.0)
                and cost_sensitivity_loss <= policy.max_cost_sensitivity_loss
            ),
            StatisticalPromotionFailureCode.COST_FAILED,
            "net return survives spread and impact stress",
        ),
        _finding(
            "capacity",
            capacity_utilization <= policy.max_capacity_utilization,
            StatisticalPromotionFailureCode.CAPACITY_FAILED,
            "capacity utilization is within policy",
        ),
        _finding(
            "data_coverage",
            coverage >= policy.min_coverage,
            StatisticalPromotionFailureCode.COVERAGE_FAILED,
            "data coverage meets policy",
        ),
        _finding(
            "parameter_sensitivity",
            parameter_delta <= policy.max_parameter_sensitivity,
            StatisticalPromotionFailureCode.PARAMETER_SENSITIVITY_FAILED,
            "parameter sensitivity is within policy",
        ),
    )
    return RobustnessAssessment(
        passed=all(item.passed for item in findings),
        findings=findings,
    )

