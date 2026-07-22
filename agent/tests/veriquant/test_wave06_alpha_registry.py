from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from src.veriquant.contracts import (
    AuxiliaryEvidence,
    Component,
    ContractValidationError,
    EvidenceClass,
    FailureCode,
    ResearchState,
    validate_component_payload,
)
from src.veriquant.downstream import (
    AlphaCalibrationRequest,
    AlphaRegistry,
    DeterministicAlphaService,
)
from src.veriquant.evidence import CourtOutcome, MechanicalDecision, MetricSet, OfficialEvidence


NOW = datetime(2025, 1, 31, 16, 0, tzinfo=timezone.utc)
IDENTITY_HASH = "sha256:" + "1" * 64
CANDIDATE_HASH = "sha256:" + "2" * 64
INPUT_HASH = "sha256:" + "3" * 64


def official_evidence() -> OfficialEvidence:
    return OfficialEvidence(
        identity_hash=IDENTITY_HASH,
        partition="VALIDATION",
        metrics=MetricSet(
            pearson_ic=0.08,
            rank_ic=0.07,
            ic_mean=0.06,
            ic_std=0.03,
            icir=2.0,
            quantile_spread=0.02,
            turnover=0.20,
            gross_return=0.03,
            net_return=0.02,
            spread_cost=0.004,
            linear_impact_cost=0.006,
            max_drawdown=-0.04,
            subperiod_positive_fraction=0.75,
            subperiod_min_ic=0.01,
            regime_positive_fraction=0.80,
            regime_min_ic=0.01,
            max_registered_abs_correlation=0.25,
            incremental_ic_mean=0.04,
            observation_count=80,
            period_count=20,
        ),
        input_hashes=(INPUT_HASH,),
    )


def court_decision(
    evidence: OfficialEvidence, outcome: CourtOutcome = CourtOutcome.VALIDATED_RESEARCH
) -> MechanicalDecision:
    return MechanicalDecision(
        decision_id="court-decision-001",
        identity_hash=evidence.identity_hash,
        evidence_hash=evidence.artifact_hash,
        proposer_id="desk-fundamental",
        approver_id="evidence-court-controller",
        outcome=outcome,
        reason_codes=("MECHANICAL_POLICY",),
        decided_at=NOW,
    )


def calibration_request(
    *, state: ResearchState = ResearchState.VALIDATED_RESEARCH
) -> AlphaCalibrationRequest:
    evidence = official_evidence()
    return AlphaCalibrationRequest(
        candidate_id="candidate-001",
        candidate_hash=CANDIDATE_HASH,
        state=state,
        universe=("AAA", "BBB", "CCC", "DDD"),
        factor_scores={"AAA": 2.0, "BBB": 1.0, "CCC": -1.0, "DDD": -2.0},
        horizon="5d",
        decision_time=NOW,
        evidence=evidence,
        decision=court_decision(evidence),
        decay_half_life_days=20.0,
    )


def test_alpha_service_is_deterministic_and_records_required_calibration_fields() -> None:
    service = DeterministicAlphaService()
    request = calibration_request()
    first = service.calibrate(request)
    second = service.calibrate(request)

    assert first.artifact_hash == second.artifact_hash
    assert first.expected_returns == second.expected_returns
    assert first.horizon == "5d"
    assert 0.0 <= first.confidence <= 1.0
    assert first.decay_half_life_days == 20.0
    assert first.factor_correlation == 0.25
    assert first.evidence_identity_hash == IDENTITY_HASH
    assert first.produced_by is Component.ALPHA_SERVICE
    assert first.state is ResearchState.CHALLENGER


def test_draft_auxiliary_and_rejected_candidates_cannot_be_calibrated() -> None:
    with pytest.raises(ContractValidationError) as draft:
        calibration_request(state=ResearchState.DRAFT)
    assert draft.value.code is FailureCode.INVALID_ALPHA_SOURCE

    evidence = official_evidence()
    auxiliary = AuxiliaryEvidence(
        evidence_id="aux-001",
        source_component=Component.QLIB,
        experiment_identity_hash=IDENTITY_HASH,
        metrics={"rank_ic": 0.9},
        artifact_hashes=(INPUT_HASH,),
        event_time=NOW,
        published_at=NOW,
        available_at=NOW,
        state=ResearchState.EVALUATED,
        evidence_class=EvidenceClass.AUXILIARY_RESEARCH_EVIDENCE,
    )
    with pytest.raises(ContractValidationError) as aux:
        AlphaCalibrationRequest(
            candidate_id="candidate-aux",
            candidate_hash=CANDIDATE_HASH,
            state=ResearchState.VALIDATED_RESEARCH,
            universe=("AAA",),
            factor_scores={"AAA": 1.0},
            horizon="5d",
            decision_time=NOW,
            evidence=aux,  # type: ignore[arg-type]
            decision=court_decision(evidence),
        )
    assert aux.value.code is FailureCode.INVALID_ALPHA_SOURCE

    with pytest.raises(ContractValidationError) as rejected:
        AlphaCalibrationRequest(
            candidate_id="candidate-rejected",
            candidate_hash=CANDIDATE_HASH,
            state=ResearchState.VALIDATED_RESEARCH,
            universe=("AAA",),
            factor_scores={"AAA": 1.0},
            horizon="5d",
            decision_time=NOW,
            evidence=evidence,
            decision=court_decision(evidence, CourtOutcome.REJECT),
        )
    assert rejected.value.code is FailureCode.INVALID_ALPHA_SOURCE


def test_agent_payload_cannot_write_expected_returns() -> None:
    with pytest.raises(ContractValidationError) as caught:
        validate_component_payload(
            Component.LLM_RESEARCHER,
            {"candidate": {"expected_returns": {"AAA": 0.10}}},
        )
    assert caught.value.code is FailureCode.UNAUTHORIZED_CAPABILITY


def test_alpha_registry_is_versioned_idempotent_and_retirement_is_explicit() -> None:
    service = DeterministicAlphaService()
    first_forecast = service.calibrate(calibration_request())
    registry = AlphaRegistry()

    first = registry.register(
        alpha_id="alpha-momentum",
        forecast=first_forecast,
        identity_hash=IDENTITY_HASH,
        registered_at=NOW,
    )
    duplicate = registry.register(
        alpha_id="alpha-momentum",
        forecast=first_forecast,
        identity_hash=IDENTITY_HASH,
        registered_at=NOW + timedelta(minutes=1),
    )
    assert duplicate == first
    assert len(registry.events) == 1
    assert first.version == 1
    assert first.status is ResearchState.CHALLENGER

    second_forecast = replace(first_forecast, horizon="10d")
    second = registry.register(
        alpha_id="alpha-momentum",
        forecast=second_forecast,
        identity_hash=IDENTITY_HASH,
        registered_at=NOW + timedelta(days=1),
    )
    assert second.version == 2
    assert second.forecast_hash != first.forecast_hash

    retired = registry.retire(
        alpha_id="alpha-momentum",
        version=1,
        retired_at=NOW + timedelta(days=2),
    )
    retired_again = registry.retire(
        alpha_id="alpha-momentum",
        version=1,
        retired_at=NOW + timedelta(days=3),
    )
    assert retired_again == retired
    assert retired.status is ResearchState.RETIRED
    assert registry.history("alpha-momentum") == (retired, second)
    assert len(registry.events) == 3
    assert registry.events[-1].previous_event_hash == registry.events[-2].artifact_hash
