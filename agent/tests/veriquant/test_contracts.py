from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.veriquant.contracts import (
    AlphaForecast,
    AttributionReport,
    AuxiliaryEvidence,
    CandidateFamily,
    CanonicalContract,
    Component,
    ContractValidationError,
    CriticFinding,
    DatasetCard,
    EvidenceClass,
    EvidencePack,
    EvolutionLineage,
    ExperimentIdentity,
    ExperimentSpec,
    FactorDAG,
    FactorNode,
    FactorSpec,
    FactorSpecDraft,
    FailureCode,
    FillEvent,
    Hypothesis,
    LedgerEntry,
    OrderPlan,
    PointInTimeRecord,
    PortfolioPlan,
    PromotionDecision,
    ResearchDirection,
    ResearchQuestion,
    ResearchState,
    RiskDecision,
    TrialEvent,
    validate_transition,
)


NOW = datetime(2025, 1, 15, 16, 0, tzinfo=timezone.utc)
HASH = "sha256:" + "a" * 64


def research_question_payload() -> dict[str, object]:
    return {
        "contract_type": "ResearchQuestion",
        "schema_version": "1.0.0",
        "question_id": "question-001",
        "text": "Does a lagged price-volume interaction predict next-day returns?",
        "universe": ["AAA", "BBB"],
        "horizon": "1d",
        "decision_time": NOW.isoformat(),
        "availability_assumptions": ["bars are usable only after close publication"],
        "state": "DRAFT",
    }


def draft_payload() -> dict[str, object]:
    return {
        "contract_type": "FactorSpecDraft",
        "schema_version": "1.0.0",
        "draft_id": "draft-001",
        "hypothesis_hash": HASH,
        "name": "lagged-volume-price",
        "description": "Research-only factor proposal",
        "proposed_expression": "rank(delta(close, 5) * lag(volume, 1))",
        "required_fields": ["close", "volume"],
        "lookback_bars": 6,
        "neutralization": ["sector"],
        "universe": ["AAA", "BBB"],
        "horizon": "1d",
        "decision_time": NOW.isoformat(),
        "availability_assumptions": ["close and volume publish after the session"],
        "state": "DRAFT",
    }


@pytest.mark.parametrize(
    "missing_field",
    ["universe", "horizon", "decision_time", "availability_assumptions"],
)
def test_research_context_missing_required_fields_fail_closed(missing_field: str) -> None:
    payload = research_question_payload()
    payload.pop(missing_field)

    with pytest.raises(ContractValidationError) as caught:
        ResearchQuestion.from_payload(payload, component=Component.LLM_RESEARCHER)

    assert caught.value.code is FailureCode.MISSING_REQUIRED_FIELD
    assert caught.value.details["field"] == missing_field


@pytest.mark.parametrize(
    "component",
    [Component.LLM_RESEARCHER, Component.QUANTAALPHA, Component.QLIB],
)
@pytest.mark.parametrize(
    ("field_name", "field_value"),
    [
        ("official_metrics", {"rank_ic": 0.1}),
        ("promotion", {"approved": True}),
        ("risk_approval", True),
        ("target_weights", {"AAA": 1.0}),
        ("orders", [{"symbol": "AAA", "quantity": 1}]),
    ],
)
def test_research_components_cannot_submit_authority_fields(
    component: Component, field_name: str, field_value: object
) -> None:
    payload = draft_payload()
    payload[field_name] = field_value

    with pytest.raises(ContractValidationError) as caught:
        FactorSpecDraft.from_payload(payload, component=component)

    assert caught.value.code is FailureCode.UNAUTHORIZED_CAPABILITY


def test_auxiliary_evidence_cannot_masquerade_as_official() -> None:
    payload = {
        "contract_type": "AuxiliaryEvidence",
        "schema_version": "1.0.0",
        "evidence_id": "aux-001",
        "source_component": "QLIB",
        "experiment_identity_hash": HASH,
        "metrics": {"rank_ic": 0.12},
        "artifact_hashes": [HASH],
        "event_time": NOW.isoformat(),
        "published_at": (NOW + timedelta(seconds=1)).isoformat(),
        "available_at": (NOW + timedelta(seconds=2)).isoformat(),
        "state": "EVALUATED",
        "evidence_class": "OFFICIAL_EVIDENCE",
    }

    with pytest.raises(ContractValidationError) as caught:
        AuxiliaryEvidence.from_payload(payload, component=Component.QLIB)

    assert caught.value.code is FailureCode.AUXILIARY_MASQUERADE


def test_illegal_state_transition_is_rejected() -> None:
    with pytest.raises(ContractValidationError) as caught:
        validate_transition(ResearchState.DRAFT, ResearchState.CHALLENGER)

    assert caught.value.code is FailureCode.INVALID_STATE_TRANSITION


