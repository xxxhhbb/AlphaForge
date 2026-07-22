from __future__ import annotations

import json
from dataclasses import replace

import pytest

from veriquant.contracts import Component, EvidenceClass
from veriquant.evidence.models import MetricSet, OfficialEvidence
from veriquant.statistical_confidence_gate_v2 import (
    ConfidenceGateError,
    ConfidenceGateFailureCode,
    ConfidenceGateRequest,
    ConfidenceGateStatus,
    OfficialConfidenceStatistic,
    StatisticalConfidenceGateV2,
)


def h(label: str) -> str:
    import hashlib

    return "sha256:" + hashlib.sha256(label.encode("utf-8")).hexdigest()


def statistic(
    *,
    estimate: float = 0.04,
    std: float = 0.08,
    size: int = 80,
) -> OfficialConfidenceStatistic:
    return OfficialConfidenceStatistic(
        candidate_id="candidate.momentum50",
        identity_hash=h("identity"),
        evidence_hash=h("evidence"),
        metric_name="period_ic_mean",
        estimate=estimate,
        sample_std=std,
        sample_size=size,
    )


def request(item: OfficialConfidenceStatistic) -> ConfidenceGateRequest:
    return ConfidenceGateRequest(
        statistic=item,
        expected_identity_hash=item.identity_hash,
        expected_evidence_hash=item.evidence_hash,
    )


def metrics(*, mean: float = 0.04, std: float = 0.08, periods: int = 80) -> MetricSet:
    return MetricSet(
        pearson_ic=0.04,
        rank_ic=0.05,
        ic_mean=mean,
        ic_std=std,
        icir=1.0,
        quantile_spread=0.02,
        turnover=0.3,
        gross_return=0.05,
        net_return=0.03,
        spread_cost=0.01,
        linear_impact_cost=0.01,
        max_drawdown=0.08,
        subperiod_positive_fraction=0.7,
        subperiod_min_ic=0.01,
        regime_positive_fraction=0.7,
        regime_min_ic=0.01,
        max_registered_abs_correlation=0.3,
        incremental_ic_mean=0.02,
        observation_count=400,
        period_count=periods,
    )


def official(*, partition: str = "VALIDATION") -> OfficialEvidence:
    return OfficialEvidence(
        identity_hash=h("identity"),
        partition=partition,
        metrics=metrics(),
        input_hashes=(h("data"), h("factor")),
    )


def test_confidence_interval_crossing_zero_is_rejected() -> None:
    item = statistic(estimate=0.005, std=0.08, size=80)
    receipt = StatisticalConfidenceGateV2().evaluate(request(item))
    assert receipt.status is ConfidenceGateStatus.REJECT
    assert receipt.failure_code is ConfidenceGateFailureCode.INTERVAL_CROSSES_THRESHOLD
    assert receipt.lower_bound is not None and receipt.lower_bound < 0.0
    assert receipt.promotion_precondition_satisfied is False


def test_insufficient_period_sample_is_typed_unavailable() -> None:
    item = statistic(size=12)
    receipt = StatisticalConfidenceGateV2().evaluate(request(item))
    assert receipt.status is ConfidenceGateStatus.UNAVAILABLE
    assert receipt.failure_code is ConfidenceGateFailureCode.INSUFFICIENT_SAMPLE
    assert receipt.lower_bound is None and receipt.upper_bound is None


def test_degenerate_variance_is_typed_unavailable_not_fabricated_ci() -> None:
    receipt = StatisticalConfidenceGateV2().evaluate(request(statistic(std=0.0)))
    assert receipt.status is ConfidenceGateStatus.UNAVAILABLE
    assert receipt.failure_code is ConfidenceGateFailureCode.DEGENERATE_VARIANCE


def test_hash_mismatch_fails_closed() -> None:
    item = statistic()
    req = replace(request(item), expected_evidence_hash=h("other-evidence"))
    with pytest.raises(ConfidenceGateError) as exc:
        StatisticalConfidenceGateV2().evaluate(req)
    assert exc.value.code is ConfidenceGateFailureCode.INVALID_HASH_BINDING


def test_same_input_replays_to_same_receipt_hash() -> None:
    gate = StatisticalConfidenceGateV2()
    req = request(statistic())
    first = gate.evaluate(req)
    second = gate.evaluate(req)
    assert first.status is ConfidenceGateStatus.PASS
    assert first.receipt_hash == second.receipt_hash
    assert first.lower_bound == second.lower_bound


def test_llm_or_auxiliary_statistic_is_rejected() -> None:
    with pytest.raises(ConfidenceGateError) as llm:
        replace(statistic(), produced_by=Component.LLM_RESEARCHER)
    assert llm.value.code is ConfidenceGateFailureCode.UNAUTHORIZED_PRODUCER

    with pytest.raises(ConfidenceGateError) as auxiliary:
        replace(statistic(), evidence_class=EvidenceClass.AUXILIARY_RESEARCH_EVIDENCE)
    assert auxiliary.value.code is ConfidenceGateFailureCode.AUXILIARY_EVIDENCE_REJECTED


def test_rank_ic_only_cannot_substitute_for_period_ic_statistics() -> None:
    with pytest.raises(ConfidenceGateError) as exc:
        replace(statistic(), metric_name="rank_ic")
    assert exc.value.code is ConfidenceGateFailureCode.UNSUPPORTED_STATISTIC


def test_adapter_binds_real_official_validation_evidence() -> None:
    evidence = official()
    item = OfficialConfidenceStatistic.from_official_evidence(
        candidate_id="candidate.momentum50",
        evidence=evidence,
        expected_identity_hash=evidence.identity_hash,
    )
    assert item.evidence_hash == evidence.artifact_hash
    assert item.sample_size == evidence.metrics.period_count
    assert item.estimate == evidence.metrics.ic_mean


def test_adapter_rejects_sealed_partition_and_does_not_leak() -> None:
    evidence = official(partition="SEALED")
    with pytest.raises(ConfidenceGateError) as exc:
        OfficialConfidenceStatistic.from_official_evidence(
            candidate_id="candidate.momentum50",
            evidence=evidence,
            expected_identity_hash=evidence.identity_hash,
        )
    assert exc.value.code is ConfidenceGateFailureCode.SEALED_EVIDENCE_REJECTED

    serialized = json.dumps(StatisticalConfidenceGateV2().evaluate(request(statistic())).to_dict())
    assert "sealed_handle" not in serialized.lower()
    assert "sealed_metric" not in serialized.lower()


def test_gate_cannot_issue_challenger_or_promotion_verdict() -> None:
    receipt = StatisticalConfidenceGateV2().evaluate(request(statistic()))
    assert receipt.status is ConfidenceGateStatus.PASS
    assert receipt.promotion_precondition_satisfied is True
    assert not hasattr(receipt, "promotion_verdict")
    assert not hasattr(receipt, "challenger")

