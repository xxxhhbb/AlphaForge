from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone

import pytest

from src.veriquant.adapters import (
    FactorLintPolicy,
    FactorRegulatorAdapter,
    ProposalContext,
    QuantaAlphaProposalAdapter,
)
from src.veriquant.contracts import ContractValidationError, FailureCode, ResearchState
from src.veriquant.data import FieldRole
from src.veriquant.factors import parse_compile_factor_dag


HASH_A = "sha256:" + "a" * 64
NOW = datetime(2024, 6, 28, 16, 0, tzinfo=timezone.utc)


def context() -> ProposalContext:
    return ProposalContext(
        direction_hash=HASH_A,
        universe=("AAA", "BBB"),
        horizon="5d",
        decision_time=NOW,
        availability_assumptions=("daily close available after session end",),
        field_roles={
            "close": FieldRole.FEATURE,
            "volume": FieldRole.FEATURE,
            "industry": FieldRole.FEATURE,
            "future_return_5d": FieldRole.LABEL,
        },
    )


def valid_payload() -> dict[str, object]:
    return {
        "hypothesis": {
            "hypothesis": "Recent relative price strength persists over the next five sessions.",
            "reason": "Underreaction can create short-lived continuation.",
            "expected_direction": "positive",
            "falsification_tests": ["Reject when net validation IC is non-positive."],
        },
        "factor_task": {
            "factor_name": "momentum_20_rank",
            "factor_description": "Cross-sectional rank of trailing twenty-session return.",
            "expression": "rank_cross_section(pct_change(close, 20))",
            "variables": ["close"],
            "lookback_bars": 20,
            "neutralization": [],
        },
    }


def dag_payload() -> dict[str, object]:
    return {
        "schema_version": "veriquant.factor-dag/v1",
        "dag_id": "momentum-lint",
        "nodes": [
            {"id": "close", "op": "field", "field": "close"},
            {"id": "change", "op": "pct_change", "inputs": ["close"], "periods": 20},
            {"id": "rank", "op": "rank_cross_section", "inputs": ["change"]},
        ],
        "output_node": "rank",
    }


def test_quantaalpha_payload_maps_to_draft_contracts_and_internal_dag() -> None:
    proposal = QuantaAlphaProposalAdapter().adapt(valid_payload(), context=context())

    assert proposal.state is ResearchState.DRAFT
    assert proposal.hypothesis.state is ResearchState.DRAFT
    assert proposal.factor_draft.state is ResearchState.DRAFT
    assert proposal.factor_draft.hypothesis_hash == proposal.hypothesis.artifact_hash
    assert proposal.factor_draft.proposed_expression == proposal.factor_dag.canonical_json()
    assert proposal.factor_dag.required_fields == ("close",)
    assert "factor.py" not in proposal.canonical_json().lower()


@pytest.mark.parametrize(
    "field,value",
    [
        ("code", "print('unsafe')"),
        ("official_metrics", {"RankIC": 1.0}),
        ("promotion", "PROMOTE"),
        ("portfolio", {"AAA": 1.0}),
        ("risk_approval", True),
        ("orders", [{"symbol": "AAA"}]),
        ("sealed_reference", "sealed/fold-1"),
    ],
)
def test_authority_and_code_fields_are_rejected(field: str, value: object) -> None:
    payload = valid_payload()
    payload[field] = value
    with pytest.raises(ContractValidationError) as caught:
        QuantaAlphaProposalAdapter().adapt(payload, context=context())
    assert caught.value.code is FailureCode.FORBIDDEN_PROPOSAL_PAYLOAD


@pytest.mark.parametrize(
    "expression",
    [
        "factor.py",
        "__import__('os').system('whoami')",
        "lambda x: x",
        "close + volume",
    ],
)
def test_python_factor_file_and_unrestricted_expressions_are_rejected(expression: str) -> None:
    payload = deepcopy(valid_payload())
    payload["factor_task"]["expression"] = expression
    with pytest.raises(ContractValidationError) as caught:
        QuantaAlphaProposalAdapter().adapt(payload, context=context())
    assert caught.value.code in {
        FailureCode.FORBIDDEN_PROPOSAL_PAYLOAD,
        FailureCode.INVALID_FACTOR_DAG,
    }


def test_static_candidate_batch_is_bounded_and_has_no_evolution_api() -> None:
    adapter = QuantaAlphaProposalAdapter(max_candidates=2)
    proposals = adapter.adapt_candidates([valid_payload(), valid_payload()], context=context())
    assert len(proposals) == 2
    assert not hasattr(adapter, "mutate")
    assert not hasattr(adapter, "crossover")
    with pytest.raises(ContractValidationError):
        adapter.adapt_candidates([valid_payload()] * 3, context=context())


def test_regulator_parser_success_is_lint_only_and_stays_draft() -> None:
    report = FactorRegulatorAdapter().lint(
        dag_payload(), field_roles=context().field_roles
    )

    assert report.parser_accepted is True
    assert report.lint_passed is True
    assert report.report_kind == "lint"
    assert report.state is ResearchState.DRAFT
    assert report.ast_node_count == 3
    assert report.base_features == ("close",)
    assert report.failure_code is None


def test_regulator_reports_factor_zoo_similarity_without_validation_claim() -> None:
    compiled = parse_compile_factor_dag(dag_payload(), field_roles=context().field_roles)
    report = FactorRegulatorAdapter(
        policy=FactorLintPolicy(max_factor_zoo_similarity=0.90)
    ).lint(dag_payload(), field_roles=context().field_roles, factor_zoo=(compiled,))

    assert report.parser_accepted is True
    assert report.lint_passed is False
    assert report.factor_zoo_similarity == 1.0
    assert report.findings == ("FACTOR_ZOO_SIMILARITY",)
    assert report.state is ResearchState.DRAFT


def test_regulator_parser_rejection_is_typed_lint_failure() -> None:
    payload = dag_payload()
    payload["nodes"][1]["op"] = "arbitrary_python"
    report = FactorRegulatorAdapter().lint(payload, field_roles=context().field_roles)
    assert report.parser_accepted is False
    assert report.lint_passed is False
    assert report.failure_code is FailureCode.INVALID_FACTOR_DAG
    assert report.state is ResearchState.DRAFT