def test_canonical_hash_is_independent_of_mapping_insertion_order() -> None:
    first = PortfolioPlan(
        plan_id="plan-001",
        forecast_hash=HASH,
        universe=("AAA", "BBB"),
        decision_time=NOW,
        target_weights={"AAA": 0.5, "BBB": -0.5},
        constraints_hash=HASH,
    )
    second = PortfolioPlan(
        plan_id="plan-001",
        forecast_hash=HASH,
        universe=("AAA", "BBB"),
        decision_time=NOW,
        target_weights={"BBB": -0.5, "AAA": 0.5},
        constraints_hash=HASH,
    )

    assert first.canonical_json() == second.canonical_json()
    assert first.artifact_hash == second.artifact_hash
    assert first.artifact_hash.startswith("sha256:")


def test_canonical_round_trip_preserves_hash() -> None:
    question = ResearchQuestion.from_payload(
        research_question_payload(), component=Component.RESEARCH_DESK
    )
    restored = ResearchQuestion.from_payload(
        question.to_dict(), component=Component.RESEARCH_DESK
    )

    assert restored.artifact_hash == question.artifact_hash


def test_unknown_schema_version_is_rejected() -> None:
    payload = research_question_payload()
    payload["schema_version"] = "2.0.0"

    with pytest.raises(ContractValidationError) as caught:
        ResearchQuestion.from_payload(payload, component=Component.RESEARCH_DESK)

    assert caught.value.code is FailureCode.INVALID_SCHEMA_VERSION


def test_proposer_cannot_approve_self() -> None:
    with pytest.raises(ContractValidationError) as caught:
        PromotionDecision(
            decision_id="promotion-001",
            candidate_hash=HASH,
            evidence_hash=HASH,
            proposer_id="agent-001",
            approver_id="agent-001",
            from_state=ResearchState.EVALUATED,
            to_state=ResearchState.VALIDATED_RESEARCH,
            decision_time=NOW,
            reason_codes=("MULTI_METRIC_GATE",),
            approved=True,
        )

    assert caught.value.code is FailureCode.SELF_APPROVAL


def test_factor_dag_rejects_python_execution_and_cycles() -> None:
    with pytest.raises(ContractValidationError) as generated_python:
        FactorNode(
            node_id="node-1",
            operator="PYTHON_EVAL",
            inputs=("close",),
        )
    assert generated_python.value.code is FailureCode.INVALID_FACTOR_DAG

    first = FactorNode(node_id="node-1", operator="ADD", inputs=("node-2", "close"))
    second = FactorNode(node_id="node-2", operator="LAG", inputs=("node-1",))
    with pytest.raises(ContractValidationError) as cycle:
        FactorDAG(
            dag_id="dag-001",
            factor_spec_hash=HASH,
            nodes=(first, second),
            output_node="node-1",
            required_fields=("close",),
            universe=("AAA",),
            horizon="1d",
            decision_time=NOW,
            availability_assumptions=("close available after publication",),
        )
    assert cycle.value.code is FailureCode.INVALID_FACTOR_DAG


def test_all_requested_contract_names_are_canonical_contracts() -> None:
    requested = (
        DatasetCard,
        PointInTimeRecord,
        ResearchQuestion,
        ResearchDirection,
        Hypothesis,
        FactorSpecDraft,
        FactorSpec,
        FactorDAG,
        ExperimentSpec,
        ExperimentIdentity,
        CandidateFamily,
        EvolutionLineage,
        TrialEvent,
        AuxiliaryEvidence,
        EvidencePack,
        CriticFinding,
        PromotionDecision,
        AlphaForecast,
        PortfolioPlan,
        RiskDecision,
        OrderPlan,
        FillEvent,
        LedgerEntry,
        AttributionReport,
    )

    assert len(requested) == 24
    assert all(issubclass(contract, CanonicalContract) for contract in requested)


def test_research_state_vocabulary_is_complete() -> None:
    assert {state.value for state in ResearchState} >= {
        "DRAFT",
        "DATA_VALIDATED",
        "PIT_VALIDATED",
        "EVALUATED",
        "REVISE",
        "REJECTED",
        "VALIDATED_RESEARCH",
        "CHALLENGER",
        "RETIRED",
        "BLOCKED",
    }


def test_trial_failure_still_requires_positive_family_budget() -> None:
    with pytest.raises(ContractValidationError) as caught:
        TrialEvent(
            trial_id="trial-001",
            sequence=1,
            family_id="family-001",
            candidate_hash=HASH,
            proposer_id="agent-001",
            component=Component.QUANTAALPHA,
            from_state=ResearchState.DRAFT,
            to_state=ResearchState.REVISE,
            event_time=NOW,
            budget_units=0,
            failure_code=FailureCode.INVALID_VALUE,
            artifact_hashes=(),
        )

    assert caught.value.code is FailureCode.INVALID_VALUE


def test_time_fields_reject_naive_decision_time() -> None:
    payload = research_question_payload()
    payload["decision_time"] = "2025-01-15T16:00:00"

    with pytest.raises(ContractValidationError) as caught:
        ResearchQuestion.from_payload(payload, component=Component.RESEARCH_DESK)

    assert caught.value.code is FailureCode.INVALID_TIMESTAMP

